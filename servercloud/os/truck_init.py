"""truck_init — TruckOS init (PID 1 equivalent).

A tiny service supervisor for the embedded userland.  Services are declared
in truck/etc/services.json; each entry names a python module callable or an
external command.  Init can:

    boot <runlevel>   start all services enabled for the runlevel
    status            show every service and its state
    start|stop|restart <name>
    shutdown          stop everything, write shutdown log

ServerCloud's own long-running pieces (the network API server, the ollama
backend probe) are registered as services here so the 'half-OS' really owns
process lifecycle, just like systemd would on a full distro.
"""

import json
import os
import subprocess
import sys
import time

from servercloud.os import trk
from servercloud.os.sysconfig import SysConfig

# built-in services managed in-process
_BUILTIN = {}


def _register(name, start_fn, stop_fn, desc=""):
    _BUILTIN[name] = {"start": start_fn, "stop": stop_fn, "desc": desc}


SERVICES_FILE = None
LOG_FILE = None
_state = {}       # name -> running bool
_proc = {}        # name -> Popen for external services


def _paths(data_dir):
    global SERVICES_FILE, LOG_FILE
    root = trk.init_truck(data_dir)
    SERVICES_FILE = os.path.join(root, "etc", "services.json")
    LOG_FILE = os.path.join(root, "var", "log", "init.log")
    if not os.path.exists(SERVICES_FILE):
        os.makedirs(os.path.dirname(SERVICES_FILE), exist_ok=True)
        default = {
            "api-server": {"enabled": False, "runlevels": ["multi-user"],
                           "desc": "ServerCloud network API (:8777) — owned by the app process"},
            "ollama":     {"enabled": False, "runlevels": ["default", "multi-user"],
                           "cmd": ["ollama", "serve"],
                           "desc": "local Ollama backend (external process)"},
        }
        with open(SERVICES_FILE, "w", encoding="utf-8") as f:
            json.dump(default, f, indent=2)


def _log(msg):
    try:
        os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")
    except OSError:
        pass


def _load_defs():
    try:
        with open(SERVICES_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


# ---------------------------------------------------------------- actions
def start_service(name, cfg=None):
    defs = _load_defs()
    d = defs.get(name)
    if d is None:
        print(f"init: unknown service '{name}'.")
        return 1
    if name in _state and _state[name]:
        print(f"init: {name} already running.")
        return 0
    if name in _BUILTIN:
        ok = _BUILTIN[name]["start"](cfg)
        _state[name] = bool(ok)
        _log(f"start builtin {name}: {'ok' if ok else 'fail'}")
        print(f"init: {name} {'started' if ok else 'FAILED to start'}.")
        return 0 if ok else 1
    cmd = d.get("cmd")
    if cmd:
        try:
            p = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
            _proc[name] = p
            _state[name] = True
            _log(f"start external {name} pid={p.pid}")
            print(f"init: {name} started (pid {p.pid}).")
            return 0
        except OSError as e:
            print(f"init: {name} could not start: {e}")
            return 1
    print(f"init: service '{name}' has no implementation.")
    return 1


def stop_service(name):
    if name in _BUILTIN and _state.get(name):
        _BUILTIN[name]["stop"]()
        _state[name] = False
        _log(f"stop builtin {name}")
        print(f"init: {name} stopped.")
        return 0
    p = _proc.pop(name, None)
    if p and p.poll() is None:
        p.terminate()
        try:
            p.wait(timeout=5)
        except subprocess.TimeoutExpired:
            p.kill()
        _state[name] = False
        _log(f"stop external {name}")
        print(f"init: {name} stopped.")
        return 0
    if _state.get(name):
        _state[name] = False
        print(f"init: {name} marked stopped.")
        return 0
    print(f"init: {name} not running.")
    return 1


def boot(runlevel, cfg=None):
    defs = _load_defs()
    hostname = cfg.get("hostname") if cfg else "truck-os"
    print(f"init: PID 1 — welcoming {hostname} (runlevel: {runlevel})")
    _log(f"boot runlevel={runlevel}")
    for name, d in sorted(defs.items()):
        if not d.get("enabled", False):
            continue
        if runlevel not in d.get("runlevels", ["default"]):
            continue
        start_service(name, cfg)
    if runlevel == "rescue":
        print("init: rescue mode — only essential services started.")
    return 0


def status(cfg=None):
    defs = _load_defs()
    host = cfg.get("hostname") if cfg else "truck-os"
    up = time.strftime("%H:%M:%S")
    print(f"{host}: init status @ {up}")
    if not defs:
        print("  (no services defined)")
        return 0
    for name, d in sorted(defs.items()):
        st = "RUNNING" if _state.get(name) else "stopped"
        ext = _proc.get(name)
        if ext and ext.poll() is not None:
            st = "DEAD"
        kind = "builtin" if name in _BUILTIN else "external"
        print(f"  {name:<12} [{st:^7}] ({kind}) {d.get('desc', '')}")
    return 0


def shutdown():
    for name in list(_state):
        if _state[name]:
            stop_service(name)
    _log("shutdown")
    print("init: system halted.")
    return 0


def main(argv, data_dir=None):
    _paths(data_dir or os.environ.get(
        "SERVERCLOUD_DATA", os.path.expanduser("~/.servercloud")))
    cfg = SysConfig(os.path.join(trk.TRUCK_ROOT, "etc", "sysconfig.json"))
    if not argv:
        print(__doc__)
        return 0
    cmd, rest = argv[0], argv[1:]
    if cmd == "boot":
        return boot(rest[0] if rest else cfg.get("runlevel", "default"), cfg)
    if cmd == "status":
        return status(cfg)
    if cmd == "start":
        return start_service(rest[0], cfg) if rest else _usage()
    if cmd == "stop":
        return stop_service(rest[0]) if rest else _usage()
    if cmd == "restart":
        if not rest:
            return _usage()
        stop_service(rest[0])
        return start_service(rest[0], cfg)
    if cmd == "shutdown":
        return shutdown()
    print(f"init: unknown command '{cmd}'.")
    return _usage()


def _usage():
    print("usage: truck-init boot|status|start|stop|restart|shutdown [service]")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
