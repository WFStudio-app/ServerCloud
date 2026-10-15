"""Tests for the simulated TruckOS kernel (truck_kernel + truck_syscall)."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from servercloud.os import truck_kernel as K
from servercloud.os import truck_syscall as S


def fresh_kernel():
    k = K.Kernel(ram_bytes=8 << 20)
    k.start()
    return k


class KernelBoot(unittest.TestCase):
    def test_boot_running(self):
        k = fresh_kernel()
        self.assertEqual(k.state, "running")
        self.assertTrue(k.boot_log)

    def test_e820(self):
        k = fresh_kernel()
        self.assertGreater(len(k.phys.e820), 1)
        self.assertIn("usable", k.phys.e820_dump())


class PageAllocator(unittest.TestCase):
    def test_alloc_free(self):
        k = fresh_kernel()
        free0 = k.phys.npages_free
        pfns = k.phys.alloc_pages(2)          # order-2 = 4 pages
        self.assertEqual(len(pfns), 4)
        self.assertEqual(k.phys.npages_free, free0 - 4)
        k.phys.free_pages(pfns)
        self.assertEqual(k.phys.npages_free, free0)

    def test_double_free_panics(self):
        k = fresh_kernel()
        pfns = k.phys.alloc_pages(0)
        k.phys.free_pages(pfns)
        with self.assertRaises(K.KernelPanic):
            k.phys.free_pages(pfns)


class Slab(unittest.TestCase):
    def test_kmalloc_kfree(self):
        k = fresh_kernel()
        a = k.slab.kmalloc(100)               # size class 128
        b = k.slab.kmalloc(64 * 1024)         # page-backed big alloc
        self.assertNotEqual(a, b)
        k.slab.kfree(a)
        k.slab.kfree(b)
        with self.assertRaises(K.KernelPanic):
            k.slab.kfree(a)                   # double free


class VirtualMemory(unittest.TestCase):
    def test_mmap_translate_tlb(self):
        k = fresh_kernel()
        pid = S.sysint(12, 1, "vm-test", kernel=k)
        va = S.sysint(7, pid, 3 * K.PAGE_SIZE, kernel=k)
        frame = k.mm.translate(pid, va)
        self.assertIsInstance(frame, int)
        hits_before = k.mm.tlb_hits
        k.mm.translate(pid, va)               # should hit TLB now
        self.assertEqual(k.mm.tlb_hits, hits_before + 1)

    def test_bad_address_panics(self):
        k = fresh_kernel()
        with self.assertRaises(K.KernelPanic):
            k.mm.translate(999, 0x1234_0000)


class SyscallGate(unittest.TestCase):
    def test_proc_reads(self):
        k = fresh_kernel()
        txt = S.sysint(0, "/proc/version", kernel=k)
        self.assertIn(K.KERNEL_NAME, txt)
        self.assertIn("MemTotal", S.sysint(0, "/proc/meminfo", kernel=k))
        self.assertIn("TruckOS", S.sysint(0, "/sys/kernel/ostype", kernel=k))

    def test_enosys_enoent(self):
        k = fresh_kernel()
        self.assertEqual(S.sysint(999, kernel=k), -38)      # -ENOSYS
        self.assertEqual(S.sysint(2, "/proc/nope", kernel=k), -2)  # -ENOENT

    def test_lifecycle(self):
        k = fresh_kernel()
        pid = S.sysint(12, 1, "life", kernel=k)
        self.assertGreater(pid, 1)
        self.assertEqual(S.sysint(13, pid, "life-prog", kernel=k), 0)
        self.assertEqual(S.sysint(14, pid, 3, kernel=k), 0)
        self.assertEqual(S.sysint(15, pid, kernel=k), 3)   # reaped exit code
        self.assertNotIn(pid, k.sched.tasks)

    def test_kill_init_panics(self):
        k = fresh_kernel()
        with self.assertRaises(K.KernelPanic):
            S.sysint(16, 1, kernel=k)

    def test_ioctl_stats_ps(self):
        k = fresh_kernel()
        st = S.sysint(11, "stats", kernel=k)
        self.assertEqual(st["state"], "running")
        ps = S.sysint(11, "ps", kernel=k)
        self.assertTrue(any("swapper" in ln for ln in ps))


class PanicHandler(unittest.TestCase):
    def test_panic_sets_state(self):
        k = fresh_kernel()
        with self.assertRaises(K.KernelPanic):
            k.panic("test panic")
        self.assertEqual(k.state, "panicked")

    def test_unhandled_fault_oops(self):
        k = fresh_kernel()
        with self.assertRaises(K.KernelPanic):
            S.sysint(0, 12345, kernel=k)     # non-string path -> ring-0 fault


if __name__ == "__main__":
    unittest.main(verbosity=2)
