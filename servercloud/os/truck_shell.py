"""truck_shell — interactive sub-shells for the four TruckOS menu entries.

Each function returns control to the main menu when the user types
'back' / 'exit' or presses Ctrl-C / Ctrl-D.  All UI text is English.

UI conventions
--------------
* Every screen redraw clears the terminal first (``clear_screen``) so only
  the current menu/function is visible — a clean "GUI" feel.
* In every settings sub-menu you can enter just the NUMBER of a section
  (e.g. ``1``) to jump straight into it — no command word required.
"""

import os
import subprocess
import sys

from servercloud.utils.colors import (c, GREEN, YELLOW, RED, CYAN,
                                    BOLD, DIM, MAGENTA)


def clear_screen():
    """Wipe the terminal completely — works in Termux and every other TTY.

    ANSI escape sequence is used directly (never curses: it repaints the old
    screen on endwin, which looks like 'nothing was cleared'):
      * \\033[3J — erase the SCROLLBACK buffer (Termux honours this),
      * \\033[2J — erase the visible screen,
      * \\033[H  — move cursor to home so new output starts at the top.
    """
    if os.environ.get("TERM") == "dumb" or not sys.stdout.isatty():
        return
    try:
        sys.stdout.write("\033[3J\033[2J\033[H")
        sys.stdout.flush()
    except Exception:                              # noqa: BLE001
        pass


def _header(title, subtitle=""):
    """Standard framed title used by every sub-shell screen."""
    line = "═" * max(24, len(title) + 6)
    print(c(f"╔{line}╗", CYAN))
    print(c(f"║  {title}" + " " * (len(line) - len(title) - 2) + "║", CYAN + BOLD))
    if subtitle:
        print(c(f"║  {subtitle}" + " " * (len(line) - len(subtitle) - 2) + "║",
                CYAN))
    print(c(f"╚{line}╝", CYAN))


def _prompt(shell, color=GREEN):
    return c(f"{shell}> ", color)


def _line(prompt_color=GREEN):
    try:
        return input(_prompt("truck", prompt_color)).strip()
    except (EOFError, KeyboardInterrupt):
        return "back"


# ------------------------------------------------------------ TERMINAL
def terminal_shell(app):
    """Menu [1] Terminal — host shell + trk package commands inline."""
    clear_screen()
    _header("TRUCKOS TERMINAL", "type 'help' for commands · 'back' → main menu")
    if app.trk_root:
        print(DIM + f"  truck root: {app.trk_root}" + "\033[0m")
    while True:
        line = _line(CYAN)
        low = line.lower()
        if low in ("back", "exit", "quit", ""):
            return
        if low == "help":
            print("  <any shell command>   run on host OS ($ prefix optional)")
            print("  cd <dir>             change working directory")
            print("  ls                   list current directory")
            print("  trk <args>           Truck package manager")
            print("  init <args>          TruckOS init (status/start/stop/boot)")
            print("  kernel <sub>         simulated TruckOS kernel "
                  "(info|ps|dmesg|vmstat|syscalls|cat <procfile>)")
            print("  ollama <args>        talk to the local Ollama binary")
            print("  env                  show environment")
            continue
        if low.startswith("cd "):
            target = os.path.expanduser(line[3:].strip() or "~")
            try:
                os.chdir(target)
                print(DIM + os.getcwd() + "\033[0m")
            except OSError as e:
                print(c(str(e), RED))
            continue
        if low == "ls":
            try:
                for name in sorted(os.listdir(".")):
                    mark = "/" if os.path.isdir(name) else ""
                    print(f"  {name}{mark}")
            except OSError as e:
                print(c(str(e), RED))
            continue
        if low == "env":
            for k, v in sorted(os.environ.items()):
                print(f"  {k}={v}")
            continue
        # trk / init / kernel / ollama shortcuts
        if low.startswith("trk"):
            args = line.split()[1:]
            rc = app.trk_main(args)
            print(DIM + f"[trk exit {rc}]" + "\033[0m")
            continue
        if low.startswith("init"):
            args = line.split()[1:] or ["status"]
            rc = app.init_main(args)
            print(DIM + f"[init exit {rc}]" + "\033[0m")
            continue
        if low.startswith("kernel"):
            args = line.split()[1:] or ["info"]
            rc = app.trk_main(["kernel"] + args)
            print(DIM + f"[int 0x80 -> kernel, exit {rc}]" + "\033[0m")
            continue
        if low.startswith("ollama"):
            args = line.split()
            try:
                subprocess.call(args)
            except (FileNotFoundError, OSError) as e:
                print(c(f"terminal: {e}", RED))
            continue
        # generic shell command
        cmd = line[1:] if line.startswith("$") else line
        try:
            rc = subprocess.call(cmd, shell=True)
            if rc != 0:
                print(DIM + f"[exit code {rc}]" + "\033[0m")
        except OSError as e:
            print(c(f"terminal: {e}", RED))


# ------------------------------------------------------------ LLM LOCAL
def llm_local_shell(app):
    """Menu [2] LLM Local — thin wrapper over the existing ServerCloud REPL."""
    print(c("LLM Local — local model factory console.", YELLOW))
    print("  Quick keys: /models /dl <n> /list /w <model> <text> /status "
          "/agent /apis ... | 'back' returns to TruckOS menu.")
    print("  Type 'help' inside for the full command reference.")
    app.llm_local_entry()


# ------------------------------------------------------------ SCRIPT LIBRARY
def script_library_shell(app):
    lib = app.scriptlib
    lib.seed_defaults()
    while True:
        clear_screen()
        _header("SCRIPT LIBRARY", "reusable server-ops snippets")
        items = lib.list()
        print(BOLD + f"  Scripts ({len(items)}):" + "\033[0m")
        if not items:
            print("  (empty — add one with 'new')")
        for i, r in enumerate(items, 1):
            tags = ",".join(r.get("tags", []))
            print(f"  [{i:>2}] {r['name']:<28} ({r['kind']}) "
                  f"uses:{r.get('uses', 0)} #{tags}  {r.get('desc', '')}")
        print("\n  run <n|slug> | view <n|slug> | new | del <n|slug> | "
              "find <query> | back")
        line = _line(GREEN).lower()
        if not line:
            continue
        if line in ("back", "exit", "quit"):
            return
        parts = line.split(maxsplit=1)
        cmd = parts[0]
        arg = parts[1].strip() if len(parts) > 1 else ""
        if cmd == "find":
            hits = lib.list(query=arg)
            for r in hits:
                print(f"  {r['slug']:<28} {r.get('desc', '')}")
            if not hits:
                print(c("no matches.", YELLOW))
            continue
        rec = None
        if arg.isdigit() and 1 <= int(arg) <= len(items):
            rec = items[int(arg) - 1]
        elif arg:
            rec = lib.get(arg)
        if cmd == "view" and rec:
            print(DIM + "-" * 40 + "\033[0m")
            print(rec["body"])
            print(DIM + "-" * 40 + "\033[0m")
            continue
        if cmd == "run" and rec:
            print(c(f"running {rec['name']} ...", YELLOW))
            lib.mark_used(rec["slug"])
            try:
                subprocess.call(rec["body"], shell=True)
            except OSError as e:
                print(c(str(e), RED))
            continue
        if cmd == "del" and rec:
            lib.delete(rec["slug"])
            print(c(f"deleted {rec['name']}.", GREEN))
            continue
        if cmd == "new":
            name = _input("  name: ")
            if not name:
                continue
            desc = _input("  description: ")
            kind = _input("  kind [shell]: ") or "shell"
            tags = [t.strip() for t in _input("  tags (comma sep): ").split(",")
                    if t.strip()]
            print("  body — end with a single '.' on its own line:")
            body_lines = []
            while True:
                try:
                    l = input("  | ")
                except (EOFError, KeyboardInterrupt):
                    break
                if l.strip() == ".":
                    break
                body_lines.append(l)
            rec = lib.save(name, "\n".join(body_lines), kind, tags, desc)
            print(c(f"saved {rec['slug']}.", GREEN))
            continue
        print(c("unknown action.", RED))


def _input(prompt):
    try:
        return input(c(prompt, CYAN)).strip()
    except (EOFError, KeyboardInterrupt):
        return ""


# ------------------------------------------------------------ AUTO-UPDATE
REASON_LABEL = {
    "security_update": "urgent security update",
    "vulnerability_found": "vulnerability found & fixed",
    "critical_bug_fix": "critical bug fix",
}


def _flag(r):
    """Red [!] urgent flag + short status line."""
    mark = c("[!]", RED + BOLD) + " " if r.get("urgent") else ""
    return mark


def _show_os_check(r):
    if not r.get("ok"):
        print(c(f"  check failed: {r.get('error', 'unknown error')}", RED))
        return
    print(BOLD + f"\n  OS UPDATE CHECK — {r['owner']}/{r['repo']}@{r['branch']}"
          + "\033[0m")
    print(f"  installed version : ServerCloud {r['installed_version']}")
    if r.get("available"):
        print(_flag(r) + BOLD + f"latest push: {r['title']} [NEW]" + "\033[0m")
    else:
        print(_flag(r) + BOLD + f"latest push: {r['title']}" + "\033[0m")
    print(f"  commit            : {r['short_sha']}  by {r['author']}  {r['date']}")
    print("  update available  : "
          + (c("YES", GREEN + BOLD) if r.get("available") else "no — up to date"))
    body = [l for l in r["message"].splitlines()[1:] if l.strip()]
    if body:
        print(DIM + "  what changed (commit message):" + "\033[0m")
        for l in body[:8]:
            print(DIM + f"    {l}" + "\033[0m")
    print(f"  files in push     : {r['changed_files']}"
          + ("  (kernel)" if r["touches_kernel"] else "")
          + ("  (security)" if r["touches_security"] else ""))
    if r["release"]:
        rel = r["release"]
        pre = " [pre-release]" if rel["prerelease"] else ""
        print(f"  release with this push : YES — {rel['tag']} “{rel['name']}”{pre}")
    else:
        print("  release with this push : no")
    if r["urgent"]:
        print(c(f"  urgency           : URGENT — "
                f"{REASON_LABEL.get(r['urgent_reason'], r['urgent_reason'])}",
                RED + BOLD))
    else:
        print(c("  urgency           : routine", GREEN))


def _show_kernel_check(r):
    if not r.get("ok"):
        print(c(f"  check failed: {r.get('error', 'unknown error')}", RED))
        return
    print(BOLD + f"\n  KERNEL UPDATE CHECK — markers in {r['owner']}/{r['repo']}"
          "@main" + "\033[0m")
    print(f"  installed kernel build  : {r['installed_kernel']} "
          f"(release marker {r['installed_release_version']})")
    print(f"  kernel version on GitHub: {r['remote_version']}  ({r['date']})")
    print(_flag(r) + "update available      : "
          + ("YES" if r["available"] else "no — up to date"))
    if r["urgent"]:
        print(c(f"  urgency               : URGENT — "
                f"{REASON_LABEL.get(r['urgent_reason'], '?')}", RED + BOLD))
    else:
        print("  urgency               : normal")
    sec_line = ("  contains security fixes : " + c("YES", GREEN + BOLD)
                if r["has_security_fixes"] else
                "  contains security fixes : no")
    print(sec_line)
    print(f"  written in              : {r['languages']}")
    print(DIM + f"  description             : {r['description']}" + "\033[0m")


def _show_security_check(r):
    if not r.get("ok"):
        print(c(f"  check failed: {r.get('error', 'unknown error')}", RED))
        return
    print(BOLD + f"\n  SECURITY UPDATE CHECK — {r['owner']}/{r['repo']}"
          "/servercloud/security" + "\033[0m")
    print(f"  installed ruleset   : {r['installed_db']}")
    print(f"  latest ruleset      : {r['remote_version']}  ({r['date']})")
    print(_flag(r) + "update available    : "
          + ("YES" if r["available"] else "no — current"))
    sev_col = {"critical": RED + BOLD, "high": RED,
               "moderate": YELLOW, "low": GREEN}.get(r["severity"], DIM)
    print("  severity            : " + c(r["severity"], sev_col))
    if r["urgent"]:
        print(c(f"  urgency             : URGENT — "
                f"{REASON_LABEL.get(r['urgent_reason'], r['urgent_reason'])}",
                RED + BOLD))
    print(DIM + f"  changelog           : {r['description']}" + "\033[0m")


KIND_NAME = {"os": "OS", "kernel": "KERNEL", "security": "SECURITY"}


def _offer_install(truck_update, cfg, kind, result):
    """If a check found an available update, immediately offer to install it.

    Returns True when an install was attempted (caller may pause afterwards).
    """
    if not (isinstance(result, dict) and result.get("ok")
            and result.get("available")):
        return False
    urgent = bool(result.get("urgent"))
    name = KIND_NAME.get(kind, kind)
    print()
    if urgent:
        print(c("  [!] URGENT UPDATE AVAILABLE — recommended to install now",
                RED + BOLD))
    else:
        print(c("  >> UPDATE AVAILABLE — install it now?", GREEN + BOLD))
    prompt = ("  install %s update now? [y/N]: " % name if not urgent else
              "  install %s update NOW? [Y/n]: " % name)
    try:
        ans = input(c(prompt, YELLOW)).strip().lower()
    except (EOFError, KeyboardInterrupt):
        ans = ""
    default_yes = urgent
    if ans in ("y", "yes") or (ans == "" and default_yes):
        print()
        print(truck_update.apply_update(kind, cfg, yes=True))
        return True
    print(c(f"  skipped — you can still run: apply {kind}", DIM))
    return False


def auto_update_section(app):
    from truck.modules import update_engine as truck_update
    cfg = app.sysconfig
    last = {}
    while True:
        clear_screen()
        _header("SETTINGS ▸ AUTO-UPDATE",
                "checks the GitHub repo · nothing applied without confirmation")
        print("   ┌──────────────────────────────────────────────┐")
        print("   │  [1] Check OS Updates                        │")
        print("   │  [2] Kernel Update                           │")
        print("   │  [3] Security Update                         │")
        print("   │  [a] Run all three checks                    │")
        print("   │  apply os | kernel | security — pull update  │")
        print("   │  back / 0 — return to Settings               │")
        print("   └──────────────────────────────────────────────┘")
        line = _line(CYAN).lower()
        if line in ("back", "exit", "quit", "", "0"):
            return
        try:
            installed = False
            if line in ("1", "check os"):
                last["os"] = truck_update.check_os_updates(cfg)
                _show_os_check(last["os"])
                installed = _offer_install(truck_update, cfg, "os", last["os"])
            elif line in ("2", "kernel"):
                last["kernel"] = truck_update.check_kernel_update(cfg)
                _show_kernel_check(last["kernel"])
                installed = _offer_install(truck_update, cfg, "kernel",
                                           last["kernel"])
            elif line in ("3", "security"):
                last["sec"] = truck_update.check_security_update(cfg)
                _show_security_check(last["sec"])
                installed = _offer_install(truck_update, cfg, "security",
                                           last["sec"])
            elif line == "a":
                for kind, fn, show in (
                        ("os", truck_update.check_os_updates, _show_os_check),
                        ("kernel", truck_update.check_kernel_update,
                         _show_kernel_check),
                        ("security", truck_update.check_security_update,
                         _show_security_check)):
                    last[kind] = fn(cfg)
                    show(last[kind])
                    if _offer_install(truck_update, cfg, kind, last[kind]):
                        installed = True
                        print()
                        if not _confirm("  continue checking the rest? [Y/n] "):
                            break
            elif line.startswith("apply "):
                kind = line.split(maxsplit=1)[1].strip()
                kind = {"os-updates": "os", "updates": "os",
                        "sec": "security"}.get(kind, kind)
                if kind not in ("os", "kernel", "security"):
                    print(c("usage: apply os | apply kernel | apply security",
                            YELLOW))
                    continue
                if _confirm(f"  pull '{kind}' update from GitHub? [y/N] "):
                    print(truck_update.apply_update(kind, cfg, yes=True))
            else:
                print(c("unknown option.", RED))
            if installed:
                _pause()
        except Exception as e:  # network/JSON safety net — never crash menu
            print(c(f"auto-update: {e}", RED))


# ------------------------------------------------------------ SETTINGS
SETTINGS_SECTIONS = {
    "1": ("Auto-Update", "OS / kernel / security update checks"),
    "2": ("Services", "init service manager status"),
    "3": ("Kernel Info", "ring 0 build, memory, scheduler"),
    "4": ("Package Manager", "trk — install/remove/search packages"),
}


def settings_shell(app):
    cfg = app.sysconfig
    while True:
        clear_screen()
        _header("TRUCKOS SETTINGS", "changes are saved immediately")
        cur = cfg.all()
        print(BOLD + "  ── Sections (enter the number to open) ──" + "\033[0m")
        for num, (name, desc) in SETTINGS_SECTIONS.items():
            print(f"   [{num}] {name:<18} — {desc}")
        print("   [5] Reset settings to defaults")
        print(BOLD + "\n  ── Current configuration ──" + "\033[0m")
        for k in ("hostname", "runlevel", "boot_timeout_sec", "require_trust",
                  "ollama_bin", "api_port", "tps_cap",
                  "kernel_ram_mb", "kernel_ncpu", "kernel_hz"):
            print(f"    {k:<16} = {cur.get(k)}")
        print("\n  set <key> <value> | back / 0 → main menu")
        line = _line(MAGENTA)
        low = line.lower().strip()
        if low in ("back", "exit", "quit", "", "0"):
            return
        # ---- numeric section shortcuts ----
        if low == "1":
            auto_update_section(app)
            continue
        if low == "2":
            clear_screen()
            _header("SETTINGS ▸ SERVICES")
            app.init_main(["status"])
            _pause()
            continue
        if low == "3":
            clear_screen()
            _header("SETTINGS ▸ KERNEL INFO")
            app.trk_main(["kernel", "info"])
            _pause()
            continue
        if low == "4":
            clear_screen()
            _header("SETTINGS ▸ PACKAGE MANAGER (trk)")
            app.trk_main(["list"])
            print(DIM + "  trk search <pkg> | trk install <pkg> | "
                  "trk remove <pkg>" + "\033[0m")
            sub = _input("  trk> ")
            if sub:
                app.trk_main(sub.split())
                _pause()
            continue
        if low == "5":
            if _confirm("  wipe all settings to defaults? [y/N] "):
                cfg.set("hostname", "truck-os")
                cfg.set("runlevel", "default")
                cfg.set("boot_timeout_sec", 5)
                cfg.set("require_trust", True)
                print(c("settings reset.", GREEN))
                _pause()
            continue
        # ---- text commands still work ----
        if low in ("u", "auto-update", "auto update", "autoupdate"):
            auto_update_section(app)
            continue
        if low == "services":
            app.init_main(["status"])
            continue
        if low == "kernel":
            app.trk_main(["kernel", "info"])
            continue
        if low.startswith("set "):
            parts = line.split(maxsplit=2)
            if len(parts) < 3:
                print(c("usage: set <key> <value>", YELLOW))
                continue
            key, val = parts[1], parts[2]
            if key in ("boot_timeout_sec", "api_port", "kernel_ram_mb",
                       "kernel_ncpu", "kernel_hz"):
                try:
                    val = int(val)
                except ValueError:
                    print(c("value must be an integer.", RED)); continue
            elif key == "tps_cap":
                try:
                    val = float(val)
                except ValueError:
                    print(c("value must be a number.", RED)); continue
            elif key == "require_trust":
                val = val.lower() in ("1", "true", "yes", "on")
            cfg.set(key, val)
            print(c(f"{key} = {val} (saved)", GREEN))
            continue
        if low.startswith("trk"):
            app.trk_main(line.split()[1:])
            continue
        print(c("unknown command or section number.", RED))
        _pause()


def _pause():
    """Wait for Enter so sub-screen output isn't wiped by the next redraw."""
    try:
        input(c("\n  press ENTER to continue…", DIM))
    except (EOFError, KeyboardInterrupt):
        pass


def _confirm(prompt):
    try:
        return input(c(prompt, YELLOW)).strip().lower() in ("y", "yes")
    except (EOFError, KeyboardInterrupt):
        return False
