"""TruckOS — the embedded half-OS layer of ServerCloud.

Components:
  truck_kernel.py -- simulated ring-0 kernel (user-space emulation): page
                     allocator, slab, VM/TLB, CFS-like scheduler, timer IRQ,
                     /proc & /sys, panic handler
  truck_syscall.py -- int 0x80 gate: numbered syscall table userland uses to
                      reach the kernel (sys_read/mmap/fork/exit/panic ...)
  truck_init.py  -- PID 1: service supervisor (start/stop/status/restart)
  truck_boot.py  -- bootloader: POST, kernel load, init handoff, runlevels
  trk.py         -- 'trk' package manager (install/remove/search/list/trust ...)
  sysconfig.py   -- persistent system configuration store (JSON-backed)

Everything here is stdlib-only and cross-platform (Linux / Termux / macOS /
Windows), so TruckOS runs as a self-contained userland on top of any host OS.
"""
