"""TruckOS kernel space — the simulated ring-0 core of ServerCloud.

This package holds everything that conceptually lives *inside* the OS:

  truck_kernel.py  -- the microkernel itself (user-space emulation):
                      e820 memory map, page allocator, slab kmalloc,
                      VM/TLB, CFS-like scheduler, timer IRQ, /proc & /sys,
                      ring-0 panic handler
  truck_syscall.py -- int 0x80 gate: numbered syscall table userland uses
                      to reach the kernel (sys_read/mmap/fork/exit/panic...)
  truck_boot.py    -- bootloader: POST, GRUB-style menu, kernel load,
                      init handoff, runlevels
  truck_init.py    -- PID 1: service supervisor (start/stop/status/restart)

The public API is re-exported here so callers can simply do::

    from servercloud.kernel import truck_kernel, truck_syscall
"""

from servercloud.kernel import (  # noqa: F401
    truck_boot,
    truck_init,
    truck_kernel,
    truck_syscall,
)
