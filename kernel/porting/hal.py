"""HAL — hardware abstraction layer for TruckOS ports.

The kernel core asks the HAL for everything platform-specific:
page size, syscall mechanism, timer source, display backend, network.
`HostHAL` auto-detects the machine ServerCloud is actually running on and
maps it onto a Port Matrix target, so `truck boot` works identically on
Android, iOS (simulated), SteamOS, Windows/WSL, macOS and bare Linux PCs.
"""

import os
import platform

from .ports import PORT_MATRIX, Target


class HAL:
    """Abstract interface every port must implement."""

    name = "abstract"

    # -- memory model --------------------------------------------------
    def page_size(self):
        raise NotImplementedError

    def word_bits(self):
        raise NotImplementedError

    # -- control plane ---------------------------------------------------
    def invoke_syscall(self, number, *args):
        """How userland reaches ring 0 on this platform."""
        raise NotImplementedError

    def now_monotonic(self):
        raise NotImplementedError

    # -- output ----------------------------------------------------------
    def display_backend(self):
        return "ansi-tty"

    def network_backend(self):
        return "posix-sockets"

    # -- lifecycle ---------------------------------------------------------
    def describe(self):
        return {"hal": self.name,
                "page_size": self.page_size(),
                "word_bits": self.word_bits(),
                "display": self.display_backend(),
                "network": self.network_backend()}


class HostHAL(HAL):
    """HAL bound to whatever machine we are really sitting on."""

    def __init__(self, target: Target):
        self.target = target
        self.name = f"host[{target.id}]"
        self._abi_handlers = {
            "syscall": self._abi_syscall,
            "mach_trap": self._abi_mach_trap,
            "wsl_bridge": self._abi_wsl_bridge,
        }

    # -- memory model: taken straight from the certified target ----------
    def page_size(self):
        return self.target.page_size

    def word_bits(self):
        return self.target.word_bits

    # -- ABIs -------------------------------------------------------------
    def _abi_syscall(self, number, args):
        # Linux/Android style: svc / int 0x80 emulation
        return ("svc", number, args)

    def _abi_mach_trap(self, number, args):
        # Apple style: mach trap via software breakpoint
        return ("mach_trap", number, args)

    def _abi_wsl_bridge(self, number, args):
        # WSL2: hypercall bridge into the Linux guest kernel
        return ("wsl_bridge", number, args)

    def invoke_syscall(self, number, *args):
        handler = self._abi_handlers.get(self.target.abi)
        if handler is None:
            raise ValueError(f"unknown ABI: {self.target.abi}")
        return handler(number, args)

    def now_monotonic(self):
        return platform.time_seconds() if hasattr(platform, "time_seconds") \
            else os.times()[4]

    def display_backend(self):
        if self.target.os_base == "ios":
            return "coregraphics-sim"
        if self.target.kind in ("phone", "handheld"):
            return "fb-console"
        return "ansi-tty"

    def network_backend(self):
        if self.target.vendor == "apple":
            return "nwconnection-sim"
        return "posix-sockets"


# --------------------------------------------------------------- detection
def detect_host():
    """Map the current machine onto the closest certified target."""
    sysname = platform.system().lower()          # linux/darwin/windows
    machine = platform.machine().lower()         # x86_64/arm64/aarch64...

    # Android: TERM_PROGRAM or $ANDROID_DATA present
    if os.environ.get("ANDROID_DATA") or "android" in sysname:
        tid = "pixel-8"
    elif sysname == "darwin":
        tid = "mac-m2" if machine.startswith("arm") else "pc-x86_64"
    elif sysname == "windows":
        tid = "wsl2" if os.environ.get("WSL_DISTRO_NAME") else "pc-x86_64"
    elif machine in ("aarch64", "arm64") and os.path.exists("/dev/mmcblk0"):
        tid = "rpi-5"
    elif machine in ("aarch64", "arm64"):
        tid = "rpi-5"
    elif machine.startswith("riscv"):
        tid = "riscv-hifive"
    else:
        tid = "pc-x86_64"

    target = PORT_MATRIX.find(tid) or PORT_MATRIX.find("pc-x86_64")
    return HostHAL(target)
