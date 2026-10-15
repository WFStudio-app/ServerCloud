"""truck_shell — interactive sub-shells for the four TruckOS menu entries.

Each function returns control to the main menu when the user types
'back' / 'exit' or presses Ctrl-C / Ctrl-D.  All UI text is English.
"""

import os
import subprocess
import sys

from servercloud.utils.colors import (c, GREEN, YELLOW, RED, CYAN,
                                    BOLD, DIM, MAGENTA)


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
    print(c("TruckOS Terminal — type 'help' for commands, 'back' for menu.",
            YELLOW))
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
    print(c("Script Library — reusable server-ops snippets.", YELLOW))
    while True:
        items = lib.list()
        print(BOLD + "\nScripts:" + "\033[0m")
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


# ------------------------------------------------------------ SETTINGS
def settings_shell(app):
    cfg = app.sysconfig
    print(c("TruckOS Settings — changes are saved immediately.", YELLOW))
    while True:
        cur = cfg.all()
        print(BOLD + "\nCurrent configuration:" + "\033[0m")
        for k in ("hostname", "runlevel", "boot_timeout_sec", "require_trust",
                  "ollama_bin", "api_port", "tps_cap",
                  "kernel_ram_mb", "kernel_ncpu", "kernel_hz"):
            print(f"  {k:<16} = {cur.get(k)}")
        print("\n  set <key> <value> | trk <args> | services | kernel | "
              "reset | back")
        line = _line(MAGENTA)
        low = line.lower()
        if low in ("back", "exit", "quit", ""):
            return
        if low == "services":
            app.init_main(["status"])
            continue
        if low == "kernel":
            app.trk_main(["kernel", "info"])
            continue
        if low == "reset":
            if _confirm("  wipe all settings to defaults? [y/N] "):
                cfg.set("hostname", "truck-os")
                cfg.set("runlevel", "default")
                cfg.set("boot_timeout_sec", 5)
                cfg.set("require_trust", True)
                print(c("settings reset.", GREEN))
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
        print(c("unknown command.", RED))


def _confirm(prompt):
    try:
        return input(c(prompt, YELLOW)).strip().lower() in ("y", "yes")
    except (EOFError, KeyboardInterrupt):
        return False
