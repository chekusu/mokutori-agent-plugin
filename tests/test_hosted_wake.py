"""`wake set/status/clear` registers the routine webhook with the gateway's owner API. The URL and key are read
from private files, sent only in the request body and never printed. An older gateway without hosted wakes is
reported plainly.

Synthetic state and credentials only; the gateway API is replaced by a recorder.
"""
import io
import json
import os
from contextlib import redirect_stdout
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import mokutori as plugin

GATEWAY = "https://gateway.invalid"
URL = "https://hooks.example.invalid/routine/abc123-token"
KEY = "synthetic-routine-key-0123456789"


class HostedWakeCommandTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = plugin.Home(Path(self.temp.name) / "data")
        self.home.ensure()
        self.home.save({"v": 1, "setup": {"gateway": GATEWAY, "bot_id": "bot_fixture", "handle": "@fixture"},
                        "devices": {}, "pairings": {}})
        credentials = self.home.default_owner / "public" / "credentials.json"
        credentials.parent.mkdir(parents=True)
        plugin.C.write_private_json(credentials, {"v": 1, "gateways": {GATEWAY: {"owner": {"token": "own_secret"}}}})
        self.files = {}
        for name, value in (("url", URL), ("key", KEY)):
            path = Path(self.temp.name) / name
            path.write_text(value + "\n")
            os.chmod(str(path), 0o600)
            self.files[name] = str(path)
        self.calls = []

    def run_wake(self, command, answer, **options):
        def api_ok(base, method, path, body=None, token=None, **_):
            self.calls.append((base, method, path, body, token))
            if isinstance(answer, Exception):
                raise answer
            return 200, dict(answer, ok=True)
        stream = io.StringIO()
        arguments = SimpleNamespace(wake_cmd=command, url_file=self.files["url"], secret_file=self.files["key"],
                                    no_review=options.get("no_review", False))
        with mock.patch.object(plugin.C, "api_ok", side_effect=api_ok), redirect_stdout(stream):
            with self.assertRaises(SystemExit) as done:
                plugin.cmd_wake(self.home, arguments)
        return done.exception.code, stream.getvalue()

    def test_set_sends_the_files_to_the_owner_api_and_prints_neither(self):
        code, printed = self.run_wake("set", {"configured": True, "host": "hooks.example.invalid",
                                              "header": "Authorization", "scheme": "Bearer", "wake_on_review": True})
        self.assertEqual(0, code)
        base, method, path, body, token = self.calls[0]
        self.assertEqual((GATEWAY, "PUT", "/v1/owner/bots/bot_fixture/wake", "own_secret"), (base, method, path, token))
        self.assertEqual({"url": URL, "secret": KEY, "wake_on_review": True}, body)
        result = json.loads(printed)
        self.assertTrue(result["wake"]["configured"])
        for secret in (KEY, "abc123-token", "own_secret"):
            self.assertNotIn(secret, printed)

    def test_no_review_wakes_only_for_paired_devices(self):
        self.run_wake("set", {"configured": True}, no_review=True)
        self.assertFalse(self.calls[0][3]["wake_on_review"])

    def test_status_and_clear(self):
        self.run_wake("status", {"configured": False})
        self.run_wake("clear", {"configured": False})
        self.assertEqual([("GET", None), ("DELETE", None)], [(c[1], c[3]) for c in self.calls])

    def test_an_older_gateway_is_reported_plainly(self):
        with self.assertRaises(plugin.MokutoriError) as raised:
            self.run_wake("set", plugin.C.ApiError(404, "not_found"))
        self.assertIn("does not offer hosted wakes", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
