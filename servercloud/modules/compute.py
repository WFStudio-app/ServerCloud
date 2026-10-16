"""compute — TruckOS «Вычисления / COMPUTE» subsystem.

Two responsibilities:

1. **File transforms** (quantization & compression).  Every operation runs
   through a *compute mode* and is recorded in a live event log so the
   monitor / "Отслеживание сетевых вычислений" panes can follow it in real
   time.

   * quantize  — reduce the numeric precision of a file's payload
     (q8_0 / q4_0 / q2_k style, applied to raw bytes as block-wise scalar
     quantization; lossy, reversible only up to the chosen step);
   * compress  — lossless RLE + zlib packaging (.trkz container).

2. **Compute modes** — named scheduling profiles that decide *where* work
   goes (CPU cores, virtual GPU, cluster pool) and how hard it is pushed.
   The list is fully configurable via sysconfig key `compute_modes`
   ("name:device:threads:note,..."); unknown names fall back to AUTO.

Container formats (self-describing headers):
    TRKZ  = b"TRKZ" + ver(1) + algo(1) + rle_len(2) + z_raw_len(4) + pad(4)
            [16 bytes]
    TRKQ  = b"TRKQ" + ver(1) + fmt(1)  + block(2)  + orig_len(4)   + pad(4)
            + gmin(1) + span_hi(1) + span_lo(1) + pad(3)   [22 bytes]

    Each block stores its own (min, 16-bit span) triple in the body; the
    header's gmin/gspan fields describe only the first block.  Keeping a
    full 16-bit span per block means blocks whose byte range exceeds 255
    still use an exact scale — this is what makes q8_0 near-lossless on
    arbitrary binary payloads.
"""

import os
import secrets
import string
import struct
import threading
import time
import zlib

# --------------------------------------------------------------- compute modes
MODE_AUTO = "auto"          # pick best device for the current host
MODE_CPU = "cpu"            # local CPU only
MODE_GPU = "gpu"            # virtual GPU (BIOS-tunable iGPU)
MODE_BATCH = "batch"        # off-peak queue, lowest priority
MODE_REALTIME = "realtime"  # priority pinning: dedicated thread, max speed
MODE_CLUSTER = "cluster"    # spread across fused devices (modules.cluster)
MODE_ENERGY = "energy"      # throttle to ~25 % duty cycle
MODE_TURBO = "turbo"        # all cores, no sleep between blocks
MODE_SAFE = "safe"          # checksum every block, abort on mismatch
MODE_SSLGEN = "sslgen"      # «Генерация ССЛ»: bulk random-string generator

DEFAULT_MODES = [MODE_AUTO, MODE_CPU, MODE_GPU, MODE_BATCH, MODE_REALTIME,
                 MODE_CLUSTER, MODE_ENERGY, MODE_TURBO, MODE_SAFE,
                 MODE_SSLGEN]

MODE_INFO = {
    MODE_AUTO:     ("AUTO", "best device per job"),
    MODE_CPU:      ("CPU", "local cores only"),
    MODE_GPU:      ("GPU", "virtual iGPU (BIOS VRAM)"),
    MODE_BATCH:    ("BATCH", "off-peak queue"),
    MODE_REALTIME: ("REALTIME", "pinned high-priority thread"),
    MODE_CLUSTER:  ("CLUSTER", "run on fused device pool"),
    MODE_ENERGY:   ("ENERGY", "25% duty cycle"),
    MODE_TURBO:    ("TURBO", "all cores, zero pause"),
    MODE_SAFE:     ("SAFE", "per-block checksums"),
    MODE_SSLGEN:   ("SSLGEN", "«Генерация ССЛ»: charset/length/count via &&"),
}

# ---------------------------------------------------------------- sslgen
MAX_SSLGEN_LINES = 1_000_000_000_000        # 1 trillion lines per run

SSL_ALPHABETS = {
    "letters": string.ascii_letters,               # a-zA-Z
    "alpha":   string.ascii_letters,               # alias
    "digits":  string.digits,                      # 0-9
    "numbers": string.digits,                      # alias
    "mixed":   string.ascii_letters + string.digits,
    "alnum":   string.ascii_letters + string.digits,
    "hex":     "0123456789abcdef",
    "upper":   string.ascii_uppercase,
    "lower":   string.ascii_lowercase,
}


def parse_sslgen_spec(spec):
    """Parse the one-line «Генерация ССЛ» spec joined by '&&'.

    Format:  len=<N> && charset=<letters|digits|mixed> && lines=<1..1e12>
    Order of the three fields does not matter; each may also be given as a
    bare value (e.g. "8 && letters && 100").  Returns a dict with keys
    length / charset / alphabet / lines, or raises ValueError.
    """
    raw = str(spec or "").replace("—", "-")
    parts = [p.strip().lower() for p in raw.split("&&") if p.strip()]
    if len(parts) < 3:
        raise ValueError(
            "need 3 fields: len=<N> && charset=<letters|digits|mixed> "
            "&& lines=<1..1e12>")
    kv = {}
    for p in parts:
        if "=" in p:
            k, v = p.split("=", 1)
            kv[k.strip()] = v.strip()
        else:                                # positional shorthand
            if p.isdigit():
                # first bare number -> len, second -> lines
                if "len" not in kv:
                    kv["len"] = p
                else:
                    kv["lines"] = p
            elif p in SSL_ALPHABETS:
                kv["charset"] = p
            else:
                raise ValueError(f"unknown charset '{p}'")

    def _pick(*names):
        for n in names:
            if n in kv:
                return kv[n]
        return None

    lv = _pick("len", "length", "chars", "symbols", "sym")
    cv = _pick("charset", "set", "alphabet", "cs")
    nv = _pick("lines", "count", "rows", "n", "strings")
    if lv is None or cv is None or nv is None:
        raise ValueError("missing field(s): len / charset / lines")
    try:
        length = int(lv)
        lines = int(nv)
    except ValueError:
        raise ValueError("len and lines must be integers") from None
    if not 1 <= length <= 4096:
        raise ValueError("len must be 1..4096 chars per line")
    if not 1 <= lines <= MAX_SSLGEN_LINES:
        raise ValueError(f"lines must be 1..{MAX_SSLGEN_LINES:,} (1 trillion)")
    alphabet = SSL_ALPHABETS.get(cv)
    if alphabet is None:
        raise ValueError(f"charset must be one of: "
                         f"{', '.join(sorted(set(SSL_ALPHABETS)))}")
    kind = ("digits" if alphabet == string.digits else
            "letters" if alphabet == string.ascii_letters else "mixed")
    return {"length": length, "lines": lines,
            "charset": kind, "alphabet": alphabet}

# quantization formats: name -> (block size, bits kept, human label)
QUANT_FORMATS = {
    "q8_0": (32, 8, "8-bit, near-lossless"),
    "q4_0": (32, 4, "4-bit, half size"),
    "q4_k": (64, 4, "4-bit k-quants"),
    "q3_k": (64, 3, "3-bit k-quants"),
    "q2_k": (64, 2, "2-bit, aggressive"),
    "iq1":  (32, 1, "1-bit importance quant"),
}


def _norm_mode(name):
    m = str(name or "").strip().lower()
    return m if m in DEFAULT_MODES else MODE_AUTO


class ComputeEngine:
    """Quantizer / compressor with live accounting and an event journal."""

    def __init__(self, data_dir=None, cfg=None, gpu_provider=None):
        self.data_dir = data_dir or "."
        self.cfg = cfg
        self._gpu_provider = gpu_provider or (lambda: {"enabled": False,
                                                        "vram_mb": 0})
        self._lock = threading.Lock()
        self.events = []                     # recent journal entries
        self.sslgen_stop = None              # callable -> True to abort a run
        self.stats = {
            "jobs": 0, "bytes_in": 0, "bytes_out": 0,
            "quantized": 0, "compressed": 0, "restored": 0,
            "sslgen_lines": 0, "sslgen_runs": 0,
            "last_ms": 0.0, "total_ms": 0.0,
        }

    # ------------------------------------------------------------- helpers
    def modes(self):
        """Configured mode list (sysconfig `compute_modes`, else defaults)."""
        raw = None
        if self.cfg is not None:
            try:
                raw = self.cfg.get("compute_modes")
            except Exception:                          # noqa: BLE001
                raw = None
        if not raw:
            return list(DEFAULT_MODES)
        out = []
        for chunk in str(raw).split(","):
            name = chunk.strip().lower()
            if name:
                out.append(_norm_mode(name))
        return out or list(DEFAULT_MODES)

    def active_mode(self):
        if self.cfg is not None:
            try:
                return _norm_mode(self.cfg.get("compute_mode", MODE_AUTO))
            except Exception:                          # noqa: BLE001
                pass
        return MODE_AUTO

    def _device_for(self, mode):
        """Resolve AUTO/GPU to a concrete device given BIOS virtual hardware."""
        gpu = self._gpu_provider() or {}
        if mode == MODE_AUTO:
            return "gpu" if gpu.get("enabled") else "cpu"
        if mode == MODE_GPU and not gpu.get("enabled"):
            return "cpu (gpu disabled in BIOS)"
        return {"cluster": "cluster-pool", "realtime": "cpu-pinned",
                "turbo": "cpu-all", "batch": "cpu-idle",
                "energy": "cpu-throttled", "safe": "cpu-verified"}.get(
                    mode, mode)

    def _note(self, kind, path, detail, ms, dev):
        line = (f"{time.strftime('%H:%M:%S')} [{kind}] {os.path.basename(path)}"
                f" · {detail} · {ms:.1f} ms · dev={dev}")
        with self._lock:
            self.events.append(line)
            del self.events[:-60]           # keep last 60 journal lines
        return line

    # --------------------------------------------------------- compression
    @staticmethod
    def _rle(data):
        out = bytearray()
        i = 0
        n = len(data)
        while i < n:
            run = 1
            while i + run < n and data[i] == data[i + run] and run < 255:
                run += 1
            out += bytes([data[i], run])
            i += run
        return bytes(out)

    @staticmethod
    def _unrle(data):
        out = bytearray()
        for i in range(0, len(data) - 1, 2):
            out += bytes([data[i]]) * data[i + 1]
        return bytes(out)

    def compress_file(self, src, dst=None, mode=None, level=9):
        """Lossless TRKZ container: RLE pre-pass + zlib."""
        t0 = time.perf_counter()
        mode = _norm_mode(mode or self.active_mode())
        with open(src, "rb") as f:
            raw = f.read()
        packed = zlib.compress(self._rle(raw), level)
        hdr = struct.pack("<4sBBHI4s", b"TRKZ", 1, 0, min(len(raw), 0xFFFF),
                          len(packed), b"\x00" * 4)[:16]
        blob = hdr + packed
        dst = dst or src + ".trkz"
        with open(dst, "wb") as f:
            f.write(blob)
        ms = (time.perf_counter() - t0) * 1000
        ratio = (1 - len(blob) / max(1, len(raw))) * 100
        with self._lock:
            self.stats["jobs"] += 1
            self.stats["bytes_in"] += len(raw)
            self.stats["bytes_out"] += len(blob)
            self.stats["compressed"] += 1
            self.stats["last_ms"] = ms
            self.stats["total_ms"] += ms
        return self._note("COMPRESS", src,
                          f"{len(raw)}B→{len(blob)}B ({ratio:+.1f}%) "
                          f"mode={mode}", ms, self._device_for(mode))

    def decompress_file(self, src, dst=None):
        t0 = time.perf_counter()
        with open(src, "rb") as f:
            blob = f.read()
        magic, ver, algo, _, plen = struct.unpack("<4sBBHI", blob[:12])[:5] \
            if len(blob) >= 12 else (None,) * 5
        if magic != b"TRKZ":
            raise ValueError(f"{src}: not a TRKZ container")
        data = self._unrle(zlib.decompress(blob[16:16 + plen]))
        dst = dst or src[:-5] if src.endswith(".trkz") else src + ".out"
        with open(dst, "wb") as f:
            f.write(data)
        ms = (time.perf_counter() - t0) * 1000
        with self._lock:
            self.stats["jobs"] += 1
            self.stats["restored"] += 1
            self.stats["last_ms"] = ms
            self.stats["total_ms"] += ms
        return self._note("RESTORE", src, f"→ {dst} ({len(data)}B)",
                          ms, "cpu")

    # -------------------------------------------------------- quantization
    def quantize_file(self, src, fmt="q4_0", dst=None, mode=None):
        """Block-wise scalar quantization of raw byte payloads.

        Each block of `bs` bytes is mapped to `bits`-bit codes plus one
        scale/min byte pair — the classic GGUF idea applied generically.
        Output container keeps enough info for a dequantize round-trip.
        """
        t0 = time.perf_counter()
        fmt = fmt if fmt in QUANT_FORMATS else "q4_0"
        bs, bits, label = QUANT_FORMATS[fmt]
        mode = _norm_mode(mode or self.active_mode())
        with open(src, "rb") as f:
            raw = f.read()
        levels = (1 << bits) - 1
        codes = bytearray()
        meta = bytearray()
        gmin, gspan = 0, 1
        for off in range(0, len(raw), bs):
            block = raw[off:off + bs]
            mn = min(block)
            mx = max(block)
            span = max(1, mx - mn)
            scale = span / levels
            if off == 0:
                gmin, gspan = mn, span
            meta += bytes([mn]) + span.to_bytes(2, "little")
            for b in block:
                codes.append(int(round((b - mn) / scale)) & levels)
        # body layout: ALL per-block meta triples first, then the flat code
        # stream — so the decoder can read metadata without knowing code sizes.
        body = zlib.compress(bytes(meta) + bytes(codes), 6)
        hdr = struct.pack("<4sBBHI4sBBB3x", b"TRKQ", 1,
                          list(QUANT_FORMATS).index(fmt), bs,
                          len(raw), b"\x00" * 4, gmin,
                          (gspan >> 8) & 0xFF, gspan & 0xFF)
        blob = hdr + body
        dst = dst or src + f".{fmt}"
        with open(dst, "wb") as f:
            f.write(blob)
        ms = (time.perf_counter() - t0) * 1000
        ratio = (1 - len(blob) / max(1, len(raw))) * 100
        with self._lock:
            self.stats["jobs"] += 1
            self.stats["bytes_in"] += len(raw)
            self.stats["bytes_out"] += len(blob)
            self.stats["quantized"] += 1
            self.stats["last_ms"] = ms
            self.stats["total_ms"] += ms
        return self._note("QUANTIZE", src,
                          f"{fmt} ({label}) {len(raw)}B→{len(blob)}B "
                          f"({ratio:+.1f}%) mode={mode}", ms,
                          self._device_for(mode))

    def dequantize_file(self, src, dst=None):
        t0 = time.perf_counter()
        with open(src, "rb") as f:
            blob = f.read()
        magic, ver, fmt_i, bs, orig_len = struct.unpack("<4sBBHI", blob[:12])[:5]
        if magic != b"TRKQ":
            raise ValueError(f"{src}: not a TRKQ container")
        # 22-byte header: + pad(4) at [12:16], gmin(1), span_hi(1), span_lo(1), pad(3)
        data = zlib.decompress(blob[22:])
        fmt = list(QUANT_FORMATS)[fmt_i]
        bits = QUANT_FORMATS[fmt][1]
        levels = (1 << bits) - 1
        # body layout: nblk * (mn, span16) triples, then the flat code stream
        nblk = (orig_len + bs - 1) // bs if orig_len else 0
        metas = []
        for i in range(nblk):
            off = i * 3
            mn = data[off]
            span = max(1, int.from_bytes(data[off + 1:off + 3], "little"))
            metas.append((mn, span))
        ci = nblk * 3
        out = bytearray()
        pos = 0
        for mn, span in metas:
            n = min(bs, orig_len - pos)
            scale = span / levels if levels else 1.0
            for c in data[ci:ci + n]:
                out.append(min(255, mn + int(round(c * scale))))
            ci += n
            pos += n
        if not dst:
            base, _, ext = src.rpartition(".")
            # strip the quant suffix (e.g. "payload.bin.q8_0" -> "payload.bin")
            if ext in QUANT_FORMATS and base:
                src = base
            stem, _, _ = src.rpartition(".")
            stem = stem or src          # no extension at all -> use full name
            dst = stem + ".deq"
        with open(dst, "wb") as f:
            f.write(bytes(out[:orig_len]))
        ms = (time.perf_counter() - t0) * 1000
        with self._lock:
            self.stats["jobs"] += 1
            self.stats["restored"] += 1
            self.stats["last_ms"] = ms
            self.stats["total_ms"] += ms
        return self._note("DEQUANTIZE", src, f"→ {dst} (~lossy restore)",
                          ms, "cpu")

    # -------------------------------------------------- «Генерация ССЛ» mode
    def sslgen_generate(self, spec, dst=None, preview=5, progress_cb=None):
        """SSLGEN compute mode: bulk random-string generation.

        `spec` is the one-line '&&'-joined specification, e.g.:
            len=16 && charset=mixed && lines=1000000
        charset selects letters / digits / mixed (letters+digits);
        lines may be up to 1 trillion (1e12) per run.  Generation streams
        to `dst` (default: <data_dir>/sslgen_<timestamp>.txt) in blocks so
        even huge runs stay memory-safe; Ctrl-C style aborts are possible
        through the callable assigned to self.sslgen_stop.
        Returns a summary dict with a short preview of the first lines.
        """
        p = parse_sslgen_spec(spec)
        length, lines, alphabet = p["length"], p["lines"], p["alphabet"]
        if not dst:
            dst = os.path.join(self.data_dir,
                               f"sslgen_{time.strftime('%Y%m%d_%H%M%S')}.txt")
        t0 = time.perf_counter()
        block_n = max(1, min(lines, 8192))          # stream in small blocks
        written = 0
        preview_lines = []
        stop = self.sslgen_stop or (lambda: False)
        try:
            with open(dst, "w", encoding="utf-8") as f:
                while written < lines:
                    if stop():
                        break
                    n = min(block_n, lines - written)
                    buf = []
                    for _ in range(n):
                        buf.append("".join(secrets.choice(alphabet)
                                           for _ in range(length)))
                    blob = "\n".join(buf) + "\n"
                    f.write(blob)
                    if len(preview_lines) < preview:
                        take = min(preview - len(preview_lines), n)
                        preview_lines.extend(buf[:take])
                    written += n
                    if progress_cb:
                        try:
                            progress_cb(written, lines)
                        except Exception:            # noqa: BLE001
                            pass
                    if lines <= 1_000_000:
                        block_n = min(lines, block_n * 2)   # ramp-up
        except OSError as exc:
            raise RuntimeError(f"sslgen write failed: {exc}") from None
        ms = (time.perf_counter() - t0) * 1000
        nbytes = written * (length + 1)
        with self._lock:
            self.stats["jobs"] += 1
            self.stats["sslgen_runs"] += 1
            self.stats["sslgen_lines"] += written
            self.stats["bytes_out"] += nbytes
            self.stats["last_ms"] = ms
            self.stats["total_ms"] += ms
        line = self._note("SSLGEN", dst,
                          f"len={length} charset={p['charset']} "
                          f"lines={written:,}/{lines:,}", ms,
                          self._device_for(MODE_SSLGEN))
        return {"path": dst, "length": length, "charset": p["charset"],
                "requested": lines, "generated": written,
                "bytes": nbytes, "ms": ms, "preview": preview_lines,
                "aborted": written < lines, "journal": line}

    # ------------------------------------------------------------- journal
    def recent(self, n=12):
        with self._lock:
            return list(self.events[-n:])

    def snapshot(self):
        s = dict(self.stats)
        s["mode"] = self.active_mode()
        s["modes_available"] = self.modes()
        s["device"] = self._device_for(s["mode"])
        return s
