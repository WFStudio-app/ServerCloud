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
    "runlevel": "default",          # default | multi-user | rescue | cluster
    "boot_timeout_sec": 5,          # bootloader menu countdown
    "require_trust": True,          # trk refuses untrusted packages by default
    "ollama_bin": "ollama",         # path/name of the ollama binary
    "api_port": 8777,
    "tps_cap": 8.0,                 # generation speed cap (tokens/sec)
    # ---- staged boot layers (truck_boot) --------------------------------
    "boot_menu_enabled": 1,         # show GRUB-style stage at all
    "boot_quick_mode": 0,           # 1 = collapse stages 1-3 (CI/headless)
    "boot_quiet": 0,                # 1 = hide cosmetic POST/stage banners
    "boot_log_verbose": 1,          # 0 = silent kernel _klog output
    "boot_memtest": 0,              # 1 = extended memory pass in POST
    "boot_bios_key_enabled": 1,     # 'b' at the menu opens BIOS setup
    "boot_stage_delay_sec": 0.3,    # pause between boot layers (0..2)
    "boot_order": ["truck0 (rootfs)", "usb", "pxe/network", "efi-shell"],
    # ---- cluster (multi-device server fusion) ----------------------------
    "cluster_enabled": False,       # auto-join configured links at boot
    "cluster_name": "truck-cloud",
    "cluster_role": "auto",         # auto | controller | worker
    "cluster_port": 8790,           # inter-node control plane port
    "cluster_group": "",            # multicast/join group key
    # ---- simulated kernel tuning (truck_kernel / truck_boot) ----
    "kernel_ram_mb": 64,            # virtual RAM size in MiB
    "kernel_ncpu": 2,               # emulated CPU count (/proc/cpuinfo)
    "kernel_hz": 100,               # timer interrupt frequency
    # ---- BIOS-tunable virtual hardware (editable live in firmware setup) --
    "bios_gpu_vram_mb": 512,        # virtual GPU video memory in MiB
    "bios_gpu_enabled": 1,          # 0 = iGPU disabled (POST skips the device)
    "bios_ssd_gb": 128,             # virtual SSD capacity in GiB
    "bios_ssd_enabled": 1,          # 0 = disk hidden from POST/rootfs probe
    # ---- live monitor (servercloud/modules/monitor.py) ------------------
    "mon_token_stream": 1,          # mirror every generated token to the log
    "mon_refresh_sec": 0.5,         # dashboard redraw interval
    "mon_api_log": 1,               # record API-key traffic into api_calls.log
    "mon_mask_len": 35,             # fixed-width encrypted key fingerprint
    "mon_net_track": 1,             # «Отслеживание сетевых вычислений» pane on
    # ---- compute section («Вычисления», servercloud/modules/compute.py) --
    "compute_mode": "auto",         # active scheduling profile
    "compute_modes": "",            # comma list; empty = all built-in modes
    "quant_format": "q4_0",         # default quantization format
    # ---- network section («Сеть», servercloud/modules/netkit.py) ---------
    "net_dns_enabled": 1,           # serve local DNS zone from netkit
    "net_proxy_port": 8800,         # default front port for proxy rules
    "net_vault_dir": "",            # vault root (empty = <data>/truck/vault)
    "services": {},                 # per-service overrides: {"name": {...}}
    "settings": {},                 # free-form user settings
}

_INT_KEYS = ("boot_timeout_sec", "api_port", "kernel_ram_mb", "kernel_ncpu",
             "kernel_hz", "boot_menu_enabled", "boot_quick_mode", "boot_quiet",
             "boot_log_verbose", "boot_memtest", "boot_bios_key_enabled",
             "cluster_port", "bios_gpu_vram_mb", "bios_gpu_enabled",
             "bios_ssd_gb", "bios_ssd_enabled", "mon_refresh_sec",
             "mon_mask_len", "net_proxy_port")

_BOOL_KEYS = ("mon_token_stream", "mon_api_log", "mon_net_track",
              "net_dns_enabled")


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
