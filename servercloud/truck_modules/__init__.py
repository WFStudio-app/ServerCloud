"""TruckOS kernel modules & system daemons.

Loadable-service style components that plug into the kernel through the
syscall gate, mirroring how real OSes ship drivers/daemons outside the
core image:

  trk.py        -- 'trk' (Truck) package manager module:
                   install / remove / search / list / trust / update
  sysconfig.py  -- persistent system configuration daemon (JSON-backed),
                   mounted at /etc via the kernel VFS

Like the kernel, everything here is stdlib-only and cross-platform
(Linux / Termux / macOS / Windows).
"""

from servercloud.truck_modules import trk, sysconfig  # noqa: F401
