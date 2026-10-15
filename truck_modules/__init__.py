"""TruckOS kernel modules & system daemons (repo-root main plane).

Loadable-service style components that plug into the kernel through the
syscall gate, mirroring how real OSes ship drivers/daemons outside the
core image:

  trk.py            'trk' (Truck) package manager: install/remove/search/
                    list/trust/update
  sysconfig.py      persistent system configuration daemon (JSON-backed),
                    mounted at /etc via the kernel VFS
  update_markers.py canonical reader/parser of the .data version markers
                    (kernel/, security/, upd_file/)
  update_engine.py  TruckOS Auto-Update service behind
                    Settings -> Auto-Update (OS / Kernel / Security checks)

Like the kernel, everything here is stdlib-only and cross-platform
(Linux / Termux / macOS / Windows).
"""

from . import sysconfig, trk, update_engine, update_markers  # noqa: F401
from .sysconfig import SysConfig  # noqa: F401
