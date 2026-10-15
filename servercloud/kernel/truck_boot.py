"""truck_boot — TruckOS bootloader.

Emulates a boot firmware + GRUB-style menu:

  1. POST-like checks (python version, data dir writable, host OS probe)
  2. countdown menu (boot_timeout_sec from sysconfig):
        [1] TruckOS default          -> runlevel 'default'
        [2] TruckOS multi-user       -> runlevel 'multi-user'
        [3] Rescue shell             -> runlevel 'rescue'
        [4] Boot ServerCloud directly (skip init services)
     keys: 1-4 select, space starts immediately, any other key waits
  3. loads the simulated kernel (truck_kernel) — ring-0 comes up first,
     exactly like a real bootloader jumps into vmlinux after menu selection
  4. hands control to truck_init.boot(runlevel) then returns it to caller.

Used by servercloud_app.py as the entry sequence so every launch of the
program looks and feels like booting a small OS.
"""

import os
import platform
import sys
import time

from servercloud.truck_modules import trk
from servercloud.kernel import truck_init, truck_kernel
from servercloud.truck_modules.sysconfig import SysConfig

# sanity bounds for kernel tuning coming from the (user-editable) config
RAM_MB_RANGE = (8, 4096)          # virtual RAM in MiB
NCPU_RANGE = (1, 64)
HZ_RANGE = (10, 1000)


def _bounded(val, lo, hi, fallback, name):
    try:
        v = int(val)
    except (TypeError, ValueError):
        print(f"boot: invalid {name}={val!r}, using {fallback}")
        return fallback
    if not lo <= v <= hi:
        print(f"boot: {name}={v} out of range [{lo},{hi}], using {fallback}")
        return fallback
    return v

BOOTLOGO = r"""
  _____                         _  ___        ___
 |_   _|_ __ _   _ _ __| | ___| |/ _ \      / _ \/ ___
   | |/ __|`| | | | '_ \| |/ _ \ | (_)_____| | | | |_) |
   | | (_| || |_| | |_) | |  __/ |\__/_____| |_| |  __/
   |_|\___|__,\__,_| .__/|_|\___|_| \___/   \___/|_|
                   |_|      TruckOS bootloader v1.0
"""

ENTRIES = [
    ("TruckOS default (menu)", "default"),
    ("TruckOS multi-user (services + API)", "multi-user"),
    ("Rescue shell", "rescue"),
    ("Boot ServerCloud directly (bypass init)", "servercloud"),
]


def _post(data_dir):
    """Power-on self test; prints one line per check."""
    ok = True
    py = ".".join(map(str, sys.version_info[:3]))
    print(f"  [POST] python {py} ................ OK")
    try:
        os.makedirs(data_dir, exist_ok=True)
        probe = os.path.join(data_dir, ".truck_probe")
        with open(probe, "w") as f:
            f.write("x")
        os.remove(probe)
        print(f"  [POST] data dir {data_dir} ... WRITABLE")
    except OSError as e:
        print(f"  [POST] data dir NOT writable: {e}")
        ok = False
    print(f"  [POST] host OS {platform.system()} {platform.release()}"
          f" ({platform.machine()}) .. OK")
    return ok


def _menu(timeout):
    print()
    for i, (label, _) in enumerate(ENTRIES, 1):
        print(f"   [{i}] {label}")
    print(f"\n   Select entry (1-{len(ENTRIES)}), space=boot #1,"
          f" timeout {timeout}s: ", end="", flush=True)
    deadline = time.time() + max(1, int(timeout))
    choice = None
    if sys.stdin.isatty():
        try:
            import msvcrt  # Windows
            getch = lambda: msvcrt.getwch()
        except ImportError:
            import tty
            import termios
            def getch():
                fd = sys.stdin.fileno()
                old = termios.tcgetattr(fd)
                try:
                    tty.setraw(fd)
                    return sys.stdin.read(1)
                finally:
                    termios.tcsetattr(fd, termios.TCSADRAIN, old)
        while time.time() < deadline and choice is None:
            ch = getch()
            if ch.isdigit() and 1 <= int(ch) <= len(ENTRIES):
                choice = int(ch)
            elif ch == " ":
                choice = 1
            elif ch in ("\x03", "q"):
                choice = 1
    else:
        # non-interactive (CI / piped stdin): auto-select default
        print(f"<auto:1> ({timeout}s)")
        choice = 1
    return choice or 1


def _load_kernel(cfg):
    """Bootloader stage: jump into the simulated ring-0 kernel."""
    ram_mb = _bounded(cfg.get("kernel_ram_mb", 64), *RAM_MB_RANGE,
                      fallback=64, name="kernel_ram_mb")
    ncpu = _bounded(cfg.get("kernel_ncpu", 2), *NCPU_RANGE,
                    fallback=2, name="kernel_ncpu")
    hz = _bounded(cfg.get("kernel_hz", truck_kernel.HZ_DEFAULT), *HZ_RANGE,
                  fallback=truck_kernel.HZ_DEFAULT, name="kernel_hz")
    hostname = str(cfg.get("hostname", "truck-os"))[:64]
    k = truck_kernel.Kernel(hostname=hostname, ncpu=ncpu,
                            ram_bytes=ram_mb << 20)
    k.hz = hz
    print(f"\nboot: loading kernel image vmlinuz-{truck_kernel.KERNEL_VERSION}"
          f" ({ram_mb} MiB virtual RAM) ...")
    k.start()
    truck_kernel._DEFAULT = k            # publish the singleton
    return k


def boot(data_dir, interactive=True):
    root = trk.init_truck(data_dir)
    cfg = SysConfig(os.path.join(root, "etc", "sysconfig.json"))
    print(BOOTLOGO)
    print("Booting TruckOS ...")
    if not _post(data_dir):
        print("boot: POST failed — dropping to rescue shell.")
        return "rescue", cfg
    if interactive:
        sel = _menu(cfg.get("boot_timeout_sec", 5))
    else:
        sel = 1
    label, runlevel = ENTRIES[sel - 1]
    print(f"\nboot: selected '{label}'")
    if runlevel != "servercloud":
        _load_kernel(cfg)                 # ring 0 comes up first
        truck_init._paths(data_dir)
        truck_init.boot(runlevel, cfg)
    return runlevel, cfg


if __name__ == "__main__":
    dd = os.environ.get("SERVERCLOUD_DATA",
                        os.path.expanduser("~/.servercloud"))
    rl, _ = boot(dd)
    print(f"boot complete: runlevel={rl}")
