"""truck_update — TruckOS Auto-Update service (Settings -> Auto-Update).

Three user-facing functions, all read-only against GitHub by default:

  1. check_os_updates()      latest commit on main + release info of that
                             push, urgency derived from changed paths;
  2. check_kernel_update()   reads kernel/{VERSION.data,RELEASE.data}
                             markers from GitHub and compares them with the
                             local kernel build;
  3. check_security_update() reads security/SECURITY.data and
                             reports the security-database status.

Security posture
----------------
* Network I/O only to https://api.github.com / raw.githubusercontent.com.
* The GitHub token (if configured) is attached ONLY to api.github.com
  requests; raw marker files are fetched anonymously over HTTPS.
* Redirects are disabled so credentials can never leak to another host.
* All remote data is treated as untrusted: sizes capped, versions validated
  against a strict pattern, strings truncated before display.
* apply_* functions refuse to run unless the remote repo identity matches
  the configured one AND the checkout's git 'origin' points at it too.
"""

import json
import os
import re
import shutil
import subprocess
import urllib.error
import urllib.parse
import urllib.request

from servercloud.agent.config import load_github_token, redact
from servercloud.core.version import version_string
from . import update_markers as um

API = "https://api.github.com"
RAW = "https://raw.githubusercontent.com"

DEFAULT_OWNER = "WFStudio-app"
DEFAULT_REPO = "ServerCloud"
DEFAULT_BRANCH = "main"

MAX_JSON_BYTES = 4 * 1024 * 1024      # 4 MiB cap on any fetched payload
MAX_TEXT_BYTES = 64 * 1024            # markers are tiny; anything bigger is hostile
_FIELD_MAX = 500                      # per-field display truncation

_URGENT_REASONS = set(um.URGENT_REASONS)
_KERNEL_PATHS = ("kernel/", "truck_modules/")
_SEC_PATHS = ("security/",)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


_opener = urllib.request.build_opener(_NoRedirect)


# --------------------------------------------------------------- low-level IO
def _clamp(s, n=_FIELD_MAX):
    s = str(s if s is not None else "")
    s = s.replace("\x00", "").replace("\r", " ").strip()
    return (s[:n] + "…") if len(s) > n else s


def _http(url, auth=False, timeout=20, binary_ok=False):
    """GET url over HTTPS only; optional token; size-capped response."""
    if not url.startswith("https://"):
        raise RuntimeError("refusing non-HTTPS url: %r" % url[:80])
    req = urllib.request.Request(url, method="GET")
    req.add_header("User-Agent", "TruckOS-update")
    if auth:
        tok = load_github_token()
        if tok:
            req.add_header("Authorization", f"Bearer {tok}")
            req.add_header("X-GitHub-Api-Version", "2022-11-28")
    try:
        with _opener.open(req, timeout=timeout) as resp:
            raw = resp.read(MAX_JSON_BYTES + 1)
    except urllib.error.HTTPError as e:
        raise RuntimeError(redact(f"GitHub HTTP {e.code}")) from e
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise RuntimeError(redact(f"network error: {e}")) from e
    if not binary_ok and len(raw) > MAX_JSON_BYTES:
        raise RuntimeError("payload too large")
    return raw


def _api_json(path, params=None, auth=True):
    url = API + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    return json.loads(_http(url, auth=auth).decode("utf-8", "replace"))


def _raw_file(owner, repo, branch, relpath):
    url = f"{RAW}/{owner}/{repo}/{branch}/{relpath}"
    data = _http(url, auth=False, binary_ok=True)
    if len(data) > MAX_TEXT_BYTES:
        raise RuntimeError("marker file too large")
    return data.decode("utf-8", "replace")


def _ver_tuple(v):
    """Parse '2.0.3-truck' -> (2, 0, 3); garbage -> (0, 0, 0)."""
    m = re.match(r"^(\d+)\.(\d+)\.(\d+)", str(v or ""))
    return tuple(int(x) for x in m.groups()) if m else (0, 0, 0)


def _repo_cfg(cfg):
    owner = _clamp(cfg.get("update_owner", DEFAULT_OWNER), 100)
    repo = _clamp(cfg.get("update_repo", DEFAULT_REPO), 100)
    branch = _clamp(cfg.get("update_branch", DEFAULT_BRANCH), 100)
    for val in (owner, repo):
        if not re.match(r"^[\w.-]+$", val):
            raise RuntimeError("invalid update_owner/update_repo in settings")
    if not re.match(r"^[\w./-]+$", branch):
        raise RuntimeError("invalid update_branch in settings")
    return owner, repo, branch


# ------------------------------------------------------- 1. OS update check
def check_os_updates(cfg):
    """Latest push on the default branch + release + urgency classification."""
    owner, repo, branch = _repo_cfg(cfg)
    out = {"ok": False, "owner": owner, "repo": repo, "branch": branch,
           "installed_version": version_string()}
    commits = _api_json(f"/repos/{owner}/{repo}/commits",
                        {"per_page": 1, "sha": branch})
    if not commits:
        out["error"] = "no commits found"
        return out
    c0 = commits[0] or {}
    commit = c0.get("commit") or {}
    msg = _clamp(commit.get("message", ""), 2000)
    sha = _clamp(c0.get("sha", ""), 64)
    files = []
    try:
        detail = _api_json(f"/repos/{owner}/{repo}/commits/{sha}")
        files = detail.get("files") or []
    except RuntimeError:
        pass  # file list is best-effort

    changed = [f.get("filename", "") for f in files][:300]
    kernel_touched = any(p.startswith(_KERNEL_PATHS) for p in changed)
    sec_touched = any(p.startswith(_SEC_PATHS) for p in changed)

    urgent, reason = False, ""
    first_line = (msg.splitlines() or [""])[0].lower()
    if "[urgent]" in first_line or "urgent" in first_line:
        urgent, reason = True, "security_update"
    elif "[cve-" in first_line or "cve-" in first_line:
        urgent, reason = True, "vulnerability_found"
    elif "[critical-fix]" in first_line:
        urgent, reason = True, "critical_bug_fix"
    if urgent and reason not in _URGENT_REASONS:
        urgent, reason = True, "security_update"

    release = None
    try:
        rels = _api_json(f"/repos/{owner}/{repo}/releases", {"per_page": 5})
        for r in rels or []:
            target = _clamp((r.get("target_commitish") or ""), 100)
            rsha = _clamp(((r.get("commit") or {}).get("sha") or ""), 64)
            if rsha == sha or target == sha or target == branch:
                release = {"tag": _clamp(r.get("tag_name", ""), 100),
                           "name": _clamp(r.get("name", ""), 200),
                           "url": _clamp(r.get("html_url", ""), 300),
                           "prerelease": bool(r.get("prerelease"))}
                break
    except RuntimeError:
        pass

    out.update({
        "ok": True,
        "commit_sha": sha,
        "short_sha": sha[:7],
        "title": _clamp(msg.splitlines()[0] if msg else "", 200),
        "message": msg,
        "author": _clamp((commit.get("author") or {}).get("name", ""), 100),
        "date": _clamp((commit.get("author") or {}).get("date", ""), 40),
        "changed_files": len(changed),
        "touches_kernel": kernel_touched,
        "touches_security": sec_touched,
        "urgent": urgent,
        "urgent_reason": reason,
        "release": release,
    })
    return out


# ---------------------------------------------------- 2. kernel update check
def check_kernel_update(cfg):
    """Compare GitHub kernel markers (VERSION.data / RELEASE.data) with local."""
    owner, repo, branch = _repo_cfg(cfg)
    installed = um.local_version_file() or "0.0.0"   # installed kernel build label
    out = {"ok": False, "owner": owner, "repo": repo, "branch": branch,
           "installed_kernel": installed,
           "installed_release_version": um.local_version_file() or "?"}
    try:
        # upd_file/ is the dedicated marker directory; kernel/ + security/
        # copies act as fallback so older repos still check out.
        try:
            ver_txt = _raw_file(owner, repo, branch, um.UPD_KERNEL_VERSION_REL)
            rel_txt = _raw_file(owner, repo, branch, um.UPD_KERNEL_RELEASE_REL)
        except RuntimeError:
            ver_txt = _raw_file(owner, repo, branch, um.VERSION_FILE_REL)
            rel_txt = _raw_file(owner, repo, branch, um.RELEASE_FILE_REL)
    except RuntimeError as e:
        out["error"] = f"kernel markers unavailable: {e}"
        return out

    remote_ver = ver_txt.strip().splitlines()[0].strip() if ver_txt.strip() else ""
    if not um.valid_version(remote_ver):
        out["error"] = "remote VERSION.data malformed"
        return out
    rec = um.parse_dotdata(rel_txt)
    rec_ver = rec.get("version", "")
    if rec_ver and not um.valid_version(rec_ver):
        out["error"] = "remote RELEASE.data version malformed"
        return out

    urgent = um.as_bool(rec.get("urgent", "false"))
    reason = _clamp(rec.get("urgent_reason", ""), 60)
    if urgent and reason not in _URGENT_REASONS:
        reason = "security_update"       # enforce the 3-reason rule
    if not urgent:
        reason = ""

    available = _ver_tuple(rec_ver or remote_ver) > _ver_tuple(
        out["installed_release_version"])
    out.update({
        "ok": True,
        "remote_version": _clamp(rec_ver or remote_ver, 60),
        "available": bool(available),
        "urgent": bool(urgent),
        "urgent_reason": reason,
        "has_security_fixes": um.as_bool(rec.get("security", "false")),
        "languages": _clamp(rec.get("languages", ""), 200),
        "description": _clamp(rec.get("description", ""), 1000),
        "date": _clamp(rec.get("date", ""), 40),
    })
    return out


# --------------------------------------------------- 3. security update check
def check_security_update(cfg):
    """Read servercloud/security/SECURITY.data from GitHub."""
    owner, repo, branch = _repo_cfg(cfg)
    local = um.local_security_data()
    out = {"ok": False, "owner": owner, "repo": repo, "branch": branch,
           "installed_db": local.get("version", "?")}
    try:
        try:
            txt = _raw_file(owner, repo, branch, um.UPD_SECURITY_DATA_REL)
        except RuntimeError:
            txt = _raw_file(owner, repo, branch, um.SECURITY_DATA_FILE_REL)
    except RuntimeError as e:
        out["error"] = f"security database unavailable: {e}"
        return out
    rec = um.parse_dotdata(txt)
    remote_ver = _clamp(rec.get("version", ""), 60)
    if not remote_ver:
        out["error"] = "remote SECURITY.data malformed"
        return out
    urgent = um.as_bool(rec.get("urgent", "false"))
    reason = _clamp(rec.get("urgent_reason", ""), 60)
    severity = _clamp(rec.get("severity", "unknown"), 30).lower()
    if severity not in ("low", "moderate", "high", "critical"):
        severity = "unknown"
    available = remote_ver != out["installed_db"]
    out.update({
        "ok": True,
        "remote_version": remote_ver,
        "available": bool(available),
        "urgent": bool(urgent),
        "urgent_reason": reason,
        "severity": severity,
        "languages": _clamp(rec.get("languages", ""), 200),
        "description": _clamp(rec.get("description", ""), 1000),
        "date": _clamp(rec.get("date", ""), 40),
    })
    return out


# ------------------------------------------------------------ applying updates
def _checkout_dir():
    # <root>/truck_modules/update_engine.py -> repo root
    return um.REPO_ROOT


def _origin_matches(owner, repo):
    try:
        url = subprocess.run(["git", "-C", _checkout_dir(), "remote", "get-url",
                              "origin"], capture_output=True, text=True,
                             timeout=10).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return False
    return url.lower().rstrip("/").removesuffix(".git").endswith(
        f"/{owner.lower()}/{repo.lower()}")


def apply_update(kind, cfg, yes=False):
    """kind: 'os' | 'kernel' | 'security'. Fast-forward pull restricted to paths.

    Refuses to touch anything if origin does not match the configured repo.
    """
    owner, repo, branch = _repo_cfg(cfg)
    if kind not in ("os", "kernel", "security"):
        return "unknown update kind"
    if not shutil.which("git"):
        return "git not available on this system"
    if not _origin_matches(owner, repo):
        return ("refused: local git origin does not match "
                f"{owner}/{repo} — manual review required")
    paths = {"os": ".",
             "kernel": um.KERNEL_DIR_REL,
             "security": um.SECURITY_DIR_REL}[kind]
    if not yes:
        return (f"dry-run: would fast-forward '{branch}' from "
                f"{owner}/{repo} ({paths}); rerun with confirmation")
    try:
        r = subprocess.run(
            ["git", "-C", _checkout_dir(), "pull", "--ff-only", "origin",
             branch, "--", paths],
            capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.SubprocessError) as e:
        return f"git failed: {e}"
    tail = redact((r.stdout or r.stderr or "").strip())[-800:]
    return ("updated" if r.returncode == 0 else f"git rc={r.returncode}") + \
        ("\n" + tail if tail else "")
