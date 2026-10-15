"""TruckOS kernel space — the simulated ring-0 core of ServerCloud.

Lives at the REPO ROOT (main plane) together with its update markers:

  truck_kernel.py   microkernel: e820 map, page allocator, slab kmalloc,
                    VM/TLB, CFS-like scheduler, timer IRQ, /proc & /sys,
                    ring-0 panic handler
  truck_syscall.py  int 0x80 gate: numbered syscall table userland uses
                    to reach the kernel (sys_read/mmap/fork/exit/panic...)
  truck_boot.py     bootloader: POST, GRUB-style menu, kernel load,
                    init handoff, runlevels
  truck_init.py     PID 1: service supervisor (start/stop/status/restart)

  VERSION.data      single-line kernel version label — bumped on every
                    kernel release; THE version marker checked by
                    Settings -> Auto-Update -> Kernel Update
  RELEASE.data      key = value record: urgent flag + reason (one of
                    security_update / vulnerability_found /
                    critical_bug_fix), security-fixes presence,
                    implementation languages, description, date

Public API::

    from truck import kernel
    kernel.truck_kernel.default_kernel()
"""

from . import truck_boot, truck_init, truck_kernel, truck_syscall  # noqa: F401
