"""Saved legacy pairing records must not block fresh proofs or replay permissions.

Synthetic state only; never opens production credentials or contacts a gateway.
"""
import copy
import datetime as dt
import io
import json
from contextlib import redirect_stdout
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import mokutori as plugin


class LegacyPairingStateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = plugin.Home(self.temp.name)
        self.secret = plugin.b64u(bytes(range(32)))
        self.state = {"v": 1, "setup": {"gateway": "https://gateway.invalid", "bot_id": "bot_fixture",
                                       "bot_key": self.secret, "handle": "@fixture"},
                      "devices": {}, "pairings": {}}

    def pending(self, **changes):
        record = {"secret": self.secret, "expires_at": plugin.iso(plugin.now() + dt.timedelta(minutes=30))}
        record.update(changes)
        return record

    def output(self, command, *args):
        stream = io.StringIO()
        with redirect_stdout(stream), self.assertRaises(SystemExit) as done:
            command(*args)
        self.assertEqual(done.exception.code, 0)
        return json.loads(stream.getvalue())

    def test_legacy_used_by_is_preserved_without_admission_or_false_completion(self):
        # Pre-jobs releases saved used_by and devices, but no proof_job_id or
        # embedded device. Replaying this as a modern job caused the upgrade crash.
        self.state["pairings"]["0123456789ab"] = self.pending(used_by="actor_old")
        self.state["devices"]["actor_old"] = {"name": "Old iPad", "e2e_key": self.secret}
        self.home.save(self.state)
        before = self.home.state_file.read_bytes()
        with mock.patch.object(plugin, "engine", return_value={"inbound": []}) as engine, \
                mock.patch.object(plugin, "finish_pairing") as finish:
            result = self.output(plugin.cmd_pair_check, self.home, SimpleNamespace(wait=0))
        self.assertEqual(result["paired"], [])
        self.assertEqual(result["pairing_warnings"][0]["code"], "legacy_pairing_not_resumable")
        self.assertNotIn(self.secret, json.dumps(result))
        finish.assert_not_called()
        self.assertEqual(engine.call_args.args[1:3], ("review", "list"))
        self.assertEqual(engine.call_count, 1)
        self.assertEqual(self.home.state_file.read_bytes(), before)

    def test_invalid_and_expired_records_do_not_crash_status_or_disappear_on_new_qr(self):
        entries = {"missing": {}, "bad-time": self.pending(expires_at="yesterday"), "scalar": "legacy",
                   "bad-secret": self.pending(secret="not a pairing secret"),
                   "expired": self.pending(expires_at="2020-01-01T00:00:00Z"),
                   "revoked": self.pending(revoked=True)}
        self.state["pairings"] = copy.deepcopy(entries)
        self.home.save(self.state)
        status = plugin.status_view(self.home, self.state)
        self.assertEqual(status["pending_pairings"], 0)
        self.assertEqual(len(status["pairing_warnings"]), 4)
        result = self.output(plugin.cmd_pair, self.home,
                             SimpleNamespace(minutes=30, label=None, png=None, terminal=False, wait=0))
        saved = self.home.state()
        for key, value in entries.items():
            self.assertEqual(saved["pairings"][key], value)
        self.assertEqual(plugin.status_view(self.home, saved)["pending_pairings"], 1)
        self.assertTrue(Path(result["qr_png"]).read_bytes().startswith(b"\x89PNG"))
        self.assertTrue(result["pair_url"].startswith("mokutori://pair?"))

    def test_new_valid_proof_pairs_alongside_legacy_record(self):
        self.state["pairings"] = {"0123456789ab": self.pending(used_by="actor_old"),
                                   "aabbccddeeff": self.pending()}
        self.home.save(self.state)
        old = copy.deepcopy(self.state["pairings"]["0123456789ab"])
        sender, job_id = "actor_new", "msg_abcdefghijklmnop"
        proof = plugin.pairing_proof(self.secret, "aabbccddeeff", "bot_fixture", sender)
        review = {"job_id": job_id, "from": {"principal_id": sender, "e2e_key": self.secret},
                  "message": {"text": "%s aabbccddeeff %s" % (plugin.PAIR_PREFIX, proof),
                              "e2e": {"opened": True, "sealed_by": sender}}}
        with mock.patch.object(plugin, "engine", return_value={"inbound": [review]}), \
                mock.patch.object(plugin, "finish_pairing", return_value={"actor_id": sender}) as finish:
            paired = plugin.check_pairings(self.home)
        self.assertEqual(paired, [{"actor_id": sender}])
        self.assertEqual(finish.call_count, 1)
        self.assertEqual(finish.call_args.args[2], "aabbccddeeff")
        saved = self.home.state()["pairings"]
        self.assertEqual(saved["0123456789ab"], old)
        self.assertEqual(saved["aabbccddeeff"]["used_by"], sender)
        self.assertEqual(saved["aabbccddeeff"]["proof_job_id"], job_id)

    def test_expired_revoked_malformed_or_used_records_cannot_accept_a_proof(self):
        records = [self.pending(expires_at="2020-01-01T00:00:00Z"), self.pending(revoked=True),
                   self.pending(used_by="actor_old"), self.pending(proof_finished=True),
                   self.pending(secret=None), self.pending(expires_at=None), {}, None]
        sender = "actor_new"
        proof = plugin.pairing_proof(self.secret, "aabbccddeeff", "bot_fixture", sender)
        review = {"job_id": "msg_abcdefghijklmnop", "from": {"principal_id": sender, "e2e_key": self.secret},
                  "message": {"text": "%s aabbccddeeff %s" % (plugin.PAIR_PREFIX, proof),
                              "e2e": {"opened": True, "sealed_by": sender}}}
        for record in records:
            with self.subTest(record=record):
                self.state["pairings"] = {"aabbccddeeff": record}
                self.home.save(self.state)
                before = self.home.state_file.read_bytes()
                with mock.patch.object(plugin, "engine", return_value={"inbound": [review]}), \
                        mock.patch.object(plugin, "finish_pairing") as finish:
                    self.assertEqual(plugin.check_pairings(self.home), [])
                finish.assert_not_called()
                self.assertEqual(self.home.state_file.read_bytes(), before)

    def test_modern_verified_job_remains_resumable_after_qr_expiry(self):
        record = self.pending(expires_at="2020-01-01T00:00:00Z", used_by="actor_reserved",
                              proof_job_id="msg_abcdefghijklmnop", device={"e2e_key": self.secret})
        self.state["pairings"]["aabbccddeeff"] = record
        self.home.save(self.state)
        with mock.patch.object(plugin, "engine", return_value={"inbound": []}), \
                mock.patch.object(plugin, "finish_pairing", return_value={"actor_id": "actor_reserved"}) as finish:
            self.assertEqual(plugin.check_pairings(self.home), [{"actor_id": "actor_reserved"}])
        self.assertEqual(finish.call_count, 1)
        self.assertEqual(plugin.pairing_warnings(self.home.state()), [])

    def test_timezone_offset_expiry_is_compared_as_time_and_naive_time_is_rejected(self):
        fixed = dt.datetime(2026, 10, 26, 14, 0, tzinfo=dt.timezone.utc)
        with mock.patch.object(plugin, "now", return_value=fixed):
            self.assertEqual(plugin.pairing_state(self.pending(expires_at="2026-10-26T23:30:00+09:00")), "pending")
            self.assertEqual(plugin.pairing_state(self.pending(expires_at="2026-10-26T22:30:00+09:00")), "expired")
            self.assertEqual(plugin.pairing_state(self.pending(expires_at="2026-10-26T23:30:00")), "invalid")


if __name__ == "__main__":
    unittest.main()
