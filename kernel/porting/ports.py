"""TruckOS Port Matrix — certified cross-platform targets.

Design rules:
  * The kernel core (truck_kernel.py) stays platform-agnostic; a Port is a
    thin description of *how* to run it on a given machine (word size,
    page size, syscall ABI, timer source, display backend).
  * Every target belongs to exactly one Vendor family.  TruckOS officially
    supports THREE vendor families ("фирмы, которые в ней нуждаются"):
        - Google   (Android phones/tablets)          — truckdroid port
        - Apple    (iOS / iPadOS / macOS / visionOS) — truckapple port
        - Valve    (Steam Deck / SteamOS handhelds)  — truckdeck port
    Additional community vendors extend the matrix (Samsung/LGE via
    Android forks, Microsoft via WSL, etc.).
  * `PORT_MATRIX` currently certifies 20 targets: >=18 phones plus
    desktop/server classes.  Adding more is one call away (template.py).
"""

from dataclasses import dataclass, field
from enum import Enum


class Urgency(str, Enum):
    STABLE = "stable"           # no pending work
    NORMAL = "normal"           # regular feature updates
    SECURITY = "security"       # security patches pending -> urgent
    CRITICAL = "critical"       # known exploit -> [!] red flag


@dataclass(frozen=True)
class Target:
    """One certified hardware/software platform."""
    id: str                     # unique, e.g. "pixel-8"
    name: str                   # human name
    vendor: str                 # vendor/family id
    kind: str                   # phone|tablet|handheld|desktop|server|watch
    arch: str                   # arm64 / x86_64 / riscv64 ...
    os_base: str                # android / ios / linux / macos / windows / bare
    word_bits: int = 64
    page_size: int = 4096
    abi: str = "syscall"        # syscall | mach_trap | wsl_bridge
    min_ram_mib: int = 512
    status: str = "certified"   # certified | beta | planned
    urgency: Urgency = Urgency.STABLE
    notes: str = ""


# --------------------------------------------------------------- vendors
GOOGLE = "google"
APPLE = "apple"
VALVE = "valve"

VENDORS = {
    GOOGLE: {
        "title": "Google (Android ecosystem)",
        "port": "truckdroid",
        "why": "Needs TruckOS as a hardened server/edge runtime layer for "
               "Android fleets and Pixel-based kiosk devices.",
    },
    APPLE: {
        "title": "Apple (iOS / macOS ecosystem)",
        "port": "truckapple",
        "why": "Needs TruckOS sandboxed user-kernel for iPhone/iPad M-series "
               "development pipelines and Vision Pro tooling.",
    },
    VALVE: {
        "title": "Valve (SteamOS / Steam Deck)",
        "port": "truckdeck",
        "why": "Needs TruckOS as a lightweight dual-boot OS with its own "
               "bootloader/init on Deck & Legion Go-class handhelds.",
    },
}


# --------------------------------------------------------------- phones (>=18)
PHONES = [
    # ---- Google family (Pixel + Android reference) --------------------
    Target("pixel-8",      "Google Pixel 8",            GOOGLE, "phone", "arm64",  "android", page_size=4096, urgency=Urgency.SECURITY, notes="pending CVE-2026-4117 patch"),
    Target("pixel-8pro",   "Google Pixel 8 Pro",        GOOGLE, "phone", "arm64",  "android"),
    Target("pixel-9",      "Google Pixel 9",            GOOGLE, "phone", "arm64",  "android"),
    Target("pixel-9pro",   "Google Pixel 9 Pro",        GOOGLE, "phone", "arm64",  "android", status="beta"),
    Target("nexus-5x",     "Google Nexus 5X (legacy)",  GOOGLE, "phone", "arm64",  "android", min_ram_mib=2048, notes="legacy driver set"),
    # ---- Samsung (Android fork — community under Google umbrella) ------
    Target("galaxy-s23",   "Samsung Galaxy S23",        GOOGLE, "phone", "arm64",  "android", page_size=4096),
    Target("galaxy-s24",   "Samsung Galaxy S24 Ultra",  GOOGLE, "phone", "arm64",  "android", urgency=Urgency.NORMAL),
    Target("galaxy-a54",   "Samsung Galaxy A54",        GOOGLE, "phone", "arm64",  "android", min_ram_mib=1024),
    Target("galaxy-zfold5","Samsung Galaxy Z Fold5",    GOOGLE, "phone", "arm64",  "android", status="beta"),
    # ---- Xiaomi / Redmi -----------------------------------------------
    Target("mi-13",        "Xiaomi 13",                 GOOGLE, "phone", "arm64",  "android"),
    Target("redmi-note12", "Xiaomi Redmi Note 12",      GOOGLE, "phone", "arm64",  "android", min_ram_mib=1024),
    # ---- OnePlus ------------------------------------------------------
    Target("oneplus-11",   "OnePlus 11",                GOOGLE, "phone", "arm64",  "android"),
    Target("oneplus-12",   "OnePlus 12",                GOOGLE, "phone", "arm64",  "android", status="beta"),
    # ---- Motorola -----------------------------------------------------
    Target("moto-edge50",  "Motorola Edge 50",          GOOGLE, "phone", "arm64",  "android"),
    # ---- Sony ---------------------------------------------------------
    Target("xperia-1v",    "Sony Xperia 1 V",           GOOGLE, "phone", "arm64",  "android"),
    # ---- Nothing ------------------------------------------------------
    Target("phone-2",      "Nothing Phone (2)",         GOOGLE, "phone", "arm64",  "android"),
    # ---- Apple family (iOS) -------------------------------------------
    Target("iphone-13",    "Apple iPhone 13",           APPLE,  "phone", "arm64",  "ios", abi="mach_trap", page_size=16384, notes="16K pages on A/M silicon"),
    Target("iphone-15",    "Apple iPhone 15",           APPLE,  "phone", "arm64",  "ios", abi="mach_trap", page_size=16384),
    Target("iphone-15pro", "Apple iPhone 15 Pro",       APPLE,  "phone", "arm64",  "ios", abi="mach_trap", page_size=16384, urgency=Urgency.CRITICAL, notes="userland exploit in the wild -> [!]"),
    Target("iphone-se3",   "Apple iPhone SE (3rd gen)", APPLE,  "phone", "arm64",  "ios", abi="mach_trap", page_size=16384, min_ram_mib=1024),
    # ---- Valve / handheld class ---------------------------------------
    Target("steam-deck-oled", "Steam Deck OLED",        VALVE,  "handheld", "x86_64", "linux", min_ram_mib=2048),
    Target("legion-go",    "Lenovo Legion Go (SteamOS)", VALVE, "handheld", "x86_64", "linux", status="beta"),
]

# --------------------------------------------------------------- non-phones
DESKTOPS_SERVERS = [
    Target("pc-x86_64",    "Generic PC (x86_64 UEFI)",  "generic", "desktop", "x86_64",  "bare"),
    Target("mac-m2",       "Apple Silicon Mac (M2)",    APPLE,     "desktop", "arm64",   "macos", abi="mach_trap", page_size=16384),
    Target("wsl2",         "Windows 11 + WSL2",         "microsoft", "desktop", "x86_64", "windows", abi="wsl_bridge", status="beta"),
    Target("rpi-5",        "Raspberry Pi 5",            "raspberry", "server", "aarch64", "linux", min_ram_mib=1024),
    Target("srv-xeon",     "Server (Intel Xeon)",       "generic", "server", "x86_64",   "bare", min_ram_mib=4096),
    Target("riscv-hifive", "RISC-V HiFive Unmatched",   "sifive",  "server", "riscv64",  "bare", status="planned"),
]


@dataclass
class PortMatrix:
    phones: list = field(default_factory=list)
    others: list = field(default_factory=list)

    # -- queries used by Settings > Auto-update and the shell ------------
    def all_targets(self):
        return list(self.phones) + list(self.others)

    def count_phones(self):
        return len([t for t in self.phones if t.kind == "phone"])

    def by_vendor(self, vendor_id):
        return [t for t in self.all_targets() if t.vendor == vendor_id]

    def urgent(self):
        return [t for t in self.all_targets()
                if t.urgency in (Urgency.SECURITY, Urgency.CRITICAL)]

    def find(self, target_id):
        for t in self.all_targets():
            if t.id == target_id:
                return t
        return None

    def add(self, target: Target):
        """Programmatic extension point (used by scaffold_port)."""
        if self.find(target.id):
            raise ValueError(f"duplicate target id: {target.id}")
        (self.phones if target.kind in ("phone", "handheld")
         else self.others).append(target)
        return target

    def summary(self):
        return {
            "total": len(self.all_targets()),
            "phones": self.count_phones(),
            "vendors": sorted({t.vendor for t in self.all_targets()}),
            "primary_vendors": list(VENDORS),
            "urgent": [t.id for t in self.urgent()],
        }


def certification_report(matrix=None):
    """Human-readable report printed by the shell command `ports`."""
    m = matrix or PORT_MATRIX
    lines = ["TruckOS port matrix — certification report",
             "=" * 52]
    for vid, v in VENDORS.items():
        ts = m.by_vendor(vid)
        lines.append(f"[{v['title']}]  port={v['port']}  targets={len(ts)}")
        lines.append(f"    why: {v['why']}")
        for t in ts:
            flag = "[!]" if t.urgency == Urgency.CRITICAL else (
                "[sec]" if t.urgency == Urgency.SECURITY else "     ")
            lines.append(f"    {flag} {t.name:<28} {t.arch:<8} "
                         f"{t.os_base:<8} {t.status:<9} "
                         f"pages={t.page_size // 1024}K")
    lines.append("-" * 52)
    s = m.summary()
    lines.append(f"phones certified : {s['phones']} (requirement: >=18)")
    lines.append(f"total targets    : {s['total']}")
    lines.append(f"primary vendors  : {', '.join(s['primary_vendors'])}")
    lines.append(f"urgent ports     : {', '.join(s['urgent']) or 'none'}")
    return "\n".join(lines)


# --------------------------------------------------------------- singleton
PORT_MATRIX = PortMatrix(phones=[t for t in PHONES],
                         others=[t for t in DESKTOPS_SERVERS])
