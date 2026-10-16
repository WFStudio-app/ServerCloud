"""cluster — fuse several devices into one TruckOS server.

A device running ServerCloud can *join* other ServerCloud nodes over any of
the supported link types and present the whole pool as a single machine:
one hostname, aggregated CPU/RAM/disk view, shared model registry and a
combined API endpoint.

Link types (all first-class, selectable per-peer):

  lan      direct TCP to the peer's cluster port (same subnet)
  manual   same as lan but with an explicit host you typed yourself
  wifi     LAN over Wi-Fi — discovered via UDP broadcast probe (:8791)
  internet WAN peer addressed by public IP / hostname + key
  relay    both sides dial out to a shared relay hub (NAT-friendly)
  cpts     reuse an existing /cpts remote ServerCloud API slot as a node
  mesh     every node also forwards frames for its neighbours (gossip)

Wire protocol: one JSON object per TCP connection line ("jsonl").
Control plane listens on `cluster_port` (sysconfig), discovery on UDP 8791.
Nothing here requires root; it is pure stdlib sockets.
"""

import json
import os
import socket
import threading
import time
import uuid

LINK_TYPES = {
    "lan":      "Direct TCP inside the local subnet",
    "manual":   "Manual host/IP entry (any reachable address)",
    "wifi":     "Wi-Fi/LAN auto-discovery via UDP broadcast",
    "internet": "Public internet node (WAN IP/hostname + key)",
    "relay":    "Both nodes dial out to a shared relay hub",
    "cpts":     "Attach through an existing /cpts API slot",
    "mesh":     "Gossip mesh — peers forward frames for each other",
}

DEFAULT_PORT = 8790
DISCOVERY_PORT = 8791
PROTOCOL_VERSION = 1


# ------------------------------------------------------------------ helpers
def _local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
        finally:
            s.close()
    except OSError:
        return "127.0.0.1"


def _send_line(sock, obj):
    sock.sendall((json.dumps(obj) + "\n").encode("utf-8"))


def _recv_lines(sock, handler):
    """Read jsonl frames from a socket until it closes."""
    buf = b""
    while True:
        try:
            chunk = sock.recv(4096)
        except OSError:
            break
        if not chunk:
            break
        buf += chunk
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            if not line.strip():
                continue
            try:
                frame = json.loads(line.decode("utf-8"))
            except ValueError:
                continue
            handler(frame)


# ------------------------------------------------------------------ node info
class NodeInfo:
    """What every node announces about itself during the handshake."""

    def __init__(self, hostname=None, cpu=1, ram_mb=64, disk_gb=0,
                 ollama=False, models=None, api_port=0, role="worker"):
        self.node_id = str(uuid.uuid4())[:8]
        self.hostname = hostname or socket.gethostname()
        self.cpu = int(cpu)
        self.ram_mb = int(ram_mb)
        self.disk_gb = float(disk_gb)
        self.ollama = bool(ollama)
        self.models = list(models or [])
        self.api_port = int(api_port)
        self.role = role
        self.link = "unknown"
        self.addr = ""
        self.seen = time.time()
        self.alive = True

    def as_dict(self):
        return {"v": PROTOCOL_VERSION, "node_id": self.node_id,
                "hostname": self.hostname, "cpu": self.cpu,
                "ram_mb": self.ram_mb, "disk_gb": self.disk_gb,
                "ollama": self.ollama, "models": self.models,
                "api_port": self.api_port, "role": self.role,
                "link": self.link, "addr": self.addr}

    @classmethod
    def from_frame(cls, fr):
        n = cls(hostname=fr.get("hostname"), cpu=fr.get("cpu", 1),
                ram_mb=fr.get("ram_mb", 64), disk_gb=fr.get("disk_gb", 0),
                ollama=fr.get("ollama", False), models=fr.get("models"),
                api_port=fr.get("api_port", 0), role=fr.get("role", "worker"))
        n.node_id = fr.get("node_id", n.node_id)
        n.link = fr.get("link", "unknown")
        n.addr = fr.get("addr", "")
        return n

    def row(self):
        return (f"{self.node_id:<8} {self.hostname:<18} {self.link:<9} "
                f"{self.addr:<21} {self.cpu:>3} vCPU {self.ram_mb:>5} MiB "
                f"{len(self.models):>3} models {'[ollama]' if self.ollama else ''}"
                f" {'ONLINE' if self.alive else 'offline'}")


# ------------------------------------------------------------------ manager
class ClusterManager:
    """Owns the control-plane listener, the peer table and discovery."""

    def __init__(self, hostname=None, port=DEFAULT_PORT, name="truck-cloud",
                 cpu=1, ram_mb=64, disk_gb=0, ollama=False, models=None,
                 api_port=0, group="", data_dir=None):
        self.name = name
        self.group = group
        self.port = int(port)
        self.me = NodeInfo(hostname=hostname, cpu=cpu, ram_mb=ram_mb,
                           disk_gb=disk_gb, ollama=ollama, models=models,
                           api_port=api_port, role="controller")
        self.peers = {}                    # node_id -> NodeInfo
        self._lock = threading.Lock()
        self._sock = None
        self._thread = None
        self._disc_thread = None
        self._stop = threading.Event()
        self.log = []                      # last joined-pool events
        self.state_file = os.path.join(data_dir or ".", "cluster.json")

    # ---------------- status ----------------
    @property
    def running(self):
        return self._sock is not None

    @property
    def online_peers(self):
        with self._lock:
            return [p for p in self.peers.values() if p.alive]

    def pool(self):
        """Aggregated single-server view: this node + every online peer."""
        nodes = [self.me] + self.online_peers
        return {
            "name": self.name,
            "nodes": len(nodes),
            "cpu": sum(n.cpu for n in nodes),
            "ram_mb": sum(n.ram_mb for n in nodes),
            "disk_gb": round(sum(n.disk_gb for n in nodes), 1),
            "models": sorted({m for n in nodes for m in n.models}),
            "ollama_nodes": sum(1 for n in nodes if n.ollama),
        }

    # ---------------- control plane ----------------
    def start(self):
        if self.running:
            return True
        try:
            srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            srv.bind(("0.0.0.0", self.port))
            srv.listen(8)
            srv.settimeout(1.0)
        except OSError:
            return False
        self._sock = srv
        self._stop.clear()
        self._thread = threading.Thread(target=self._accept_loop,
                                        daemon=True, name="cluster-listen")
        self._thread.start()
        self._start_discovery()
        return True

    def stop(self):
        self._stop.set()
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    def _accept_loop(self):
        while not self._stop.is_set():
            try:
                conn, addr = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self._serve_conn, args=(conn, addr),
                             daemon=True).start()

    def _serve_conn(self, conn, addr):
        def handle(fr):
            if fr.get("type") == "hello":
                self._on_hello(fr, conn, addr)
            elif fr.get("type") == "ping":
                try:
                    _send_line(conn, {"type": "pong",
                                      **self.me.as_dict()})
                except OSError:
                    pass
        try:
            conn.settimeout(10)
            _recv_lines(conn, handle)
        finally:
            conn.close()

    def _on_hello(self, fr, conn, addr):
        peer = NodeInfo.from_frame(fr)
        peer.addr = f"{addr[0]}:{addr[1]}"
        if not peer.link or peer.link == "unknown":
            peer.link = "lan"
        with self._lock:
            self.peers[peer.node_id] = peer
        self._note(f"node {peer.hostname} ({peer.node_id}) joined via "
                   f"{peer.link} from {peer.addr}")
        try:
            conn.settimeout(5)
            _send_line(conn, {"type": "welcome", "pool": self.pool(),
                              **self.me.as_dict()})
        except OSError:
            pass

    # ---------------- outbound joins ----------------
    def join(self, host, port=None, link="manual", timeout=5):
        """Connect out to another node's control plane and exchange hellos."""
        port = int(port or self.port)
        try:
            s = socket.create_connection((host, port), timeout=timeout)
        except OSError as e:
            return False, f"cannot reach {host}:{port} — {e}"
        fr = self.me.as_dict()
        fr["type"] = "hello"
        fr["link"] = link
        fr["addr"] = f"{_local_ip()}:{self.port}"
        result = {"ok": False, "msg": "no welcome received"}

        def handle(w):
            if w.get("type") == "welcome":
                peer = NodeInfo.from_frame(w)
                peer.addr = f"{host}:{port}"
                peer.link = link
                peer.alive = True
                with self._lock:
                    self.peers[peer.node_id] = peer
                result.update(ok=True,
                              msg=f"joined {peer.hostname} ({peer.node_id}) "
                                  f"via {link}; pool={w.get('pool', {})}")
                self._note(result["msg"])
                # announce ourselves so the remote learns us too
                try:
                    _send_line(s, {"type": "hello", **fr})
                except OSError:
                    pass
        try:
            s.settimeout(timeout)
            _send_line(s, fr)
            _recv_lines(s, handle)
        finally:
            s.close()
        return result["ok"], result["msg"]

    def ping(self, host, port=None, timeout=3):
        port = int(port or self.port)
        try:
            s = socket.create_connection((host, port), timeout=timeout)
        except OSError as e:
            return False, f"unreachable: {e}"
        try:
            s.settimeout(timeout)
            _send_line(s, {"type": "ping", **self.me.as_dict()})
            resp = s.recv(65536)
            fr = json.loads(resp.decode().strip().split("\n")[0])
            n = NodeInfo.from_frame(fr)
            n.addr = f"{host}:{port}"
            return True, (f"{n.hostname} alive — {n.cpu} vCPU, "
                          f"{n.ram_mb} MiB, {len(n.models)} models")
        except (OSError, ValueError, IndexError):
            return False, "no valid reply"
        finally:
            s.close()

    # ---------------- UDP discovery (Wi-Fi/LAN browse) ----------------
    def _start_discovery(self):
        def loop():
            try:
                u = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                u.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                u.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                u.bind(("", DISCOVERY_PORT))
                u.settimeout(1.0)
            except OSError:
                return
            with self._lock:
                self._disc_sock = u
            while not self._stop.is_set():
                try:
                    data, addr = u.recvfrom(2048)
                except socket.timeout:
                    continue
                except OSError:
                    break
                try:
                    fr = json.loads(data.decode())
                except ValueError:
                    continue
                if fr.get("type") == "discover" and (
                        not self.group or fr.get("group") == self.group):
                    rep = self.me.as_dict()
                    rep["type"] = "discovery-reply"
                    try:
                        u.sendto(json.dumps(rep).encode(), addr)
                    except OSError:
                        pass
                elif fr.get("type") == "discovery-reply":
                    n = NodeInfo.from_frame(fr)
                    n.addr = f"{addr[0]}:{n.api_port or self.port}"
                    n.link = "wifi"
                    with self._lock:
                        self.peers.setdefault(n.node_id, n)
                        self.peers[n.node_id].alive = True
        self._disc_thread = threading.Thread(target=loop, daemon=True,
                                             name="cluster-discovery")
        self._disc_thread.start()

    def discover(self, wait=1.5, group=None):
        """Broadcast a probe and collect replies (Wi-Fi/LAN browsing)."""
        group = group if group is not None else self.group
        seen_before = set(self.peers)
        try:
            u = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            u.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            u.settimeout(0.5)
            probe = json.dumps({"type": "discover", "group": group,
                                **self.me.as_dict()}).encode()
            for target in ("255.255.255.255", "<broadcast>"):
                try:
                    u.sendto(probe, (target, DISCOVERY_PORT))
                except OSError:
                    pass
            deadline = time.time() + wait
            while time.time() < deadline:
                try:
                    data, addr = u.recvfrom(4096)
                except (socket.timeout, OSError):
                    continue
                try:
                    fr = json.loads(data.decode())
                except ValueError:
                    continue
                if fr.get("type") == "discovery-reply":
                    n = NodeInfo.from_frame(fr)
                    n.addr = f"{addr[0]}"
                    n.link = "wifi"
                    with self._lock:
                        self.peers[n.node_id] = n
            new = [nid for nid in self.peers if nid not in seen_before]
        finally:
            u.close()
        return [self.peers[nid] for nid in new]

    # ---------------- persistence ----------------
    def save(self):
        try:
            os.makedirs(os.path.dirname(self.state_file), exist_ok=True)
            with open(self.state_file, "w", encoding="utf-8") as f:
                json.dump({"name": self.name, "group": self.group,
                           "port": self.port,
                           "peers": [{"addr": p.addr, "link": p.link}
                                     for p in self.peers.values()]}, f,
                          indent=2)
        except OSError:
            pass

    def _note(self, msg):
        self.log.append(f"[{time.strftime('%H:%M:%S')}] {msg}")
        del self.log[:-20]
