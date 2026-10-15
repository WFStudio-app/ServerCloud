"""TruckOS userland — the embedded half-OS layer of ServerCloud.

Layout was split into proper OS-style packages:

  servercloud/kernel/        -- kernel space (ring 0, simulated):
      truck_kernel.py           microkernel: page allocator, slab, VM/TLB,
                                CFS-like scheduler, timer IRQ, /proc & /sys,
                                panic handler
      truck_syscall.py          int 0x80 gate / numbered syscall table
      truck_boot.py             bootloader: POST, GRUB-style menu, runlevels
      truck_init.py             PID 1 service supervisor

  servercloud/truck_modules/ -- kernel modules & system daemons:
      trk.py                    'trk' (Truck) package manager
      sysconfig.py              persistent system configuration store

  servercloud/os/ (this pkg) -- userland shells & app-facing services:
      truck_shell.py            interactive sub-shells for the four menu
                                entries (Terminal / LLM Local / Script
                                Library / Settings)
      scriptlib.py              script library backing store

For backward compatibility this package re-exports the kernel and module
symbols, so old imports like ``from servercloud.os import truck_kernel``
keep working; new code should import from ``servercloud.kernel`` and
``servercloud.truck_modules`` directly.

Everything is stdlib-only and cross-platform (Linux / Termux / macOS /
Windows), so TruckOS runs as a self-contained userland on top of any host OS.
"""

from servercloud.kernel import (  # noqa: F401  (back-compat re-exports)
    truck_boot,
    truck_init,
    truck_kernel,
    truck_syscall,
)
from servercloud.truck_modules import sysconfig, trk  # noqa: F401
from servercloud.truck_modules.sysconfig import SysConfig  # noqa: F401
