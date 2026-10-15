"""Tests for the TruckOS cross-platform porting layer + kernel v2.1 additions."""

import os
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import truck  # noqa: E402,F401  (sets up package aliases)
from truck.kernel import truck_kernel as K                      # noqa: E402
from truck.kernel import truck_syscall                          # noqa: E402
from truck.kernel.porting import (PORT_MATRIX, HAL, HostHAL, Urgency,  # noqa: E402
                                  certification_report, detect_host,
                                  kernel_languages, list_templates,
                                  scaffold_port)


def _booted_kernel():
    k = K.Kernel()
    k.start()
    return k


class PortMatrixRequirements(unittest.TestCase):
    def test_at_least_18_phones(self):
        self.assertGreaterEqual(PORT_MATRIX.count_phones(), 18)

    def test_three_primary_vendor_families(self):
        s = PORT_MATRIX.summary()
        self.assertEqual(len(s["primary_vendors"]), 3)
        for v in ("google", "apple", "valve"):
            self.assertIn(v, s["primary_vendors"])
            self.assertTrue(PORT_MATRIX.by_vendor(v))

    def test_total_targets_ge_20(self):
        self.assertGreaterEqual(PORT_MATRIX.summary()["total"], 20)

    def test_urgent_targets_have_reasons_and_red_flag(self):
        for t in PORT_MATRIX.all_targets():
            if t.urgency in (Urgency.SECURITY, Urgency.CRITICAL):
                self.assertTrue(t.notes, f"{t.id} urgent but no reason given")
        crit = [t for t in PORT_MATRIX.urgent() if t.urgency is Urgency.CRITICAL]
        self.assertTrue(crit, "at least one CRITICAL port must exist for [!] demo")
        rep = certification_report()
        self.assertIn("[!]", rep)          # red flag rendered for critical
        self.assertIn("[sec]", rep)        # security-pending marker rendered

    def test_find_and_duplicate_guard(self):
        self.assertIsNotNone(PORT_MATRIX.find("pixel-8"))
        self.assertIsNone(PORT_MATRIX.find("no-such-device"))
        with self.assertRaises(ValueError):
            PORT_MATRIX.add(PORT_MATRIX.find("pixel-8"))


class HalAbstraction(unittest.TestCase):
    def test_detect_host_returns_bound_hal(self):
        hal = detect_host()
        self.assertIsInstance(hal, HostHAL)
        d = hal.describe()
        self.assertIn(d["page_size"], (4096, 16384))
        self.assertEqual(d["word_bits"], 64)

    def test_abi_dispatch_per_platform(self):
        hal = HostHAL(PORT_MATRIX.find("iphone-15"))
        self.assertEqual(hal.invoke_syscall(7, 4096)[0], "mach_trap")
        self.assertEqual(hal.page_size(), 16384)          # Apple 16K pages
        wsl = HostHAL(PORT_MATRIX.find("wsl2"))
        self.assertEqual(wsl.invoke_syscall(0)[0], "wsl_bridge")
        andr = HostHAL(PORT_MATRIX.find("galaxy-s23"))
        self.assertEqual(andr.invoke_syscall(0)[0], "svc")
        self.assertEqual(andr.display_backend(), "fb-console")

    def test_unknown_abi_rejected(self):
        from truck.kernel.porting.ports import Target
        bogus = Target("bogus-x", "Bogus", "community", "desktop",
                       "x86_64", "bare", abi="magic")
        with self.assertRaises(ValueError):
            HostHAL(bogus).invoke_syscall(0)

    def test_abstract_hal_not_usable(self):
        with self.assertRaises(NotImplementedError):
            HAL().page_size()


class KernelIntegration(unittest.TestCase):
    def test_kernel_boots_with_hal_bound(self):
        k = _booted_kernel()
        self.assertIsNotNone(k.port_hal,
                             "HAL must be bound at boot now that "
                             "porting/__init__.py exports detect_host")
        self.assertEqual(k.port_hal.page_size(), 4096)
        self.assertEqual(k.state, "running")

    def test_language_probe_scans_whole_kernel_tree(self):
        info = kernel_languages()
        langs = info["languages"]
        self.assertIn("Python", langs)
        # core files (kernel/boot/init/syscall) + porting files
        self.assertGreaterEqual(langs["Python"]["files"], 8)


class ScaffoldPort(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="trk-port-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        # remove any live registration made during the test
        PORT_MATRIX.phones[:] = [x for x in PORT_MATRIX.phones
                                 if not x.id.startswith("test-phone-")]
        PORT_MATRIX.others[:] = [x for x in PORT_MATRIX.others
                                 if not x.id.startswith("test-phone-")]

    def test_scaffold_creates_files_and_registers(self):
        path = scaffold_port("test-phone-nokia1", name="Nokia Test 1",
                             vendor="community", template="phone-android",
                             base_dir=self.tmp)
        for fn in ("port.py", "hal_impl.py", "manifest.data",
                   "README.md", "matrix_add.snippet"):
            self.assertTrue(os.path.exists(os.path.join(path, fn)), fn)
        t = PORT_MATRIX.find("test-phone-nokia1")
        self.assertIsNotNone(t)
        self.assertEqual(t.status, "planned")

    def test_scaffold_rejects_bad_ids(self):
        for bad in ("../evil", "pixel-8", ""):
            with self.assertRaises(ValueError):
                scaffold_port(bad, base_dir=self.tmp)

    def test_scaffold_rejects_unknown_template(self):
        with self.assertRaises(ValueError):
            scaffold_port("test-phone-x", template="toaster",
                          base_dir=self.tmp)

    def test_templates_available(self):
        tpl = list_templates()
        self.assertGreaterEqual(len(tpl), 4)
        self.assertIn("phone-android", tpl)
        self.assertIn("phone-ios", tpl)


class KernelV21Features(unittest.TestCase):
    """The expansion side of 'update & extend the kernel' (v2.1.0)."""

    def setUp(self):
        self.k = _booted_kernel()

    def test_tmpfs_roundtrip_and_traversal_block(self):
        self.k.vfs.write("/etc/motd", "hello")
        self.assertEqual(self.k.vfs.read("/etc/motd"), b"hello")
        self.assertIn("motd", self.k.vfs.ls("/etc"))
        with self.assertRaises(K.KernelFault):
            self.k.vfs.read("/etc/../etc/motd")

    def test_tmpfs_size_cap(self):
        with self.assertRaises(K.KernelFault) as ctx:
            self.k.vfs.write("/big", "x" * (256 * 1024 + 1))
        self.assertEqual(ctx.exception.errno, -27)      # EFBIG

    def test_init_immune_to_kill_signal_via_gate(self):
        r = truck_syscall.sysint(30, 1, 9, kernel=self.k)   # signal_send
        self.assertEqual(r, -1)                             # EPERM: init immortal

    def test_signal_send_to_real_task(self):
        pid = self.k.sched.fork_exec(1, "worker").pid
        r = truck_syscall.sysint(30, pid, 15, kernel=self.k)
        self.assertEqual(r, 0)
        pend = truck_syscall.sysint(31, pid, kernel=self.k)  # signal_check
        self.assertIn("SIGTERM", pend)

    def test_signal_bad_number_is_errno_not_panic(self):
        r = truck_syscall.sysint(30, 1, 12345, kernel=self.k)
        self.assertEqual(r, -22)                            # EINVAL
        self.assertEqual(self.k.state, "running")           # kernel survived

    def test_futex_owner_semantics_via_gate(self):
        # ops are strings: "lock"/"unlock"/"stats"
        self.assertEqual(truck_syscall.sysint(32, "lock", "k1", 2, kernel=self.k), 0)
        self.assertEqual(truck_syscall.sysint(32, "lock", "k1", 3, kernel=self.k), -11)
        self.assertEqual(truck_syscall.sysint(32, "unlock", "k1", 3, kernel=self.k), -1)
        st = truck_syscall.sysint(32, "stats", kernel=self.k)
        self.assertEqual(st["held"], 1)
        self.assertEqual(st["contended"], 1)

    def test_watchdog_arm_pet_expire_via_gate(self):
        self.k.watchdog.timeout = 0.01
        self.assertEqual(truck_syscall.sysint(33, "arm", kernel=self.k), 0)
        self.assertTrue(self.k.watchdog.armed)
        time.sleep(0.02)
        self.assertEqual(truck_syscall.sysint(33, "check", kernel=self.k), -62)
        self.assertEqual(self.k.watchdog.expirations, 1)
        self.assertEqual(self.k.state, "running")           # recovery, no panic
        self.assertEqual(truck_syscall.sysint(33, "pet", kernel=self.k), 0)

    def test_ports_syscall_exposes_matrix(self):
        out = truck_syscall.sysint(34, kernel=self.k)
        text = out if isinstance(out, str) else str(out)
        for needle in ("pixel-8", "iphone-15", "steam-deck"):
            self.assertIn(needle, text.replace("_", "-") + text)


if __name__ == "__main__":
    unittest.main()
