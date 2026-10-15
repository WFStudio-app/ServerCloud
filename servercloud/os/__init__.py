"""TruckOS — the embedded half-OS layer of ServerCloud.

Components:
  truck_init.py  -- PID 1: service supervisor (start/stop/status/restart)
  truck_boot.py  -- bootloader: boot sequence, runlevel selection, recovery
  trk.py         -- 'trk' package manager (install/remove/search/list/trust ...)
  sysconfig.py   -- persistent system configuration store (JSON-backed)

Everything here is stdlib-only and cross-platform (Linux / Termux / macOS /
Windows), so TruckOS runs as a self-contained userland on top of any host OS.
"""
