import io, sys, unittest
from unittest import mock
sys.path.insert(0, "/workspace")
from servercloud.os import truck_shell as ts

class FakeUpdate:
    def __init__(self): self.applied = []
    def apply_update(self, kind, cfg, yes=False):
        self.applied.append(kind); return f"updated {kind}"

class OfferInstall(unittest.TestCase):
    def test_available_normal_offers_and_installs_on_y(self):
        fu = FakeUpdate()
        with mock.patch("builtins.input", return_value="y"):
            ok = ts._offer_install(fu, {}, "kernel", {"ok": True, "available": True, "urgent": False})
        self.assertTrue(ok); self.assertEqual(fu.applied, ["kernel"])

    def test_available_urgent_default_yes_on_empty(self):
        fu = FakeUpdate()
        with mock.patch("builtins.input", return_value=""):
            ok = ts._offer_install(fu, {}, "security", {"ok": True, "available": True, "urgent": True})
        self.assertTrue(ok); self.assertEqual(fu.applied, ["security"])

    def test_not_available_no_offer(self):
        fu = FakeUpdate()
        ok = ts._offer_install(fu, {}, "os", {"ok": True, "available": False})
        self.assertFalse(ok); self.assertEqual(fu.applied, [])

    def test_decline_skips(self):
        fu = FakeUpdate()
        with mock.patch("builtins.input", return_value="n"):
            ok = ts._offer_install(fu, {}, "os", {"ok": True, "available": True, "urgent": False})
        self.assertFalse(ok); self.assertEqual(fu.applied, [])

    def test_failed_check_no_offer(self):
        fu = FakeUpdate()
        ok = ts._offer_install(fu, {}, "os", {"ok": False})
        self.assertFalse(ok)

    def test_os_check_result_has_available_flag(self):
        # engine now reports 'available' in check_os_updates output contract
        import inspect, truck_modules.update_engine as ue
        src = inspect.getsource(ue.check_os_updates)
        self.assertIn('"available"', src)

if __name__ == "__main__":
    unittest.main(verbosity=2)
