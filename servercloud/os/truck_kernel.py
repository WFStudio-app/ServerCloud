"""truck_kernel — simulated TruckOS microkernel (ring 0), user-space emulation.

This module emulates the *inside* of an operating-system kernel entirely on
the user side: no privileges, no real hardware access — everything is a
faithful simulation that runs inside the ServerCloud process.

Emulated subsystems (one class each, wired together by `Kernel`):

  * E820-style physical memory map + direct (buddy-like) page allocator
  * slab-style kmalloc/kfree heap with per-size caches
  * virtual-memory manager: VMA map, mmap/brk, demand paging, LRU TLB
  * scheduler: O(1)-style run queues, timeslices, preemption, nice values
  * timer interrupt driven by CLOCK_MONOTONIC (HZ configurable)
  * /proc & /sys pseudo filesystems built in kernel memory
  * syscall dispatch table (int 0x80 ABI style) shared with truck_syscall
  * ring-0 panic handler with oops dump + stack trace

The kernel is booted by truck_boot (after POST, before init/PID 1 hands
control to the userland menu).  User programs reach it through the syscall
gate in truck_syscall, exactly like applications reach a real kernel via
software interrupts.
"""

import random
import time

# ------------------------------------------------------------------ constants
KERNEL_NAME = "TruckOS Kernel"
KERNEL_VERSION = "1.0.0-truck"
KERNEL_BUILDOPT = "SMP PREEMPT LOCALSIM"

PAGE_SHIFT = 12                 # 4 KiB pages, like x86_64 Linux
PAGE_SIZE = 1 << PAGE_SHIFT     # 4096
HZ_DEFAULT = 100                # timer ticks per second
TIMER_SLICE_SEC = 1.0 / HZ_DEFAULT

USER_VA_START = 0x0000_5555_5555_0000   # where userland mappings start
KERNEL_VA_BASE = 0xFFFF_8880_0000_0000   # direct-mapped kernel region
KMALLOC_MAX = 8 * PAGE_SIZE              # above this -> page allocator


def _hex(n, width=16):
    return f"0x{n:0{width}x}"


class KernelPanic(RuntimeError):
    """Ring-0 fatal error.  The syscall gate catches this and shows the oops."""


# --------------------------------------------------------------- e820 / memmap
class PhysMem:
    """E820-style physical memory map over a bytearray 'RAM'."""

    def __init__(self, ram_bytes=64 * 1024 * 1024):     # 64 MiB simulated RAM
        self.ram = bytearray(ram_bytes)
        self.size = ram_bytes
        self.e820 = [
            {"base": 0x0, "len": 0x9_FC00, "type": "usable"},
            {"base": 0x9_FC00, "len": 0x400, "type": "reserved"},          # BIOS area
            {"base": 0xF000_0000 - ram_bytes, "len": ram_bytes // 2,
             "type": "ACPI data"},
            {"base": 0, "len": 0, "type": None},                            # placeholder
        ][:-1]
        self.e820.append({"base": 0x10_0000,
                          "len": ram_bytes - 0x10_0000, "type": "usable"})
        # free-page bitmap for the usable region above 1 MiB
        self.bitmap_start = 0x10_0000
        npages = (ram_bytes - self.bitmap_start) // PAGE_SIZE
        self.bitmap = bytearray((npages + 7) // 8)
        self.npages_total = npages
        self.npages_free = npages

    def e820_dump(self):
        lines = []
        for e in self.e820:
            lines.append(f"  [mem {_hex(e['base'])}-{_hex(e['base'] + e['len'] - 1)}"
                         f"] {e['type']}")
        return "\n".join(lines)

    # ---- buddy-ish page allocator -------------------------------------
    def alloc_pages(self, order=0, zero=True):
        """Return list of 2**order contiguous page frames (PFNs)."""
        want = 1 << order
        bm, total = self.bitmap, self.npages_total
        run = 0
        for pfn in range(total):
            byte, bit = divmod(pfn, 8)
            if bm[byte] & (1 << bit):
                run = 0
                continue
            run += 1
            if run == want:
                start = pfn - want + 1
                for p in range(start, start + want):
                    b, i = divmod(p, 8)
                    bm[b] |= 1 << i
                self.npages_free -= want
                base = self.bitmap_start + start * PAGE_SIZE
                if zero:
                    for off in range(0, want * PAGE_SIZE, PAGE_SIZE):
                        self.ram[base + off: base + off + PAGE_SIZE] = \
                            bytes(PAGE_SIZE)
                return list(range(start, start + want))
        raise KernelPanic(f"out of memory: order={order} requested, "
                          f"{self.npages_free} pages free")

    def free_pages(self, pfns):
        for pfn in pfns:
            b, i = divmod(pfn, 8)
            if not self.bitmap[b] & (1 << i):
                raise KernelPanic(f"double-free detected on pfn {pfn}")
            self.bitmap[b] &= ~(1 << i)
        self.npages_free += len(pfns)

    def read(self, addr, n):
        return bytes(self.ram[addr: addr + n])

    def write(self, addr, data):
        self.ram[addr: addr + len(data)] = data


# ------------------------------------------------------------------- slab
class KmallocSlab:
    """kmalloc-style heap carved out of kernel pages, with size-class caches."""

    CLASSES = [32, 64, 128, 256, 512, 1024, 2048, 4096]

    def __init__(self, phys):
        self.phys = phys
        self.objects = {}          # addr -> (size_class or ('page', n))
        self.next_addr = KERNEL_VA_BASE + 0x1000
        self.stats = {"alloc": 0, "free": 0, "live": 0, "bytes_live": 0}

    def _class_for(self, size):
        for c in self.CLASSES:
            if size <= c:
                return c
        return None

    def kmalloc(self, size):
        if size <= 0:
            raise KernelPanic(f"kmalloc({size}): invalid size")
        self.stats["alloc"] += 1
        cl = self._class_for(size)
        if cl is None:                                    # big allocation
            order = 0
            while (1 << order) * PAGE_SIZE < size:
                order += 1
            pfns = self.phys.alloc_pages(order)
            addr = self.next_addr
            self.next_addr += (1 << order) * PAGE_SIZE
            self.objects[addr] = ("page", order, pfns)
        else:
            addr = self.next_addr
            self.next_addr += cl
            self.objects[addr] = ("slab", cl)
        self.stats["live"] += 1
        self.stats["bytes_live"] += size
        return addr

    def kfree(self, addr):
        obj = self.objects.pop(addr, None)
        if obj is None:
            raise KernelPanic(f"kfree({_hex(addr)}): invalid pointer / "
                              f"already freed")
        if obj[0] == "page":
            self.phys.free_pages(obj[2])
        self.stats["free"] += 1
        self.stats["live"] -= 1

    def live_bytes(self):
        return sum(o[1] if o[0] == "slab" else (1 << o[1]) * PAGE_SIZE
                   for o in self.objects.values())


# -------------------------------------------------------------------- mm
class VMManager:
    """Virtual memory: VMA map, mmap/brk, demand paging, small LRU TLB."""

    PROT_READ, PROT_WRITE, PROT_EXEC = 1, 2, 4
    FLAGS_SHARED = 1

    def __init__(self, phys):
        self.phys = phys
        self.vmas = {}            # task_pid -> list of VMA dicts
        self.tlb = []             # list of vpn -> pfu entries, MRU last
        self.tlb_size = 64
        self.tlb_hits = 0
        self.tlb_misses = 0
        self.page_faults = 0

    def add_task(self, pid):
        self.vmas[pid] = []

    def del_task(self, pid):
        for vma in self.vmas.pop(pid, []):
            self.phys.free_pages(vma["pages"])

    def do_mmap(self, pid, length, prot=3, flags=0, addr=None):
        npages = (length + PAGE_SIZE - 1) // PAGE_SIZE
        start = USER_VA_START + pid * 0x1000_0000
        for vma in self.vmas[pid]:                     # bump allocator
            start = max(start, vma["end"])
        start = (start + PAGE_SIZE - 1) & ~(PAGE_SIZE - 1)
        pages = [self.phys.alloc_pages(0)[0] for _ in range(npages)]
        self.vmas[pid].append({"start": start, "end": start + npages * PAGE_SIZE,
                               "prot": prot, "flags": flags,
                               "pages": pages})
        return start

    def brk_grow(self, pid, extra):
        # emulate heap growth: one anonymous VMA per task
        for vma in self.vmas[pid]:
            if vma.get("heap"):
                need = (extra + PAGE_SIZE - 1) // PAGE_SIZE
                have = len(vma["pages"])
                for _ in range(need - have):
                    vma["pages"].append(self.phys.alloc_pages(0)[0])
                vma["end"] = vma["start"] + len(vma["pages"]) * PAGE_SIZE
                return vma["end"]
        start = self.do_mmap(pid, extra)
        self.vmas[pid][-1]["heap"] = True
        return start

    def translate(self, pid, va):
        """Page-walk + TLB emulation; returns the backing frame index."""
        vpn = va >> PAGE_SHIFT
        key = (pid, vpn)
        for i, ent in enumerate(self.tlb):
            if ent[0] == key:
                self.tlb_hits += 1
                self.tlb.append(self.tlb.pop(i))       # touch -> MRU
                return ent[1]
        self.tlb_misses += 1
        self.page_faults += 1
        for vma in self.vmas.get(pid, []):
            if vma["start"] <= va < vma["end"]:
                idx = (va - vma["start"]) // PAGE_SIZE
                frame = vma["pages"][idx]
                self.tlb.append((key, frame))
                if len(self.tlb) > self.tlb_size:
                    self.tlb.pop(0)
                return frame
        raise KernelPanic(f"page fault at {_hex(va)} (pid {pid}): "
                          f"no such mapping — SIGSEGV")

    def vmstat(self):
        return {"tlb_entries": len(self.tlb), "tlb_hits": self.tlb_hits,
                "tlb_misses": self.tlb_misses,
                "hit_rate_pct": round(100.0 * self.tlb_hits /
                                      max(1, self.tlb_hits + self.tlb_misses), 1),
                "minor_faults": self.page_faults,
                "mappings": sum(len(v) for v in self.vmas.values())}


# ------------------------------------------------------------------ sched
class Task:
    TASK_RUNNING = "R"
    TASK_SLEEPING = "S"
    TASK_ZOMBIE = "Z"

    def __init__(self, pid, ppid, name, prio=120):
        self.pid = pid
        self.ppid = ppid
        self.name = name
        self.prio = prio                      # 100..139, 120 = nice 0
        self.state = self.TASK_RUNNING
        self.vruntime = 0.0                   # ns of virtual runtime (CFS-ish)
        self.timeslice_used = 0.0
        self.utime = 0.0
        self.stime = 0.0
        self.started = time.monotonic()
        self.exit_code = None

    @property
    def nice(self):
        return self.prio - 120

    def stat_line(self):
        return (f"{self.pid:>3} {self.name:<20} {self.state} "
                f"ppid={self.ppid:<3} nice={self.nice:+d} "
                f"vruntime={self.vruntime:8.1f}ms "
                f"cpu={self.utime:6.2f}s")


class Scheduler:
    """CFS-flavoured simulation: pick the task with the smallest vruntime."""

    QUANTUM_MS = 10.0

    def __init__(self):
        self.tasks = {}                       # pid -> Task
        self.next_pid = 1
        self.current = None
        self.context_switches = 0
        self.ticks = 0

    def fork_exec(self, ppid, name, prio=120):
        pid = self.next_pid
        self.next_pid += 1
        t = Task(pid, ppid, name, prio)
        self.tasks[pid] = t
        return t

    def runnable(self):
        return [t for t in self.tasks.values()
                if t.state == Task.TASK_RUNNING]

    def schedule(self):
        """Pick next task; charge current task its consumed slice."""
        ready = self.runnable()
        if not ready:
            self.current = None
            return None
        weight = lambda t: 1024 / max(1, 1.15 ** t.nice)   # nicer -> heavier
        best = min(ready, key=lambda t: t.vruntime * weight(t))
        if self.current is not best:
            self.context_switches += 1
            prev, self.current = self.current, best
            if prev is not None:
                prev.state = Task.TASK_RUNNING
        return self.current

    def tick(self, elapsed_sec):
        """Timer-interrupt handler: account CPU, maybe preempt."""
        self.ticks += 1
        cur = self.schedule()
        if cur is None:
            return
        cur.utime += elapsed_sec
        cur.vruntime += elapsed_sec * 1000.0
        cur.timeslice_used += elapsed_sec * 1000.0
        if cur.timeslice_used >= self.QUANTUM_MS:
            cur.timeslice_used = 0.0          # voluntary preemption point

    def exit(self, pid, code=0):
        t = self.tasks.get(pid)
        if t is None:
            raise KernelPanic(f"exit({pid}): no such task")
        t.state = Task.TASK_ZOMBIE
        t.exit_code = code
        return t

    def kill(self, pid):
        t = self.tasks.get(pid)
        if t is None:
            return False
        if t.pid == 1:
            raise KernelPanic("attempted to kill init!")
        del self.tasks[pid]
        return True


# ------------------------------------------------------------------ procfs
class ProcFs:
    """/proc and /sys pseudo-files, generated on demand from kernel state."""

    def __init__(self, kern):
        self.k = kern

    def listdir(self, path):
        if path in ("/proc", "/proc/"):
            names = ["cmdline", "cpuinfo", "meminfo", "uptime", "version",
                     "partitions", "devices", "interrupts", "vmstat",
                     "filesystems"]
            names += [str(t.pid) for t in self.k.sched.tasks.values()]
            return sorted(names)
        if path.startswith("/proc/") and path.strip("/").isdigit():
            return ["status", "cmdline", "stat"]
        if path in ("/sys", "/sys/"):
            return ["kernel", "bus", "class", "module"]
        if path == "/sys/kernel":
            return ["hostname", "ostype", "osrelease", "profiling"]
        return []

    def read(self, path):
        k = self.k
        p = path.rstrip("/") or "/proc"
        if p == "/proc/version":
            return (f"{KERNEL_NAME} version {KERNEL_VERSION} "
                    f"(servercloud@truck-os) #1 SMP {KERNEL_BUILDOPT}\n")
        if p == "/proc/uptime":
            up = time.monotonic() - k.boot_time
            idle = sum(t.utime for t in k.sched.tasks.values())
            return f"{up:.2f} {max(0.0, up * 8 - idle):.2f}\n"
        if p == "/proc/meminfo":
            ph = k.phys
            total_kb = ph.size // 1024
            free_kb = ph.npages_free * PAGE_SIZE // 1024
            slab_kb = k.slab.live_bytes() // 1024
            return (f"MemTotal:       {total_kb:>8} kB\n"
                    f"MemFree:        {free_kb:>8} kB\n"
                    f"Slab:           {slab_kb:>8} kB\n"
                    f"KernelStack:    {len(k.sched.tasks) * 16:>8} kB\n"
                    f"PageTables:     {k.mm.vmstat()['mappings'] * 4:>8} kB\n")
        if p == "/proc/cpuinfo":
            out = []
            for i in range(k.ncpu):
                out.append(f"processor\t: {i}\n"
                           f"model name\t: Truck Virtual CPU v1\n"
                           f"vendor id\t: SERVERCLOUD\n"
                           f"bogomips\t: {random.randint(4000, 6000)}\n")
            return "\n".join(out)
        if p == "/proc/cmdline":
            return k.cmdline + "\n"
        if p == "/proc/filesystems":
            return ("nodev\tsysfs\nnodev\tproc\nnodev\ttrucktmpfs\n"
                    "\text4\txfs\tbtrfs\n")
        if p == "/proc/vmstat":
            v = k.mm.vmstat()
            return "".join(f"{a} {b}\n" for a, b in v.items())
        if p == "/proc/interrupts":
            return (f"           CPU0       CPU1\n"
                    f"  0: {k.timer_irq:>10}  timer (PIC)\n"
                    f"  1: {k.syscall_count:>10}  syscall (int 0x80)\n")
        if p == "/sys/kernel/hostname":
            return k.hostname + "\n"
        if p == "/sys/kernel/ostype":
            return "TruckOS\n"
        if p == "/sys/kernel/osrelease":
            return KERNEL_VERSION + "\n"
        if p.startswith("/proc/") and p.strip("/").split("/")[0].isdigit():
            pid = int(p.strip("/").split("/")[0])
            t = k.sched.tasks.get(pid)
            if t is None:
                raise FileNotFoundError(path)
            if p.endswith("/status"):
                return (f"Name:\t{t.name}\nState:\t{t.state}\n"
                        f"Tgid:\t{t.pid}\nPPid:\t{t.ppid}\n"
                        f"nice:\t{t.nice}\n")
            if p.endswith("/stat"):
                return (f"{t.pid} ({t.name}) {t.state} {t.ppid} 0 0 0 0 0 "
                        f"0 0 0 {t.utime:.0f} 0 0 0 0 0 0 0 0 0 0 0 "
                        f"{int(t.vruntime)}\n")
            return t.name + "\0"
        raise FileNotFoundError(path)


# ------------------------------------------------------------------ kernel
class Kernel:
    """Singleton-ish kernel object; owns every simulated subsystem."""

    def __init__(self, hostname="truck-os", ncpu=2, ram_bytes=64 << 20):
        self.state = "halted"               # halted|booting|running|panicked
        self.hostname = hostname
        self.ncpu = ncpu
        self.hz = HZ_DEFAULT
        self.boot_time = time.monotonic()
        self.boot_log = []
        self.syscall_count = 0
        self.timer_irq = 0
        self.cmdline = ("BOOT_TRUCK root=/dev/truck0 ro quiet "
                        "loglevel=4 nomodeset")

        self.phys = PhysMem(ram_bytes)
        self.slab = KmallocSlab(self.phys)
        self.mm = VMManager(self.phys)
        self.sched = Scheduler()
        self.proc = ProcFs(self)

        self._last_tick = time.monotonic()
        self._panic_trace = []

    # ------------------------------------------------------------- logging
    def _klog(self, level, msg):
        line = f"[{time.monotonic() - self.boot_time:9.6f}] {level}: {msg}"
        self.boot_log.append(line)
        print("  " + line)

    # ------------------------------------------------------------- boot path
    def start(self, interactive=False):
        """Called right after the bootloader picks an entry."""
        self.state = "booting"
        self._klog("INFO", f"Linux-compat {KERNEL_NAME} v{KERNEL_VERSION} "
                          f"booting (build: {KERNEL_BUILDOPT})")
        self._klog("INFO", f"command line: {self.cmdline}")
        self._klog("INFO", "CPU features: FPU VME DE TSC SIMULATE RING0-EMU")
        print("  [    0.000000] BIOS-e820-map:")
        print(self.phys.e820_dump())
        self._klog("INFO", f"memory: {self.phys.size >> 20} MiB virtual RAM, "
                           f"{PAGE_SIZE >> 10} KiB pages, "
                           f"{self.phys.npages_total} frames")
        # carve out kernel image + slab arenas
        self.kernel_text = self.slab.kmalloc(256 * 1024)
        self._klog("INFO", f"kernel text mapped at {_hex(self.kernel_text)}")
        self._klog("INFO", f"scheduler: CFS-emulation quantum="
                           f"{Scheduler.QUANTUM_MS}ms hz={self.hz}")
        self._klog("INFO", f"timer: PIT emulated via monotonic clock "
                           f"({self.hz} Hz)")
        self._klog("INFO", "syscall: int 0x80 gate registered "
                           "(see truck_syscall.table)")
        self._klog("INFO", "procfs: /proc /sys mounted (in-memory)")
        # PID 1 exists already — the bootloader hands control to init later
        init = self.sched.fork_exec(0, "swapper/0", prio=100)
        init.state = Task.TASK_SLEEPING
        self.idle_task = init
        self.state = "running"
        self._klog("INFO", "Run/init: starting userspace handoff")
        return True

    stop = shutdown = None  # set below

    # ------------------------------------------------------------- ticking
    def tick(self):
        """Advance simulated time based on wall-clock since last tick."""
        now = time.monotonic()
        dt = now - self._last_tick
        self._last_tick = now
        n_ticks = int(dt * self.hz)
        for _ in range(min(n_ticks, 5000)):        # cap catch-up work
            self.timer_irq += 1
            self.sched.tick(TIMER_SLICE_SEC)
        return n_ticks

    def uptime(self):
        return time.monotonic() - self.boot_time

    # ------------------------------------------------------------- panics
    def panic(self, message, cause=None):
        self.state = "panicked"
        frames = [f"  <TASK>",
                  f"  ? truck_panic+0x42/0x100",
                  f"  ? syscall_dispatch+0x{self.syscall_count % 256:02x}/0x1f0",
                  f"  ? truck_init_main+0x1b/0x40 [init]",
                  f"  </TASK>"]
        self._panic_trace = frames
        banner = ("  ------------------------------------------------------------"
                  "\n  Kernel panic - not syncing: " + message)
        if cause:
            banner += f"\n  caused by: {cause!r}"
        banner += ("\n  RIP: 0010:truck_panic+0x42   "
                   f"RSP: 0000:{_hex(KERNEL_VA_BASE + 0x2000)}\n"
                   "  Call Trace:\n" + "\n".join(frames) +
                   "\n  Modules linked in: truck_sched truck_mm truck_slab "
                   "truck_timer (+core)\n"
                   "  ---[ end Kernel panic ]---\n  "
                   "------------------------------------------------------------")
        print(banner)
        raise KernelPanic(message)

    halt = None  # set below


def _halt(self, reason="Power down"):
    self.state = "halted"
    self._klog("INFO", f"shutdown: {reason}")
    self._klog("INFO", "syncing filesystems... done")
    self._klog("INFO", "sending SIGTERM to all processes... done")
    self._klog("INFO", "remounting root read-only... done")
    self._klog("INFO", "Killing all remaining processes... done")
    print(f"\n  {reason}. It is now safe to turn off your computer.")


def _stop(self):
    self.halt("System halted")


Kernel.halt = _halt
Kernel.stop = _stop


# ------------------------------------------------------------- syscalls
SYSCALL_NAMES = [
    "read", "write", "open", "close", "stat", "fstat", "lseek", "mmap",
    "brk", "munmap", "ioctl", "fork", "execve", "exit", "wait4", "kill",
    "getpid", "socket", "connect", "mount", "umount", "chdir", "time",
    "sched_yield", "clock_gettime", "panic",
]


def default_kernel():
    """Process-wide singleton used by the app / shells / syscall gate."""
    global _DEFAULT
    if _DEFAULT is None or _DEFAULT.state == "panicked":
        _DEFAULT = Kernel()
    return _DEFAULT


_DEFAULT = None
