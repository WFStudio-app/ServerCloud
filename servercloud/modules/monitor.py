"""monitor — TruckOS live monitor (TWO-pane terminal window).

Opened from the OS with `/mon` (LLM console) or `monitor` (TruckOS
terminal).  It splits the terminal into two side-by-side panes and keeps
redrawing them in real time while generation jobs run:

  LEFT pane   COMPUTE — everything the system is computing right now:
              token stream mirror (every generated token, live), active
              generation jobs, kernel ring-0 counters (timer IRQs,
              syscalls, scheduler tasks, slab, free pages), cluster pool.

  RIGHT pane  LIVE API AUDIT — every request made with an AI/API key,
              streamed as it happens.  Keys are NEVER shown in clear
              text: each one appears as its TRK-35 fingerprint — a fixed
              35-character encrypted digest (see keycipher.py).

The log files under <data_dir>/var/log/ (token_stream.log, api_calls.log)
are the persistent twin of what you see on screen; `tail -f` works too.

All knobs live in sysconfig (editable in BIOS setup / Settings):
  mon_token_stream   1/0  mirror tokens to log+pane
  mon_refresh_sec    redraw interval
  mon_api_log        1/0  record API-key traffic
  mon_mask_len       fingerprint width (fixed at 35 by design)
"""

import os
import sys
import threading
import time

from ..utils.colors import c, GREEN, YELLOW, RED, CYAN, MAGENTA, BOLD, DIM
from .keycipher import TrkCipher, MASK_LEN


def _clear_screen():
    """ANSI clear+home (no-op on dumb/piped terminals) — Termux-friendly."""
    if os.environ.get("TERM") == "dumb" or not sys.stdout.isatty():
        return
    try:
        sys.stdout.write("\033[2J\033[H")
        sys.stdout.flush()
    except Exception:                              # noqa: BLE001
        pass

WIDTH_RATIO = 0.5          # left pane width fraction


class LiveMonitor:
    """Ring buffers + file sinks fed by the app; renders the 2-pane view."""

    RING = 400             # max events kept in memory per channel

    def __init__(self, data_dir, cfg=None, kernel_provider=None,
                 jobs_provider=None, cluster_provider=None):
        self.data_dir = data_dir
        self.cfg = cfg
        self.cipher = TrkCipher(data_dir)
        self._lock = threading.Lock()
        self.tokens = []            # (t, job_label, token)
        self.api_events = []        # (t, key_number, fingerprint, method, path, code)
        self.kernel_provider = kernel_provider
        self.jobs_provider = jobs_provider
        self.cluster_provider = cluster_provider
        self.log_dir = os.path.join(data_dir, "truck", "var", "log")
        os.makedirs(self.log_dir, exist_ok=True)
        self.token_log = os.path.join(self.log_dir, "token_stream.log")
        self.api_log = os.path.join(self.log_dir, "api_calls.log")

    # ------------------------------------------------------- config helpers
    def _flag(self, name, default=1):
        if self.cfg is None:
            return default
        try:
            return int(bool(self.cfg.get(name, default)))
        except (TypeError, ValueError):
            return default

    # ------------------------------------------------------------ ingestion
    def note_token(self, job_label, token):
        """Called for EVERY generated token (from the jobs manager hook)."""
        if not self._flag("mon_token_stream"):
            return
        t = time.time()
        with self._lock:
            self.tokens.append((t, job_label, token))
            if len(self.tokens) > self.RING:
                del self.tokens[:len(self.tokens) - self.RING]
        try:
            with open(self.token_log, "a", encoding="utf-8") as f:
                f.write(f"{time.strftime('%H:%M:%S', time.localtime(t))} "
                        f"[{job_label}] {token}\n")
        except OSError:
            pass

    def note_api_call(self, key_number, method, path, code):
        """Hosted-API traffic — called from the ApiServer audit hook.

        The raw key never reaches this function; the caller (api_server)
        already masked it through TRK-35 before handing it over.
        """
        raise RuntimeError("internal: use note_api_call_masked")

    def note_api_call_masked(self, key_number, fingerprint, method, path, code):
        if not self._flag("mon_api_log"):
            return
        t = time.time()
        ev = (t, str(key_number), fingerprint, method, path, code)
        with self._lock:
            self.api_events.append(ev)
            if len(self.api_events) > self.RING:
                del self.api_events[:len(self.api_events) - self.RING]
        try:
            with open(self.api_log, "a", encoding="utf-8") as f:
                f.write(f"{time.strftime('%H:%M:%S', time.localtime(t))} "
                        f"key=#{key_number} trk35={fingerprint} "
                        f"{method} {path} -> {code}\n")
        except OSError:
            pass

    def note_remote_key(self, slot, raw_key, status):
        """Outbound /cpts calls: mask the remote key before logging it."""
        fp = self.cipher.mask(raw_key)
        self.note_api_call_masked(f"cpts#{slot}", fp, "POST",
                                  "/v1/chat", status)

    # ------------------------------------------------------------- snapshot
    def snapshot(self, n_lines=16):
        """Build the two pane texts (also used headless by tests)."""
        left = self._pane_compute(n_lines)
        right = self._pane_api(n_lines)
        return left, right

    def _pane_compute(self, n_lines):
        lines = [c("╔═ TTY1 · COMPUTE ══════════════════════════╗", CYAN)]
        k = self.kernel_provider() if self.kernel_provider else None
        if k is not None and getattr(k, "state", "halted") != "halted":
            k.tick()
            lines.append(f"  ring0 {k.state} | hz={k.hz} | up {k.uptime():.0f}s")
            lines.append(f"  timer IRQ : {k.timer_irq:>10}")
            lines.append(f"  syscalls  : {k.syscall_count:>10}")
            lines.append(f"  tasks     : {len(k.sched.tasks):>10}")
            lines.append(f"  pages free: {k.phys.npages_free}/{k.phys.npages_total}")
            lines.append(f"  slab live : {k.slab.live_bytes()} B")
        jobs = self.jobs_provider() if self.jobs_provider else []
        lines.append(c("  ── generation jobs ──", BOLD))
        if jobs:
            for j in jobs:
                lines.append(f"  ▸ {j.status_line()}")
        else:
            lines.append(DIM + "  (idle — no compute right now)" + "\033[0m")
        cl = self.cluster_provider() if self.cluster_provider else None
        if cl:
            pool = cl.pool()
            lines.append(c("  ── cluster pool ──", BOLD))
            lines.append(f"  nodes={pool['nodes']} cpu={pool['cpu']} "
                         f"ram={pool['ram_mb']}MB disk={pool['disk_gb']}GB")
        lines.append(c("  ── token stream (live) ──", BOLD))
        with self._lock:
            tail = list(self.tokens[-n_lines:])
        if tail:
            buf, cur, chunks = "", None, []
            for (_t, label, tok) in tail:
                if label != cur:
                    if cur is not None:
                        chunks.append((cur, buf))
                    cur, buf = label, ""
                buf += tok
            if cur is not None:
                chunks.append((cur, buf))
            for label, txt in chunks[-4:]:
                txt = txt.strip().replace("\n", " ")
                if len(txt) > 46:
                    txt = "…" + txt[-46:]
                lines.append(f"  [{label}] ".ljust(14) + c(txt, GREEN))
        else:
            lines.append(DIM + "  (no tokens yet — run /w …)" + "\033[0m")
        lines.append(c("╚══════════════════════════════════════════╝", CYAN))
        return lines

    def _pane_api(self, n_lines):
        lines = [c("╔═ TTY2 · API AUDIT (TRK-35) ═════════════╗", MAGENTA)]
        lines.append(DIM + f"  keys shown as {MASK_LEN}-char encrypted"
                     " fingerprints" + "\033[0m")
        lines.append(c("  ── live key traffic ──", BOLD))
        with self._lock:
            tail = list(self.api_events[-n_lines:])
        if tail:
            for (t, kno, fp, method, path, code) in tail:
                col = GREEN if isinstance(code, int) and code == 200 \
                    else (YELLOW if isinstance(code, str) else RED)
                hhmmss = time.strftime("%H:%M:%S", time.localtime(t))
                lines.append(f"  {hhmmss} #{kno:<7} {c(fp, col)}")
                lines.append(f"           {method} {path} → {code}")
        else:
            lines.append(DIM + "  (quiet — no API calls yet;"
                         " create a key with /apis)" + "\033[0m")
        lines.append(c("╚══════════════════════════════════════════╝", MAGENTA))
        return lines

    # --------------------------------------------------------------- render
    def render(self, n_lines=14):
        """Print both panes side by side (the 'second terminal window')."""
        left, right = self.snapshot(n_lines)
        w = max(len(s) for s in left) if left else 40
        out = []
        for i in range(max(len(left), len(right))):
            l = left[i] if i < len(left) else ""
            r = right[i] if i < len(right) else ""
            pad = max(w - _plain_len(l), 0)
            out.append(l + " " * pad + " │ " + r)
        return "\n".join(out)

    def watch(self, seconds=None, refresh=None, stop_event=None):
        """Live loop: re-render until stop_event / Ctrl-C / timeout."""
        if refresh is None:
            try:
                refresh = float(self.cfg.get("mon_refresh_sec", 0.5)) \
                    if self.cfg else 0.5
            except (TypeError, ValueError):
                refresh = 0.5
        refresh = min(max(refresh, 0.2), 5.0)
        start = time.time()
        try:
            while True:
                if stop_event is not None and stop_event.is_set():
                    break
                if seconds is not None and time.time() - start > seconds:
                    break
                _clear_screen()
                print(self.render())
                print(DIM + "  Ctrl-C stops the monitor" + "\033[0m")
                time.sleep(refresh)
        except KeyboardInterrupt:
            pass
        print(c("monitor: stopped.", YELLOW))

    # ---------------------------------------------------------------- dump
    def tail_files(self, n=10):
        """Last n persisted lines of each log (for non-tty contexts)."""
        out = {}
        for name, path in (("tokens", self.token_log), ("api", self.api_log)):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    out[name] = f.readlines()[-n:]
            except OSError:
                out[name] = []
        return out


def _plain_len(s):
    """Visible length ignoring ANSI color escapes."""
    import re
    return len(re.sub(r"\x1b\[[0-9;]*m", "", str(s)))
