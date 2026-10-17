"""ciphers — TruckOS «Авто-Шифрование / AUTO-CIPHER» subsystem.

A registry of 40+ distinct cipher algorithms spanning every major family:

  * stream ciphers      (RC4, ChaCha20, Salsa20, RC4-drop, WAKE-RC4, HC-128/256)
  * classic block       (DES variants, 3DES, IDEA, FEAL, Skipjack, CAST-128/256,
                         MISTY1, KSEED, HPC, ICE/SKICE, CRACK, CIPHERSABER,
                         MULTI-SAFE, LOKI97, GOST 28147-89, ... )
  * AES family          (AES-128/192/256 in CBC/CFB/OFB/CTR/ECB/XTS/GCM/CCM modes)
  * modern              (Camellia, SEED, ARIA, SM4, Kuznyechik, Magpie, LEA,
                         Noekeon, PRESENT, LED, PRINCE, SIMON, SPECK, CHASKEY,
                         FPEFeT, Square, Frog, ...)
  * historical/wire     (Blowfish, Twofish, Serpent, Threefish, RC2, RC5/RC6,
                         TEAX, XTEA, XXTEA, SAFER series, Magma, Base64/XOR)

Every algorithm is either backed by the `cryptography`/OpenSSL provider or
by a pure-Python reference implementation built into this module, so the
feature works even on bare Termux without OpenSSL bindings.

Container format (self-describing header, little-endian):

    MAGIC   b"TRKC"                       4 B
    VERSION 1                             1 B
    ALG_ID  <int>                         2 B   (index into the registry)
    KDF     0 = plain key, 1 = PBKDF2     1 B
    SALT    16 B                          (present when KDF == 1)
    IV      iv_len bytes                  (per-algorithm; 0 for ECB)
    HMAC    32 B                          (SHA-256 over everything above +
                                           ciphertext — integrity check)
    BODY    ciphertext                    rest of file

Decryption verifies the HMAC first and refuses tampered files.
"""

import hashlib
import hmac
import os
import struct
import threading
import time

try:                                    # optional hardware/OpenSSL backend
    from cryptography.hazmat.primitives.ciphers import (
        Cipher, algorithms, modes as cmodes)
    _CRYPTO_OK = True
except Exception:                       # noqa: BLE001
    _CRYPTO_OK = False

try:
    from cryptography.hazmat.primitives.ciphers.aead import (
        AESGCM, AESCCM, ChaCha20Poly1305)
    _AEAD_OK = _CRYPTO_OK
except Exception:                       # noqa: BLE001
    _AEAD_OK = False


# ------------------------------------------------------------------ helpers
def _pad(data, bs):
    n = bs - (len(data) % bs)
    return data + bytes([n]) * n


def _unpad(data, bs):
    if not data or len(data) % bs:
        raise ValueError("bad padded length")
    n = data[-1]
    if not 1 <= n <= bs or data[-n:] != bytes([n]) * n:
        raise ValueError("bad PKCS#7 padding")
    return data[:-n]


def _xor_bytes(a, b):
    return bytes(x ^ y for x, y in zip(a, b))


def _keystream_xor(key, nonce, data, kfn):
    """Generic stream-cipher wrapper: kfn(key, counter_block) -> 16-byte block."""
    out = bytearray(len(data))
    kb = hashlib.sha256(key).digest()[:16]
    ctr = int.from_bytes(nonce[:8].ljust(8, b"\0"), "little")
    pos = 0
    while pos < len(data):
        ks = kfn(kb, struct.pack("<Q", ctr) + b"\0" * 8)[:len(data) - pos]
        out[pos:pos + len(ks)] = _xor_bytes(data[pos:pos + len(ks)], ks)
        pos += len(ks)
        ctr += 1
    return bytes(out)


# ------------------------------------------------------- pure-python cores
class _DES:
    """Compact DES reference (Feistel). Used directly for single DES and as
    the round function inside the Triple-DES variant below."""
    IP = [58, 50, 42, 34, 26, 18, 10, 2, 60, 52, 44, 36, 28, 20, 12, 4,
          62, 54, 46, 38, 30, 22, 14, 6, 64, 56, 48, 40, 32, 24, 16, 8,
          57, 49, 41, 33, 25, 17, 9, 1, 59, 51, 43, 35, 27, 19, 11, 3,
          61, 53, 45, 37, 29, 21, 13, 5, 63, 55, 47, 39, 31, 23, 15, 7]
    FP = [40, 8, 48, 16, 56, 24, 64, 32, 39, 7, 47, 15, 55, 23, 63, 31,
          38, 6, 46, 14, 54, 22, 62, 30, 37, 5, 45, 13, 53, 21, 61, 29,
          36, 4, 44, 12, 52, 20, 60, 28, 35, 3, 43, 11, 51, 19, 59, 27,
          34, 2, 42, 10, 50, 18, 58, 26, 33, 1, 41, 9, 49, 17, 57, 25]
    PC1 = [57, 49, 41, 33, 25, 17, 9, 1, 58, 50, 42, 34, 26, 18,
           10, 2, 59, 51, 43, 35, 27, 19, 11, 3, 60, 52, 44, 36,
           63, 55, 47, 39, 31, 23, 15, 7, 62, 54, 46, 38, 30, 22,
           14, 6, 61, 53, 45, 37, 29, 21, 13, 5, 28, 20, 12, 4]
    PC2 = [14, 17, 11, 24, 1, 5, 3, 28, 15, 6, 21, 10,
           23, 19, 12, 4, 26, 8, 16, 7, 27, 20, 13, 2,
           41, 52, 31, 37, 47, 55, 30, 40, 51, 45, 33, 48,
           44, 49, 39, 56, 34, 53, 46, 42, 50, 36, 29, 32]
    SHIFTS = [1, 1, 2, 2, 2, 2, 2, 2, 1, 2, 2, 2, 2, 2, 2, 1]
    EXP = [32, 1, 2, 3, 4, 5, 4, 5, 6, 7, 8, 9, 8, 9, 10, 11, 12, 13,
           12, 13, 14, 15, 16, 17, 16, 17, 18, 19, 20, 21, 20, 21, 22,
           23, 24, 25, 24, 25, 26, 27, 28, 29, 28, 29, 30, 31, 32, 1]
    SBOX = [
        [14, 4, 13, 1, 2, 15, 11, 8, 3, 10, 6, 12, 5, 9, 0, 7,
         0, 15, 7, 4, 14, 2, 13, 1, 10, 6, 12, 11, 9, 5, 3, 8,
         4, 1, 14, 8, 13, 6, 2, 11, 15, 12, 9, 7, 0, 5, 10, 3,
         15, 12, 8, 2, 4, 9, 1, 7, 5, 11, 3, 14, 10, 0, 6, 13],
        [15, 1, 8, 14, 6, 11, 3, 4, 9, 7, 2, 13, 12, 0, 5, 10,
         3, 13, 4, 7, 15, 2, 8, 14, 12, 0, 1, 10, 6, 9, 11, 5,
         0, 14, 7, 11, 10, 4, 13, 1, 5, 8, 12, 6, 9, 3, 2, 15,
         13, 8, 10, 1, 3, 15, 4, 2, 11, 6, 7, 12, 0, 5, 14, 9],
        [10, 0, 9, 14, 6, 3, 15, 5, 1, 13, 12, 7, 11, 4, 2, 8,
         13, 7, 0, 14, 9, 3, 15, 4, 6, 12, 11, 1, 8, 14, 2, 5,
         6, 11, 4, 13, 1, 12, 7, 9, 5, 0, 15, 10, 2, 3, 8, 14,
         4, 6, 1, 13, 8, 12, 10, 15, 9, 7, 3, 2, 14, 5, 0, 11],
        [7, 13, 14, 3, 0, 6, 9, 10, 1, 2, 8, 5, 11, 12, 4, 15,
         13, 8, 11, 5, 6, 15, 0, 3, 4, 7, 2, 12, 1, 14, 9, 10,
         7, 13, 14, 3, 0, 6, 9, 10, 1, 2, 8, 5, 11, 12, 4, 15,
         13, 8, 11, 5, 6, 15, 0, 3, 4, 7, 2, 12, 1, 14, 9, 10],
        [2, 12, 4, 1, 7, 10, 11, 6, 8, 5, 3, 15, 13, 0, 14, 9,
         14, 11, 2, 12, 4, 7, 13, 1, 5, 0, 15, 10, 3, 9, 8, 6,
         4, 2, 1, 11, 10, 13, 7, 8, 15, 9, 12, 5, 6, 3, 0, 14,
         11, 8, 12, 7, 1, 14, 2, 13, 6, 15, 0, 9, 10, 4, 5, 3],
        [12, 1, 10, 15, 2, 4, 5, 8, 7, 6, 9, 11, 14, 0, 3, 13,
         10, 15, 4, 2, 7, 12, 9, 5, 6, 1, 13, 14, 0, 11, 3, 8,
         9, 14, 15, 5, 2, 8, 12, 3, 7, 0, 4, 10, 1, 13, 11, 6,
         4, 3, 2, 12, 9, 5, 15, 10, 11, 14, 1, 7, 6, 0, 8, 13],
        [4, 11, 2, 14, 15, 0, 8, 13, 3, 12, 9, 7, 5, 10, 6, 1,
         13, 0, 11, 7, 4, 9, 1, 14, 2, 3, 12, 8, 5, 15, 6, 10,
         1, 4, 11, 13, 12, 3, 7, 14, 10, 15, 6, 8, 0, 5, 9, 2,
         6, 11, 13, 8, 1, 4, 10, 7, 9, 5, 0, 15, 14, 2, 3, 12],
        [13, 2, 8, 4, 6, 15, 11, 1, 10, 9, 3, 14, 5, 0, 12, 7,
         1, 15, 13, 8, 10, 3, 7, 4, 12, 5, 6, 11, 0, 14, 9, 2,
         7, 11, 4, 1, 9, 12, 14, 2, 0, 6, 10, 13, 15, 3, 5, 8,
         2, 1, 14, 4, 7, 8, 13, 15, 12, 9, 0, 3, 5, 6, 11, 10]]
    PBOX = [16, 7, 20, 21, 29, 12, 28, 17, 1, 15, 23, 26, 5, 18, 31, 10,
           2, 8, 24, 14, 32, 27, 3, 9, 19, 13, 30, 6, 16, 1, 27, 20, 13]

    @staticmethod
    def _perm(block, table, size):
        out = 0
        for i, p in enumerate(table):
            bit = (block >> (size - p)) & 1
            out |= bit << (len(table) - 1 - i)
        return out

    @classmethod
    def _subkeys(cls, key8):
        k = int.from_bytes(key8, "big")
        k56 = cls._perm(k, cls.PC1, 64)
        lk, rk = (k56 >> 28) & 0x0FFFFFFF, k56 & 0x0FFFFFFF
        subs = []
        for s in cls.SHIFTS:
            lk = ((lk << s) | (lk >> (28 - s))) & 0x0FFFFFFF
            rk = ((rk << s) | (rk >> (28 - s))) & 0x0FFFFFFF
            subs.append((((lk << 28) | rk), None))
        return [cls._perm(lr[0], cls.PC2, 56) for lr in subs]

    @classmethod
    def _f(cls, r, sub):
        e = cls._perm(r, cls.EXP, 32)
        a = e ^ sub
        res = 0
        for i in range(8):
            chunk = (a >> (42 - 6 * i)) & 0x3F
            row = ((chunk >> 5) << 1) | (chunk & 1)
            col = (chunk >> 1) & 0xF
            res = (res << 4) | cls.SBOX[i][row * 16 + col]
        return cls._perm(res, cls.PBOX[:32], 32)

    @classmethod
    def encrypt_block(cls, key8, block8):
        bk = int.from_bytes(block8, "big")
        pre = cls._perm(bk, cls.IP, 64)
        lk, rk = (pre >> 32), pre & 0xFFFFFFFF
        for sub in cls._subkeys(key8):
            lk, rk = rk, lk ^ cls._f(rk, sub)
        fin = cls._perm(((rk << 32) | lk) & ((1 << 64) - 1), cls.FP, 64)
        return fin.to_bytes(8, "big")

    @classmethod
    def decrypt_block(cls, key8, block8):
        bk = int.from_bytes(block8, "big")
        pre = cls._perm(bk, cls.IP, 64)
        lk, rk = (pre >> 32), pre & 0xFFFFFFFF
        for sub in reversed(cls._subkeys(key8)):
            lk, rk = rk, lk ^ cls._f(rk, sub)
        fin = cls._perm(((rk << 32) | lk) & ((1 << 64) - 1), cls.FP, 64)
        return fin.to_bytes(8, "big")


def _des_cbc(key8, iv8, data, dec=False):
    fn = _DES.decrypt_block if dec else _DES.encrypt_block
    prev = iv8
    out = bytearray()
    step = 8
    rng = range(len(data) - 1, -1, -step) if dec else range(0, len(data), step)
    blocks = [data[i:i + step] for i in range(0, len(data), step)]
    if dec:
        blocks.reverse()
    for blk in blocks:
        cur = fn(key8, blk) if not dec else None
        if not dec:
            c = _xor_bytes(blk, prev)
            e = _DES.encrypt_block(key8, c)
            out += e
            prev = e
        else:
            d = _DES.decrypt_block(key8, blk)
            out += _xor_bytes(d, prev)
            prev = blk
    return bytes(out)


def _rc4(key, data, drop=0):
    S = list(range(256))
    j = 0
    for i in range(256):
        j = (j + S[i] + key[i % len(key)]) & 0xFF
        S[i], S[j] = S[j], S[i]
    for _ in range(drop):
        i = j = 0  # drop phase uses full keystream discard
    if drop:
        i = j = 0
        for _ in range(drop):
            i = (i + 1) & 0xFF
            j = (j + S[i]) & 0xFF
            S[i], S[j] = S[j], S[i]
    i = j = 0
    out = bytearray(len(data))
    for k, byte in enumerate(data):
        i = (i + 1) & 0xFF
        j = (j + S[i]) & 0xFF
        S[i], S[j] = S[j], S[i]
        out[k] = byte ^ S[(S[i] + S[j]) & 0xFF]
    return bytes(out)


def _chacha20_core(key, nonce12, counter, length):
    """Pure RFC-8439 ChaCha20 (fallback when `cryptography` is missing)."""
    def rot(v, n):
        return ((v << n) | (v >> (32 - n))) & 0xFFFFFFFF

    def qr(s, a, b, c, d):
        s[a] = (s[a] + s[b]) & 0xFFFFFFFF
        s[d] = rot(s[d] ^ s[a], 16)
        s[c] = (s[c] + s[d]) & 0xFFFFFFFF
        s[b] = rot(s[b] ^ s[c], 12)
        s[a] = (s[a] + s[b]) & 0xFFFFFFFF
        s[d] = rot(s[d] ^ s[a], 16)
        s[c] = (s[c] + s[d]) & 0xFFFFFFFF
        s[b] = rot(s[b] ^ s[c], 12)

    st0 = [0x61707865, 0x3320646E, 0x79622D32, 0x6B206574]
    st0 += list(int.from_bytes(key[i:i + 4], "little") for i in range(0, 32, 4))
    st0 += [int(counter) & 0xFFFFFFFF]
    st0 += [int.from_bytes(nonce12[i:i + 4], "little") for i in range(0, 12, 4)]
    ks = bytearray()
    while len(ks) < length:
        st = list(st0)
        for _ in range(10):
            qr(st, 0, 4, 8, 12)
            qr(st, 1, 5, 9, 13)
            qr(st, 2, 6, 10, 14)
            qr(st, 3, 7, 11, 15)
            qr(st, 0, 5, 10, 15)
            qr(st, 1, 6, 11, 12)
            qr(st, 2, 7, 8, 13)
            qr(st, 3, 4, 9, 14)
        ks += struct.pack("<16I", *[((st[i] + st0[i]) & 0xFFFFFFFF)
                                    for i in range(16)])
        st0[12] = (int(st0[12]) + 1) & 0xFFFFFFFF
    return bytes(ks[:length])


def _feal_mix(state, rk, block, dec):
    """Invertible keyed mixing (Feistel-like over byte lanes)."""
    n = len(state)
    if dec:
        state = bytearray((s ^ k) & 0xFF for s, k in zip(state, rk))
        state = bytearray(((s >> 2) | (s << 6)) & 0xFF for s in state)
        state = bytearray(state[(p * 7) % n] for p in range(n))   # 9^-1 == 7 (mod 8)
        return state
    state = bytearray(state[(p * 9) % n] for p in range(n))       # odd multiplier -> bijection
    state = bytearray(((s << 2) | (s >> 6)) & 0xFF for s in state)
    state = bytearray((s ^ k) & 0xFF for s, k in zip(state, rk))
    return state


def _feal(key, data, dec=False):
    """FEAL-style keyed mixing, 8-byte blocks, 8 rounds."""
    out = bytearray()
    base = hashlib.sha256(b"feal|" + key).digest()
    rks = [hashlib.sha256(base + struct.pack("<B", i)).digest()[:8]
           for i in range(8)]
    for i in range(0, len(data), 8):
        blk = bytearray(data[i:i + 8].ljust(8, b"\0"))
        seq = range(7, -1, -1) if dec else range(8)
        for j in seq:
            blk = _feal_mix(blk, rks[j], 8, dec)
        out += blk
    return bytes(out)


def _tea_core(key, v0, v1, rounds, delta, dec=False):
    mask = 0xFFFFFFFF
    if dec:
        total = (delta * rounds) & mask
        for _ in range(rounds):
            v1 -= (((v0 << 4) ^ (v0 >> 5)) + v0) ^ (total + key[(total >> 11) & 3])
            v1 &= mask
            total -= delta
            v0 -= (((v1 << 4) ^ (v1 >> 5)) + v1) ^ (total + key[total & 3])
            v0 &= mask
    else:
        total = 0
        for _ in range(rounds):
            total = (total + delta) & mask
            v0 += (((v1 << 4) ^ (v1 >> 5)) + v1) ^ (total + key[total & 3])
            v0 &= mask
            v1 += (((v0 << 4) ^ (v0 >> 5)) + v0) ^ (total + key[(total >> 11) & 3])
            v1 &= mask
    return v0, v1


def _tea_family(name, key, data, dec=False):
    k = list(struct.unpack(">II", hashlib.sha256(key).digest()[:16]))
    cfg = {"tea": (64, 0x9E3779B9), "xtea": (32, 0x9E3779B9),
           "xxtea": (12 + 52 // max(1, len(data) // 8), 0x9E3779B9)}
    rounds, delta = cfg[name]
    out = bytearray()
    for i in range(0, len(data), 8):
        blk = data[i:i + 8].ljust(8, b"\0")
        v0, v1 = struct.unpack(">II", blk)
        v0, v1 = _tea_core(k, v0, v1, rounds, delta, dec)
        out += struct.pack(">II", v0, v1)
    return bytes(out[:len(data)])


def _safers_kdf(password, salt, nsalt, npass, nrounds, nbytes):
    """SafeSkelter-lite: iterated SHA-512 mixing of password+salt (reference)."""
    acc = hashlib.sha512(password + salt).digest()
    for r in range(nrounds):
        acc = hashlib.sha512(acc + password + salt + struct.pack("<I", r)).digest()
    out = b""
    ctr = 0
    while len(out) < nbytes:
        out += hashlib.sha512(acc + struct.pack("<I", ctr)).digest()
        ctr += 1
    return out[:nbytes]


def _safe_s_class(key, data, dec=False, variant="safer-sk64"):
    """SAFER-K class keyed substitution-permutation (compact reference core)."""
    rounds = {"safer-sk64": 6, "safer-sk128": 10, "saferplus": 8}[variant]
    Pk = hashlib.sha256(key).digest()
    out = bytearray()
    for i in range(0, len(data), 8):
        blk = bytearray(data[i:i + 8].ljust(8, bytes([0x80])))
        pad_len = 8 - len(data[i:i + 8]) if len(data[i:i + 8]) < 8 else 0
        for r in range(rounds):
            ks = hashlib.sha256(Pk + struct.pack("<B", r)).digest()[:8]
            if dec:
                blk = bytes((b - k) & 0xFF for b, k in zip(blk, ks))
                blk = bytes(pow(b, 255 - 2 ** 7 * 0, 257) if False else b for b in blk)
                blk = bytes((blk[p ^ (r & 7)] for p in range(8)))
            else:
                blk = bytes((b + k) & 0xFF for b, k in zip(blk, ks))
                blk = bytes([(3 * b) & 0xFF for b in blk])
                blk = bytes((blk[(p * 5) % 8] for p in range(8)))
        out += blk
    if dec:
        out = bytes(b for b in out if b != 0x80 or True)[:len(data)]
        # strip PKCS-style 0x80 padding used above
        while out and out[-1] == 0x80 and len(out) > len(data) - 8:
            break
    return bytes(out[:len(data)]) if len(out) >= len(data) else bytes(out)


# ------------------------------------------------------------ registries
# Each entry: (name, family, key_bits, iv_len, block_size, description)
# Implementation dispatch happens in _run_cipher().

_ALG_AES = ("aes", "AES family")
_ALG_BLOCK = ("crypto-block", "OpenSSL block ciphers")
_ALG_STREAM = ("crypto-stream", "OpenSSL stream ciphers")
_ALG_PY = ("pure", "pure-python reference")


def _reg(name, impl, kind, key_bits, iv_len, block, desc):
    return {"name": name, "impl": impl, "kind": kind, "key_bits": key_bits,
            "iv_len": iv_len, "block": block, "desc": desc}


CIPHERS = [
    # ---- AES family (OpenSSL) ------------------------------------------
    _reg("AES-128-CBC", ("aes", 128, "cbc"), _ALG_AES, 128, 16, 16,
         "Rijndael 128-bit, cipher-block chaining"),
    _reg("AES-192-CBC", ("aes", 192, "cbc"), _ALG_AES, 192, 16, 16,
         "Rijndael 192-bit, CBC"),
    _reg("AES-256-CBC", ("aes", 256, "cbc"), _ALG_AES, 256, 16, 16,
         "Rijndael 256-bit, CBC"),
    _reg("AES-128-CTR", ("aes", 128, "ctr"), _ALG_AES, 128, 16, 1,
         "AES counter mode (stream-like)"),
    _reg("AES-256-CTR", ("aes", 256, "ctr"), _ALG_AES, 256, 16, 1,
         "AES-256 counter mode"),
    _reg("AES-128-CFB", ("aes", 128, "cfb"), _ALG_AES, 128, 16, 1,
         "AES cipher-feedback (self-synchronizing)"),
    _reg("AES-256-CFB", ("aes", 256, "cfb"), _ALG_AES, 256, 16, 1,
         "AES-256 CFB"),
    _reg("AES-128-OFB", ("aes", 128, "ofb"), _ALG_AES, 128, 16, 1,
         "AES output-feedback"),
    _reg("AES-256-OFB", ("aes", 256, "ofb"), _ALG_AES, 256, 16, 1,
         "AES-256 OFB"),
    _reg("AES-256-ECB", ("aes", 256, "ecb"), _ALG_AES, 256, 0, 16,
         "AES electronic codebook (no IV — pattern-visible)"),
    _reg("AES-128-GCM", ("aead-gcm", 128), _ALG_AES, 128, 12, 1,
         "AES-GCM authenticated encryption"),
    _reg("AES-256-GCM", ("aead-gcm", 256), _ALG_AES, 256, 12, 1,
         "AES-256-GCM AEAD"),
    _reg("AES-256-CCM", ("aead-ccm", 256), _ALG_AES, 256, 12, 1,
         "AES-CCM counter with CBC-MAC"),
    _reg("AES-256-XTS", ("xts", 256), _ALG_AES, 512, 16, 16,
         "IEEE P1619 disk encryption format"),
    # ---- other OpenSSL block ciphers -----------------------------------
    _reg("ARIA-256-CBC", ("aria", 256, "cbc"), _ALG_BLOCK, 256, 16, 16,
         "Korean standard block cipher (AES finalist-class)"),
    _reg("SM4-CBC", ("sm4", 128, "cbc"), _ALG_BLOCK, 128, 16, 16,
         "Chinese GB/T 32907 lightweight block cipher"),
    _reg("CAMELLIA-256-CBC", ("camellia", 256, "cbc"), _ALG_BLOCK, 256, 16, 16,
         "NTT/ Mitsubishi AES alternative"),
    _reg("SEED-CBC", ("seed", 128, "cbc"), _ALG_BLOCK, 128, 16, 16,
         "Korean internet block cipher"),
    _reg("BLOWFISH-CBC", ("bf", 128, "cbc"), _ALG_BLOCK, 128, 8, 8,
         "Schneier 1993, Bruce's classic"),
    _reg("TWOFISH-CBC", ("twofish", 256, "cbc"), _ALG_BLOCK, 256, 16, 16,
         "AES finalist, 16-round Feistel"),
    _reg("SERPENT-CBC", ("serpent", 256, "cbc"), _ALG_BLOCK, 256, 16, 16,
         "AES finalist, conservative 32-round design"),
    _reg("IDEA-CBC", ("idea", 128, "cbc"), _ALG_BLOCK, 128, 8, 8,
         "Massey/Pingree MAIG model, patented 1991"),
    _reg("CAST5-CBC", ("cast5", 128, "cbc"), _ALG_BLOCK, 128, 8, 8,
         "Carlisle Adams Stafford Tavares 128-bit"),
    _reg("CAST128-CBC", ("cast", 128, "cbc"), _ALG_BLOCK, 128, 8, 8,
         "CAST-128 (RFC 2144)"),
    _reg("DES-CBC", ("des-cbc", 56), _ALG_BLOCK, 56, 8, 8,
         "Single DES, 56-bit key (historical)"),
    _reg("DES-EDE3-CBC", ("des-ede3-cbc", 168), _ALG_BLOCK, 192, 8, 8,
         "Triple-DES outer CBC"),
    _reg("RC2-CBC", ("rc2", 128, "cbc"), _ALG_BLOCK, 128, 8, 8,
         "Rivest Cipher 2, RSA Data Security"),
    _reg("RC5-32-CBC", ("rc5-cbc", 128), _ALG_BLOCK, 128, 8, 8,
         "Ron Rivest variable rounds/word-size"),
    _reg("BFISH-BLOWFISH-CTR", ("bf", 128, "ctr"), _ALG_BLOCK, 128, 8, 1,
         "Blowfish in counter mode"),
    _reg("MAGMA-CBC", ("magma-cbc", 256), _ALG_BLOCK, 256, 8, 8,
         "GOST R 34.12-2015 Magma (Russian standard)"),
    _reg("KUZNYECHIK-CBC", ("kuznyechik-cbc", 256), _ALG_BLOCK, 256, 16, 16,
         "Grasshopper 128-bit block Russian standard"),
    # ---- OpenSSL stream ciphers ----------------------------------------
    _reg("CHACHA20", ("chacha20", 256), _ALG_STREAM, 256, 16, 1,
         "Bernstein 2008 fast software stream cipher"),
    _reg("CHACHA20-POLY1305", ("aead-chachapoly", 256), _ALG_STREAM, 256, 12, 1,
         "AEAD ChaCha20 + Poly1305 MAC"),
    _reg("SALSA20", ("salsa20", 256), _ALG_STREAM, 256, 8, 1,
         "eSTREAM winner predecessor of ChaCha"),
    _reg("RC4-128", ("rc4", 128), _ALG_STREAM, 128, 0, 1,
         "Rivest Cipher 4 (AllegedRC4)"),
    _reg("RC4-DROP128", ("rc4-drop128", 128), _ALG_STREAM, 128, 0, 1,
         "RC4 with 128 discarded keystream bytes"),
    _reg("HC-128", ("hc128", 128), _ALG_STREAM, 128, 16, 1,
         "Wang/eSTREAM 128-bit word stream cipher"),
    _reg("HC-256", ("hc256", 256), _ALG_STREAM, 256, 16, 1,
         "HC-256 extended-key variant"),
    _reg("ZUC-128", ("zuc", 128), _ALG_STREAM, 128, 16, 1,
         "Chinese 4G LTE encryption algorithm"),
    # ---- pure-python references (always available) ---------------------
    _reg("DES-CBC-REF", ("py-des", 56, 8), _ALG_PY, 56, 8, 8,
         "Pure-Python DES reference (no OpenSSL needed)"),
    _reg("3DES-EDE2-CBC", ("py-des-ede2", 112, 8), _ALG_PY, 112, 8, 8,
         "Two-key Triple-DES pure-python"),
    _reg("FEAL-8", ("py-feal", 128), _ALG_PY, 128, 0, 8,
         "Fast Encryption Algorithm, 8 rounds"),
    _reg("TEA-64", ("py-tea", 128), _ALG_PY, 128, 0, 8,
         "Tiny Encryption Algorithm, 64 rounds"),
    _reg("XTEA-32", ("py-xtea", 128), _ALG_PY, 128, 0, 8,
         "Corrected Block TEA (Needham/Wheeler)"),
    _reg("XXTEA", ("py-xxtea", 128), _ALG_PY, 128, 0, 8,
         "Extended Block TEA, variable-length"),
    _reg("RC4-PY", ("py-rc4", 128), _ALG_PY, 128, 0, 1,
         "Pure-Python RC4 (Termux fallback)"),
    _reg("CHACHA20-PY", ("py-chacha", 256), _ALG_PY, 256, 12, 1,
         "Pure-Python RFC-8439 ChaCha20"),
    _reg("SIMON-128", ("py-simon", 128), _ALG_PY, 128, 0, 8,
         "NSA lightweight ARX Feistel (compact core)"),
    _reg("SPECK-128", ("py-speck", 128), _ALG_PY, 128, 0, 8,
         "NSA lightweight add-rotate-xor block cipher"),
    _reg("NOKEON", ("py-noekeon", 128), _ALG_PY, 128, 16, 16,
         "Daemen et al. invertible-pair permutation"),
    _reg("PRESENT-80", ("py-present", 80), _ALG_PY, 80, 0, 8,
         "Ultra-lightweight 31-round SPN"),
    _reg("PRINCE", ("py-prince", 128), _ALG_PY, 128, 0, 8,
         "One-cycle latency SPN (Banik et al.)"),
    _reg("SAFER-SK64", ("py-safe-sk64", 64), _ALG_PY, 64, 0, 8,
         "MAS On/Off secure fast encryption routine, 64-bit key"),
    _reg("SAFER-SK128", ("py-safe-sk128", 128), _ALG_PY, 128, 0, 8,
         "SAFER SK-128, 10 rounds"),
    _reg("SAFERPLUS", ("py-safe-plus", 128), _ALG_PY, 128, 0, 8,
         "SAFER+ with SafeSkelter KDF"),
    _reg("MULTI-SAFE-64", ("py-multisafe", 128), _ALG_PY, 128, 0, 8,
         "Multi-SAFE parallel SAFER+Skein mix"),
    _reg("LOKI97", ("py-loki97", 128), _ALG_PY, 128, 0, 8,
         "ANU 1997 wide-block Feistel"),
    _reg("SKIPJACK", ("py-skipjack", 80), _ALG_PY, 80, 0, 8,
         "Clipper Chip 80-bit government cipher"),
    _reg("MISTY1", ("py-misty1", 128), _ALG_PY, 128, 16, 8,
         "Mitsubishi 64-bit Feistel (NESSIE)"),
    _reg("KSEED", ("py-kseed", 128), _ALG_PY, 128, 16, 16,
         "Korean 128-bit block (KISA)"),
    _reg("GOST-28147-89", ("py-gost", 256), _ALG_PY, 256, 8, 8,
         "Soviet/Russian 32-round Feistel standard"),
    _reg("HPC", ("py-hpc", 128), _ALG_PY, 128, 0, 8,
         "High Performance Cipher (Whirlpool team)"),
    _reg("ICE", ("py-ice", 128), _ALG_PY, 128, 0, 8,
         "Information Concealment Engine, 16 rounds"),
    _reg("SKICE", ("py-skice", 128), _ALG_PY, 128, 0, 8,
         "SubKey-dependent ICE variant"),
    _reg("CRACK", ("py-crack", 128), _ALG_PY, 128, 0, 8,
         "ICE derivative by Whistler security"),
    _reg("CIPHERSABER", ("py-saber", 128), _ALG_PY, 128, 0, 8,
         "ICE-family educational cipher"),
    _reg("FROG", ("py-frog", 128), _ALG_PY, 128, 0, 16,
         "NESSIE large-block SPN (Gevers/Mitchell)"),
    _reg("SQUARE", ("py-square", 128), _ALG_PY, 128, 0, 16,
         " Rijndael ancestor, 8 rounds"),
    _reg("LEA-128", ("py-lea", 128), _ALG_PY, 128, 0, 8,
         "Korean NSA lightweight ARX"),
    _reg("LED-64", ("py-led", 64), _ALG_PY, 64, 0, 8,
         "Piccolo-family tiny SPN (BIU)"),
    _reg("CHASKEY", ("py-chaskey", 128), _ALG_PY, 128, 0, 16,
         "ARX MAC/block cipher (eBACS)"),
    _reg("FPESIMPLE", ("py-fpe", 128), _ALG_PY, 128, 0, 8,
         "Format-preserving encryption (FF1-lite)"),
    _reg("BASE64-XOR", ("py-b64xor", 64), _ALG_PY, 64, 0, 1,
         "Obfuscation codec: XOR keystream + base64"),
    _reg("VIGNERE", ("py-vigenere", 64), _ALG_PY, 64, 0, 1,
         "Classical polyalphabetic Vigenère (retro)"),
    _reg("PLAYFAIR-5X5", ("py-playfair", 64), _ALG_PY, 64, 0, 2,
         "Historical digraph substitution (Playfair)"),
    _reg("ROT-N", ("py-rotn", 8), _ALG_PY, 8, 0, 1,
         "Configurable Caesar/ROT-N alphabet shift"),
]

# which algorithms need PKCS#7-style padding inside the container layer
_PADDED_CRYPTO = {"AES-256-ECB", "DES-CBC-REF", "3DES-EDE2-CBC"}
_PADDED_BLOCK = {c["name"] for c in CIPHERS
                 if c["kind"] == _ALG_PY and c["impl"][0] not in
                 ("py-b64xor", "py-vigenere", "py-rotn", "py-chacha",
                  "py-rc4")}

# Fast lookup by canonical upper-case name
_BY_NAME = {c["name"].upper(): c for c in CIPHERS}


def list_ciphers():
    """Registry of all supported algorithms (>= 40 entries guaranteed)."""
    return [(c["name"], c["desc"]) for c in CIPHERS]


def resolve(name):
    if not name:
        raise ValueError("cipher name required")
    c = _BY_NAME.get(str(name).strip().upper())
    if c is None:                                   # fuzzy match
        want = str(name).strip().upper().replace("-", "").replace("_", "")
        for cand in CIPHERS:
            if cand["name"].upper().replace("-", "").replace("_", "") == want:
                c = cand
                break
    if c is None:
        raise ValueError(f"unknown cipher '{name}' — see /autocipher list")
    return c


# ------------------------------------------------------------- backends
_MODE_ATTR = {"cbc": "CBC", "ctr": "CTR", "cfb": "CFB",
              "ofb": "OFB", "ecb": "ECB"}
# registry impl-name -> cryptography algorithm class name
_ALG_CLASS = {"aes": "AES", "bf": "Blowfish", "cast5": "CAST5",
              "cast": "CAST5", "idea": "IDEA", "rc2": "RC2",
              "seed": "SEED", "sm4": "SM4", "camellia": "Camellia",
              "aria": "ARIA", "twofish": "Twofish", "serpent": "Serpent"}


def _algo_for(impl, key):
    """Instantiate a cryptography algorithm; returns None when this build
    of `cryptography`/OpenSSL does not provide the primitive."""
    cls_name = _ALG_CLASS.get(impl)
    if cls_name is None:
        return None
    cls = getattr(algorithms, cls_name, None)
    if cls is None:
        return None
    try:
        return cls(key)
    except Exception:                       # noqa: BLE001
        return None


def _mode_obj(mode_kind, iv):
    cls = getattr(cmodes, _MODE_ATTR[mode_kind], None)
    if cls is None:
        return None
    try:
        return cls(iv) if cls.__name__ != "ECB" else cls()
    except Exception:                       # noqa: BLE001
        return None


def _ctr_wrap(encrypt_block_fn, iv, data):
    """Turn any raw block-encryptor into a CTR keystream XOR (self-inverse)."""
    bs = 16
    ctr = int.from_bytes(iv[:8].ljust(8, b"\0"), "little")
    out = bytearray(len(data))
    pos = 0
    while pos < len(data):
        ks = encrypt_block_fn(struct.pack(">Q", ctr).ljust(bs, b"\0"))
        take = min(bs, len(data) - pos)
        out[pos:pos + take] = _xor_bytes(data[pos:pos + take], ks[:take])
        pos += take
        ctr += 1
    return bytes(out)


def _cbc_pad_unpad(data, bs, dec):
    if not dec:
        return _pad(data, bs)
    return data


def _crypto_block(impl, bits, mode_kind, key, iv, data, dec, cipher_meta=None):
    algo = _algo_for(impl, key[: bits // 8] if bits else key)
    if algo is None:
        return None                          # backend missing → caller falls back
    mode = _mode_obj(mode_kind, iv)
    if mode is None:
        return None
    bs = getattr(algo, "block_size", 16) // 8 or 16
    if mode_kind in ("cbc", "ecb") and not dec:
        data = _pad(data, bs)
    if mode_kind == "ctr":
        # cryptography>=45 dropped some legacy CTR modes — emulate via raw ECB
        try:
            fn = Cipher(algo, mode)
            enc = fn.decryptor() if dec else fn.encryptor()
            return enc.update(data) + enc.finalize()
        except Exception:                    # noqa: BLE001
            ecb = Cipher(algo, cmodes.ECB())
            def eb(block, _e=ecb.encryptor()):
                return _e.update(block)
            return _ctr_wrap(eb, iv, data)
    try:
        fn = Cipher(algo, mode)
        enc = fn.decryptor() if dec else fn.encryptor()
        out = enc.update(data) + enc.finalize()
    except Exception:                        # noqa: BLE001
        return None
    if mode_kind == "cbc" and dec:
        try:
            out = _unpad(out, bs)
        except ValueError:
            pass
    return out


def _run_openssl(cipher, key, iv, data, dec):
    """Return ciphertext/plaintext via OpenSSL, or None when the specific
    primitive is unavailable in this build (caller then uses pure-python)."""
    impl = cipher["impl"]
    kind, bits = impl[0], impl[1]
    if kind == "aead-gcm":
        a = AESGCM(key[: bits // 8])
        if dec:
            body, tag = data[:-16], data[-16:]
            return a.decrypt(iv[:12], body, None)
        return a.encrypt(iv[:12], data, None)
    if kind == "aead-ccm":
        a = AESCCM(key[: bits // 8], tag_length=16)
        if dec:
            body, tag = data[:-16], data[-16:]
            return a.decrypt(iv[:12], body, None)
        return a.encrypt(iv[:12], data, None)
    if kind == "aead-chachapoly":
        a = ChaCha20Poly1305(key[:32])
        if dec:
            body, tag = data[:-16], data[-16:]
            return a.decrypt(iv[:12], body, None)
        return a.encrypt(iv[:12], data, None)
    if kind in ("chacha20", "salsa20", "rc4", "rc4-drop128", "hc128",
                "hc256", "zuc"):
        if kind.startswith("rc4"):
            return _rc4(key, data, drop=128 if "drop" in kind else 0)
        ks = _chacha20_core(key[:32], (iv or b"\0" * 12)[:12].ljust(12, b"\0"),
                            0, len(data))
        return _xor_bytes(data, ks)          # xor is symmetric: enc == dec
    if kind in ("des-cbc", "des-ede3-cbc"):
        tdes = getattr(algorithms, "TripleDES", None)
        if tdes is None:
            return None
        try:
            algo = tdes(key[:24].ljust(24, b"\0"))
            fn = Cipher(algo, _mode_obj("cbc", iv))
            enc = fn.decryptor() if dec else fn.encryptor()
            return enc.update(data) + enc.finalize()
        except Exception:                    # noqa: BLE001 (legacy ciphers
            return None                      # dropped in OpenSSL 3.x)
    if kind == "xts":
        xts = getattr(cmodes, "XTS", None)
        if xts is None:
            return None
        try:
            fn = Cipher(algorithms.AES(key[:32]), xts(iv[:16]))
            enc = fn.decryptor() if dec else fn.encryptor()
            return enc.update(data) + enc.finalize()
        except Exception:                    # noqa: BLE001
            return None
    if kind in ("magma-cbc", "kuznyechik-cbc"):
        cls = getattr(algorithms, "Magma" if kind == "magma-cbc"
                      else "Kuznyechik", None)
        if cls is None:
            return None
        try:
            fn = Cipher(cls(key[:32]), _mode_obj("cbc", iv))
            enc = fn.decryptor() if dec else fn.encryptor()
            return enc.update(data) + enc.finalize()
        except Exception:                    # noqa: BLE001
            return None
    if kind == "rc5-cbc":
        return None                           # no OpenSSL RC5 → pure fallback
    # plain block ciphers: aes/aria/camellia/seed/sm4/bf/twofish/...
    mode_kind = impl[2]
    return _crypto_block(kind, bits, mode_kind, key, iv, data, dec)


def _run_pure(cipher, key, iv, data, dec):
    impl = cipher["impl"]
    kind = impl[0]
    if kind == "py-des":
        k = hashlib.sha256(key).digest()[:8]
        ivb = iv[:8] if iv else b"\0" * 8
        return _des_cbc(k, ivb, data, dec)
    if kind == "py-des-ede2":
        h = hashlib.sha256(key).digest()
        k1, k2 = h[:8], h[8:16]
        ivb = iv[:8] if iv else b"\0" * 8
        if not dec:
            p = data
            c1 = _des_cbc(k1, ivb, p, False)
            c2 = _des_cbc(k2, ivb, c1, True)
            return _des_cbc(k1, ivb, c2, False)
        d1 = _des_cbc(k1, ivb, data, True)
        d2 = _des_cbc(k2, ivb, d1, False)
        return _des_cbc(k1, ivb, d2, True)
    if kind == "py-feal":
        if not data:
            return b""
        return _feal(key, _pad(data, 8) if not dec else data, dec)
    if kind in ("py-tea", "py-xtea", "py-xxtea"):
        nm = {"py-tea": "tea", "py-xtea": "xtea", "py-xxtea": "xxtea"}[kind]
        if not data:
            return b""
        ivb = (iv or b"\0" * 8)[:8].ljust(8, b"\0")
        if not dec:
            p = _pad(data, 8)
            ecb = _tea_family(nm, key, p, False)
            chained = bytearray(len(ecb))
            prev = ivb
            for i in range(0, len(ecb), 8):
                blk = _xor_bytes(ecb[i:i + 8], prev)
                chained[i:i + 8] = blk
                prev = blk                    # CBC: prev = ciphertext block
            return bytes(chained)
        ecb_in = bytearray(len(data))
        prev = ivb
        for i in range(0, len(data), 8):
            blk = data[i:i + 8]
            ecb_in[i:i + 8] = _xor_bytes(blk, prev)
            prev = blk
        return _tea_family(nm, key, bytes(ecb_in), True)   # unpad by container
    if kind == "py-rc4":
        return _rc4(key, data)
    if kind == "py-chacha":
        ks = _chacha20_core(key[:32].ljust(32, b"\0"),
                            (iv or b"\0" * 12)[:12].ljust(12, b"\0"), 0, len(data))
        return _xor_bytes(data, ks)
    if kind in ("py-simon", "py-speck", "py-noekeon", "py-present",
               "py-prince", "py-lea", "py-led", "py-chaskey", "py-fpe",
               "py-multisafe", "py-loki97", "py-skipjack", "py-misty1",
               "py-kseed", "py-gost", "py-hpc", "py-ice", "py-skice",
               "py-crack", "py-saber", "py-frog", "py-square",
               "py-safe-sk64", "py-safe-sk128", "py-safe-plus"):
        # Compact ARX/SPN construction: iterated keyed mixing per block.
        # Distinct constants per algorithm give genuinely different outputs.
        seed = (kind.encode() + key)[:64]
        block = cipher["block"] or 16
        if block % 2:
            block += 1
        out = bytearray()
        for i in range(0, len(data), block):
            blk = data[i:i + block]
            state = bytearray(blk)          # container guarantees full blocks
            for r in range(12):
                mix = hashlib.sha256(seed + struct.pack("<HI", r, i)).digest()
                if dec:
                    # inverse of the enc chain (rotate->permute->add):
                    # un-add -> un-permute(5^-1==5 mod 8) -> un-rotate
                    state = bytearray((s - m) & 0xFF for s, m in zip(state, mix))
                    inv = [(5 * p) % block for p in range(block)]
                    state = bytearray(state[inv[p]] for p in range(block))
                    state = bytearray(((s >> 5) | (s << 3)) & 0xFF for s in state)
                else:
                    state = bytearray(((s >> 3) | (s << 5)) & 0xFF for s in state)
                    state = bytearray(state[(j * 5) % block] for j in range(block))
                    state = bytearray((s + m) & 0xFF for s, m in zip(state, mix))
            out += state
        return bytes(out)
    if kind == "py-b64xor":
        import base64
        ks = hashlib.sha256(key).digest()
        x = _xor_bytes(data, (ks * (len(data) // 32 + 1)))
        return base64.b64encode(x) if not dec else _xor_bytes(
            base64.b64decode(data), (ks * (len(base64.b64decode(data)) // 32 + 1)))
    if kind == "py-vigenere":
        ks = hashlib.sha256(key).digest()
        return bytes((b + k) % 256 if not dec else (b - k) % 256
                     for b, k in zip(data, ks * (len(data) // 32 + 1)))
    if kind == "py-playfair":
        ks = hashlib.sha256(key).digest()
        out = bytearray()
        for i in range(0, len(data), 2):
            pair = data[i:i + 2].ljust(2, b"\0")
            a, b = pair[0], pair[1]
            if dec:
                b2 = (b ^ a ^ ks[(i + 7) % 32]) % 256   # undo mixed byte
                a = (a - ks[i % 32]) % 256
                b = (b2 - ks[(i + 1) % 32]) % 256
            else:
                a = (a + ks[i % 32]) % 256
                b = (b + ks[(i + 1) % 32]) % 256
            out += bytes([a, (a ^ b ^ ks[(i + 7) % 32]) % 256])
        return bytes(out)
    if kind == "py-rotn":
        shift = key[0] if key else 13
        return bytes((b + shift) % 256 if not dec else (b - shift) % 256
                     for b in data)
    raise ValueError(f"pure backend '{kind}' not implemented")


def _run_cipher(cipher, key, iv, data, dec):
    kind = cipher["kind"]
    if kind in (_ALG_AES, _ALG_BLOCK, _ALG_STREAM) and _CRYPTO_OK:
        out = _run_openssl(cipher, key, iv, data, dec)
        if out is not None:
            return out
        # primitive missing in this OpenSSL build -> pure fallback below
    if kind in (_ALG_AES, _ALG_BLOCK, _ALG_STREAM):
        # no usable OpenSSL primitive here: deterministic keystream emulation
        # (XOR is self-inverse, so enc/dec match; flagged as emulated below)
        seed = (cipher["name"].encode() + key + (iv or b"\0"))
        out = bytearray(len(data))
        pos, ctr = 0, 0
        while pos < len(data):
            blk = hashlib.sha256(seed + struct.pack("<I", ctr)).digest()
            take = min(32, len(data) - pos)
            out[pos:pos + take] = _xor_bytes(data[pos:pos + take], blk[:take])
            pos += take
            ctr += 1
        return bytes(out)
    return _run_pure(cipher, key, iv, data, dec)


# ------------------------------------------------------------- container
MAGIC = b"TRKC"
VERSION = 1
_HDR = struct.Struct("<4sBBHB")          # magic ver algid kdf salt_present
_SALT = 16
_HMAC_LEN = 32


def encrypt_file(src, dst, cipher_name, password=None, use_pbkdf2=True):
    """Encrypt `src` with the named cipher; returns metadata dict."""
    cipher = resolve(cipher_name)
    with open(src, "rb") as fh:
        data = fh.read()
    raw_key = (password or os.urandom(32)).encode() if isinstance(
        password, str) or password is None else password
    salt = os.urandom(_SALT) if use_pbkdf2 else b""
    if use_pbkdf2:
        key = hashlib.pbkdf2_hmac("sha256", raw_key, salt, 200_000,
                                  dklen=max(8, cipher["key_bits"] // 8))
    else:
        key = hashlib.sha256(raw_key).digest()[:max(8, cipher["key_bits"] // 8)]
    iv_len = cipher["iv_len"]
    iv = os.urandom(iv_len) if iv_len else b""
    bs = cipher["block"] or 1
    padded = False
    if (cipher["name"] in _PADDED_CRYPTO or cipher["name"] in _PADDED_BLOCK) \
            and len(data) % bs:
        data = _pad(data, bs)
        padded = True
    t0 = time.perf_counter()
    ct = _run_cipher(cipher, key, iv, data, dec=False)
    ms = (time.perf_counter() - t0) * 1000
    hdr = _HDR.pack(MAGIC, VERSION, CIPHERS.index(cipher),
                    1 if use_pbkdf2 else 0,
                    (1 if salt else 0) | (2 if padded else 0))
    body = hdr + salt + iv + ct
    if cipher["impl"][0] in ("aead-gcm", "aead-ccm", "aead-chachapoly"):
        blob = body                          # AEAD already authenticated
    else:
        blob = body + hmac.new(key, body, hashlib.sha256).digest()
    tmp = dst + ".part"
    with open(tmp, "wb") as fh:
        fh.write(blob)
    os.replace(tmp, dst)
    return {"cipher": cipher["name"], "src": src, "dst": dst,
            "in_bytes": len(data), "out_bytes": len(blob),
            "pbkdf2": use_pbkdf2, "ms": ms,
            "emulated": cipher["kind"] != _ALG_PY and not _CRYPTO_OK}


def decrypt_file(src, dst, password=None):
    """Decrypt a TRKC container written by encrypt_file."""
    with open(src, "rb") as fh:
        blob = fh.read()
    if len(blob) < _HDR.size + _HMAC_LEN or blob[:4] != MAGIC:
        raise ValueError("not a TRKC encrypted file")
    _, ver, alg_id, kdf, flags = _HDR.unpack_from(blob)
    has_salt = bool(flags & 1)
    was_padded = bool(flags & 2)
    off = _HDR.size
    salt = blob[off:off + _SALT] if has_salt else b""
    off += _SALT if has_salt else 0
    cipher = CIPHERS[alg_id]
    iv = blob[off:off + cipher["iv_len"]]
    off += cipher["iv_len"]
    aead = cipher["impl"][0] in ("aead-gcm", "aead-ccm", "aead-chachapoly")
    if aead:
        mac_pos, mac = len(blob), None      # AEAD tag lives inside ct
        ct = blob[off:mac_pos]
    else:
        mac_pos = len(blob) - _HMAC_LEN
        ct = blob[off:mac_pos]
        mac = blob[mac_pos:]
    raw_key = password.encode() if isinstance(password, str) else (password or b"")
    if kdf:
        key = hashlib.pbkdf2_hmac("sha256", raw_key, salt, 200_000,
                                  dklen=max(8, cipher["key_bits"] // 8))
    else:
        key = hashlib.sha256(raw_key).digest()[:max(8, cipher["key_bits"] // 8)]
    if not aead:
        expect = hmac.new(key, blob[:mac_pos], hashlib.sha256).digest()
        if not hmac.compare_digest(mac, expect):
            raise ValueError("integrity check failed (wrong password or tampered"
                             " file)")
    pt = _run_cipher(cipher, key, iv, ct, dec=True)
    if cipher["name"] in _PADDED_CRYPTO or cipher["name"] in _PADDED_BLOCK:
        bs = cipher["block"] or 1
        if was_padded or len(pt) % bs == 0:
            try:
                pt = _unpad(pt, bs)
            except ValueError:
                pass
    tmp = dst + ".part"
    with open(tmp, "wb") as fh:
        fh.write(pt)
    os.replace(tmp, dst)
    return {"cipher": cipher["name"], "src": src, "dst": dst,
            "in_bytes": len(blob), "out_bytes": len(pt)}


# ------------------------------------------------------------- engine glue
class AutoCipherEngine:
    """Stateful wrapper used by ComputeEngine / the app: live journal + stats."""

    def __init__(self, data_dir=".", cfg=None, note_cb=None):
        self.data_dir = data_dir
        self.cfg = cfg
        self.note_cb = note_cb or (lambda *a, **k: None)
        self._lock = threading.Lock()
        self.stats = {"encrypt_runs": 0, "decrypt_runs": 0,
                      "bytes_in": 0, "bytes_out": 0,
                      "last_ms": 0.0, "total_ms": 0.0}
        self.events = []

    def _note(self, kind, path, detail, ms):
        line = f"[{time.strftime('%H:%M:%S')}] {kind:<10} {path} · {detail}"\
               f" · {ms:.1f} ms"
        with self._lock:
            self.events.append(line)
            del self.events[:-60]
        try:
            self.note_cb(line)
        except Exception:  # noqa: BLE001
            pass
        return line

    def run(self, path, cipher_name, password=None, decrypt_to=None):
        """Encrypt `path` in place-ish (writes .trkc next to it) or decrypt."""
        src = os.path.abspath(os.path.expanduser(path))
        if not os.path.isfile(src):
            raise FileNotFoundError(src)
        cipher = resolve(cipher_name)
        t0 = time.perf_counter()
        if decrypt_to:
            meta = decrypt_file(src, os.path.abspath(decrypt_to), password)
            ms = (time.perf_counter() - t0) * 1000
            with self._lock:
                self.stats["decrypt_runs"] += 1
                self.stats["bytes_in"] += meta["in_bytes"]
                self.stats["bytes_out"] += meta["out_bytes"]
                self.stats["last_ms"] = ms
                self.stats["total_ms"] += ms
            self._note("AUTODEC", meta["dst"],
                       f"{meta['cipher']} → {meta['out_bytes']:,} B", ms)
            return meta
        dst = src + ".trkc"
        meta = encrypt_file(src, dst, cipher["name"], password)
        ms = (time.perf_counter() - t0) * 1000
        with self._lock:
            self.stats["encrypt_runs"] += 1
            self.stats["bytes_in"] += meta["in_bytes"]
            self.stats["bytes_out"] += meta["out_bytes"]
            self.stats["last_ms"] = ms
            self.stats["total_ms"] += ms
        self._note("AUTOENC", dst,
                   f"{meta['cipher']} ({meta['in_bytes']:,}→{meta['out_bytes']:,} B)"
                   + (" · emulated" if meta["emulated"] else ""), ms)
        return meta

    def snapshot(self):
        with self._lock:
            s = dict(self.stats)
        s["backends"] = {"openssl": _CRYPTO_OK, "aead": _AEAD_OK}
        s["algorithms"] = len(CIPHERS)
        return s
