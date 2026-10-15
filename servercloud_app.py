#!/usr/bin/env python3
"""ServerCloud — local token factory for Ollama models on Termux.

Flow:
  1) pick from a catalog of 90 local models
  2) Download [model]? Y/n -> pull via Ollama, model gets number [01],[02]...
  3) /w [model#] [question]  ask (several questions to different models run
     in parallel)
  4) /stf [tokens/sec]       set generation speed cap
  Live dashboard while generating:
     Text — [model] [doing what] [tok/s] [ready in Ns]
  Final answer line:
     > [model] - [answer] [time] [consumed tokens]
"""

import os
import re
import sys
import time
import threading
import urllib.error

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from servercloud.core.version import APP_NAME, version_string          # noqa: E402
from servercloud.core.models import (MODEL_CATALOG, catalog_lines, get_by_index,
                                  giant_catalog_lines, HEAVY_THRESHOLD_GB)  # noqa: E402

from servercloud.core.registry import Registry                          # noqa: E402
from servercloud.core.jobs import Manager                               # noqa: E402
from servercloud.core.chatml import ChatStore                           # noqa: E402
from servercloud.core.options import GenOptions                         # noqa: E402
from servercloud.core import hardware                                   # noqa: E402
from servercloud.modules import ollama_api                               # noqa: E402
from servercloud.modules import custom_models                           # noqa: E402
from servercloud.modules import huggingface_api                         # noqa: E402
from servercloud.modules.api_server import (ApiKey, KeyStore, ApiServer,
                                         generate_key)                 # noqa: E402
from servercloud.modules.cpts_api import (CptsStore, CptsClient,
                                       validate_key as cpts_valid_key)  # noqa: E402
from servercloud.utils.colors import (banner, c, GREEN, YELLOW, RED, MAGENTA,
                                      BOLD, CYAN, DIM)                  # noqa: E402
from servercloud.truck_modules import trk                                # noqa: E402
from servercloud.truck_modules.sysconfig import SysConfig                  # noqa: E402
from servercloud.kernel import (truck_init, truck_boot,                    # noqa: E402
                                truck_kernel, truck_syscall)
from servercloud.os import truck_shell                                    # noqa: E402
from servercloud.os.scriptlib import ScriptLibrary                      # noqa: E402

# NOTE: SERVERCLOUD_HOME is used by scripts/install.sh as the *checkout* dir.
# The app's data dir must not collide with it -> use SERVERCLOUD_DATA instead.
DATA_DIR = os.environ.get("SERVERCLOUD_DATA",
                          os.path.join(os.path.expanduser("~"), ".servercloud"))
os.makedirs(DATA_DIR, exist_ok=True)


class App:
    def __init__(self):
        # ---- TruckOS layer: bootloader + init + package manager ----
        self.trk_root = trk.init_truck(DATA_DIR)
        self.sysconfig = SysConfig(os.path.join(self.trk_root, "etc",
                                                "sysconfig.json"))
        self.scriptlib = ScriptLibrary(DATA_DIR)
        truck_init._paths(DATA_DIR)
        # simulated ring-0 kernel (user-space emulation); the bootloader
        # replaces this with a tuned instance during truck_boot.boot()
        self.kernel = truck_kernel.default_kernel()
        self.reg = Registry(os.path.join(DATA_DIR, "models.json"))
        self.tps = 8.0
        self.mgr = Manager(self._runner)
        self.chat = ChatStore(os.path.join(DATA_DIR, "chat.json"))
        self.opts = GenOptions()
        self.opt_model = None   # model targeted by /opt
        self.ok = ollama_api.is_alive()
        self.ver = ollama_api.server_version()
        # ---- network API (host your local models over the LAN/WAN) ----
        self.keystore = KeyStore(os.path.join(DATA_DIR, "api_keys.json"))
        # ---- /cpts: client side — call OTHER people's ServerCloud APIs ----
        self.cpts = CptsStore(os.path.join(DATA_DIR, "cpts.json"))
        self.api = None                 # ApiServer instance when running
        api_host = os.environ.get("SERVERCLOUD_API_HOST", "0.0.0.0")
        api_port = int(os.environ.get("SERVERCLOUD_API_PORT", "8777"))
        try:
            self.api = ApiServer(api_host, api_port,
                                 app_name=APP_NAME,
                                 resolver=self._api_resolve,
                                 generator=self._api_generate,
                                 history_provider=lambda num: self.chat.history(num),
                                 keystore=self.keystore,
                                 version=version_string(),
                                 record_hook=self._api_record)
            self.api.start()
        except OSError as e:
            print(c(f"API server could not bind {api_host}:{api_port} — {e}", YELLOW))
            self.api = None

    # ---------- network API helpers ----------
    def _api_resolve(self, ref):
        """Map a key-bound model reference to a real Ollama model name.

        Accepts registry numbers ("01"), catalog numbers ("3" / "03") and
        exact model names; returns None if the model is not resolvable.
        """
        # '#N' -> remote /cpts API slot: pass through verbatim so
        # _api_generate forwards the request to that remote host.
        if str(ref).startswith("#") and str(ref)[1:].isdigit():
            return str(ref)
        num, entry = self.reg.find(ref)
        if entry:
            return entry["name"]
        if str(ref).isdigit() and 1 <= int(ref) <= len(MODEL_CATALOG):
            return MODEL_CATALOG[int(ref) - 1][0]
        matches = [n for n, _, _, _ in MODEL_CATALOG if n == ref]
        return matches[0] if matches else None

    def _api_generate(self, model_name, prompt, options):
        """Blocking generation for API requests (no throttle cap here).

        If the host itself routes through an active /cpts remote, we forward
        there instead of hitting the local Ollama — a ServerCloud chain
        (client -> my API -> someone else's API) then works transparently.
        """
        forced = model_name.startswith("#") and model_name[1:].isdigit()
        slot = model_name[1:] if forced else self.cpts.active
        if slot:
            rem = self.cpts.get(slot)
            if rem:
                fwd_model = "0" if forced else model_name
                client = CptsClient(rem["url"], rem["key"])
                # strip our ChatML framing back to the plain question so the
                # upstream host receives clean text (it re-wraps on its side)
                q = prompt
                if "<|im_start|>" in q:
                    chunks = [p for p in q.split("<|im_start|>")
                              if p.startswith("user")]
                    if chunks:
                        q = chunks[-1].split("<|im_end|>")[0].split("\n", 1)[-1]
                try:
                    text = client.chat(fwd_model, q, options)
                    self.cpts.remotes[slot]["calls_ok"] += 1
                    self.cpts.remotes[slot]["last_status"] = "ok (api)"
                    self.cpts.save()
                    return text
                except Exception as e:
                    self.cpts.remotes[slot]["calls_fail"] += 1
                    self.cpts.remotes[slot]["last_status"] = f"fail: {e}"
                    self.cpts.save()
                    raise RuntimeError(f"cpts forwarding failed: {e}")
        if not self.ok:
            # demo mode mirrors REPL behaviour when Ollama is offline.
            # Unwrap ChatML framing so the reply echoes the real question
            # instead of a raw character count of markup.
            q = prompt
            if "<|im_start|>" in q:
                chunks = [p for p in q.split("<|im_start|>")
                          if p.startswith("user")]
                if chunks:
                    q = chunks[-1].split("<|im_end|>")[0].split("\n", 1)[-1]
            q = q.strip()
            return (f"[DEMO MODE — Ollama offline] I am {model_name}. "
                    f"You asked: {q[:200] if q else '(empty)'}")
        text, _n, _el, _stats = ollama_api.generate_stream(
            model_name, prompt, tokens_per_sec=100000.0,
            on_token=None, stop_flag=None, options=options)
        return text

    def _api_record(self, model_ref, user_msg, reply):
        """Persist an API dialog turn so /v1/history and the REPL see it."""
        num, entry = self.reg.find(model_ref)
        name = entry["name"] if entry else str(model_ref)
        self.chat.add(num or name, "user", user_msg)
        self.chat.add(num or name, "assistant", reply)

    def _api_url_hint(self):
        if not self.api:
            return "offline"
        host = self.api.host
        if host in ("0.0.0.0", "::"):
            import socket
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                s.connect(("8.8.8.8", 80))
                host = s.getsockname()[0]
                s.close()
            except Exception:
                host = socket.gethostbyname(socket.gethostname())
        return f"http://{host}:{self.api.port}"

    # ---------- /apis /apim /apioff /apion ----------
    def cmd_apis(self, rest):
        """Create a new API key and start serving it.

        Usage: /apis [models] [Y/N history] [req/min] [key slot number]
        Example: /apis 01,02,03 Y 60 1
        """
        parts = rest.split()
        if len(parts) < 4:
            print(c("Usage: /apis [model numbers, e.g. 01,02,03] "
                    "[history access Y/N] [max req/min] [key slot number]", YELLOW))
            print(c("Example: /apis 01,02,03 Y 60 1", YELLOW))
            return
        models_raw, hist_raw, rpm_raw, slot_raw = parts[0], parts[1], parts[2], parts[3]
        # validate models list against registry + catalog
        models = [m.strip() for m in models_raw.split(",") if m.strip()]
        if not models:
            print(c("No model numbers given.", RED))
            return
        # A key may also be bound to a remote /cpts API (#N): requests then
        # transparently forward to that remote host instead of local Ollama.
        def _valid_ref(m):
            num, entry = self.reg.find(m)
            if entry:
                return True
            if m.isdigit() and 1 <= int(m) <= len(MODEL_CATALOG):
                return True
            if any(n == m for n, _, _, _ in MODEL_CATALOG):
                return True
            if m.startswith("#") and m[1:].isdigit() and \
                    self.cpts.get(m[1:]) is not None:
                return True
            return False
        bad = [m for m in models if not _valid_ref(m)]
        if bad:
            print(c(f"Unknown model references: {', '.join(bad)}. "
                    f"Use registry/catalog numbers (01..135), exact names, "
                    f"or remote APIs as #N (see /cptsm).", RED))
            return
        # history flag
        h = hist_raw.strip().lower()
        if h in ("y", "yes", "д"):
            allow_hist = True
        elif h in ("n", "no", "н"):
            allow_hist = False
        else:
            print(c("History access must be Y or N.", RED))
            return
        # rate limit
        try:
            rpm = int(rpm_raw)
            if not (1 <= rpm <= 10000):
                raise ValueError
        except ValueError:
            print(c("Requests/min must be an integer 1..10000.", RED))
            return
        # slot number
        if not slot_raw.isdigit() or not (1 <= int(slot_raw) <= 9999):
            print(c("Key slot number must be an integer 1..9999.", RED))
            return
        slot = str(int(slot_raw))
        if self.keystore.get_by_number(slot):
            print(c(f"API key #{slot} already exists. Remove it with "
                    f"/apidel {slot} or pick another number.", RED))
            return
        key = ApiKey(slot, generate_key(), models, allow_hist, rpm)
        self.keystore.add(key)
        print(c(f"API key #{slot} created:", BOLD))
        print(f"   Key        : {c(key.key, GREEN)}")
        print(f"   Models     : {', '.join(models)}")
        print(f"   History    : {'allowed' if allow_hist else 'denied'}")
        print(f"   Rate limit : {rpm} req/min")
        print(f"   Endpoint   : {self._api_url_hint()}")
        if self.api:
            print(c("Server is LIVE now. Client example:", BOLD))
            print(f"   curl -X POST {self._api_url_hint()}/v1/chat \\")
            print(f"     -H 'Authorization: Bearer {key.key}' \\")
            print(f"     -d '{{\"model\":\"{models[0]}\",\"messages\":[{{\"role\":\"user\",\"content\":\"Привет\"}}]}}'")
        else:
            print(c("WARNING: API server is not running (port busy?) — "
                    "the key is saved but cannot be used yet.", YELLOW))

    def cmd_apim(self, rest):
        """/apim — manage/monitor API keys."""
        keys = self.keystore.all()
        state = c("RUNNING", GREEN) if (self.api and self.api.running) else c("STOPPED", RED)
        print(c(f"API server: {state} at {self._api_url_hint()}", BOLD))
        if not keys:
            print(c("No API keys yet. Create one: /apis 01,02 Y 60 1", YELLOW))
            return
        print(c("Keys:", BOLD))
        for n in sorted(keys, key=lambda x: int(x)):
            k = keys[n]
            flag = c("[on ]", GREEN) if k.enabled else c("[OFF]", YELLOW)
            hist = "hist=Y" if k.allow_history else "hist=N"
            print(f"  #{n:>3} {flag} {k.key}  models=[{','.join(k.models)}] "
                  f"{hist} {k.rpm} req/min  served={k.total_requests}  since {k.created}")
        print(c("Commands: /apioff <#> | /apion <#> | /apidel <#>", YELLOW))

    def _set_key_enabled(self, arg, enabled):
        if not arg.isdigit():
            print(c("Usage: /apioff <key number> | /apion <key number>", YELLOW))
            return
        k = self.keystore.get_by_number(arg)
        if not k:
            print(c(f"API key #{arg} not found (see /apim).", RED))
            return
        k.enabled = enabled
        self.keystore.save()
        what = "enabled (/apion)" if enabled else "disabled (/apioff)"
        print(c(f"API key #{arg} {k.key} {what}.", GREEN if enabled else YELLOW))

    def cmd_apioff(self, arg):
        self._set_key_enabled(arg.strip(), False)

    def cmd_apion(self, arg):
        self._set_key_enabled(arg.strip(), True)

    def cmd_apidel(self, arg):
        if not arg.isdigit():
            print(c("Usage: /apidel <key number>", YELLOW))
            return
        k = self.keystore.remove(arg)
        if not k:
            print(c(f"API key #{arg} not found.", RED))
            return
        print(c(f"API key #{arg} ({k.key}) deleted.", GREEN))

    # ---------- /cpts — call OTHER people's ServerCloud APIs ----------
    def cmd_cpts(self, rest):
        """`/cpts [API token] [url]` — register a remote ServerCloud API.

        The hoster gave you a key like SCA-XXXX-XXXX-XXXX (they created it
        with their own /apis). Optionally pass their base URL; default is
        http://<host>:8777 where <host> comes from the key owner. If no url
        is given we reuse the last known / or the local demo endpoint.
        """
        parts = rest.split()
        if not parts:
            print(c("Usage: /cpts <SCA-XXXX-XXXX-XXXX> [base url]", YELLOW))
            print(c("Example: /cpts SCA-K5A6-SWTW-S4LA http://192.168.1.40:8777",
                    YELLOW))
            print(c("After adding, questions (/w) route through the remote "
                    "API. Manage with /cptsm, /cptsuse, /cptslocal, /cptsdel.",
                    YELLOW))
            return
        key = parts[0].strip().upper()
        if not cpts_valid_key(key):
            print(c("Bad key format. Expected SCA-XXXX-XXXX-XXXX "
                    "(uppercase letters/digits, no 0/O/1/I).", RED))
            return
        if len(parts) >= 2:
            url = parts[1]
        else:
            url = os.environ.get("SERVERCLOUD_CPTS_URL", "http://127.0.0.1:8777")
        if not re.match(r"^https?://", url):
            print(c("Base url must start with http:// or https://", RED))
            return
        client = CptsClient(url, key, timeout=10)
        # verify connectivity + that it really is a ServerCloud API
        try:
            info = client.health()
        except Exception as e:
            print(c(f"Cannot reach {url}/api/health — {e}", RED))
            print(c("Key NOT saved. Check the address/port the hoster gave you.",
                    YELLOW))
            return
        if str(info.get("app", "")).lower() != "servercloud":
            print(c(f"{url} answered but is not a ServerCloud API "
                    f"(got app={info.get('app')!r}). Key NOT saved.", RED))
            return
        # verify the key itself works against this server
        try:
            models = client.models()
        except urllib.error.HTTPError as e:
            reason = {401: "invalid or disabled key",
                      429: "rate limit already exhausted"}.get(e.code, str(e))
            print(c(f"Key rejected by server ({e.code}: {reason}). "
                    f"Key NOT saved.", RED))
            return
        except Exception as e:
            print(c(f"Model check failed — {e}. Key NOT saved.", RED))
            return
        slot, rem = self.cpts.add(url, key)
        self.cpts.set_active(slot)     # newly added remote becomes active
        print(c(f"Remote API #{slot} added and ACTIVATED:", BOLD))
        print(f"   URL      : {rem['url']}")
        print(f"   Key      : {c(key, GREEN)}")
        print(f"   Server   : ServerCloud {info.get('version', '?')}")
        print(f"   Models   : {', '.join(str(m.get('name', m)) for m in models) or '(none listed)'}")
        print(c(f"From now on /w questions go through this remote API. "
                f"Switch back to local models with /cptslocal.", YELLOW))

    def cmd_cptsm(self, rest):
        """List configured remote APIs."""
        remotes = self.cpts.all()
        if not remotes:
            print(c("No remote APIs yet. Add one: /cpts <SCA-key> [url]",
                    YELLOW))
            return
        mode = (f"ACTIVE → remote #{self.cpts.active}" if self.cpts.active
                else "routing: LOCAL Ollama (see /cptsuse)")
        print(c(f"Remote ServerCloud APIs ({mode}):", BOLD))
        for n in sorted(remotes, key=lambda x: int(x)):
            r = remotes[n]
            flag = c("[>>]", GREEN) if self.cpts.active == n else "   "
            print(f"  {flag} #{n:>3} {r['url']}  key={r['key']}  "
                  f"ok={r['calls_ok']} fail={r['calls_fail']}  last={r['last_status']}")
        print(c("Commands: /cptsuse <#> | /cptslocal | /cptsdel <#> | "
                "/clear cpts#<n>", YELLOW))

    def cmd_cptsuse(self, arg):
        arg = arg.strip()
        if not arg.isdigit() or not self.cpts.get(arg):
            print(c("Usage: /cptsuse <remote number> (see /cptsm)", YELLOW))
            return
        self.cpts.set_active(arg)
        print(c(f"Routing switched to remote API #{arg} "
                f"({self.cpts.get(arg)['url']}).", GREEN))

    def cmd_cptslocal(self, arg):
        self.cpts.deactivate()
        print(c("Routing switched back to LOCAL models (Ollama/demo).", GREEN))

    def cmd_cptsdel(self, arg):
        arg = arg.strip()
        if not arg.isdigit():
            print(c("Usage: /cptsdel <remote number>", YELLOW))
            return
        was_active = self.cpts.active == arg
        r = self.cpts.remove(arg)
        if not r:
            print(c(f"Remote API #{arg} not found.", RED))
            return
        msg = f"Remote API #{arg} ({r['url']}) deleted."
        if was_active:
            msg += " Routing fell back to local models."
        print(c(msg, GREEN))


    # ---- ollama runner with graceful fallback (demo mode if offline) ----
    def _runner(self, model, prompt, tps, on_token, stop_flag, number=None):
        # /cpts routing: an active remote ServerCloud API answers instead of
        # the local Ollama. ChatML context is unwrapped to the last user
        # turn — the remote host keeps its own history per key.
        remote_slot = self.cpts.active
        if remote_slot:
            return self._cpts_runner(remote_slot, model, prompt, tps,
                                     on_token, stop_flag)
        if not self.ok:
            return self._demo_runner(model, prompt, tps, on_token,
                                     stop_flag, number)
        try:
            return ollama_api.generate_stream(model, prompt, tps,
                                              on_token, stop_flag,
                                              options=self.opts.payload_options(number))
        except Exception as e:
            raise RuntimeError(f"ollama error: {e}")

    def _cpts_runner(self, slot, model, prompt, tps, on_token, stop_flag):
        """Route one generation through a remote ServerCloud API (/cpts)."""
        rem = self.cpts.get(slot)
        if not rem:
            raise RuntimeError(f"cpts remote #{slot} disappeared")
        client = CptsClient(rem["url"], rem["key"])
        # unwrap ChatML -> last user turn (remote has no access to our ctx)
        question = prompt
        if "<|im_start|>" in prompt:
            chunks = [p for p in prompt.split("<|im_start|>")
                      if p.startswith("user")]
            if chunks:
                question = chunks[-1].split("<|im_end|>")[0].split("\n", 1)[-1]
        start = time.time()
        try:
            text = client.chat(model, question,
                               options=self.opts.payload_options(None))
        except Exception as e:
            self.cpts.remotes[slot]["calls_fail"] += 1
            self.cpts.remotes[slot]["last_status"] = f"fail: {e}"
            self.cpts.save()
            raise RuntimeError(f"cpts remote #{slot} error: {e}")
        if stop_flag.is_set():
            # user aborted while we waited for the remote — don't stream
            n = max(len(text.split()), 1)
            dur = max(time.time() - start, 0.001)
        else:
            n = max(len(text.split()), 1)
            dur = max(time.time() - start, 0.001)
            # stream the answer in word-chunks so the UI looks identical
            for w in text.split(" "):
                if stop_flag.is_set():
                    break
                on_token(w + " ")
        self.cpts.remotes[slot]["calls_ok"] += 1
        self.cpts.remotes[slot]["last_status"] = f"ok ({dur:.1f}s)"
        self.cpts.save()
        stats = {"source": f"cpts#{slot}", "eval_count": n,
                 "eval_duration_us": int(dur * 1e6),
                 "prompt_eval_count": len(question), "real_tps": n / dur}
        return text, n, dur, stats

    @staticmethod
    def _demo_runner(model, prompt, tps, on_token, stop_flag, number=None):
        """Offline demo so the UX is testable without Ollama installed."""
        # show only the last user turn, not the raw ChatML context blob
        last_user = prompt
        if "<|im_start|>" in prompt:
            chunks = [part for part in prompt.split("<|im_start|>")
                      if part.startswith("user")]
            if chunks:
                last_user = chunks[-1].split("<|im_end|>")[0]
                last_user = last_user.split("\n", 1)[-1]
        text = (f"[DEMO MODE — Ollama offline] I am {model}. You asked: "
                f"{last_user!r}. Install & start Ollama (pkg install ollama; "
                f"ollama serve) to get real answers generated locally.")
        start = time.time()
        words = text.split(" ")
        n = 0
        period = 1.0 / max(tps, 0.1)
        for w in words:
            if stop_flag.is_set():
                break
            time.sleep(period)
            on_token(w + " ")
            n += 1
        stats = {"source": "demo", "eval_count": n, "eval_duration_us": 0,
                 "prompt_eval_count": 0, "real_tps": 0.0}
        return text, n, time.time() - start, stats

    # ---- commands ----
    def cmd_models(self):
        n = len(MODEL_CATALOG)
        heavy = sum(1 for _, s, _, _ in MODEL_CATALOG if s >= HEAVY_THRESHOLD_GB)
        print(c(f"Catalog of {n} local models ({heavy} heavy >16 GB; "
                f"use /bmc for 25 GB+ giants):", BOLD))
        for line in catalog_lines():
            print("  " + line)

    def cmd_bmc(self):
        """/bmc — Big Model Catalog: 25 GB+ giants only."""
        lines = giant_catalog_lines()
        print(c(f"BIG MODEL CATALOG (/bmc) — {len(lines)} models needing "
                f">=25 GB free SSD:", BOLD))
        for line in lines:
            print("  " + c(line, MAGENTA))
        print(c("Tip: /dl <catalog number from /models> to download any of them. "
                "Check your disk first with /autt.", YELLOW))

    def confirm_download(self, name):
        ans = input(c(f"Download [{name}]? Y/n ", YELLOW)).strip().lower()
        return ans in ("", "y", "yes", "д")

    def _disk_check(self, size_gb, name):
        """Warn/confirm when model needs more SSD than available."""
        hw = hardware.measure()
        free = hw["disk_free_gb"]
        need = max(size_gb * 1.2, 0.5)   # headroom for unpacking
        if size_gb >= HEAVY_THRESHOLD_GB:
            print(c(f"NOTE: [{name}] is a HEAVY model (>{HEAVY_THRESHOLD_GB:.0f} GB). "
                    f"It targets servers/workstations with big SSDs.", YELLOW))
        if free and free < need:
            print(c(f"WARNING: only {free:.1f} GB free on disk, ~{need:.1f} GB needed.", RED))
            ans = input(c("Download anyway? y/N ", YELLOW)).strip().lower()
            if ans not in ("y", "yes"):
                return False
        return True

    def download(self, idx_or_name):
        name = None
        size = 0.0
        if idx_or_name.isdigit() and 1 <= int(idx_or_name) <= len(MODEL_CATALOG):
            name, size, _, _ = MODEL_CATALOG[int(idx_or_name) - 1]
        else:
            for n, s, _, _ in MODEL_CATALOG:
                if n == idx_or_name:
                    name, size = n, s
        if not name:
            print(c("Unknown model or catalog number.", RED))
            return
        if not self._disk_check(size, name):
            print(c("Cancelled.", YELLOW))
            return
        if not self.confirm_download(name):
            print(c("Cancelled.", YELLOW))
            return
        if self.ok:
            def prog(obj):
                st = obj.get("status", "")
                p = obj.get("completed", 0)
                tot = obj.get("total", 0)
                if tot:
                    pct = 100 * p / tot
                    bar = "#" * int(pct // 4)
                    print(f"\r  downloading {name}: [{bar:<25}] {pct:5.1f}%",
                          end="", flush=True)
                elif st:
                    print(f"\r  {name}: {st}", end="", flush=True)
            ok = ollama_api.pull_model(name, prog)
            print()
            if not ok:
                print(c("Download failed.", RED))
                return
        num = self.reg.add(name, size)
        print(c(f"Model ready: [{num}] {name}", GREEN))

    def _resolve_model(self, key, quiet=False, remote_aware=True):
        """Resolve model by registry number, exact name, or unique catalog name."""
        num, entry = self.reg.find(key)
        if entry:
            return num, entry["name"]
        # /cpts routing: an active remote API owns its own model list —
        # accept any reference and let the remote host resolve/validate it.
        # (remote_aware=False is used by the /w router itself, which must
        #  first try a LOCAL resolution to translate 'qwen' -> 'qwen2.5:0.5b')
        slot = self.cpts.active if remote_aware else None
        if slot:
            rem = self.cpts.get(slot)
            if rem:
                return "?", str(key)
        # not in registry: try catalog (user may ask a catalog model directly)
        matches = [(n, s) for n, s, _, _ in MODEL_CATALOG if n == key]
        if not matches:
            matches = [(n, s) for n, s, _, _ in MODEL_CATALOG if n.startswith(key)]
        if len(matches) == 1:
            return "?", matches[0][0]
        if len(matches) > 1:
            if not quiet:
                print(c(f"Ambiguous model '{key}': {', '.join(n for n, _ in matches[:5])}...", YELLOW))
            return None, None
        if not quiet:
            print(c(f"Unknown model: {key}. Download it with /dl <number> "
                    f"(see /models) or use its exact name.", RED))
        return None, None

    def ask(self, rest):
        parts = rest.split(maxsplit=1)
        if len(parts) < 2:
            print(c("Usage: /w [model number or name] [question]", YELLOW))
            return
        key, question = parts
        # /cpts routing: the remote API is the engine — its own host keeps
        # per-key history, so we send ONLY the current question (no local
        # ChatML context) and store the exchange under a dedicated
        # "cpts#<slot>" chat key to keep local model histories clean.
        slot = self.cpts.active
        if slot:
            # Forward the user's reference VERBATIM (usually a catalog/
            # registry number like "01"). The remote host maps numbers to
            # ITS OWN models and whitelists keys by number; translating
            # through our local catalog would send a name the host may
            # not have (e.g. we call #01 llama3.2:1b, host calls it mistral).
            fwd = key
            job = self.mgr.submit(slot, f"remote#{slot} ({key})", question,
                                  self.tps, prompt_full=question,
                                  chat=self.chat, user_turn_added=True,
                                  key_owner=f"cpts#{slot}")
            self.chat.add(f"cpts#{slot}", "user", question)
            print(c(f"Job #{job.id} started → remote API #{slot} "
                    f"[{fwd}] @ cap {self.tps:.1f} tok/s", GREEN))
            return
        num, model_name = self._resolve_model(key)
        if not model_name:
            return
        # chat mode: build prompt with full dialog context (system + history),
        # then register the user turn once (no double insert into history)
        prompt = self.chat.build_prompt(num, question)
        self.chat.add(num, "user", question)
        job = self.mgr.submit(num, model_name, question, self.tps,
                              prompt_full=prompt, chat=self.chat,
                              user_turn_added=True)
        hist = len(self.chat.history(num)) - 1   # exclude just-added user turn
        print(c(f"Job #{job.id} started → [{num}] {model_name} "
                f"@ cap {self.tps:.1f} tok/s (chat history: {hist} msg)", GREEN))

    def cmd_sys(self, rest):
        """/sys [model|all] [text] — system prompt (role / answer language)."""
        parts = rest.split(maxsplit=1)
        if not parts:
            cur_all = self.chat.get_system("all")
            print(c(f"System prompt (session): {cur_all or '(not set)'}", YELLOW))
            return
        if len(parts) == 1:
            target, text = "all", parts[0]
        else:
            key, text = parts
            num, _ = self._resolve_model(key)
            target = num if num and num != "?" else ("all" if key == "all" else None)
            if target is None:
                return
        self.chat.set_system(target, text)
        scope = "whole session" if target == "all" else f"model [{target}]"
        print(c(f"System prompt set for {scope}: {text!r}", GREEN))

    def cmd_opt(self, rest):
        """(/opt|/o) [model] [key value | key | unset key] — gen settings.

        Accepted forms (all documented in the usage line):
          /opt                          -> session summary
          /opt temperature              -> show one option (session)
          /opt 01 temperature           -> show one option (model 01)
          /opt temperature 0.7          -> set (session)
          /opt 01 temperature 0.7       -> set (model 01)
          /opt unset temperature        -> reset session key to default
          /opt 01 unset temperature     -> drop model override
        A bare number ('01', '1') is ALWAYS a model reference; anything
        else that resolves to exactly one model is treated as a model too
        (so '/opt qwen temperature' works), unless it is an option keyword.
        """
        parts = rest.split()
        target = None
        if parts:
            first = parts[0].lower()
            looks_like_model = bool(re.fullmatch(r"\d{1,2}", first)) or \
                               (first not in GenOptions.KEYS and first != "unset")
            if looks_like_model:
                num, name = self._resolve_model(parts[0], quiet=True)
                if name:
                    target = str(num)
                    parts = parts[1:]
                elif re.fullmatch(r"\d{1,2}", first):
                    print(c(f"Unknown model '{parts[0]}'. Download it with "
                            f"/dl <number> (see /models).", RED))
                    return
        if not parts:                     # bare /opt -> show effective summary
            scope = f" for model [{target}]" if target else " (session default)"
            print(c("Generation options" + scope + ": " + self.opts.summary(target), BOLD))
            print(c("Usage: /opt [model] temperature 0.7 | top_p 0.9 | "
                    "max_tokens 512 | num_ctx 2048 | seed -1\n"
                    "       /opt [model] temperature   (show one) | "
                    "/opt [model] unset temperature", YELLOW))
            return
        key = parts[0].lower()
        if key == "unset":
            if len(parts) >= 2:
                ok, msg = self.opts.unset(target, parts[1])
            else:
                ok, msg = (False, "Usage: /opt [model] unset <key>")
            print(c(msg, GREEN if ok else RED))
            return
        if len(parts) == 1:               # show single option
            val = self.opts.get(target, key)
            if val is None:
                scope = f"model [{target}]" if target else "session"
                print(c(f"Option '{key}' is not set for {scope} "
                        f"(Ollama default used).", YELLOW))
            else:
                print(c(f"{key} = {val}" +
                        (f" (model [{target}])" if target else " (session)"), BOLD))
            return
        ok, msg = self.opts.set(target, key, parts[1])
        print(c(msg, GREEN if ok else RED))

    def cmd_clear(self, rest):
        """/clear [model|all] — reset chat history for one model or all.

        Also accepts 'cpts#N' to wipe the local mirror of a remote API's
        conversation (see /cptsm for slot numbers).
        """
        arg = rest.strip()
        if not arg:
            self.chat.clear()
            print(c("Chat history cleared for ALL models.", GREEN))
            return
        m = re.fullmatch(r"(?i)cpts#(\d+)", arg)
        if m:
            key = f"cpts#{m.group(1)}"
            n = len(self.chat.history(key))
            self.chat.clear(key)
            print(c(f"Chat history cleared for remote API {key} "
                    f"({n} message(s)).", GREEN))
            return
        if arg.lower() == "all":
            self.chat.clear()
            print(c("Chat history cleared for ALL models.", GREEN))
            return
        num, _ = self._resolve_model(arg)
        if not num:
            return
        self.chat.clear(num)
        print(c(f"Chat history cleared for model [{num}].", GREEN))

    def dashboard(self):
        active = self.mgr.active()
        if not active:
            print(c("No active jobs.", YELLOW))
            return
        print(c("Live generation:", BOLD))
        for j in active:
            print(f"  Text — {j.status_line()}")

    # ---- /autt : hardware power measurement + auto token tuning ----
    def cmd_autt(self, arg=""):
        hw = hardware.measure()
        temp = f"{hw['temp_c']:.0f} C" if hw["temp_c"] is not None else "n/a"
        print(c("Hardware power measurement:", BOLD))
        plat = f"{hw.get('os', '?')} {hw.get('os_release', '')} ({hw.get('arch', '?')})"
        vps = hw.get("vps")
        if vps:
            plat += f" — virtualized/VPS: {vps}"
        print(f"   Platform       : {plat}")
        print(f"   CPU cores      : {hw['cores']}")
        print(f"   Load (1 min)   : {hw['load1']:.2f}")
        print(f"   RAM free       : {hw['ram_avail_mb']} MB / {hw['ram_total_mb']} MB")
        print(f"   SSD/HDD free   : {hw['disk_free_gb']} GB")
        print(f"   SoC temp       : {temp}")
        score = hw["power_score"]
        grade = ("weak phone" if score < 25 else
                 "phone / SBC" if score < 45 else
                 "laptop" if score < 70 else "server / workstation")
        print(f"   POWER SCORE    : {score}/100  -> class: {grade}")
        # pick model to tune for
        name = size = None
        if arg:
            num, entry = self.reg.find(arg)
            if entry:
                name, size = entry["name"], entry.get("size_gb", 1.0)
            else:
                for n_, s_, _, _ in MODEL_CATALOG:
                    if n_ == arg or str(MODEL_CATALOG.index((n_, s_, _, _)) + 1) == arg:
                        name, size = n_, s_
        if not name:
            items = self.reg.all()
            if items:
                last = sorted(items)[-1]
                name, size = items[last]["name"], items[last].get("size_gb", 1.0)
                print(c(f"No model given — using latest downloaded [{last}] {name}", YELLOW))
            else:
                name, size = "qwen2.5:0.5b", 0.4
                print(c("No models downloaded — showing recommendation for lightest catalog model.", YELLOW))
        tps = hardware.recommend_tps(size, hw)
        heavy_tag = " [HEAVY >16GB]" if size >= HEAVY_THRESHOLD_GB else ""
        print(c(f"Recommended for [{name}] (~{size} GB){heavy_tag}: "
                f"/stf {tps} tokens/sec", GREEN))
        ans = input(c("Apply this speed now? Y/n ", YELLOW)).strip().lower()
        if ans in ("", "y", "yes"):
            self.tps = tps
            print(c(f"Applied: generation speed = {tps} tok/s", GREEN))

    # ---- custom models: /dnm (GitHub), /dnmf (local file), /delm ----
    def cmd_dnm(self, url):
        if not url:
            print(c("Usage: /dnm [github link to .gguf/Modelfile or repo URL]", YELLOW))
            return
        norm = custom_models.normalize_github_url(url)
        m = re.match(r"https?://(?:www\.)?github\.com/([^/]+)/([^/]+)/?$", norm)
        if m:  # plain repo -> scan README for links
            owner, repo = m.groups()
            print(c(f"Scanning README of {owner}/{repo} for model files...", YELLOW))
            links = custom_models.scan_repo_readme(owner, repo)
            if not links:
                print(c("No .gguf/.bin/HF links found in README. "
                        "Link the file directly: /dnm <raw-url-to-file>", RED))
                return
            print(c("Found model links:", BOLD))
            for i, ln in enumerate(links, 1):
                print(f"  {i}. {ln}")
            sel = input(c(f"Download which number? (1-{len(links)}) ", YELLOW)).strip()
            if not sel.isdigit() or not (1 <= int(sel) <= len(links)):
                print(c("Cancelled.", YELLOW))
                return
            norm = links[int(sel) - 1]
        name = custom_models.name_from_url(norm)
        if not self.confirm_download(name):
            print(c("Cancelled.", YELLOW))
            return
        try:
            dest_dir = os.path.join(DATA_DIR, "custom")
            path = custom_models.fetch_file_to(norm, dest_dir)
        except Exception as e:
            print(c(f"Fetch failed: {e}", RED))
            return
        size = custom_models.size_gb(path)
        ok_ollama = False
        if path.endswith(".gguf") and self.ok:
            mf = os.path.join(DATA_DIR, "custom", f"Modelfile.{name.replace('/', '_').replace(':', '_')}")
            with open(mf, "w") as f:
                f.write(custom_models.make_modelfile(path))
            ok_ollama = custom_models.register_with_ollama(mf, name)
            print(c("Registered in Ollama." if ok_ollama
                    else "Ollama create failed — registry-only mode.", YELLOW))
        elif not self.ok:
            print(c("Ollama offline — registered in ServerCloud registry only (demo).", YELLOW))
        num = self.reg.add(name, size)
        print(c(f"Custom model ready: [{num}] {name} ({size} GB, source: GitHub)", GREEN))

    def cmd_dnmf(self, path):
        if not path:
            print(c("Usage: /dnmf [path to .gguf or Modelfile on device]", YELLOW))
            return
        path = os.path.expanduser(path.strip())
        if not os.path.exists(path):
            print(c(f"File not found: {path}", RED))
            return
        base = os.path.basename(path)
        name = f"custom/{re.sub(r'\\.gguf$', '', base, flags=re.I).lower()}:latest"
        if not self.confirm_download(name):
            print(c("Cancelled.", YELLOW))
            return
        size = custom_models.size_gb(path)
        if not self._disk_check(0, name):
            return
        ok_ollama = False
        if self.ok:
            if path.endswith(".gguf"):
                mf = os.path.join(DATA_DIR, "custom", f"Modelfile.{name.replace('/', '_').replace(':', '_')}")
                os.makedirs(os.path.dirname(mf), exist_ok=True)
                with open(mf, "w") as f:
                    f.write(custom_models.make_modelfile(path))
                ok_ollama = custom_models.register_with_ollama(mf, name)
            elif base.lower().startswith("modelfile"):
                ok_ollama = custom_models.register_with_ollama(path, name)
            print(c("Registered in Ollama." if ok_ollama
                    else "Ollama create failed — registry-only mode.", YELLOW))
        else:
            print(c("Ollama offline — registered in ServerCloud registry only (demo).", YELLOW))
        num = self.reg.add(name, size)
        print(c(f"Local model ready: [{num}] {name} ({size} GB, source: local file)", GREEN))

    # ---- Hugging Face: /hf (models), /hfd (datasets), /hftok ----
    def cmd_hf(self, rest):
        """Search HF text-generation models and download a GGUF into Ollama."""
        if not rest:
            print(c("Usage: /hf <search query>   e.g. /hf qwen2.5 gguf", YELLOW))
            print(c("       /hfd <query> — search HF datasets (database)", YELLOW))
            print(c("       /hftok <HF token> — set token for gated repos "
                    "(or env SERVERCLOUD_HF_TOKEN)", YELLOW))
            return
        try:
            print(c(f"Searching Hugging Face for '{rest}'...", YELLOW))
            results = huggingface_api.search_models(rest, limit=15)
        except huggingface_api.HFError as e:
            print(c(str(e), RED))
            return
        if not results:
            print(c("Nothing found.", YELLOW))
            return
        usable = []
        for m in results:
            try:
                ggufs = huggingface_api.list_gguf_files(m["id"])
            except huggingface_api.HFError:
                ggufs = []
            if ggufs:
                smallest = min(ggufs, key=lambda x: x[1])
                usable.append((m["id"], smallest[0], smallest[1] / 1048576 ** 3))
        if not usable:
            print(c("Found models but none has .gguf quantizations.", YELLOW))
            for m in results[:8]:
                print(f"    {m['id']}  (downloads {m['downloads']})")
            print(c("Tip: search with 'gguf' in the query.", YELLOW))
            return
        print(c(f"GGUF-capable models ({len(usable)}):", BOLD))
        for i, (rid, fn, gb) in enumerate(usable, 1):
            print(f"  {i:>2}. {rid:<45} smallest file ~{gb:5.1f} GB [{fn}]")
        sel = input(c(f"Download which number? (1-{len(usable)}) ", YELLOW)).strip()
        if not sel.isdigit() or not (1 <= int(sel) <= len(usable)):
            print(c("Cancelled.", YELLOW))
            return
        rid, fn, gb = usable[int(sel) - 1]
        if re.search(r"-\d{5}-of-\d{5}\.gguf$", fn, re.I):
            print(c(f"Note: '{fn}' is one shard of a multi-part GGUF; Ollama "
                    "needs all parts. Single-file quant recommended.", YELLOW))
        name = huggingface_api.name_from_repo(rid, fn)
        if not self.confirm_download(name):
            print(c("Cancelled.", YELLOW))
            return
        dest_dir = os.path.join(DATA_DIR, "custom")
        url = huggingface_api.gguf_download_url(rid, fn)
        print(c(f"Downloading {rid}/{fn} (~{gb:.1f} GB)...", YELLOW))
        try:
            path = huggingface_api.fetch_file_to(url, dest_dir)
        except huggingface_api.HFError as e:
            print(c(str(e), RED))
            return
        size = huggingface_api.size_gb(path)
        ok_ollama = False
        if self.ok:
            mf = os.path.join(dest_dir, f"Modelfile.{name.replace('/', '_').replace(':', '_')}")
            with open(mf, "w") as f:
                f.write(huggingface_api.make_modelfile(path))
            ok_ollama = huggingface_api.register_with_ollama(mf, name)
            print(c("Registered in Ollama." if ok_ollama
                    else "Ollama create failed — registry-only mode.", YELLOW))
        else:
            print(c("Ollama offline — registered in ServerCloud registry only (demo).", YELLOW))
        num = self.reg.add(name, size)
        print(c(f"HF model ready: [{num}] {name} ({size} GB, source: huggingface.co/{rid})", GREEN))

    def cmd_hfd(self, rest):
        """Access the Hugging Face dataset database: search + download files."""
        if not rest:
            print(c("Usage: /hfd <dataset query>   e.g. /hfd gsm8k", YELLOW))
            return
        try:
            print(c(f"Searching HF dataset database for '{rest}'...", YELLOW))
            results = huggingface_api.search_datasets(rest, limit=15)
        except huggingface_api.HFError as e:
            print(c(str(e), RED))
            return
        if not results:
            print(c("Nothing found.", YELLOW))
            return
        print(c("Datasets:", BOLD))
        for i, d in enumerate(results, 1):
            flag = " [gated]" if d["gated"] else ""
            print(f"  {i:>2}. {d['id']:<50} dl:{d['downloads']:>9,} likes:{d['likes']}{flag}")
        sel = input(c(f"Open which number? (1-{len(results)}, Enter=cancel) ", YELLOW)).strip()
        if not sel.isdigit() or not (1 <= int(sel) <= len(results)):
            print(c("Cancelled.", YELLOW))
            return
        rid = results[int(sel) - 1]["id"]
        try:
            files = huggingface_api.list_repo_files(rid, is_dataset=True)
        except huggingface_api.HFError as e:
            print(c(str(e), RED))
            return
        data_files = [(p, s) for p, s in files
                      if p.lower().endswith((".jsonl", ".json", ".csv", ".parquet", ".tsv", ".txt"))]
        if not data_files:
            print(c(f"No tabular/text data files in {rid} (only parquet-less repo).", YELLOW))
            return
        print(c(f"Data files in {rid}:", BOLD))
        for i, (p, s) in enumerate(data_files[:20], 1):
            print(f"  {i:>2}. {p:<60} {s / 1048576:8.2f} MB")
        sel = input(c(f"Download which file? (1-{min(len(data_files), 20)}) ", YELLOW)).strip()
        if not sel.isdigit() or not (1 <= int(sel) <= min(len(data_files), 20)):
            print(c("Cancelled.", YELLOW))
            return
        p, s = data_files[int(sel) - 1]
        url = f"https://huggingface.co/datasets/{rid}/resolve/main/{p}"
        dest_dir = os.path.join(DATA_DIR, "datasets", rid.replace("/", "__"))
        try:
            path = huggingface_api.fetch_file_to(url, dest_dir)
        except huggingface_api.HFError as e:
            print(c(str(e), RED))
            return
        print(c(f"Dataset file saved: {path} ({os.path.getsize(path)} bytes)", GREEN))

    def cmd_hftok(self, arg):
        if arg:
            os.environ["SERVERCLOUD_HF_TOKEN"] = arg.strip()
            print(c("HF token set for this session (gated repos unlocked if "
                    "license accepted on huggingface.co).", GREEN))
        else:
            print(c("Current token: " + ("set" if os.environ.get("SERVERCLOUD_HF_TOKEN")
                                          else "not set"), YELLOW))
            print(c("Usage: /hftok <your-huggingface-token>", YELLOW))

    def cmd_delm(self, key):
        if not key:
            print(c("Usage: /delm [model name or number]", YELLOW))
            return
        num, entry = self.reg.remove(key)
        if not num:
            print(c(f"Model not found: {key}", RED))
            return
        # try removing from ollama too
        removed_ollama = ""
        if self.ok:
            import subprocess
            try:
                r = subprocess.run(["ollama", "rm", entry["name"]],
                                   capture_output=True, text=True, timeout=60)
                if r.returncode == 0:
                    removed_ollama = " (+ deleted from Ollama storage)"
            except Exception:
                pass
        print(c(f"Deleted [{num}] {entry['name']}{removed_ollama}", GREEN))

    def cmd_agent(self, arg=""):
        """Launch the ServerCloud coding agent (servercloud/agent package)."""
        try:
            import argparse as _ap
            from servercloud.agent.cli import run_repl
            from servercloud.agent.config import DEFAULT_MODEL
        except ImportError as e:
            print(c(f"Agent package missing ({e}). "
                    "Extract servercloud-agent.zip into the repo root.", RED))
            return
        model = arg.strip() or DEFAULT_MODEL
        print(c(f"Starting ServerCloud Agent with model '{model}'. "
                "Type /help for agent commands, /exit to return.", MAGENTA))
        ns = _ap.Namespace(
            model=model, dir=os.getcwd(),
            url=ollama_api.base_url(),
            auto_edit=False, yolo=False, no_bash=False, no_web=False,
            max_steps=15)
        try:
            run_repl(ns)
        except Exception as e:                       # noqa: BLE001
            print(c(f"Agent error: {e}", RED))
        print(c("Back to ServerCloud.", GREEN))

    def status_loop(self, stop_event):
        last_print = 0.0
        while not stop_event.is_set():
            now = time.time()
            lines = self.mgr.result_lines()
            for ln in lines:
                sys.stdout.write("\n" + c(ln, MAGENTA) + "\n")
                sys.stdout.flush()
            if now - last_print >= 1.0 and self.mgr.active():
                last_print = now
                for j in self.mgr.active():
                    sys.stdout.write(f"\r  ⚙ {j.status_line()}   ")
                    sys.stdout.flush()
            time.sleep(0.2)
        print()

    # ---- main REPL ----
    def run(self):
        # ---- TruckOS boot sequence (POST + bootloader menu + init) ----
        interactive = sys.stdin.isatty() and not os.environ.get("SERVERCLOUD_NOBOOT")
        try:
            runlevel, _cfg = truck_boot.boot(DATA_DIR, interactive=interactive)
        except Exception as e:                        # noqa: BLE001
            print(c(f"TruckOS boot failed ({e}) — continuing in bare mode.",
                    YELLOW))
            runlevel = "servercloud"
        if self.kernel.state != "running":
            # entry 4 / bare mode: bring ring 0 up anyway so userland has a kernel
            try:
                self.kernel = truck_boot._load_kernel(self.sysconfig)
                truck_init.boot(runlevel if runlevel != "servercloud"
                                else "default", self.sysconfig)
            except Exception as e:                    # noqa: BLE001
                print(c(f"kernel load failed ({e}).", RED))
        if runlevel == "rescue":
            self.rescue_shell()
            return
        # ---- main TruckOS menu loop ----
        while True:
            choice = self.main_menu()
            if choice is None:            # user quit
                break
            if choice == 1:
                truck_shell.terminal_shell(self)
            elif choice == 2:
                self.llm_local_entry()
            elif choice == 3:
                truck_shell.script_library_shell(self)
            elif choice == 4:
                truck_shell.settings_shell(self)
        self._shutdown()

    # ---------- TruckOS helpers used by shells / menu ----------
    def trk_main(self, args):
        return trk.main(list(args), DATA_DIR) or 0

    def init_main(self, args):
        return truck_init.main(list(args), DATA_DIR) or 0

    def rescue_shell(self):
        print(c("Rescue shell — minimal environment. Type 'exit' to halt.",
                YELLOW))
        truck_shell.terminal_shell(self)
        self._shutdown()

    def llm_local_entry(self):
        """Menu [2] LLM Local — runs the ServerCloud model console."""
        api_line = (f"API {self._api_url_hint()}" if (self.api and self.api.running)
                    else "API offline")
        print(banner(version_string(), self.ok, self.ver, len(self.reg.all()),
                     extra=[f"    /apis /apim /apioff /apion   network API keys | {api_line}"]))
        if not self.ok:
            print(c("Ollama not reachable — running in DEMO mode "
                    "(fake generation).", YELLOW))
        stop_event = threading.Event()
        dash = None
        if not getattr(self, '_dash_started', False):
            self._dash_started = True
            dash = threading.Thread(target=self.status_loop, args=(stop_event,),
                                    daemon=False)
            dash.start()
        print(c("Type 'help' for commands, 'back' for the TruckOS menu.",
                YELLOW))
        self.repl_loop(prefix="servercloud", on_back=True)
        deadline = time.time() + 60
        while self.mgr.active() and time.time() < deadline:
            time.sleep(0.2)
        time.sleep(0.3)  # final flush of result lines
        stop_event.set()
        if dash:
            dash.join(timeout=2)
        print()

    def _shutdown(self):
        truck_init.shutdown()
        if self.api:
            self.api.stop()
        print("Bye.")

    # ---------- main menu ----------
    TRUCK_LOGO = r"""
   ___                    _    ___  ____
  / _ \_ __ _   _ _ __ | |_ / _ \/ ___|
 | | | | '__| | | | '_ \| __| (_) \___ \
 | |_| | |  | |_| | |_) | |_ \__, |____) |
  \___/|_|   \__,_| .__/ \__|  /_/|____/
                  |_|   ServerCloud embedded userland
"""

    def main_menu(self):
        host = self.sysconfig.get("hostname", "truck-os")
        n_models = len(self.reg.all())
        services = truck_init._load_defs()
        running = sum(1 for s in truck_init._state.values() if s)
        k = self.kernel
        k.tick()                       # keep timer IRQs / scheduler alive
        kern_tag = (f"kernel {truck_kernel.KERNEL_VERSION} up {k.uptime():.0f}s"
                    if k.state == "running" else f"kernel {k.state}")
        print(c(self.TRUCK_LOGO, CYAN))
        print(f"  {host} | v{version_string()} | models: {n_models} | "
              f"services: {running}/{len(services)} running | "
              f"ollama: {'ONLINE' if self.ok else 'OFFLINE'}")
        print(DIM + f"  ring0: {kern_tag} | {k.ncpu} vCPU | "
              f"{k.phys.npages_free}/{k.phys.npages_total} pages free | "
              f"{k.syscall_count} syscalls | {k.timer_irq} timer IRQs"
              + "\033[0m")
        print(BOLD + "\n  ── TruckOS Menu ──────────────────────────────" +
              "\033[0m")
        print("   [1] Terminal          — shell, trk packages, init control")
        print("   [2] LLM Local         — local model factory console")
        print("   [3] Script Library    — reusable server-ops scripts")
        print("   [4] Settings          — TruckOS system configuration")
        print("   [0] Quit")
        try:
            line = input(c(f"{host}# ", GREEN)).strip().lower()
        except (EOFError, KeyboardInterrupt):
            return None
        if line in ("0", "quit", "exit"):
            return None
        if line in ("1", ""):
            return 1
        if line == "2":
            return 2
        if line == "3":
            return 3
        if line == "4":
            return 4
        print(c("Invalid choice.", RED))
        return 0

    # ---- LLM REPL (extracted from old run()) ----
    def repl_loop(self, prefix="servercloud", on_back=False):
        while True:
            try:
                line = input(c(f"{prefix}> ", GREEN)).strip()
            except (EOFError, KeyboardInterrupt):
                break
            if not line:
                continue
            low = line.lower()
            if on_back and low in ("back", "menu"):
                break
            # dispatch on the FIRST whitespace-delimited word only, so that
            # '/watson test' is never mistaken for '/w atson test', and
            # '/dlsx' is reported as an unknown command instead of running /dl.
            head = low.split(maxsplit=1)[0]
            arg = line[len(head):].strip()
            if head in ("quit", "exit"):
                break
            elif head == "help":
                print("/models | /bmc (25GB+ giants) | /dl <n> | /list | "
                      "/w <model> <text> (chat) | "
                      "/sys [model|all] <prompt> | /opt [model] <key> [val] | "
                      "/clear [model] | /stf <tps> | /autt [model] | "
                      "/dnm <github-url> | /dnmf <path> | /delm <name/#> | "
                      "/hf <query> (HuggingFace models) | /hfd <query> (HF datasets) | "
                      "/hftok [token] | "
                      "/apis <01,02> <Y/N hist> <req/min> <slot#> | "
                      "/apim | /apioff <#> | /apion <#> | /apidel <#> | "
                      "/cpts <SCA-key> [url] | /cptsm | /cptsuse <#> | "
                      "/cptslocal | /cptsdel <#> | "
                      "/agent [model] (coding agent) | "
                      "/status | /stop <job id> | back | quit")
            elif head == "/models":
                self.cmd_models()
            elif head == "/bmc":
                self.cmd_bmc()
            elif head == "/dnmf":
                self.cmd_dnmf(arg)
            elif head == "/dnm":
                self.cmd_dnm(arg)
            elif head == "/hf":
                self.cmd_hf(arg)
            elif head == "/hfd":
                self.cmd_hfd(arg)
            elif head == "/hftok":
                self.cmd_hftok(arg)
            elif head == "/delm":
                self.cmd_delm(arg)
            elif head == "/autt":
                self.cmd_autt(arg)
            elif head == "/dl":
                if arg:
                    self.download(arg)
                else:
                    print(c("Usage: /dl <catalog number>", YELLOW))
            elif head == "/list":
                items = self.reg.all()
                if not items:
                    print(c("Nothing downloaded yet. Use /dl <number>.", YELLOW))
                for n, e in items.items():
                    print(f"  [{n}] {e['name']} (~{e['size_gb']} GB)")
            elif head == "/w":
                self.ask(arg)
            elif head == "/sys":
                self.cmd_sys(arg)
            elif head == "/opt":
                self.cmd_opt(arg)
            elif head == "/clear":
                self.cmd_clear(arg)
            elif head == "/stf":
                try:
                    v = float(arg)
                    if not (0.1 <= v <= 1000):
                        raise ValueError
                    self.tps = v
                    print(c(f"Generation speed set to {v:.1f} tokens/sec", GREEN))
                except ValueError:
                    print(c("Usage: /stf <tokens per second> (0.1..1000)", YELLOW))
            elif head == "/status":
                self.dashboard()
            elif head == "/apis":
                self.cmd_apis(arg)
            elif head == "/apim":
                self.cmd_apim(arg)
            elif head == "/apioff":
                self.cmd_apioff(arg)
            elif head == "/apion":
                self.cmd_apion(arg)
            elif head == "/apidel":
                self.cmd_apidel(arg)
            elif head == "/cpts":
                self.cmd_cpts(arg)
            elif head == "/cptsm":
                self.cmd_cptsm(arg)
            elif head == "/cptsuse":
                self.cmd_cptsuse(arg)
            elif head == "/cptslocal":
                self.cmd_cptslocal(arg)
            elif head == "/cptsdel":
                self.cmd_cptsdel(arg)
            elif head == "/agent":
                self.cmd_agent(arg)
            elif head == "/trk":
                self.trk_main(line.split()[1:])
            elif head == "/stop":
                found = False
                for j in self.mgr.jobs:
                    if str(j.id) == arg and j.state == "generating":
                        j.stop_flag.set()
                        found = True
                print(c("Job stopped." if found else "Job not found/active.",
                        YELLOW if found else RED))
            else:
                print(c("Unknown command. Type help.", RED))

def main():
    App().run()


if __name__ == "__main__":
    main()
