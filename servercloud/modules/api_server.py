"""ServerCloud Network API — serve local models over the network.

The host creates API keys inside the REPL (/apis) and runs a small
HTTP server (stdlib only, cross-platform: Linux / Termux / macOS / Windows).
Remote clients authenticate with  Authorization: Bearer SCA-xxxx-xxxx-xxxx
and can query ONLY the models bound to that key.

Endpoints
---------
GET  /api/health                  -> {"ok": true, ...}   (no auth)
GET  /v1/models                   -> list of model numbers/names allowed by key
POST /v1/chat                     -> {"model":"01","messages":[{"role":..,"content":..}],
                                      "stream": false}
POST /v1/generate                 -> {"model":"01","prompt":"...", "stream": false}
GET  /v1/history?model=01         -> chat history (only if key allows history)

Auth: header  "Authorization: Bearer SCA-1234-1234-1234"
      or      "X-API-Key: SCA-1234-1234-1234"
Rate limit: per-key requests-per-minute sliding window (429 on overflow).
"""

import json
import re
import secrets
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

KEY_RE = re.compile(r"^SCA-[A-Za-z0-9]{4}-[A-Za-z0-9]{4}-[A-Za-z0-9]{4}$")


def generate_key() -> str:
    """Random key in the documented SCA-XXXX-XXXX-XXXX shape."""
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no look-alikes
    block = lambda: "".join(secrets.choice(alphabet) for _ in range(4))  # noqa: E731
    return f"SCA-{block()}-{block()}-{block()}"


class ApiKey:
    """One hosted key: models whitelist + history flag + rate limit."""

    def __init__(self, number, key, models, allow_history, rpm, created=None):
        self.number = number                    # host-chosen slot id ("1", "2"...)
        self.key = key
        self.models = [str(m) for m in models]  # registry/catalog numbers allowed
        self.allow_history = bool(allow_history)
        self.rpm = int(rpm)
        self.created = created or time.strftime("%Y-%m-%d %H:%M")
        self.enabled = True
        self.total_requests = 0
        self._hits = deque()                    # timestamps for sliding window

    def check_rate(self) -> bool:
        """True if this request is allowed under the rpm cap."""
        now = time.time()
        while self._hits and now - self._hits[0] > 60.0:
            self._hits.popleft()
        if len(self._hits) >= self.rpm:
            return False
        self._hits.append(now)
        self.total_requests += 1
        return True

    def to_dict(self):
        return {"number": self.number, "key": self.key, "models": self.models,
                "allow_history": self.allow_history, "rpm": self.rpm,
                "created": self.created, "enabled": self.enabled,
                "total_requests": self.total_requests}

    @classmethod
    def from_dict(cls, d):
        k = cls(d["number"], d["key"], d.get("models", []),
                d.get("allow_history", False), d.get("rpm", 60),
                d.get("created"))
        k.enabled = d.get("enabled", True)
        k.total_requests = d.get("total_requests", 0)
        return k


class KeyStore:
    """Persistent store of API keys (JSON file, atomic writes)."""

    def __init__(self, path):
        self.path = path
        self.keys = {}          # number(str) -> ApiKey
        self._lock = threading.Lock()
        self.load()

    def load(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
            self.keys = {str(k): ApiKey.from_dict(v)
                         for k, v in data.get("keys", {}).items()}
        except Exception:
            self.keys = {}

    def save(self):
        try:
            import os
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"keys": {k: v.to_dict()
                                    for k, v in self.keys.items()}}, f, indent=2)
            os.replace(tmp, self.path)
        except Exception:
            pass

    def add(self, key: ApiKey):
        with self._lock:
            self.keys[str(key.number)] = key
            self.save()

    def get_by_number(self, number):
        return self.keys.get(str(number).strip())

    def get_by_key(self, raw_key):
        for k in self.keys.values():
            if k.key == raw_key:
                return k
        return None

    def remove(self, number):
        with self._lock:
            k = self.keys.pop(str(number), None)
            if k:
                self.save()
            return k

    def all(self):
        return dict(self.keys)


class ApiServer:
    """Threaded HTTP server exposing selected local models via keys."""

    def __init__(self, host, port, resolver, generator, history_provider,
                 keystore, app_name="ServerCloud", version="?",
                 record_hook=None, audit_hook=None):
        self.host = host
        self.port = port
        self.resolve_model = resolver        # fn(num_or_name) -> ollama model name | None
        self.generate = generator            # fn(model, prompt, options) -> text
        self.get_history = history_provider  # fn(model_num) -> [{role,content}]
        self.keystore = keystore
        self.app_name = app_name
        self.version = version
        if record_hook is not None:          # fn(model_ref, user_msg, reply) -> persist dialog
            self.record_exchange = record_hook
        # fn(key_number, masked_fingerprint, method, path, code) — called for
        # every authenticated request; the live monitor tails these lines.
        # Keys reach this hook ONLY as TRK-35 fingerprints (see keycipher).
        self.audit_hook = audit_hook
        self._httpd = None
        self._thread = None
        # live HTTP/1.1 connections (keep-alive clients) tracked so that
        # stop() can force-close them instead of hanging on shutdown()
        self._live_conns = set()

    # ---------- lifecycle ----------
    def start(self):
        handler = _make_handler(self)
        self._httpd = ThreadingHTTPServer((self.host, self.port), handler)
        self._thread = threading.Thread(target=self._httpd.serve_forever,
                                        daemon=True)
        self._thread.start()
        return self._httpd.server_address

    def stop(self):
        if self._httpd:
            self._httpd.shutdown()
            # BUGFIX: BaseHTTPRequestHandler with protocol_version="HTTP/1.1"
            # keeps sockets alive; a lingering client connection made
            # shutdown() block forever and the app never exited after `quit`.
            for conn in list(self._live_conns):
                try:
                    conn.close()
                except Exception:
                    pass
            self._live_conns.clear()
            self._httpd.server_close()
            self._httpd = None

    @property
    def running(self):
        return self._httpd is not None

    # ---------- request helpers used by the handler ----------
    def auth(self, headers, count_rate=True):
        raw = ""
        a = headers.get("Authorization", "")
        if a.lower().startswith("bearer "):
            raw = a[7:].strip()
        if not raw:
            raw = headers.get("X-API-Key", "").strip()
        if not raw or not KEY_RE.match(raw):
            return None, "invalid api key format (expected SCA-XXXX-XXXX-XXXX)", 401
        k = self.keystore.get_by_key(raw)
        if not k:
            return None, "unknown api key", 401
        if not k.enabled:
            return None, "api key disabled", 401
        # BUGFIX: rate limit must only apply to generation requests.
        # GET /v1/models and GET /v1/history are metadata calls — counting
        # them burned the rpm budget and made clients hit 429 before they
        # could even see which models their key can use.
        if count_rate and not k.check_rate():
            return None, f"rate limit exceeded ({k.rpm} req/min)", 429
        return k, None, 200


def _make_handler(app_ctx: ApiServer):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "ServerCloud-API"

        # silence default stderr logging into the REPL
        def log_message(self, fmt, *args):
            pass

        def handle_one_request(self):
            app_ctx._live_conns.add(self.connection)
            try:
                super().handle_one_request()
            finally:
                if getattr(self, "close_connection", True):
                    app_ctx._live_conns.discard(self.connection)

        def _json(self, code, obj):
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _read_body(self):
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0:
                return {}
            raw = self.rfile.read(min(length, 4 * 1024 * 1024))
            try:
                return json.loads(raw.decode("utf-8"))
            except Exception:
                return None

        # ---- routes ----
        def _audit(self, key, code):
            """Fire the audit hook with the TRK-35 fingerprint — never raw."""
            if app_ctx.audit_hook is None:
                return
            try:
                from .keycipher import mask_key
                fp = mask_key(key.key)          # 35-char encrypted fingerprint
                app_ctx.audit_hook(key.number, fp, self.command,
                                   self.path.split("?", 1)[0], code)
            except Exception:                   # auditing must never break API
                pass

        def do_GET(self):
            if self.path.startswith("/api/health"):
                return self._json(200, {"ok": True,
                                        "app": app_ctx.app_name,
                                        "version": app_ctx.version})
            key, err, code = app_ctx.auth(self.headers, count_rate=False)
            if not key:
                return self._json(code, {"error": err})
            self._audit(key, 200)
            if self.path.startswith("/v1/models"):
                out = []
                for num in key.models:
                    name = app_ctx.resolve_model(num)
                    if name:
                        out.append({"number": num, "name": name})
                return self._json(200, {"models": out})
            if self.path.startswith("/v1/history"):
                if not key.allow_history:
                    return self._json(403, {"error": "history access disabled for this key"})
                q = self.path.split("?", 1)
                model_num = ""
                if len(q) > 1:
                    for kv in q[1].split("&"):
                        if kv.startswith("model="):
                            model_num = kv[6:]
                if (model_num.lstrip("0") or model_num) not in \
                        [m.lstrip("0") or m for m in key.models]:
                    return self._json(403, {"error": f"model {model_num!r} not bound to this key"})
                return self._json(200, {"history": app_ctx.get_history(model_num)})
            return self._json(404, {"error": "not found"})

        def do_POST(self):
            key, err, code = app_ctx.auth(self.headers)
            if not key:
                return self._json(code, {"error": err})
            self._audit(key, 200)
            body = self._read_body()
            if body is None:
                return self._json(400, {"error": "invalid JSON body"})
            if self.path.startswith("/v1/chat"):
                msgs = body.get("messages") or []
                if not isinstance(msgs, list) or not msgs:
                    return self._json(400, {"error": "messages[] required"})
                parts = []
                for m in msgs:
                    role = str(m.get("role", "user"))
                    content = str(m.get("content", ""))
                    parts.append(f"<|im_start|>{role}\n{content}<|im_end|>")
                prompt = "\n".join(parts) + "\n<|im_start|>assistant\n"
            elif self.path.startswith("/v1/generate"):
                prompt = str(body.get("prompt", ""))
                if not prompt:
                    return self._json(400, {"error": "prompt required"})
            else:
                return self._json(404, {"error": "not found"})
            model_ref = str(body.get("model", ""))
            # a key may be referenced by slot number ("01"), by the real
            # model name ("llama3.2:1b") or by any alias the host resolves
            def _norm(m):
                m = str(m)
                if m.startswith("#"):
                    return m.lstrip("#0") or "#"     # remote slot #N
                return m.lstrip("0") or m
            def _bound(ref):
                r = _norm(ref)
                if r in [_norm(m) for m in key.models]:
                    return True
                resolved = app_ctx.resolve_model(ref)
                if resolved:
                    for m in key.models:
                        if app_ctx.resolve_model(m) == resolved:
                            return True
                return False
            if not _bound(model_ref):
                return self._json(403, {"error": f"model {model_ref!r} not bound to this key",
                                        "allowed": key.models})
            real = app_ctx.resolve_model(model_ref)
            if not real:
                return self._json(404, {"error": f"model {model_ref!r} not available on host"})
            # accept our own names, Ollama-style aliases and an options{} bag;
            # unknown keys are ignored so a stray field never breaks the call
            aliases = {"num_predict": "max_tokens", "max_new_tokens": "max_tokens"}
            options = {}
            for k, v in (body.get("options") or {}).items():
                options[aliases.get(k, k)] = v
            for k in ("temperature", "top_p", "max_tokens", "num_ctx", "seed"):
                if k in body and k not in options:
                    options[k] = body[k]
            try:
                t0 = time.time()
                text = app_ctx.generate(real, prompt, options)
                elapsed = time.time() - t0
            except Exception as e:
                return self._json(502, {"error": f"generation failed: {e}"})
            # persist the dialog so /v1/history (Y-keys) and the REPL see it
            if key.allow_history and hasattr(app_ctx, "record_exchange"):
                if self.path.startswith("/v1/chat"):
                    user_msg = ""
                    for m in reversed(msgs):
                        if str(m.get("role", "user")) == "user":
                            user_msg = str(m.get("content", ""))
                            break
                else:
                    user_msg = prompt
                app_ctx.record_exchange(model_ref, user_msg, text)
            return self._json(200, {"model": real, "requested": model_ref,
                                    "response": text,
                                    "elapsed_s": round(elapsed, 2)})

    return Handler
