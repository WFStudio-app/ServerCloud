"""Hardware power measurement + automatic token-rate tuning (/autt).

Measures: CPU cores, load average, free RAM, disk space, thermal zone.
Computes a 0..100 "power score" and recommends tokens/sec for the
selected model (heavier models need more headroom).
Pure stdlib — works on Termux, Linux, macOS and Windows.
On Windows/macOS where /proc is absent, falls back to ctypes
(GlobalMemoryStatusEx / sysctl) and psutil if installed.
"""

import os
import platform
import re
import shutil
import time

IS_WINDOWS = platform.system() == "Windows"
IS_MACOS = platform.system() == "Darwin"
IS_LINUX = platform.system() == "Linux"


def _cpu_count():
    try:
        return os.cpu_count() or 1
    except Exception:
        return 1


def _load_avg():
    try:
        return os.getloadavg()[0]
    except (OSError, AttributeError):
        # Windows has no getloadavg; estimate CPU busy from os.times() deltas.
        # BUGFIX: the previous version read nonexistent attributes
        # (_last_user/_last_sys) off os.times(), so busy was always ~0 and
        # load on Windows was permanently 0. Now we keep real module-level
        # snapshots of (elapsed_wall, user+system time) between calls.
        if IS_WINDOWS:
            global _LAST_TIMES
            try:
                t = os.times()
                now = time.monotonic()
                cpu = (t.user + t.system + t.children_user + t.children_system)
                if _LAST_TIMES is None:
                    _LAST_TIMES = (now, cpu)
                    return 0.0
                dt = max(now - _LAST_TIMES[0], 1e-6)
                dcpu = max(cpu - _LAST_TIMES[1], 0.0)
                _LAST_TIMES = (now, cpu)
                # busy cores over the interval, clamped to core count
                return min(dcpu / dt, float(_cpu_count()))
            except Exception:
                return 0.0
        return 0.0


_LAST_TIMES = None


def _mem_windows():
    """GlobalMemoryStatusEx via ctypes -> (total_mb, avail_mb)."""
    import ctypes

    class MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [
            ("dwLength", ctypes.c_ulong),
            ("dwMemoryLoad", ctypes.c_ulong),
            ("ullTotalPhys", ctypes.c_ulonglong),
            ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong),
            ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong),
            ("ullAvailVirtual", ctypes.c_ulonglong),
            ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]

    stat = MEMORYSTATUSEX()
    stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
    try:
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))
        return (stat.ullTotalPhys / 1048576.0,
                stat.ullAvailPhys / 1048576.0)
    except Exception:
        return 0.0, 0.0


def _mem_macos():
    """sysctl hw.memsize + vm_stat -> (total_mb, avail_mb)."""
    import subprocess
    total = 0.0
    avail = 0.0
    try:
        out = subprocess.run(["sysctl", "-n", "hw.memsize"],
                             capture_output=True, text=True, timeout=5)
        total = float(out.stdout.strip()) / 1048576.0
        page = 4096.0
        try:
            p = subprocess.run(["sysctl", "-n", "hw.pagesize"],
                               capture_output=True, text=True, timeout=5)
            page = float(p.stdout.strip())
        except Exception:
            pass
        vm = subprocess.run(["vm_stat"], capture_output=True, text=True, timeout=5)
        free = inactive = speculative = 0.0
        for line in vm.stdout.splitlines():
            if "Pages free" in line:
                free = float(line.split(":")[1].strip().rstrip("."))
            elif "Pages inactive" in line:
                inactive = float(line.split(":")[1].strip().rstrip("."))
            elif "Pages speculative" in line:
                speculative = float(line.split(":")[1].strip().rstrip("."))
        avail = (free + inactive + speculative) * page / 1048576.0
    except Exception:
        pass
    return total, avail


def _mem_psutil():
    try:
        import psutil
        vm = psutil.virtual_memory()
        return vm.total / 1048576.0, vm.available / 1048576.0
    except Exception:
        return 0.0, 0.0


def _mem_info():
    """Return (total_mb, available_mb) cross-platform."""
    total = avail = 0.0
    if IS_LINUX:
        try:
            with open("/proc/meminfo") as f:
                for line in f:
                    if line.startswith("MemTotal:"):
                        total = float(line.split()[1]) / 1024.0
                    elif line.startswith("MemAvailable:"):
                        avail = float(line.split()[1]) / 1024.0
        except OSError:
            pass
    elif IS_WINDOWS:
        total, avail = _mem_windows()
    elif IS_MACOS:
        total, avail = _mem_macos()
    if not total:
        total, avail = _mem_psutil()
    return total, avail


def _disk_free_gb(path=None):
    try:
        u = shutil.disk_usage(path or os.path.expanduser("~"))
        return u.free / (1024 ** 3)
    except OSError:
        return 0.0


# ---- public helpers (used by servercloud_app / cluster / monitor) ----

def total_ram_mb():
    """Total physical RAM in megabytes (cross-platform)."""
    total, _avail = _mem_info()
    return int(total) if total else 0


def available_ram_mb():
    """Available RAM in megabytes (cross-platform)."""
    _total, avail = _mem_info()
    return int(avail) if avail else 0


def free_disk_gb(path=None):
    """Free disk space in gigabytes at *path* (default: home dir)."""
    return round(_disk_free_gb(path), 2)


def _thermal_linux():
    """Best-effort SoC temperature via thermal zones (Termux/RPi/x86 Linux)."""
    base = "/sys/class/thermal"
    try:
        for zone in sorted(os.listdir(base)):
            tp = os.path.join(base, zone, "temp")
            tz = os.path.join(base, zone, "type")
            if not os.path.exists(tp):
                continue
            ztype = ""
            try:
                with open(tz) as f:
                    ztype = f.read().strip().lower()
            except OSError:
                pass
            if any(k in ztype for k in ("soc", "cpu", "x86_pkg", "core")):
                with open(tp) as f:
                    return int(f.read().strip()) / 1000.0
    except OSError:
        pass
    return None


def _thermal_windows():
    """WMI MSAcpi_ThermalZoneTemperature via WMI (needs admin on Win)."""
    try:
        import subprocess
        ps = ("Get-CimInstance -Namespace root/cimv2 "
              "-ClassName MSAcpi_ThermalZoneTemperature "
              "| Select-Object -ExpandProperty CurrentTemperature")
        out = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                             capture_output=True, text=True, timeout=15)
        val = out.stdout.strip().splitlines()
        if val:
            raw = float(val[-1])          # deci-Kelvin
            return round(raw / 10.0 - 273.15, 1)
    except Exception:
        pass
    return None


def _thermal_macos():
    """Try powermetrics (sudo) / osx-cpu-temp if installed; else None."""
    import subprocess
    for cmd in (["osx-cpu-temp"], ["sudo", "-n", "powermetrics", "--samplers",
                                   "smc", "-i1", "--show-temperature"]):
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=8)
            for line in out.stdout.splitlines():
                nums = re.findall(r"(\d+\.\d+)\s*C", line)
                if nums and any(k in line.lower() for k in
                                ("cpu", "package", "average", "temp")):
                    return float(nums[0])
        except Exception:
            continue
    return None


def _thermal_psutil():
    try:
        import psutil
        temps = psutil.sensors_temperatures() or {}
        for name, entries in temps.items():
            if any(k in name.lower() for k in ("core", "cpu", "k10", "acpi")):
                for e in entries:
                    if e.current:
                        return float(e.current)
    except Exception:
        pass
    return None


def _thermal_c():
    """Cross-platform best-effort CPU temperature in Celsius."""
    t = None
    if IS_LINUX:
        t = _thermal_linux()
    elif IS_WINDOWS:
        t = _thermal_windows()
    elif IS_MACOS:
        t = _thermal_macos()
    if t is None:
        t = _thermal_psutil()
    return t


def detect_vps():
    """Best-effort VPS/container detection: systemd-detect-virt, DMI product
    name, hypervisor flag in /proc/cpuinfo. Returns e.g. 'kvm', 'docker',
    'xen', 'vmware', or None for bare metal."""
    try:
        import subprocess
        out = subprocess.run(["systemd-detect-virt"], capture_output=True,
                             text=True, timeout=5)
        v = out.stdout.strip()
        if v and v != "none":
            return v
    except Exception:
        pass
    # /sys/class/dmi/id/product_name (Linux)
    try:
        with open("/sys/class/dmi/id/product_name") as f:
            p = f.read().strip().lower()
        for k in ("digitalocean", "droplet", "ecs", "compute", "amazon web",
                  "hetzner", "vultr", "qemu", "kvm", "virtualbox", "vmware",
                  "xen", "hyperv", "openstack", "cloud"):
            if k in p:
                return k
    except OSError:
        pass
    # /proc/cpuinfo hypervisor flag
    try:
        with open("/proc/cpuinfo") as f:
            for line in f:
                if line.startswith("flags") and "hypervisor" in line:
                    return "vm"
    except OSError:
        pass
    return None


def measure():
    """Collect hardware snapshot."""
    cores = _cpu_count()
    total_mb, avail_mb = _mem_info()
    data = {
        "cores": cores,
        "load1": _load_avg(),
        "ram_total_mb": round(total_mb),
        "ram_avail_mb": round(avail_mb),
        "disk_free_gb": round(_disk_free_gb(), 1),
        "temp_c": _thermal_c(),
        "os": platform.system(),
        "os_release": platform.release(),
        "arch": platform.machine(),
        "vps": detect_vps(),
    }
    # ---- power score 0..100 ----
    cpu_score = min(cores / 8.0, 1.0) * 45                      # up to 45
    ram_score = min(avail_mb / 4096.0, 1.0) * 35 if avail_mb else 0.15 * 35
    load_penalty = max(0.0, min(data["load1"] / max(cores, 1), 1.0)) * 15
    temp_penalty = 0.0
    if data["temp_c"] is not None and data["temp_c"] > 70:
        temp_penalty = min((data["temp_c"] - 70) / 30.0, 1.0) * 10
    score = max(0.0, cpu_score + ram_score - load_penalty - temp_penalty)
    data["power_score"] = round(score, 1)
    return data


def recommend_tps(model_size_gb: float, hw: dict) -> float:
    """Tokens/sec recommendation: heavier model -> slower; stronger iron -> faster."""
    score = hw.get("power_score", 20)
    size = max(float(model_size_gb or 1.0), 0.3)
    base = 4.0 + score * 0.9          # weak phone ~8, desktop ~85
    tps = base / (size ** 0.55)       # heavy models scaled down
    avail = hw.get("ram_avail_mb", 0)
    if avail and avail < size * 1024 * 1.2:   # model barely fits in RAM
        tps *= 0.5
    return round(max(0.5, min(tps, 250.0)), 1)
