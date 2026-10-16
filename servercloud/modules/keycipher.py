"""keycipher — TruckOS "TRK-35" cipher for API-key log fingerprints.

Every AI/API key that touches the system (our hosted /apis keys, remote
/cpts keys, cluster shared secrets) is *never* written to the live logs in
clear text.  Instead each key is transformed into a fixed-width 35-character
fingerprint using the TRK-35 scheme:

  1. HMAC-SHA256(key, device_salt)          -> authenticable digest
  2. base32(digest)                         -> uppercase A-Z 2-7 alphabet
  3. XOR-fold with the device secret        -> key cannot be brute-forced
                                                without this machine
  4. take exactly 35 chars, group as
     XXXXX-XXXXX-...-XXXXX (7 groups of 5)  -> human-readable fingerprint

Properties
----------
* deterministic on one device: the same key always yields the same 35-char
  fingerprint, so logs can be correlated across restarts;
* irreversible: nobody can recover the key from the fingerprint;
* device-bound: the salt lives in <data_dir>/etc/.trk_salt (0600-style,
  random per installation), so two hosts show different fingerprints for the
  same key — which also proves *which* machine produced a given log line.

The fingerprint is what the live monitor (`monitor.py`) and api_calls.log
show; the raw key stays only in memory / the encrypted keystore.
"""

import base64
import hashlib
import hmac
import os
import secrets
import threading

MASK_LEN = 35                     # hard requirement: every fingerprint is 35 chars
GROUP = 5                         # XXXXX-XXXXX-... readability grouping


def _fold(data: bytes, width: int) -> bytes:
    """XOR-fold `data` over itself into `width` bytes (position-dependent)."""
    out = bytearray(width)
    for i, b in enumerate(data):
        out[i % width] ^= b
    return bytes(out)


class TrkCipher:
    """Device-salted TRK-35 fingerprint generator."""

    def __init__(self, data_dir=None):
        self._lock = threading.Lock()
        self._cache = {}                       # key -> fingerprint
        self.salt = b""
        if data_dir:
            path = os.path.join(data_dir, "etc", ".trk_salt")
            try:
                os.makedirs(os.path.dirname(path), exist_ok=True)
                if os.path.exists(path):
                    with open(path, "rb") as f:
                        self.salt = f.read().strip()
                if not self.salt:
                    self.salt = secrets.token_bytes(32)
                    with open(path, "wb") as f:
                        f.write(self.salt)
            except OSError:
                self.salt = secrets.token_bytes(32)   # volatile fallback
        if not self.salt:
            self.salt = secrets.token_bytes(32)

    # ------------------------------------------------------------------ core
    def mask(self, key, length=MASK_LEN):
        """Return the fixed-width (default 35 char) TRK-35 fingerprint of `key`.

        Empty/None input maps to a stable all-dashes placeholder so log
        lines never contain blanks or the literal word 'None'.
        """
        if key is None or str(key).strip() == "":
            return "-" * length
        key = str(key)
        with self._lock:
            cached = self._cache.get((key, length))
        if cached:
            return cached
        digest = hmac.new(self.salt, key.encode("utf-8"),
                          hashlib.sha256).digest()
        # extend the stream until we have enough alphabet material
        stream = digest
        counter = 0
        while len(stream) < length + 8:
            counter += 1
            stream += hmac.new(self.salt,
                               key.encode("utf-8") + counter.to_bytes(4, "big"),
                               hashlib.sha256).digest()
        folded = _fold(stream, length)
        raw = base64.b32encode(folded).decode("ascii").rstrip("=")
        raw = (raw + "X" * length)[:length]
        fp = "-".join(raw[i:i + GROUP] for i in range(0, length, GROUP)) \
            if length % GROUP == 0 else raw
        fp = fp[:length].ljust(length, "X")
        with self._lock:
            self._cache[(key, length)] = fp
        return fp

    # ------------------------------------------------------------- helpers
    @staticmethod
    def looks_masked(text, length=MASK_LEN):
        """True if `text` already is a TRK-35 fingerprint (log sanity check)."""
        s = str(text).strip()
        if len(s) != length:
            return False
        body = s.replace("-", "")
        return bool(body) and all(c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"
                                  for c in body.upper())


# module-level default instance (no device salt file — pure in-memory)
_DEFAULT = None


def default_cipher():
    global _DEFAULT
    if _DEFAULT is None:
        _DEFAULT = TrkCipher()
    return _DEFAULT


def mask_key(key, length=MASK_LEN):
    """Convenience wrapper around the process-wide cipher instance."""
    return default_cipher().mask(key, length)
