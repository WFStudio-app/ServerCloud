"""sync_upd_file — regenerate the upd_file/ mirror of update markers.

upd_file/ holds the plain .txt/.data files that the Settings -> Auto-Update
functions read (they are fetched from GitHub by truck.modules.update_engine):

    upd_file/kernel_version.txt   <- kernel/VERSION.data      (single line)
    upd_file/kernel_release.data  <- kernel/RELEASE.data      (key = value)
    upd_file/security.data        <- security/SECURITY.data   (key = value)
    upd_file/os_latest.txt        <- generated: latest local commit summary

Run after every release bump:  python -m truck_modules.sync_upd_file
"""

import os
import subprocess

from . import update_markers as um

OUT_DIR = os.path.join(um.REPO_ROOT, "upd_file")


def _copy(src_rel, dst_name):
    with open(os.path.join(um.REPO_ROOT, src_rel), encoding="utf-8") as f:
        text = f.read()
    path = os.path.join(OUT_DIR, dst_name)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return path


def _os_latest():
    try:
        r = subprocess.run(
            ["git", "-C", um.REPO_ROOT, "log", "-1",
             "--pretty=%H%n%s%n%an%n%aI"],
            capture_output=True, text=True, timeout=10)
        if r.returncode == 0:
            lines = r.stdout.strip().splitlines()
            if len(lines) >= 4:
                return ("commit = %s\nsubject = %s\nauthor = %s\ndate = %s\n"
                        % tuple(l[:200] for l in lines[:4]))
    except (OSError, subprocess.SubprocessError):
        pass
    return "commit = unknown\nsubject = git unavailable\n"


def sync():
    os.makedirs(OUT_DIR, exist_ok=True)
    written = [
        _copy(um.VERSION_FILE_REL, "kernel_version.txt"),
        _copy(um.RELEASE_FILE_REL, "kernel_release.data"),
        _copy(um.SECURITY_DATA_FILE_REL, "security.data"),
    ]
    p = os.path.join(OUT_DIR, "os_latest.txt")
    with open(p, "w", encoding="utf-8") as f:
        f.write(_os_latest())
    written.append(p)
    return written


if __name__ == "__main__":
    for w in sync():
        print("wrote", w)
