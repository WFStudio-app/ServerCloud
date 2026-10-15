"""Language probe for the kernel tree.

Used by Settings > Auto-update ("Update Kernel") to show, for every
release, *which languages the kernel is written in*.  Detection is done
with a real AST/lexical analysis of the shipped sources — not a hardcoded
list — so it stays correct as the tree grows (e.g. when C/Rust modules or
shell helpers are added).
"""

import ast
import os

# file extension -> language name
_EXT_LANG = {
    ".py": "Python",
    ".c": "C",
    ".h": "C header",
    ".rs": "Rust",
    ".sh": "POSIX shell",
    ".asm": "Assembly",
    ".s": "Assembly",
}


def _scan_dir(root):
    langs = {}
    files = []
    for dirpath, _dirs, names in os.walk(root):
        if "__pycache__" in dirpath:
            continue
        for n in sorted(names):
            ext = os.path.splitext(n)[1].lower()
            lang = _EXT_LANG.get(ext)
            if not lang:
                continue
            path = os.path.join(dirpath, n)
            files.append(path)
            entry = langs.setdefault(lang, {"files": 0, "lines": 0})
            entry["files"] += 1
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as f:
                    entry["lines"] += sum(1 for _ in f)
            except OSError:
                pass
    return langs, files


def _python_flavours(files):
    """Extra fidelity: pure-python vs ctypes/syscall-heavy code."""
    stdlib_only = True
    uses_ctypes = False
    for p in files:
        if not p.endswith(".py"):
            continue
        try:
            with open(p, encoding="utf-8", errors="replace") as f:
                src = f.read()
            tree = ast.parse(src)
        except (SyntaxError, OSError):
            continue
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        if "ctypes" in imported:
            uses_ctypes = True
        if imported & {"socket", "mmap", "termios", "fcntl"}:
            stdlib_only = False
    tags = ["pure-Python (stdlib only)" if stdlib_only else
            "Python + OS-facing stdlib (mmap/socket/termios)"]
    if uses_ctypes:
        tags.append("FFI bridge (ctypes)")
    return tags


def kernel_languages(kernel_dir=None):
    """Return {'Python': {...}, ...} plus implementation notes.

    Defaults to the whole kernel/ tree (core + porting layer), not just
    the directory this probe lives in.
    """
    if kernel_dir is None:
        here = os.path.dirname(os.path.abspath(__file__))     # kernel/porting
        kernel_dir = os.path.dirname(here)                     # kernel/
    root = kernel_dir
    langs, files = _scan_dir(root)
    notes = _python_flavours(files)
    return {"languages": langs, "implementation_notes": notes,
            "scanned_files": len(files)}


def format_languages(info):
    lines = ["kernel implementation languages:"]
    for lang, st in sorted(info["languages"].items(),
                           key=lambda kv: -kv[1]["lines"]):
        lines.append(f"  - {lang:<12} {st['files']:>3} file(s), "
                     f"{st['lines']:>6} lines")
    for note in info["implementation_notes"]:
        lines.append(f"  * {note}")
    return "\n".join(lines)
