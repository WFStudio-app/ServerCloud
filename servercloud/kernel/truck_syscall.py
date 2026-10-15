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

from servercloud.kernel import truck_kernel as K


class SyscallError(Exception):
    """Returned to userland as -errno instead of raising into the UI."""


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
    t = k.sched.fork_exec(int(ppid), str(name), int(prio))
    k.mm.add_task(t.pid)
    return t.pid


def sys_execve(k, pid, argv0):
    t = k.sched.tasks.get(int(pid))
    if t is None:
        return -3                       # -ESRCH
    t.name = str(argv0)[:20]
    return 0


def sys_exit(k, pid, code=0):
    try:
        t = k.sched.exit(int(pid), int(code))
    except K.KernelFault as e:
        if e.errno == -3:                     # already reaped -> ESRCH, ok
            return 0
        raise
    k.mm.del_task(t.pid)
    return 0


def sys_wait4(k, pid):
    t = k.sched.tasks.get(int(pid))
    if t is None:
        return -3
    if t.state == K.Task.TASK_ZOMBIE:
        del k.sched.tasks[t.pid]
        k.mm.add_task(t.pid)                  # ensure vmas dict exists
        k.mm.del_task(t.pid)                  # free any lingering pages
        return t.exit_code
    return None                          # would block in a real kernel


def sys_kill(k, pid):
    pid = int(pid)
    if pid == 1:
        # SIGKILL to init is ignored by the kernel (real-init semantics);
        # report success quietly instead of panicking the whole machine.
        return 0
    if not k.sched.kill(pid):
        return -3
    k.mm.del_task(pid)
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
