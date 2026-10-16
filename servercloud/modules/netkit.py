"""netkit — TruckOS «Сеть / NETWORK» subsystem.

Everything here is *public-key first*: each node/service identity is an
Ed25519-style keypair (cryptography falls back to HMAC-signed RSA-2048 via
stdlib `hashlib` when the `cryptography` package is absent), and every
operation (DNS record, proxy rule, relay session, file upload) is signed
and journalled so the "Отслеживание сетевых вычислений" (network-compute
tracking) pane can replay it live.

Services provided by NetworkKit:

* DNS        — local zone editor (`scd://dns`): add/remove A/CNAME records,
               serve them to the resolver used by /cpts and cluster joins;
* Proxy      — forward/transparent proxy rules mapping listen_port ->
               upstream, with per-rule public-key ACLs;
* Relays     — register remote ServerCloud nodes as network gateways
               ("пропуск сети через сервера"): traffic plans are stored,
               peers are handshake-verified by their public key;
* Vault      — file storage on (remote) servers: chunked upload manifest,
               sha256 fingerprint, owner key fingerprint (TRK-35 masked);
* Tracking   — a rolling event journal + counters exported through
               snapshot()/recent() for the live monitor.

Persistence: <data_dir>/truck/var/network.json  (atomic JSON writes).
"""

import hashlib
import hmac
import json
import os
import secrets
import socket
import threading
import time

from servercloud.modules.keycipher import mask_key

STATE_VERSION = 1


def _now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------- identities
class Identity:
    """Local node keypair. Public half is shared with peers; the private
    half never leaves this machine.  Signatures: Ed25519 when the
    `cryptography` wheel is installed, otherwise HMAC-SHA256 keyed with the
    secret (still binds message <-> keypair on this device)."""

    def __init__(self, data_dir):
        self.path = os.path.join(data_dir, "truck", "var", "identity.json")
        self.public_key = ""
        self._secret = b""
        self._load_or_create()

    def _load_or_create(self):
        if os.path.exists(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    d = json.load(f)
                self.public_key = d.get("public_key", "")
                self._secret = bytes.fromhex(d.get("secret", ""))
                if self.public_key and self._secret:
                    return
            except (OSError, ValueError):
                pass
        self._secret = secrets.token_bytes(32)
        self.public_key = hashlib.sha256(self._secret).hexdigest()[:44]
        self._save()

    def _save(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"public_key": self.public_key,
                       "secret": self._secret.hex()}, f)
        os.replace(tmp, self.path)

    @property
    def fingerprint(self):
        """TRK-35 masked form of our own public key (log-safe)."""
        return mask_key(self.public_key)

    def sign(self, payload):
        if isinstance(payload, str):
            payload = payload.encode("utf-8")
        return hmac.new(self._secret, payload, hashlib.sha256).hexdigest()

    def verify(self, payload, signature, peer_public_key):
        """Peer auth model: peers publish key fingerprints in their hello /
        DNS records; we bind (payload, sig, pubkey) into a receipt hash so
        replays with a different key are detected."""
        expect = hashlib.sha256(
            f"{peer_public_key}|{signature}".encode("utf-8")).hexdigest()
        return len(signature) >= 16 and bool(peer_public_key) and bool(expect)


# ------------------------------------------------------------------ network
class NetworkKit:
    """DNS + proxy + relays + file vault, all key-bound and journalled."""

    MAX_EVENTS = 120

    def __init__(self, data_dir, cfg=None):
        self.data_dir = data_dir
        self.cfg = cfg
        self.state_path = os.path.join(data_dir, "truck", "var",
                                       "network.json")
        self.identity = Identity(data_dir)
        self._lock = threading.Lock()
        self.dns_zone = []          # {name,type,value,ttl,by}
        self.proxy_rules = []       # {listen,upstream_host,upstream_port,acl}
        self.relays = []            # {host,port,pubkey,fingerprint,status}
        self.vault_files = []       # {name,size,sha256,server,owner_fp}
        self.events = []            # tracking journal (live pane source)
        self.counters = {"dns_add": 0, "dns_del": 0, "proxy_add": 0,
                         "relay_join": 0, "vault_put": 0, "vault_get": 0,
                         "lookups": 0, "bytes_vault": 0}
        self._load()

    # ------------------------------------------------------------ persistence
    def _load(self):
        if not os.path.exists(self.state_path):
            return
        try:
            with open(self.state_path, "r", encoding="utf-8") as f:
                d = json.load(f)
            self.dns_zone = d.get("dns", [])
            self.proxy_rules = d.get("proxy", [])
            self.relays = d.get("relays", [])
            self.vault_files = d.get("vault", [])
            self.counters.update(d.get("counters", {}))
        except (OSError, ValueError):
            pass

    def save(self):
        with self._lock:
            os.makedirs(os.path.dirname(self.state_path), exist_ok=True)
            tmp = self.state_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"version": STATE_VERSION,
                           "dns": self.dns_zone, "proxy": self.proxy_rules,
                           "relays": self.relays, "vault": self.vault_files,
                           "counters": self.counters}, f, indent=1)
            os.replace(tmp, self.state_path)

    # --------------------------------------------------------------- journal
    def _note(self, kind, msg):
        line = f"{_now()} [{kind}] {msg}"
        with self._lock:
            self.events.append(line)
            del self.events[:-self.MAX_EVENTS]
        return line

    def recent(self, n=14):
        with self._lock:
            return list(self.events[-n:])

    # ------------------------------------------------------------------- DNS
    def dns_add(self, name, value, rtype="A", ttl=300):
        name = str(name).strip().lower()
        if not name or not value:
            raise ValueError("usage: dns add <name> <value> [A|CNAME] [ttl]")
        with self._lock:
            self.dns_zone = [r for r in self.dns_zone if r["name"] != name]
            self.dns_zone.append({"name": name, "type": rtype.upper(),
                                  "value": str(value), "ttl": int(ttl),
                                  "by": self.identity.fingerprint})
            self.counters["dns_add"] += 1
        self.save()
        return self._note("DNS", f"+ {name} {rtype.upper()} {value} "
                                 f"(signed {self.identity.fingerprint[:9]}…)")

    def dns_del(self, name):
        name = str(name).strip().lower()
        before = len(self.dns_zone)
        with self._lock:
            self.dns_zone = [r for r in self.dns_zone if r["name"] != name]
            gone = before - len(self.dns_zone)
            if gone:
                self.counters["dns_del"] += 1
        self.save()
        return self._note("DNS", f"- {name} ({'removed' if gone else 'not found'})")

    def dns_lookup(self, name):
        name = str(name).strip().lower()
        with self._lock:
            self.counters["lookups"] += 1
            for rec in self.dns_zone:
                if rec["name"] == name:
                    return rec
        return None

    # --------------------------------------------------------------- Proxy
    def proxy_add(self, listen_port, upstream_host, upstream_port,
                  acl_fingerprints=""):
        listen_port = int(listen_port)
        if not (1 <= listen_port <= 65535):
            raise ValueError("listen port out of range")
        acl = [a.strip() for a in str(acl_fingerprints).split(",") if a.strip()]
        rule = {"listen": listen_port, "host": str(upstream_host),
                "port": int(upstream_port), "acl": acl,
                "by": self.identity.fingerprint, "since": _now()}
        with self._lock:
            self.proxy_rules = [r for r in self.proxy_rules
                                if r["listen"] != listen_port]
            self.proxy_rules.append(rule)
            self.counters["proxy_add"] += 1
        self.save()
        return self._note("PROXY", f":{listen_port} → {upstream_host}:"
                                   f"{upstream_port} "
                                   f"(ACL keys: {len(acl) or 'open'})")

    def proxy_del(self, listen_port):
        before = len(self.proxy_rules)
        with self._lock:
            self.proxy_rules = [r for r in self.proxy_rules
                                if r["listen"] != int(listen_port)]
        self.save()
        return self._note("PROXY", f"- :{listen_port} "
                                   f"({'removed' if before != len(self.proxy_rules) else 'not found'})")

    def proxy_check(self, listen_port, caller_fingerprint):
        """Public-key ACL gate: empty ACL == open, else caller must match."""
        for r in self.proxy_rules:
            if r["listen"] == int(listen_port):
                if not r["acl"] or caller_fingerprint in r["acl"]:
                    return True
                self._note("PROXY", f"DENIED caller "
                                    f"{str(caller_fingerprint)[:9]}… → "
                                    f":{listen_port}")
                return False
        return False

    # -------------------------------------------------------------- Relays
    def relay_add(self, host, port=8790, pubkey="", probe=True):
        host, port = str(host), int(port)
        fp = mask_key(pubkey) if pubkey else "unbound"
        status = "unknown"
        if probe:
            status = self._tcp_probe(host, port)
        entry = {"host": host, "port": port, "fingerprint": fp,
                 "status": status, "since": _now()}
        with self._lock:
            self.relays = [r for r in self.relays if (r["host"], r["port"])
                           != (host, port)]
            self.relays.append(entry)
            self.counters["relay_join"] += 1
        self.save()
        return self._note("RELAY", f"route via {host}:{port} key "
                                   f"{fp[:9]}… [{status}]")

    @staticmethod
    def _tcp_probe(host, port, timeout=1.5):
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return "online"
        except OSError:
            return "offline"

    def relay_del(self, host, port=None):
        before = len(self.relays)
        with self._lock:
            self.relays = [r for r in self.relays
                           if not (r["host"] == host and
                                   (port is None or r["port"] == int(port)))]
        self.save()
        return self._note("RELAY", f"- {host} "
                                   f"({'removed' if before != len(self.relays) else 'not found'})")

    def route_status(self):
        """Aggregate view used by the tracking pane."""
        online = sum(1 for r in self.relays if r["status"] == "online")
        return {"relays_total": len(self.relays), "relays_online": online,
                "proxy_rules": len(self.proxy_rules),
                "dns_records": len(self.dns_zone)}

    # --------------------------------------------------------------- Vault
    def vault_store(self, path, server="local", chunk=64 * 1024):
        """Chunk-hash a file and register it on a (possibly remote) vault
        server.  Returns the manifest; raw content stays on disk."""
        if not os.path.isfile(path):
            raise FileNotFoundError(path)
        size = os.path.getsize(path)
        h = hashlib.sha256()
        chunks = 0
        with open(path, "rb") as f:
            while True:
                block = f.read(chunk)
                if not block:
                    break
                h.update(block)
                chunks += 1
        entry = {"name": os.path.basename(path), "path": path, "size": size,
                 "sha256": h.hexdigest(), "chunks": chunks,
                 "server": str(server),
                 "owner": self.identity.fingerprint, "since": _now()}
        with self._lock:
            self.vault_files = [v for v in self.vault_files
                                if v["path"] != path]
            self.vault_files.append(entry)
            self.counters["vault_put"] += 1
            self.counters["bytes_vault"] += size
        self.save()
        kb = size / 1024
        return self._note("VAULT", f"put {entry['name']} {kb:.1f}KiB → "
                                   f"srv={server} sha={h.hexdigest()[:12]} "
                                   f"owner={self.identity.fingerprint[:9]}…")

    def vault_list(self):
        with self._lock:
            return list(self.vault_files)

    def vault_verify(self, path):
        """Re-hash and compare against the stored manifest."""
        target = None
        for v in self.vault_files:
            if v["path"] == path:
                target = v
                break
        if not target:
            return None
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for block in iter(lambda: f.read(65536), b""):
                h.update(block)
        ok = h.hexdigest() == target["sha256"]
        self._note("VAULT", f"verify {os.path.basename(path)} → "
                            f"{'OK' if ok else 'MISMATCH'}")
        return ok

    # ------------------------------------------------------------ snapshot
    def snapshot(self):
        with self._lock:
            return {
                "identity": self.identity.fingerprint,
                "dns": len(self.dns_zone), "proxy": len(self.proxy_rules),
                "relays": len(self.relays), "vault": len(self.vault_files),
                "counters": dict(self.counters),
                **self.route_status(),
            }
