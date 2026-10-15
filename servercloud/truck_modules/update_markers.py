#!/usr/bin/env python3
"""TruckOS update markers — canonical version metadata for the GitHub repo.

These files are what the Settings -> Auto-Update section checks on GitHub:

  * servercloud/kernel/VERSION.data   kernel version label (single line)
  * servercloud/kernel/RELEASE.data   kernel release record (key = value)
  * servercloud/security/SECURITY.data security database release record

Marker semantics
----------------
RELEASE.data / SECURITY.data keys:
  version        - the version this record describes
  urgent         - true|false  (kernel rule below; security: maintainer's call)
  urgent_reason  - one of: security_update | vulnerability_found |
                   critical_bug_fix (empty if not urgent)
  security       - true|false  (this build contains security fixes)
  languages      - comma-separated implementation languages of the component
  description    - human-readable changelog / summary for this version
  date           - ISO date of the record

Urgent-kernel rule (exactly three allowed reasons):
  1. security_update      - a scheduled/urgent security refresh
  2. vulnerability_found  - an actual vulnerability was found and fixed here
  3. critical_bug_fix     - a critical bug fix that breaks operation otherwise
An urgent record is flagged in the UI with a red [!] marker.
"""

import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(os.path.dirname(HERE))

KERNEL_DIR_REL = "servercloud/kernel"
SECURITY_DIR_REL = "servercloud/security"
VERSION_FILE_REL = f"{KERNEL_DIR_REL}/VERSION.data"
RELEASE_FILE_REL = f"{KERNEL_DIR_REL}/RELEASE.data"
SECURITY_DATA_FILE_REL = f"{SECURITY_DIR_REL}/SECURITY.data"

URGENT_REASONS = ("security_update", "vulnerability_found", "critical_bug_fix")

_VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:-[A-Za-z0-9._]+)?$")


def parse_dotdata(text):
    """Parse a `key = value` .data file into a dict (comments start with #)."""
    out = {}
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip().lower()] = v.strip()
    return out


def as_bool(value):
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def valid_version(s):
    return bool(_VERSION_RE.match((s or "").strip()))


# ---------------------------------------------------------------- local read
def _read(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""


def local_version_file(root=REPO_ROOT):
    return _read(os.path.join(root, VERSION_FILE_REL)).strip()


def local_kernel_release(root=REPO_ROOT):
    return parse_dotdata(_read(os.path.join(root, RELEASE_FILE_REL)))


def local_security_data(root=REPO_ROOT):
    return parse_dotdata(_read(os.path.join(root, SECURITY_DATA_FILE_REL)))


if __name__ == "__main__":
    print("kernel version :", local_version_file())
    print("kernel release :", local_kernel_release())
    print("security data  :", local_security_data())
