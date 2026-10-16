"""truck_boot — TruckOS staged bootloader (UEFI-firmware style).

The boot process is split into several independent *stages* (layers), each
one configurable through sysconfig.json ("boot" section / BOOT defaults):

  stage 1  firmware/POST   power-on self test, memory probe, device scan
  stage 2  BIOS/UEFI setup  optional setup screen (boot_menu_enabled) —
                            lets the user re-order devices, tune the kernel
                            and jump straight into TruckOS
  stage 3  bootloader menu  GRUB-style countdown entries (ENTRIES below)
  stage 4  kernel           ring-0 comes up (vmlinuz load, e820, subsystems)
  stage 5  init             PID 1 hands off to the requested runlevel

Keys at the menu: 1-N select, space boots #1, `b` opens the BIOS setup,
any other key waits out the timeout.  Everything is skippable via config:
boot_quick_mode=1 collapses stages 1-3 into a fast silent path (CI/headless).

Used by servercloud_app.py as the entry sequence so every launch of the
program looks and feels like booting a small OS.
"""

import os
import platform
import sys
import time

from truck_modules import trk
from . import truck_init, truck_kernel
from truck_modules.sysconfig import SysConfig

# sanity bounds for kernel tuning coming from the (user-editable) config
RAM_MB_RANGE = (8, 4096)          # virtual RAM in MiB
NCPU_RANGE = (1, 64)
HZ_RANGE = (10, 1000)
STAGE_DELAY_RANGE = (0.0, 2.0)    # cosmetic pause between boot layers


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


def _stage_delay(cfg):
    """Seconds to pause between boot stages (0 disables all pauses)."""
    try:
        v = float(cfg.get("boot_stage_delay_sec", 0.3))
    except (TypeError, ValueError):
        v = 0.3
    return min(max(v, STAGE_DELAY_RANGE[0]), STAGE_DELAY_RANGE[1])

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
    ("TruckOS cluster node (headless)", "cluster"),
    ("Rescue shell", "rescue"),
    ("Boot ServerCloud directly (bypass init)", "servercloud"),
]

# BIOS device boot order — user can re-order it in the setup screen
DEFAULT_BOOT_ORDER = ["truck0 (rootfs)", "usb", "pxe/network", "efi-shell"]


def _getch():
    try:
        import msvcrt  # Windows
        return lambda: msvcrt.getwch()
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
        return getch


def _post(cfg):
    """Stage 1 — firmware/POST.  Prints one line per layer check.

    Every sub-check honours the boot config: quiet mode hides the cosmetic
    lines, memtest toggles the extended memory pass, quick_mode collapses
    the pauses between checks.
    """
    ok = True
    quiet = bool(int(cfg.get("boot_quiet", 0)))
    delay = _stage_delay(cfg)

    def line(txt):
        if not quiet:
            print(txt)

    line("── STAGE 1/5 · TRUCK-FIRMWARE v2.1 POST ─────────────────────")
    py = ".".join(map(str, sys.version_info[:3]))
    print(f"  [POST] python {py} ................ OK")
    ram_mb = _bounded(cfg.get("kernel_ram_mb", 64), *RAM_MB_RANGE,
                      fallback=64, name="kernel_ram_mb")
    ncpu = _bounded(cfg.get("kernel_ncpu", 2), *NCPU_RANGE,
                    fallback=2, name="kernel_ncpu")
    gpu_en = int(bool(cfg.get("bios_gpu_enabled", 1)))
    vram_mb = _bounded(cfg.get("bios_gpu_vram_mb", 512), 64, 24576,
                       512, "bios_gpu_vram_mb")
    ssd_en = int(bool(cfg.get("bios_ssd_enabled", 1)))
    ssd_gb = _bounded(cfg.get("bios_ssd_gb", 128), 1, 8192, 128, "bios_ssd_gb")
    line(f"  [POST] CPU: {ncpu} × Truck Virtual CPU v1 ........ OK")
    line(f"  [POST] RAM: {ram_mb} MiB virtual .................. OK")
    if int(cfg.get("boot_memtest", 0)):
        line(f"  [MEMTEST] pattern pass on {ram_mb} MiB ......... PASS")
    if gpu_en:
        line(f"  [POST] GPU: Truck iGPU {vram_mb} MiB VRAM ...... OK")
    else:
        line("  [POST] GPU: disabled in BIOS setup ............ SKIP")
    if ssd_en:
        line(f"  [POST] SSD: Truck NVMe {ssd_gb} GiB ............ OK")
    else:
        line("  [POST] SSD: hidden in BIOS setup .............. SKIP")
    line(f"  [POST] host OS {platform.system()} {platform.release()}"
         f" ({platform.machine()}) .. OK")
    order = cfg.get("boot_order") or DEFAULT_BOOT_ORDER
    line(f"  [POST] boot devices: {' > '.join(order)}")
    try:
        root = trk.init_truck(cfg.path and os.path.dirname(
            os.path.dirname(cfg.path)) or ".")
        probe = os.path.join(root, ".truck_probe")
        with open(probe, "w") as f:
            f.write("x")
        os.remove(probe)
        if ssd_en:
            line(f"  [POST] rootfs {root} ... WRITABLE")
        else:
            line(f"  [POST] rootfs {root} ... tmpfs (SSD off)")
    except OSError as e:
        print(f"  [POST] data dir NOT writable: {e}")
        ok = False
    if not quiet:
        time.sleep(delay)
    return ok


# ---------------------------------------------------------------- BIOS setup
# Screen redraw helper: ANSI clear + home, no-op on dumb/piped terminals.
def _bios_clear():
    if os.environ.get("TERM") == "dumb" or not sys.stdout.isatty():
        return
    try:
        sys.stdout.write("\033[2J\033[H")
        sys.stdout.flush()
    except Exception:                              # noqa: BLE001
        pass


def bios_setup(cfg):
    """The simulated UEFI setup screen ('BIOS') — plain-text friendly.

    Reached from the bootloader menu (key `b`) or from a running TruckOS via
    `/bios` / `reboot --bios`.

    UX design goals (Termux-safe):
      * works with BOTH arrow keys and plain ASCII keys (8/2/4/6 or k/j/h/l)
        — Termux's extra-dash row can send either;
      * every setting has an explicit command: pick a line by number, then
        press Enter to edit its value like a normal question, or use +/- to
        nudge it, or type the new value directly;
      * toggles flip with a single Enter press — no hidden modes;
      * nothing is lost: changes are saved on Save&Exit (`s`), discarded on
        Quit-without-saving (`q`);
      * the whole screen clears before each redraw so only the BIOS is
        visible — no leftover OS output above it.

    Everything editable here persists straight into sysconfig.json.  The
    Advanced page tunes the *virtual hardware* live: RAM size, vCPU count,
    iGPU (on/off + VRAM) and SSD (on/off + capacity) — POST and the kernel
    honour these values on the very next boot without any file editing.
    Returns: None (or runlevel string when the user picks 'Boot TruckOS').
    """
    order = list(cfg.get("boot_order") or DEFAULT_BOOT_ORDER)
    sel = 0
    page = 0                       # 0 = Main, 1 = Advanced (virtual HW)
    booted = None
    dirty = False

    def save():
        cfg.set("kernel_ram_mb", ram[0])
        cfg.set("kernel_ncpu", cpu[0])
        cfg.set("boot_timeout_sec", tout[0])
        cfg.set("boot_quick_mode", quick[0])
        cfg.set("boot_menu_enabled", menu_en[0])
        cfg.set("boot_log_verbose", verbose[0])
        cfg.set("boot_memtest", memtest[0])
        cfg.set("boot_order", order)
        cfg.set("bios_gpu_enabled", gpu_en[0])
        cfg.set("bios_gpu_vram_mb", vram[0])
        cfg.set("bios_ssd_enabled", ssd_en[0])
        cfg.set("bios_ssd_gb", ssd[0])

    ram = [_bounded(cfg.get("kernel_ram_mb", 64), *RAM_MB_RANGE, 64, "ram")]
    cpu = [_bounded(cfg.get("kernel_ncpu", 2), *NCPU_RANGE, 2, "ncpu")]
    tout = [int(cfg.get("boot_timeout_sec", 5))]
    quick = [int(bool(cfg.get("boot_quick_mode", 0)))]
    menu_en = [int(bool(cfg.get("boot_menu_enabled", 1)))]
    verbose = [int(bool(cfg.get("boot_log_verbose", 1)))]
    memtest = [int(bool(cfg.get("boot_memtest", 0)))]
    gpu_en = [int(bool(cfg.get("bios_gpu_enabled", 1)))]
    vram = [_bounded(cfg.get("bios_gpu_vram_mb", 512), 64, 24576, 512, "vram")]
    ssd_en = [int(bool(cfg.get("bios_ssd_enabled", 1)))]
    ssd = [_bounded(cfg.get("bios_ssd_gb", 128), 1, 8192, 128, "ssd")]

    MAIN_ITEMS = [
        ("Boot menu enabled (GRUB stage)", menu_en, (0, 1)),
        ("Quick mode (collapse stages 1-3)", quick, (0, 1)),
        ("Verbose kernel log", verbose, (0, 1)),
        ("Extended memory test at POST", memtest, (0, 1)),
        ("Menu timeout (sec)", tout, (0, 30)),
    ]
    ADV_ITEMS = [
        ("Virtual RAM (MiB)", ram, RAM_MB_RANGE),
        ("Virtual CPUs", cpu, NCPU_RANGE),
        ("iGPU controller", gpu_en, (0, 1)),
        ("iGPU video RAM (MiB)", vram, (64, 24576)),
        ("SSD device", ssd_en, (0, 1)),
        ("SSD capacity (GiB)", ssd, (1, 8192)),
    ]
    TOGGLE_VALS = (menu_en, quick, verbose, memtest, gpu_en, ssd_en)
    PAGES = (("Main", MAIN_ITEMS),
             ("Advanced \u00b7 Virtual Hardware", ADV_ITEMS))

    def fmt_val(val):
        v = val[0]
        if any(val is t for t in TOGGLE_VALS):
            return "[ Enabled ]" if v else "[ Disabled ]"
        return f"[ {v} ]"

    def read_line(prompt="  BIOS> "):
        """Read one input line; returns None on EOF/Ctrl-C (= quit w/o save)."""
        try:
            return input(prompt).strip()
        except (EOFError, KeyboardInterrupt):
            return None

    def ask_number(title, lo, hi, cur):
        """Prompt for an integer inside [lo, hi]; Enter keeps current value."""
        raw = read_line(f"    {title} ({lo}-{hi}, Enter={cur}): ")
        if raw is None or raw == "":
            return cur
        try:
            v = int(raw)
        except ValueError:
            print(f"    '{raw}' is not a number - keeping {cur}")
            return cur
        if not lo <= v <= hi:
            print(f"    out of range {lo}-{hi} - clamped")
            v = max(lo, min(hi, v))
        return v

    def edit_item(item):
        """Open one setting: toggles flip instantly, numbers get a prompt."""
        nonlocal dirty
        label, val, rng = item
        lo, hi = rng
        if any(val is t for t in TOGGLE_VALS):
            val[0] = 0 if val[0] else 1
            dirty = True
            print(f"    {label} -> {'Enabled' if val[0] else 'Disabled'}")
            return
        nv = ask_number(label, lo, hi, val[0])
        if nv != val[0]:
            val[0] = nv
            dirty = True

    def cycle_order():
        nonlocal dirty
        if len(order) > 1:
            order.append(order.pop(0))
            dirty = True
        print("    boot order: " + " > ".join(order))

    def render():
        page_name, items = PAGES[page]
        _bios_clear()
        print("=========================================================")
        print("   TRUCK-FIRMWARE SETUP UTILITY  (BIOS emulation v2.1)")
        print("   TruckOS platform \u00b7 virtual hardware config")
        print("=========================================================")
        print(f"--- Page {page + 1}/{len(PAGES)}: {page_name} "
              "(4/6 or h/l = switch page) ---")
        for i, (label, val, _) in enumerate(items):
            mark = ">" if i == sel else " "
            print(f"  {mark}{i + 1:>2}. {label:<38}{fmt_val(val)}")
        if page == 0:
            print("--- Boot priority (press o to cycle first device) ---")
            for j, dev in enumerate(order):
                print(f"    {j + 1}. {dev}")
        print("---------------------------------------------------------")
        print("  Up/Down (8/2 or k/j): move selection  Enter: edit selected")
        print("  +/-: nudge value    number: jump to & edit that line")
        print("  o: boot order       s: SAVE & exit      q: exit w/o saving")
        print("  b: boot TruckOS now (saves first)")
        print("---------------------------------------------------------")
        if dirty:
            print("  (unsaved changes - press s to save)")

    while True:
        _, items = PAGES[page]
        render()
        if not sys.stdin.isatty():
            save()                          # headless/CI: persist defaults
            return None
        raw = read_line()
        if raw is None:                     # Ctrl-C / EOF -> quit w/o save
            return None
        cmd = raw.strip().lower()
        if cmd == "":                       # bare Enter -> edit selected line
            edit_item(PAGES[page][1][sel])
            continue
        if len(cmd) == 1:
            ch = cmd
            if ch in ("8", "k"):
                sel = (sel - 1) % len(items)
                continue
            if ch in ("2", "j"):
                sel = (sel + 1) % len(items)
                continue
            if ch in ("4", "h"):
                page = (page - 1) % len(PAGES)
                sel = min(sel, len(PAGES[page][1]) - 1)
                continue
            if ch in ("6", "l"):
                page = (page + 1) % len(PAGES)
                sel = min(sel, len(PAGES[page][1]) - 1)
                continue
            if ch == "s":
                save()
                return None
            if ch == "q":
                return None
            if ch == "b":
                save()
                booted = "default"
                break
            if ch == "o" and page == 0:
                cycle_order()
                continue
            if ch in ("+", "="):
                _, val, (_, hi) = items[sel]
                if any(val is t for t in TOGGLE_VALS):
                    val[0] = 1
                else:
                    val[0] = min(val[0] + 1, hi)
                dirty = True
                continue
            if ch in ("-", "_"):
                _, val, (lo, _) = items[sel]
                if any(val is t for t in TOGGLE_VALS):
                    val[0] = 0
                else:
                    val[0] = max(val[0] - 1, lo)
                dirty = True
                continue
        if cmd.isdigit() and 1 <= int(cmd) <= len(items):
            sel = int(cmd) - 1
            edit_item(items[sel])
            continue
        print(f"  unknown command '{raw}'  (Enter=edit, s=save, q=quit,"
              f" b=boot, 8/2=move)")

    if booted:
        save()
    return booted


def _menu(timeout, allow_bios=True):
    print()
    for i, (label, _) in enumerate(ENTRIES, 1):
        print(f"   [{i}] {label}")
    hint = f"   Select entry (1-{len(ENTRIES)}), space=boot #1"
    if allow_bios:
        hint += ", b=BIOS setup"
    print(f"\n{hint}, timeout {timeout}s: ", end="", flush=True)
    deadline = time.time() + max(1, int(timeout))
    choice = None
    if sys.stdin.isatty():
        getch = _getch()
        buf = ""
        while time.time() < deadline and choice is None:
            ch = getch()
            if ch == "\x1b":                   # escape sequences (arrows/F-keys)
                buf = ch
                continue
            if buf:                             # consume rest of an esc-sequence
                buf += ch
                if ch in "ABCDS":               # end byte of CSI sequence
                    if buf == "\x1bOA":         # F1-style codes → BIOS
                        return "bios"
                    buf = ""
                continue
            if ch.isdigit() and 1 <= int(ch) <= len(ENTRIES):
                choice = int(ch)
            elif ch == " ":
                choice = 1
            elif ch in ("\x03", "q"):
                choice = 1
            elif ch.lower() == "b" and allow_bios:
                return "bios"
    else:
        # non-interactive (CI / piped stdin): auto-select default
        print(f"<auto:1> ({timeout}s)")
        choice = 1
    return choice or 1


def _load_kernel(cfg):
    """Stage 4 — jump into the simulated ring-0 kernel."""
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
    verbose = bool(int(cfg.get("boot_log_verbose", 1)))
    if not verbose:
        k._klog_orig = k._klog
        k._klog = lambda level, msg: None      # silent kernel log
    print(f"\n── STAGE 4/5 · KERNEL ──────────────────────────────────────")
    print(f"boot: loading kernel image vmlinuz-{truck_kernel.KERNEL_VERSION}"
          f" ({ram_mb} MiB virtual RAM) ...")
    k.start()
    truck_kernel._DEFAULT = k            # publish the singleton
    return k


def boot(data_dir, interactive=True):
    root = trk.init_truck(data_dir)
    cfg = SysConfig(os.path.join(root, "etc", "sysconfig.json"))
    print(BOOTLOGO)
    quick = bool(int(cfg.get("boot_quick_mode", 0)))
    delay = _stage_delay(cfg)
    print("Booting TruckOS ... (staged: firmware → BIOS → GRUB → kernel → init)")
    # ---- stage 1: firmware / POST -------------------------------------
    if not _post(cfg):
        print("boot: POST failed — dropping to rescue shell.")
        return "rescue", cfg
    if quick:
        # collapsed fast path: skip stages 2-3 entirely (CI / headless)
        print("boot: quick mode — skipping BIOS & bootloader menu.")
        return "default", cfg
    runlevel = None
    if interactive and bool(int(cfg.get("boot_menu_enabled", 1))):
        # ---- stage 3: GRUB-style countdown menu --------------------------
        sel = _menu(cfg.get("boot_timeout_sec", 5),
                    allow_bios=bool(int(cfg.get("boot_bios_key_enabled", 1))))
        if sel == "bios":
            # ---- stage 2: user jumped out into the BIOS setup ------------
            print("\nboot: opening TRUCK-FIRMWARE SETUP (BIOS) ...")
            runlevel = bios_setup(cfg) or "default"
            print("boot: exiting BIOS setup, re-reading config ...")
            cfg = SysConfig(cfg.path)     # pick up everything BIOS changed
            sel = 1
        label, rl_entry = ENTRIES[sel - 1]
        print(f"\nboot: selected '{label}'")
        runlevel = runlevel or rl_entry
    else:
        runlevel = str(cfg.get("runlevel", "default"))
        if runlevel == "servercloud":
            return runlevel, cfg
    # ---- stage 4: kernel ------------------------------------------------
    if not bool(int(cfg.get("boot_quiet", 0))):
        time.sleep(delay)
    _load_kernel(cfg)                    # ring 0 comes up first
    # ---- stage 5: init / PID 1 -----------------------------------------
    if not bool(int(cfg.get("boot_quiet", 0))):
        print("\n── STAGE 5/5 · INIT (PID 1) ────────────────────────────────")
    truck_init._paths(data_dir)
    truck_init.boot(runlevel, cfg)
    return runlevel, cfg


if __name__ == "__main__":
    dd = os.environ.get("SERVERCLOUD_DATA",
                        os.path.expanduser("~/.servercloud"))
    rl, _ = boot(dd)
    print(f"boot complete: runlevel={rl}")
