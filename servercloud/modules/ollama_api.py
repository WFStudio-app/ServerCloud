"""Ollama HTTP client (works on Termux via pkg install ollama / or remote host).

Uses only the standard library. Base URL configurable through env
SERVERCLOUD_OLLAMA_URL (default http://127.0.0.1:11434).
"""

import json
import os
import urllib.request
import urllib.error

DEFAULT_URL = "http://127.0.0.1:11434"


def base_url() -> str:
    return os.environ.get("SERVERCLOUD_OLLAMA_URL", DEFAULT_URL).rstrip("/")


def _request(path: str, payload=None, timeout=30):
    """Open an HTTP request and return the LIVE response object.

    BUGFIX: previously this returned the response from INSIDE a `with`
    block — the context manager closed the socket on return, so every
    caller that re-entered `with resp:` hit
    "ValueError: I/O operation on closed file" and Ollama looked dead
    even when it was running (forced demo mode). Callers are responsible
    for closing the returned response (they all use `with _request(...)`).
    """
    url = base_url() + path
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data,
                                 headers={"Content-Type": "application/json"})
    return urllib.request.urlopen(req, timeout=timeout)


def is_alive() -> bool:
    try:
        with _request("/api/version", timeout=5) as r:
            return r.status == 200
    except Exception:
        return False


def server_version() -> str:
    try:
        with _request("/api/version", timeout=5) as r:
            return json.loads(r.read().decode()).get("version", "?")
    except Exception:
        return "?"


def list_local_models():
    """Return names of models already pulled into the Ollama library."""
    try:
        with _request("/api/tags", timeout=10) as r:
            data = json.loads(r.read().decode())
        return [m["name"] for m in data.get("models", [])]
    except Exception:
        return []


def pull_model(name: str, on_progress=None, timeout=3600):
    """Download a model; call on_progress(dict) with status lines.

    Returns True on success.
    """
    try:
        with _request("/api/pull", {"name": name, "stream": True},
                      timeout=timeout) as resp:
            saw_error = None
            for raw in resp:
                line = raw.decode(errors="replace").strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if on_progress:
                    on_progress(obj)
                if obj.get("error"):
                    saw_error = str(obj["error"])
                    break
                st = str(obj.get("status", "")).lower()
                if st in ("success", "already exists"):
                    break   # confirmed success
            # BUGFIX: some Ollama versions end the stream without a final
            # {"status":"success"} line (last message is e.g. {"digest":...}
            # or "verifying layers"). If the stream completed WITHOUT an
            # error we treat it as success — previously this returned False
            # and printed "Download failed" for a model that actually pulled.
            if saw_error:
                return False
            return True
    except Exception:
        return False


def generate_stream(model: str, prompt: str, tokens_per_sec: float,
                    on_token=None, stop_flag=None, timeout=600, options=None):
    """Generate an answer token-by-token, throttled to <= tokens_per_sec.

    Returns (full_text, n_tokens, elapsed_seconds, stats) or raises.
    `stats` carries REAL metrics from Ollama's done-payload:
      eval_count, eval_duration, prompt_eval_count, source ("ollama").
    Throttling only caps the display rate; counts stay honest.
    """
    import time
    opts = dict(options or {})
    opts.setdefault("num_predict", 512)
    payload = {"model": model, "prompt": prompt, "stream": True,
               "options": opts}
    text_parts = []
    n_tok = 0
    start = time.time()
    stats = None
    with _request("/api/generate", payload, timeout=timeout) as resp:
        period = 1.0 / max(tokens_per_sec, 0.1)
        next_emit = time.time()
        for raw in resp:
            if stop_flag is not None and stop_flag.is_set():
                break
            line = raw.decode(errors="replace").strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            tok = obj.get("response", "")
            if tok:
                # throttle to requested tokens/sec
                wait = next_emit - time.time()
                if wait > 0:
                    time.sleep(wait)
                next_emit = max(next_emit + period, time.time())
                text_parts.append(tok)
                n_tok += 1
                if on_token:
                    on_token(tok)
            if obj.get("done"):
                # REAL metrics from Ollama (not our own timer)
                eval_count = obj.get("eval_count") or 0
                eval_dur_us = obj.get("eval_duration") or 0
                stats = {
                    "source": "ollama",
                    "eval_count": eval_count,
                    "eval_duration_us": eval_dur_us,
                    "prompt_eval_count": obj.get("prompt_eval_count") or 0,
                    "real_tps": (eval_count / (eval_dur_us / 1e6))
                                if eval_dur_us else 0.0,
                }
                n_tok = max(n_tok, eval_count)
                break
    stats = locals().get("stats") or {"source": "ollama", "eval_count": n_tok,
                                      "eval_duration_us": 0,
                                      "prompt_eval_count": 0, "real_tps": 0.0}
    return "".join(text_parts), n_tok, time.time() - start, stats
