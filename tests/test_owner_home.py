"""Credential-location migration checks using only synthetic local files."""
import json
import os
from pathlib import Path
import tempfile
import unittest

from test_mokutori_plugin import plugin


class OwnerHomeCompatibility(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = plugin.Home(self.root / "mokutori")
        self.external = self.root / "existing-yobiru"
        self.external.mkdir(mode=0o750)
        (self.external / "public").mkdir(mode=0o700)
        self.credentials = self.external / "public" / "credentials.json"
        plugin.C.write_private_json(self.credentials, {"v": 1, "gateways": {}})
        self.state = {"v": 1, "setup": {"owner_home": str(self.external)},
                      "pairings": {}, "devices": {}}
        self.home.save(self.state)

    def test_existing_owner_is_reused_without_copy_or_permission_changes(self):
        before = self.credentials.read_bytes()
        mode = self.external.stat().st_mode
        self.home.ensure()
        self.assertEqual(self.home.owner, self.external)
        self.assertEqual(self.home.credentials("owner"), {"v": 1, "gateways": {}})
        self.assertEqual(self.credentials.read_bytes(), before)
        self.assertEqual(self.external.stat().st_mode, mode)
        self.assertFalse((self.home.default_owner / "public" / "credentials.json").exists())
        self.assertEqual(self.home.state(), self.state)

    def test_missing_external_owner_never_falls_back_or_creates_it(self):
        missing = self.root / "missing-yobiru"
        self.state["setup"]["owner_home"] = str(missing)
        self.home.save(self.state)
        self.home.ensure()
        with self.assertRaises(plugin.MokutoriError):
            _ = self.home.owner
        self.assertFalse(missing.exists())

    def test_invalid_explicit_owner_home_is_not_silently_ignored(self):
        for value in (None, "", "relative-yobiru", 42):
            with self.subTest(value=value):
                self.state["setup"]["owner_home"] = value
                self.home.save(self.state)
                with self.assertRaises(plugin.MokutoriError):
                    _ = self.home.owner

    def test_local_owner_is_default_only_without_override(self):
        del self.state["setup"]["owner_home"]
        self.home.save(self.state)
        self.assertEqual(self.home.owner, self.home.default_owner)

    def test_credential_symlink_remains_rejected(self):
        target = self.external / "original.json"
        self.credentials.rename(target)
        self.credentials.symlink_to(target)
        with self.assertRaises(plugin.MokutoriError):
            self.home.credentials("owner")
        self.assertTrue(self.credentials.is_symlink())

    @unittest.skipUnless(os.name == "posix", "POSIX permissions")
    def test_publicly_readable_credentials_remain_rejected_without_chmod(self):
        self.credentials.chmod(0o644)
        with self.assertRaises(plugin.MokutoriError):
            self.home.credentials("owner")
        self.assertEqual(self.credentials.stat().st_mode & 0o777, 0o644)


if __name__ == "__main__":
    unittest.main()
