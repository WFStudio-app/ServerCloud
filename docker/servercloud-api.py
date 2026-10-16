#!/usr/bin/env python3
"""Headless ServerCloud network-API launcher (used by SC_MODE=api).

Boots the App exactly like `servercloud` does (registry, keystore, chat
store, TruckOS paths) but skips the interactive menu and keeps the HTTP
API server alive until SIGTERM/SIGINT.  Configuration via env:

    SERVERCLOUD_API_HOST   bind address (default 0.0.0.0)
    SERVERCLOUD_API_PORT   port        (default 8777)
    SERVERCLOUD_DATA       data dir    (default /var/lib/servercloud)
"""
import os
import signal
import sys
import time

os.environ.setdefault("SERVERCLOUD_DATA", "/var/lib/servercloud")
sys.path.insert(0, os.environ.get("SC_HOME", "/opt/servercloud"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

def main():
    import servercloud_app as scapp

    app = scapp.App()
    if app.api is None or not app.api.running:
        print("servercloud-api: failed to start API server", file=sys.stderr)
        return 1
    host, port = app.api.host, app.api.port
    print(f"servercloud-api: listening on http://{host}:{port}")

    stop = {"flag": False}

    def _sig(signum, frame):
        stop["flag"] = True
        print("servercloud-api: shutting down…")

    signal.signal(signal.SIGTERM, _sig)
    signal.signal(signal.SIGINT, _sig)

    while not stop["flag"]:
        time.sleep(1)

    try:
        app.api.stop()
    except Exception:
        pass
    return 0

if __name__ == "__main__":
    sys.exit(main())
