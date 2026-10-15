"""truck_syscall — the int 0x80 gate between userland and the simulated kernel.

User programs (the Truck shells, trk, init) never touch kernel objects
directly; they raise a software interrupt by calling `sysint(nr, *args)`,
which dispatches through a numbered syscall table exactly like a real
kernel's entry_64.S / sys_call_table.

Calling conventions emulated:
  * syscall number in 'rax' (first positional argument)
  * arguments rdi, rsi, rdx ...   (positional args 1..n)
  * return value in 'rax'; negative values are -errno (Linux style)
  * ring-0 faults surface as KernelPanic, caught here and rendered as an oops

Useful numbers (see SYSCALL_TABLE):
  0  sys_read(path)            read a /proc or /sys pseudo-file
  1  sys_write(fd, data)       write to kernel log / console fd
  7  sys_mmap(pid, len)        map pages into a task's address space
  9  sys_brk(pid, size)        grow the heap VMA
 11  sys_ioctl(cmd)            introspect kernel internals
 12  sys_fork_exec(ppid,name)  spawn a simulated task
 13  sys_execve                register the running program name
 14  sys_exit(pid, code)       reap a task
 16  sys_kill(pid)             deliver SIGKILL (ignored for PID 1, like init)
 25  (privileged) panic is NOT user-callable any more: the gate returns
     -ENOSYS, so userland can never crash ring 0 on purpose.  Kernel-internal
     demos/tests call truck_kernel.Kernel.panic() directly.
"""

import time

from . import truck_kernel as K


class SyscallError(Exception):
    """Returned to userland as -errno instead of raising into the UI."""


CURRENT_KERNEL = None      # last kernel seen by sysint (used by signals)


# ------------------------------------------------------------- handlers
def _require_running(k):
    if k.state != "running":
        raise SyscallError(-1, f"kernel state is '{k.state}'")


def sys_read(k, path):
    try:
        return k.proc.read(path)
    except FileNotFoundError:
        return None


def sys_write(k, fd, data):
    _require_running(k)
    data = str(data)
    if len(data) > 64 * 1024:                 # bound kernel-log writes (DoS)
        raise K.KernelFault(-27, "write too large")
    if fd == 2:
        k._klog("USER", data)
    else:
        k._klog("CONSOLE", data)
    return len(data)


def sys_open(k, path):
    _require_running(k)
    try:
        k.proc.read(path)
    except FileNotFoundError:
        return -2                       # -ENOENT
    return 3                            # fake fd, like a real syscall would


def _pid_arg(pid):
    """Validate a userland-supplied pid before it touches ring-0 state."""
    try:
        return int(pid)
    except (TypeError, ValueError):
        raise K.KernelFault(-22, f"not a valid pid: {pid!r}")


def sys_mmap(k, pid, length):
    _require_running(k)
    pid = _pid_arg(pid)
    try:
        length = int(length)
    except (TypeError, ValueError):
        raise K.KernelFault(-22, f"mmap: invalid length {length!r}")
    k.mm.add_task(pid)                  # idempotent
    return k.mm.do_mmap(pid, length)


def sys_brk(k, pid, size):
    _require_running(k)
    pid = _pid_arg(pid)
    try:
        size = int(size)
    except (TypeError, ValueError):
        raise K.KernelFault(-22, f"brk: invalid size {size!r}")
    k.mm.add_task(pid)
    return k.mm.brk_grow(pid, size)


def sys_ioctl(k, cmd, arg=None):
    """Kernel-introspection ioctl: which->table, stats->counters."""
    if cmd == "which":
        return {"nr": arg, "name": K.SYSCALL_NAMES[arg]
                if arg is not None and 0 <= arg < len(K.SYSCALL_NAMES)
                else "???"}
    if cmd == "stats":
        return {"syscall_count": k.syscall_count, "timer_irq": k.timer_irq,
                "uptime": round(k.uptime(), 3), "state": k.state,
                "context_switches": k.sched.context_switches,
                "vm": k.mm.vmstat(),
                "mem_free_pages": k.phys.npages_free,
                "mem_total_pages": k.phys.npages_total,
                "slab": dict(k.slab.stats)}
    if cmd == "ps":
        return [t.stat_line() for t in sorted(k.sched.tasks.values(),
                                              key=lambda t: t.pid)]
    raise SyscallError(-22, f"bad ioctl cmd '{cmd}'")


def sys_fork_exec(k, ppid, name, prio=120):
    _require_running(k)
    try:
        ppid = int(ppid)
        prio = int(prio)
    except (TypeError, ValueError):
        raise K.KernelFault(-22, "fork: ppid/prio must be integers")
    if not 100 <= prio <= 139:
        raise K.KernelFault(-22, f"fork: nice out of range (prio={prio})")
    t = k.sched.fork_exec(ppid, str(name), prio)
    k.mm.add_task(t.pid)
    return t.pid


def sys_execve(k, pid, argv0):
    t = k.sched.tasks.get(_pid_arg(pid))
    if t is None:
        return -3                       # -ESRCH
    t.name = str(argv0)[:20]
    return 0


def sys_exit(k, pid, code=0):
    try:
        t = k.sched.exit(_pid_arg(pid), int(code))
    except K.KernelFault as e:
        if e.errno == -3:                     # already reaped -> ESRCH, ok
            return 0
        raise
    except (TypeError, ValueError):
        return -22                            # bad code -> -EINVAL
    k.mm.del_task(t.pid)
    return 0


def sys_wait4(k, pid):
    t = k.sched.tasks.get(_pid_arg(pid))
    if t is None:
        return -3
    if t.state == K.Task.TASK_ZOMBIE:
        del k.sched.tasks[t.pid]
        k.mm.add_task(t.pid)                  # ensure vmas dict exists
        k.mm.del_task(t.pid)                  # free any lingering pages
        return t.exit_code
    return None                          # would block in a real kernel


def sys_kill(k, pid):
    pid = _pid_arg(pid)
    if pid == 1:
        # SIGKILL to init is ignored by the kernel (real-init semantics);
        # report success quietly instead of panicking the whole machine.
        return 0
    if not k.sched.kill(pid):
        return -3
    k.mm.del_task(pid)
    k.signals.pending.pop(pid, None)          # drop queued signals
    for key, owner in list(k.futex.locks.items()):
        if owner == pid:                      # release locks of a dead task
            del k.futex.locks[key]
    return 0


def sys_getpid(k):
    cur = k.sched.current or k.idle_task
    return cur.pid if cur else 0


def sys_time(k):
    return time.time()


def sys_clock_gettime(k):
    return k.uptime()


def sched_yield_(k):
    k.sched.schedule()
    return 0


def sys_panic(k, msg="forced panic via sys_panic"):
    k.panic(str(msg))                    # never returns normally


# --------------------------------------------------- v2.1: VFS / signals / sync
def sys_vfs_write(k, path, data):
    _require_running(k)
    return k.vfs.write(path, data)


def sys_vfs_read(k, path):
    _require_running(k)
    try:
        return k.vfs.read(path).decode("utf-8", errors="replace")
    except FileNotFoundError:
        raise SyscallError(-2, f"vfs read: {path}: no such file")   # ENOENT


def sys_vfs_ls(k, path="/tmp"):
    _require_running(k)
    try:
        return k.vfs.ls(path)
    except FileNotFoundError:
        raise SyscallError(-2, f"vfs ls: {path}: no such directory")


def sys_vfs_rm(k, path):
    _require_running(k)
    try:
        return k.vfs.rm(path)
    except FileNotFoundError:
        raise SyscallError(-2, f"vfs rm: {path}: no such file")


def sys_signal_send(k, pid, sig):
    _require_running(k)
    pid = _pid_arg(pid)
    if not isinstance(sig, int) or isinstance(sig, bool):
        raise SyscallError(-22, f"kill: bad signal {sig!r}")
    try:
        return 0 if k.signals.send(pid, sig) else -1     # EPERM for init
    except K.KernelFault as e:
        return e.errno


def sys_signal_check(k, pid):
    """Collect pending signals for a task (like sigpending+sigaction run)."""
    _require_running(k)
    pid = _pid_arg(pid)
    pend = k.signals.collect(pid)
    out = []
    for s in pend:
        name = K.SIG_NAMES.get(s, str(s))
        t = k.sched.tasks.get(pid)
        if t is not None and s in t.signal_mask and s not in \
                K.SignalRegistry.UNBLOCKABLE:
            out.append(f"{name}(blocked)")
            k.signals.pending.setdefault(pid, set()).add(s)   # requeue
        else:
            out.append(name)
    return out


def sys_futex(k, op, key=None, pid=0):
    _require_running(k)
    try:
        if op == "lock":
            return 0 if k.futex.try_lock(key, pid) else -11      # EAGAIN
        if op == "unlock":
            return 0 if k.futex.unlock(key, pid) else -1
        if op == "stats":
            return {"held": len(k.futex.locks),
                    "contended": k.futex.contended}
    except K.KernelFault as e:
        return e.errno
    raise SyscallError(-22, f"ioctl futex: bad op {op!r}")


def sys_watchdog(k, op):
    _require_running(k)
    if op == "arm":
        k.watchdog.arm()
        return 0
    if op == "pet":
        k.watchdog.pet()
        return 0
    if op == "check":
        return 0 if k.watchdog.check() else -62     # ETIMEDOUT -> recover
    if op == "status":
        return {"armed": k.watchdog.armed,
                "expirations": k.watchdog.expirations,
                "timeout_sec": k.watchdog.timeout}
    raise SyscallError(-22, f"watchdog: bad op {op!r}")


def sys_ports(k, query=None):
    """Port-matrix introspection for the UI (`ports` command)."""
    _require_running(k)
    try:
        from .porting import PORT_MATRIX, certification_report
    except ImportError:
        raise SyscallError(-38, "porting layer not available")   # ENOSYS
    if query == "summary":
        return PORT_MATRIX.summary()
    if query == "report":
        return certification_report()
    if query is None:
        return [t.id for t in PORT_MATRIX.all_targets()]
    t = PORT_MATRIX.find(str(query))
    if t is None:
        raise SyscallError(-2, f"no such port target: {query}")   # ENOENT
    return vars(t)


SYSCALL_TABLE = {
    0: ("read", sys_read),
    1: ("write", sys_write),
    2: ("open", sys_open),
    7: ("mmap", sys_mmap),
    8: ("brk_probe", lambda k: "brk"),
    9: ("brk", sys_brk),
    11: ("ioctl", sys_ioctl),
    12: ("fork_exec", sys_fork_exec),
    13: ("execve", sys_execve),
    14: ("exit", sys_exit),
    15: ("wait4", sys_wait4),
    16: ("kill", sys_kill),
    17: ("getpid", sys_getpid),
    20: ("time", sys_time),
    23: ("sched_yield", sched_yield_),
    24: ("clock_gettime", sys_clock_gettime),
    # --- v2.1: VFS / signals / sync / watchdog / ports ------------------
    26: ("vfs_write", sys_vfs_write),
    27: ("vfs_read", sys_vfs_read),
    28: ("vfs_ls", sys_vfs_ls),
    29: ("vfs_rm", sys_vfs_rm),
    30: ("signal_send", sys_signal_send),
    31: ("signal_check", sys_signal_check),
    32: ("futex", sys_futex),
    33: ("watchdog", sys_watchdog),
    34: ("ports", sys_ports),
    # nr 25 (panic) is deliberately NOT in the user-reachable table —
    # see USER_SYSCALL_TABLE in truck_kernel.  Only kernel-internal code
    # may call k.panic() directly; userland cannot crash ring 0.
}


# ------------------------------------------------------------- the gate
def sysint(nr, *args, kernel=None):
    """Raise int 0x80: dispatch syscall `nr` with args into ring 0.

    Returns the syscall result, or a negative errno on recoverable faults.
    A genuine ring-0 fault prints a kernel oops and raises KernelPanic.
    """
    k = kernel or K.default_kernel()
    global CURRENT_KERNEL
    CURRENT_KERNEL = k                        # so SignalRegistry can find tasks
    k.tick()                                   # let timer IRQs accrue
    k.syscall_count += 1
    if nr not in K.USER_SYSCALL_TABLE:
        # -ENOSYS for unknown vectors AND for privileged numbers such as
        # 25/sys_panic: userland must never be able to panic the kernel.
        return -38
    name, fn = SYSCALL_TABLE[nr]
    try:
        return fn(k, *args)
    except SyscallError as e:
        return e.args[0]                        # already -errno
    except K.KernelFault as e:
        # recoverable ring-0 fault (-ENOMEM / -EINVAL / -EAGAIN / SIGSEGV):
        # deliver the errno to userland, keep the kernel alive
        return e.errno
    except K.KernelPanic:
        raise                                   # caller shows the oops
    except Exception as exc:                    # noqa: BLE001
        k.panic(f"unhandled fault in sys_{name}", cause=exc)


def render_table():
    lines = ["  nr  name            handler",
             "  --  --------------  --------------------------------"]
    for nr, (name, fn) in sorted(SYSCALL_TABLE.items()):
        lines.append(f"  {nr:>2}  {name:<14}  sys_{name}")
    return "\n".join(lines)
