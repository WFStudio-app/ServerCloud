"""Tests for the TruckOS Auto-Update subsystem (truck_update / update_markers).

Network calls are mocked — these tests validate parsing, urgency rules,
version comparison and apply-safety without touching GitHub.
"""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import truck  # registers legacy aliases          # noqa: E402
from truck.modules import update_engine as tu
from truck.modules import update_markers as um        # noqa: E402


class FakeCfg(dict):
    def get(self, k, d=None):
        return dict.get(self, k, d)


CFG = FakeCfg()  # defaults -> WFStudio-app/ServerCloud/main


class MarkerParser(unittest.TestCase):
    def test_dotdata_parse(self):
        rec = um.parse_dotdata("a = 1\n# comment\nb= two \n")
        self.assertEqual(rec, {"a": "1", "b": "two"})

    def test_bool_and_version(self):
        self.assertTrue(um.as_bool("true"))
        self.assertFalse(um.as_bool("garbage"))
        self.assertTrue(um.valid_version("2.0.3-truck"))
        self.assertFalse(um.valid_version("../../etc/passwd"))
        self.assertFalse(um.valid_version(""))


class KernelCheck(unittest.TestCase):
    REMOTE_VER = "9.9.9-truck"
    REMOTE_REL = ("version = 9.9.9-truck\nurgent = true\n"
                  "urgent_reason = vulnerability_found\nsecurity = true\n"
                  "languages = Python 3\n"
                  "description = fixes a kernel hole\ndate = 2026-10-16\n")

    def _run(self, rel=REMOTE_REL):
        files = {um.VERSION_FILE_REL: self.REMOTE_VER + "\n",
                 um.RELEASE_FILE_REL: rel,
                 um.UPD_KERNEL_VERSION_REL: self.REMOTE_VER + "\n",
                 um.UPD_KERNEL_RELEASE_REL: rel}

        def fake_raw(owner, repo, branch, path):
            if path not in files:
                raise RuntimeError("HTTP 404")
            return files[path]

        with mock.patch.object(tu, "_raw_file", fake_raw):
            return tu.check_kernel_update(CFG)

    def test_update_available_and_urgent(self):
        r = self._run()
        self.assertTrue(r["ok"])
        self.assertTrue(r["available"])
        self.assertTrue(r["urgent"])
        self.assertEqual(r["urgent_reason"], "vulnerability_found")
        self.assertTrue(r["has_security_fixes"])
        self.assertIn("Python", r["languages"])

    def test_bad_reason_clamped_to_allowed_set(self):
        r = self._run(self.REMOTE_REL.replace(
            "urgent_reason = vulnerability_found", "urgent_reason = because-i-want"))
        self.assertIn(r["urgent_reason"], um.URGENT_REASONS)

    def test_malformed_version_rejected(self):
        # upd_file/ is tried first, canonical VERSION.data is the fallback —
        # poison BOTH version paths.
        def fake(o, b, br, p):
            if p.endswith(("VERSION.data", "kernel_version.txt")):
                return "/../evil"
            return ""
        with mock.patch.object(tu, "_raw_file", fake):
            r = tu.check_kernel_update(CFG)
            self.assertFalse(r["ok"])
            self.assertIn("malformed", r.get("error", ""))

    def test_up_to_date(self):
        local_ver = um.local_version_file()
        with mock.patch.object(tu, "_raw_file",
                               lambda o, b, br, p: local_ver
                               if ("VERSION.data" in p or "kernel_version" in p)
                               else "version = %s\n" % local_ver):
            r = tu.check_kernel_update(CFG)
        self.assertTrue(r["ok"])
        self.assertFalse(r["available"])
        self.assertFalse(r["urgent"])


class SecurityCheck(unittest.TestCase):
    def test_parses_severity_and_update(self):
        data = ("version = 2099.01.01\nurgent = true\n"
                "urgent_reason = security_update\nseverity = CRITICAL\n"
                "description = hot ruleset\ndate = 2099-01-01\n")
        with mock.patch.object(tu, "_raw_file",
                               lambda o, b, br, p: data):
            r = tu.check_security_update(CFG)
        self.assertTrue(r["ok"])
        self.assertTrue(r["available"])
        self.assertTrue(r["urgent"])
        self.assertEqual(r["severity"], "critical")

    def test_unknown_severity_sanitized(self):
        with mock.patch.object(tu, "_raw_file",
                               lambda o, b, br, p: "version = 2099.1.1\nseverity = wtf\n"):
            r = tu.check_security_update(CFG)
        self.assertEqual(r["severity"], "unknown")

    def test_missing_db_reports_error(self):
        def boom(o, b, br, p):
            raise RuntimeError("GitHub HTTP 404")
        with mock.patch.object(tu, "_raw_file", boom):
            r = tu.check_security_update(CFG)
        self.assertFalse(r["ok"])
        self.assertIn("unavailable", r["error"])


class OsCheck(unittest.TestCase):
    COMMITS = [{"sha": "a" * 40,
                "commit": {"message": "[CVE-2026-1234] fix ring leak\nbody line",
                           "author": {"name": "TruckDev",
                                      "date": "2026-10-16T00:00:00Z"}}}]
    DETAIL = {"files": [{"filename": "kernel/truck_kernel.py"}]}
    RELEASES = [{"tag_name": "v9", "name": "Big One", "prerelease": False,
                 "target_commitish": "main",
                 "commit": {"sha": "a" * 40}, "html_url": "https://x/y"}]

    def test_release_and_urgency(self):
        def fake_api(path, params=None, auth=True):
            if path.endswith("/commits"):
                return self.COMMITS
            if "/commits/" in path:
                return self.DETAIL
            if path.endswith("/releases"):
                return self.RELEASES
            raise AssertionError(path)
        with mock.patch.object(tu, "_api_json", fake_api):
            r = tu.check_os_updates(CFG)
        self.assertTrue(r["ok"])
        self.assertTrue(r["urgent"])
        self.assertEqual(r["urgent_reason"], "vulnerability_found")
        self.assertTrue(r["touches_kernel"])
        self.assertEqual(r["release"]["tag"], "v9")

    def test_routine_push(self):
        commits = [{"sha": "b" * 40,
                    "commit": {"message": "docs: readme",
                               "author": {"name": "x", "date": "y"}}}]

        def fake_api(path, params=None, auth=True):
            if path.endswith("/commits"):
                return commits
            if path.endswith("/releases"):
                return []
            raise RuntimeError("no detail")
        with mock.patch.object(tu, "_api_json", fake_api):
            r = tu.check_os_updates(CFG)
        self.assertTrue(r["ok"])
        self.assertFalse(r["urgent"])
        self.assertIsNone(r["release"])


class ApplySafety(unittest.TestCase):
    def test_refuses_wrong_origin(self):
        with mock.patch.object(tu, "_origin_matches", lambda o, r: False):
            msg = tu.apply_update("kernel", CFG, yes=True)
        self.assertIn("refused", msg)

    def test_dry_run_does_not_pull(self):
        with mock.patch.object(tu, "_origin_matches", lambda o, r: True), \
             mock.patch.object(tu.shutil, "which", lambda x: "/usr/bin/git"), \
             mock.patch.object(tu.subprocess, "run",
                               side_effect=AssertionError("must not run")):
            msg = tu.apply_update("os", CFG, yes=False)
        self.assertIn("dry-run", msg)

    def test_https_only_guard(self):
        with self.assertRaises(RuntimeError):
            tu._http("http://evil.example.com/x")

    def test_repo_cfg_injection_rejected(self):
        bad = FakeCfg({"update_owner": "../evil"})
        with self.assertRaises(RuntimeError):
            tu._repo_cfg(bad)


if __name__ == "__main__":
    unittest.main()
