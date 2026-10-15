"""trk — Truck Package Manager.

A self-contained, stdlib-only package manager for the embedded TruckOS
userland.  Packages are small JSON manifests (.trkpkg) describing a name,
version, license, trust level and a list of payload files (inline text or
downloaded from a URL).  Installed packages live under:

    <data_dir>/truck/var/lib/trk/<name>/          manifest.json + files/
    <data_dir>/truck/usr/bin/<name>               executable entry script

so 'trk' maintains its own /usr/bin, /var/lib and /etc just like a real
distro — but rooted inside the ServerCloud data directory.

Commands:
    trk install <name|url|path.trkpkg> [--force] [--trust]
    trk remove <name>
    trk search <query>
    trk list
    trk info <name>
    trk bin <name> [args...]        run an installed package's entry script
    trk repo add <url> | repo list | repo remove <url>
    trk doctor                      consistency check of the package DB
"""

import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import time
import urllib.request

TRUCK_ROOT = None  # set by init_truck()


def init_truck(data_dir):
    """Create the TruckOS filesystem skeleton; return the truck root path."""
    global TRUCK_ROOT
    TRUCK_ROOT = os.path.join(data_dir, "truck")
    for sub in ("usr/bin", "usr/share/truck/pkg", "var/lib/trk",
                "var/log", "etc/trk", "opt"):
        os.makedirs(os.path.join(TRUCK_ROOT, sub), exist_ok=True)
    return TRUCK_ROOT


def _db_dir():
    return os.path.join(TRUCK_ROOT, "var", "lib", "trk")


def _bin_dir():
    return os.path.join(TRUCK_ROOT, "usr", "bin")


def _repo_file():
    return os.path.join(TRUCK_ROOT, "etc", "trk", "repos.json")

DEFAULT_REPOS = [
    "https://raw.githubusercontent.com/servercloud/trk-repo/main/index.json",
]


def _load_repos():
    try:
        with open(_repo_file(), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return list(DEFAULT_REPOS)


def _save_repos(repos):
    os.makedirs(os.path.dirname(_repo_file()), exist_ok=True)
    with open(_repo_file(), "w", encoding="utf-8") as f:
        json.dump(repos, f, indent=2)


# ---------------------------------------------------------------- helpers
def _installed_names():
    try:
        return sorted(d for d in os.listdir(_db_dir())
                      if os.path.isdir(os.path.join(_db_dir(), d)))
    except OSError:
        return []


def _read_manifest(name):
    p = os.path.join(_db_dir(), name, "manifest.json")
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _fetch(url, timeout=30):
    """Fetch a URL — only http/https are allowed.

    Blocking file://, ftp:// and custom schemes prevents local-file reads
    through repo/package URLs (SSRF-style path disclosure).
    """
    if not url.startswith(("http://", "https://")):
        raise ValueError(f"trk: refused non-http(s) URL: {url!r}")
    req = urllib.request.Request(url, headers={"User-Agent": "trk/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _repo_index(query=""):
    """Search all configured repos; returns list of (repo_url, entry)."""
    hits = []
    q = query.lower()
    for url in _load_repos():
        if url.startswith(("http://", "https://")):
            try:
                data = json.loads(_fetch(url))
            except Exception as e:
                print(f"  ! repo unreachable: {url} ({e})")
                continue
        else:  # local file repo
            try:
                with open(url, encoding="utf-8") as f:
                    data = json.load(f)
            except (OSError, ValueError) as e:
                print(f"  ! repo unreadable: {url} ({e})")
                continue
        for entry in data.get("packages", []):
            hay = (entry.get("name", "") + " " +
                   entry.get("description", "")).lower()
            if not q or q in hay:
                hits.append((url, entry))
    return hits


def _make_exec(path):
    if os.name != "nt":
        mode = os.stat(path).st_mode
        os.chmod(path, mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


# ---------------------------------------------------------------- commands
def cmd_install(spec, force=False, trust=False):
    """Install from a repo name, a .trkpkg file path, or a direct URL."""
    manifest = None
    if spec.endswith(".trkpkg") or spec.startswith(("http://", "https://")):
        raw = _fetch(spec) if spec.startswith("http") else \
            open(spec, "rb").read()
        manifest = json.loads(raw)
    else:
        hits = [e for u, e in _repo_index(spec) if e.get("name") == spec]
        if not hits:
            print(f"trk: package '{spec}' not found in any repo.")
            return 1
        manifest = hits[0]
        # if the repo entry points at a full .trkpkg, fetch it
        if manifest.get("package_url"):
            pkg_url = manifest["package_url"]
            if not pkg_url.startswith(("http://", "https://", "/")):
                print("trk: refusing non-http package_url.")
                return 1
            manifest = json.loads(_fetch(pkg_url))

    name = manifest.get("name", "").strip()
    # hardening: names must be plain single filename components — blocks
    # '../'-traversal into the truck root and absolute-path DB escapes
    if (not name or os.path.sep in name or (os.altsep and os.altsep in name)
            or name in (".", "..") or os.path.isabs(name)):
        print(f"trk: invalid package name {name!r}.")
        return 1
    if manifest.get("trust", "untrusted") == "untrusted" and not trust \
            and not force:
        print(f"trk: '{name}' is UNTRUSTED. Review it, then re-run with"
              f" --trust to accept.")
        return 1
    dest = os.path.join(_db_dir(), name)
    if os.path.exists(dest) and not force:
        print(f"trk: '{name}' v{manifest.get('version','?')} already installed"
              f" (use --force to reinstall).")
        return 0

    os.makedirs(os.path.join(dest, "files"), exist_ok=True)
    files_root = os.path.abspath(dest)
    for fname, content in manifest.get("files", {}).items():
        target = os.path.normpath(os.path.join(dest, "files", fname))
        # robust containment check: exact prefix OR separator boundary
        if (os.path.isabs(fname)
                or not (target == files_root
                        or target.startswith(files_root + os.sep))):
            print(f"trk: refusing path traversal in '{fname}'.")
            shutil.rmtree(dest, ignore_errors=True)
            return 1
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "w", encoding="utf-8") as f:
            f.write(content)
    # downloaded blobs
    for url, fname in manifest.get("downloads", {}).items():
        target = os.path.normpath(os.path.join(dest, "files", fname))
        if (os.path.isabs(fname)
                or not (target == files_root
                        or target.startswith(files_root + os.sep))):
            print(f"trk: refusing path traversal in download '{fname}'.")
            shutil.rmtree(dest, ignore_errors=True)
            return 1
        os.makedirs(os.path.dirname(target), exist_ok=True)
        print(f"  fetching {url} ...")
        with open(target, "wb") as f:
            f.write(_fetch(url, timeout=120))

    digest = hashlib.sha256(json.dumps(manifest, sort_keys=True)
                            .encode()).hexdigest()[:12]
    manifest["_installed_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    manifest["_sha"] = digest
    with open(os.path.join(dest, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)

    # expose entry script in trk's own /usr/bin
    entry = manifest.get("entry")  # filename inside files/
    if entry:
        src = os.path.normpath(os.path.join(dest, "files", entry))
        if not src.startswith(files_root + os.sep):
            print(f"trk: refusing path traversal in entry '{entry}'.")
            shutil.rmtree(dest, ignore_errors=True)
            return 1
        link = os.path.join(_bin_dir(), name)
        shutil.copyfile(src, link)
        _make_exec(link)

    print(f"trk: installed {name} v{manifest.get('version', '?')}"
          f" [{manifest.get('license', 'no-license')}] sha:{digest}")
    return 0


def cmd_remove(name):
    if not name or os.path.sep in name or name in (".", ".."):
        print(f"trk: invalid package name {name!r}.")
        return 1
    dest = os.path.join(_db_dir(), name)
    if not os.path.isdir(dest):
        print(f"trk: '{name}' is not installed.")
        return 1
    shutil.rmtree(dest)
    link = os.path.join(_bin_dir(), name)
    if os.path.exists(link):
        os.remove(link)
    print(f"trk: removed {name}.")
    return 0


def cmd_list():
    names = _installed_names()
    if not names:
        print("trk: no packages installed.")
        return 0
    for n in names:
        m = _read_manifest(n) or {}
        print(f"  {n:<20} v{m.get('version', '?'):<8} "
              f"[{m.get('trust', '?')}]  {m.get('description', '')}")
    print(f"total: {len(names)}")
    return 0


def cmd_search(query):
    hits = _repo_index(query)
    if not hits:
        print(f"trk: nothing matches '{query}'.")
        return 0
    installed = set(_installed_names())
    for _, e in hits:
        mark = " (installed)" if e.get("name") in installed else ""
        print(f"  {e.get('name', '?'):<20} v{e.get('version', '?'):<8} "
              f"[{e.get('trust', '?')}]  {e.get('description', '')}{mark}")
    return 0


def cmd_info(name):
    m = _read_manifest(name)
    if not m:
        print(f"trk: '{name}' is not installed.")
        return 1
    print(json.dumps(m, indent=2))
    return 0


def cmd_bin(name, args):
    link = os.path.join(_bin_dir(), name)
    if not os.path.exists(link):
        print(f"trk: '{name}' not installed or has no entry script.")
        return 1
    return subprocess.call([sys.executable, link] + list(args))


def cmd_repo(sub, arg=None):
    repos = _load_repos()
    if sub == "list":
        for r in repos:
            print(f"  {r}")
        return 0
    if sub == "add" and arg:
        if arg not in repos:
            repos.append(arg)
            _save_repos(repos)
        print(f"trk: repo added: {arg}")
        return 0
    if sub == "remove" and arg:
        if arg in repos:
            repos.remove(arg)
            _save_repos(repos)
            print(f"trk: repo removed: {arg}")
        return 0
    print("usage: trk repo add|remove <url> | trk repo list")
    return 1


def cmd_doctor():
    ok = True
    for n in _installed_names():
        m = _read_manifest(n)
        if m is None:
            print(f"  ! {n}: corrupt manifest")
            ok = False
    # dangling entry links
    try:
        for f in os.listdir(_bin_dir()):
            if os.path.isfile(os.path.join(_bin_dir(), f)) and \
                    f not in _installed_names():
                print(f"  ! orphan entry script: usr/bin/{f}")
    except OSError:
        pass
    print("trk: database OK." if ok else "trk: problems found (see above).")
    return 0 if ok else 1


def main(argv, data_dir=None):
    if data_dir:
        init_truck(data_dir)
    if TRUCK_ROOT is None:
        print("trk: truck root not initialised.")
        return 1
    if not argv:
        print(__doc__)
        return 0
    cmd, rest = argv[0], argv[1:]
    if cmd == "install":
        force = "--force" in rest
        trust = "--trust" in rest
        pos = [a for a in rest if not a.startswith("--")]
        if not pos:
            print("usage: trk install <name|url|file.trkpkg>")
            return 1
        return cmd_install(pos[0], force, trust)
    if cmd == "remove":
        return cmd_remove(rest[0]) if rest else _usage()
    if cmd == "list":
        return cmd_list()
    if cmd == "search":
        return cmd_search(" ".join(rest))
    if cmd == "info":
        return cmd_info(rest[0]) if rest else _usage()
    if cmd == "bin":
        return cmd_bin(rest[0], rest[1:]) if rest else _usage()
    if cmd == "repo":
        return cmd_repo(rest[0] if rest else "list",
                        rest[1] if len(rest) > 1 else None)
    if cmd == "doctor":
        return cmd_doctor()
    if cmd == "kernel":
        return cmd_kernel(rest)
    print(f"trk: unknown command '{cmd}'.")
    return _usage()


# ------------------------------------------------------------- kernel cmds
def cmd_kernel(args):
    """trk kernel <sub> — inspect the simulated TruckOS ring-0 kernel.

    subcommands: info | ps | dmesg | vmstat | syscalls | cat <procpath>
    All access goes through the int 0x80 syscall gate (truck_syscall).
    """
    from kernel import truck_kernel, truck_syscall
    k = truck_kernel.default_kernel()
    if k.state != "running":
        print("trk kernel: no kernel is running (boot via truck_boot first).")
        return 1
    sub = args[0] if args else "info"
    k.tick()                                   # let timer IRQs accrue
    if sub == "info":
        stats = truck_syscall.sysint(11, "stats", kernel=k)
        mem_total_kb = k.phys.size // 1024
        mem_free_kb = stats["mem_free_pages"] * truck_kernel.PAGE_SIZE // 1024
        print(f"  kernel       : {truck_kernel.KERNEL_NAME} "
              f"{truck_kernel.KERNEL_VERSION} ({truck_kernel.KERNEL_BUILDOPT})")
        print(f"  state        : {stats['state']}")
        print(f"  uptime       : {stats['uptime']} s   hz={k.hz}")
        print(f"  cpu(s)       : {k.ncpu} x Truck Virtual CPU v1")
        print(f"  memory       : {mem_free_kb} / {mem_total_kb} kB free "
              f"({stats['mem_free_pages']}/{stats['mem_total_pages']} pages)")
        print(f"  slab         : {stats['slab']}")
        print(f"  vm           : {stats['vm']}")
        print(f"  syscalls     : {stats['syscall_count']}   "
              f"timer IRQs: {stats['timer_irq']}   "
              f"context switches: {stats['context_switches']}")
        return 0
    if sub == "ps":
        lines = truck_syscall.sysint(11, "ps", kernel=k)
        print("  PID NAME                 ST PPID NICE VRUNTIME      CPU")
        for ln in lines:
            print("  " + ln)
        return 0
    if sub == "dmesg":
        for ln in k.boot_log:
            print("  " + ln)
        return 0
    if sub == "vmstat":
        txt = truck_syscall.sysint(0, "/proc/vmstat", kernel=k) or ""
        for ln in txt.splitlines():
            print("  " + ln)
        return 0
    if sub == "syscalls":
        print(truck_syscall.render_table())
        return 0
    if sub == "cat":
        if len(args) < 2:
            print("usage: trk kernel cat <procpath>")
            return 1
        txt = truck_syscall.sysint(0, args[1], kernel=k)
        if txt is None:
            print(f"  cat: {args[1]}: No such file or directory (-ENOENT)")
            return 1
        sys.stdout.write(txt)
        return 0
    print(f"trk kernel: unknown subcommand '{sub}' "
          f"(info|ps|dmesg|vmstat|syscalls|cat).")
    return 1


def _usage():
    print("usage: trk install|remove|list|search|info|bin|repo|doctor|"
          "kernel ...")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:],
                  os.environ.get("SERVERCLOUD_DATA",
                                 os.path.expanduser("~/.servercloud"))))
