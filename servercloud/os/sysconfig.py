"""sysconfig — persistent TruckOS system configuration (JSON-backed).

Stores: hostname, default runlevel, boot timeout, trusted-package flag,
service overrides, and arbitrary key/value settings under 'settings'.
File: <data_dir>/truck/etc/sysconfig.json
"""

import json
import os
import threading

DEFAULTS = {
    "hostname": "truck-os",
    "runlevel": "default",          # default | multi-user | rescue
    "boot_timeout_sec": 5,          # bootloader menu countdown
    "require_trust": True,          # trk refuses untrusted packages by default
    "ollama_bin": "ollama",         # path/name of the ollama binary
    "api_port": 8777,
    "tps_cap": 8.0,                 # generation speed cap (tokens/sec)
    "services": {},                 # per-service overrides: {"name": {...}}
    "settings": {},                 # free-form user settings
}


class SysConfig:
    def __init__(self, path):
        self.path = path
        self._lock = threading.Lock()
        self._data = dict(DEFAULTS)
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                if isinstance(loaded, dict):
                    self._data.update(loaded)
            except (OSError, ValueError):
                pass  # corrupted file -> fall back to defaults

    # ---------- access ----------
    def get(self, key, default=None):
        with self._lock:
            return self._data.get(key, DEFAULTS.get(key, default))

    def set(self, key, value):
        with self._lock:
            self._data[key] = value
            self._save_locked()

    def setting(self, key, default=None):
        with self._lock:
            return self._data["settings"].get(key, default)

    def set_setting(self, key, value):
        with self._lock:
            self._data["settings"][key] = value
            self._save_locked()

    def all(self):
        with self._lock:
            return json.loads(json.dumps(self._data))

    # ---------- persistence ----------
    def _save_locked(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._data, f, indent=2)
        os.replace(tmp, self.path)

    def save(self):
        with self._lock:
            self._save_locked()
