"""One bad pairing, job, file or conversation must not stop a run, and must not fail the same way forever.

Synthetic state only: the engine, the gateway API and the claim are replaced by recorders; no network, no real
credentials.
"""
import base64
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import mokutori as plugin

GATEWAY = "https://gateway.invalid"
BOT = "bot_abcdefghijklmnop"
SENDER = "act_abcdefghijklmnop"
KEY = plugin.b64u(bytes(range(32)))
JOBS = ["msg_" + letter * 16 for letter in "abcdefg"]
GIF = b"GIF89a\x01\x00\x01\x00\x80\x00\x00\xff\xff\xff\x00\x00\x00;"
PDF = b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n"
MP3 = b"ID3\x03\x00\x00\x00\x00\x00\x0a\xff\xfb\x90\x00"


def gone():
    return plugin.C.ApiError(404, "not_found")


class Fixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = plugin.Home(Path(self.temp.name) / "data")
        self.home.ensure()
        self.state = {"v": 1, "setup": {"gateway": GATEWAY, "bot_id": BOT, "bot_key": KEY, "handle": "@fixture"},
                      "devices": {SENDER: {"name": "Study iPad", "e2e_key": KEY}}, "pairings": {}, "received": {}}
        self.home.save(self.state)
        credentials = self.home.default_owner / "public" / "credentials.json"
        credentials.parent.mkdir(parents=True)
        plugin.C.write_private_json(credentials, {"v": 1, "gateways": {GATEWAY: {"owner": {"token": "own_secret"}}}})
        self.addCleanup(plugin.RUN.update, started=None)

    def run_main(self, *arguments):
        """A whole command as the shell runs it. -> (exit code, parsed JSON output)"""
        stream = io.StringIO()
        with redirect_stdout(stream), self.assertRaises(SystemExit) as done:
            plugin.main(["--home", str(self.home.path)] + list(arguments))
        return done.exception.code, json.loads(stream.getvalue())

    def confirmed(self, sender, job_id, **changes):
        record = {"secret": KEY, "expires_at": "2020-01-01T00:00:00Z", "used_by": sender, "proof_job_id": job_id,
                  "awaiting_owner": True, "owner_confirmed": True,
                  "device": {"name": "iPad of %s" % sender, "e2e_key": KEY, "pairing_id": None}}
        record.update(changes)
        return record

    def batch_receipt(self, job_id, **changes):
        folder = self.home.inbox / job_id
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "message.txt").write_text("Mokutori 扫描 · 第 1–1 页")
        receipt = {"sender": SENDER, "remote_finished": True, "acknowledged": False, "seq": 1,
                   "batch": {"message_id": job_id, "from_device": "Study iPad", "received_at": "2026-11-01T00:00:00Z",
                             "summary": "Mokutori 扫描 · 第 1–1 页", "ocr_markdown": "", "files": [],
                             "folder": str(folder)}}
        receipt.update(changes)
        return receipt


class StuckPairings(Fixture):
    """Bug 1: a confirmed pairing that cannot finish is reported, never blocks, and can be cancelled."""

    def two_pairings(self):
        self.state["pairings"] = {"aaaaaaaaaaaa": self.confirmed("act_aaaaaaaaaaaaaaaa", JOBS[0]),
                                  "bbbbbbbbbbbb": self.confirmed("act_bbbbbbbbbbbbbbbb", JOBS[1])}
        self.home.save(self.state)

    def test_one_pairing_that_cannot_finish_does_not_stop_the_others(self):
        failures = (plugin.MokutoriError("review: not_found"), plugin.C.ApiError(404, "not_found"),
                    KeyError("job_id"), SystemExit(2), subprocess.TimeoutExpired(["engine"], 120),
                    UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte"))
        for failure in failures:
            with self.subTest(failure=type(failure).__name__):
                self.two_pairings()

                def finish(home, state, pairing_id):
                    if pairing_id == "aaaaaaaaaaaa":
                        raise failure
                    return {"actor_id": "act_bbbbbbbbbbbbbbbb"}
                with mock.patch.object(plugin, "finish_pairing", side_effect=finish), \
                        mock.patch.object(plugin, "engine", return_value={"inbound": []}) as engine:
                    paired, requests, stuck = plugin.check_pairings(self.home)
                self.assertEqual(paired, [{"actor_id": "act_bbbbbbbbbbbbbbbb"}])
                self.assertEqual(requests, [])
                self.assertEqual([item["pairing_id"] for item in stuck], ["aaaaaaaaaaaa"])
                self.assertEqual(stuck[0]["message_id"], JOBS[0])
                self.assertTrue(stuck[0]["error"])
                self.assertFalse(stuck[0]["admitted"])
                self.assertIn("pair-decline", stuck[0]["next"])
                self.assertNotIn("Traceback", json.dumps(stuck))
                # New requests are still looked for after the failure.
                self.assertEqual(engine.call_args.args[1:3], ("review", "list"))

    def test_every_command_that_checks_pairings_still_answers(self):
        self.two_pairings()
        self.state["received"][JOBS[2]] = self.batch_receipt(JOBS[2])
        self.home.save(self.state)

        def engine(home_dir, *args, **_):
            if args[:2] == ("review", "list"):
                return {"inbound": []}
            if args[:2] == ("job", "list"):
                return {"jobs": []}
            raise plugin.MokutoriError("review: not_found")          # admission: the request message is gone
        for command in (("pair-check",), ("pair-requests",), ("inbox",)):
            with self.subTest(command=command), mock.patch.object(plugin, "engine", side_effect=engine):
                code, result = self.run_main(*command)
                self.assertEqual(code, 0, result)
                self.assertTrue(result["ok"])
                self.assertEqual(sorted(item["pairing_id"] for item in result["pairing_errors"]),
                                 ["aaaaaaaaaaaa", "bbbbbbbbbbbb"])
                self.assertIn("not_found", result["pairing_errors"][0]["error"])
        # ... and inbox still showed the saved batch.
        self.assertEqual([batch["message_id"] for batch in result["batches"]], [JOBS[2]])

    def test_a_waiting_check_tries_a_stuck_pairing_once(self):
        self.two_pairings()
        with mock.patch.object(plugin, "finish_pairing", side_effect=plugin.MokutoriError("review: not_found")) \
                as finish, mock.patch.object(plugin, "engine", return_value={"inbound": []}) as engine, \
                mock.patch.object(plugin.time, "sleep"):
            paired, requests, stuck = plugin.check_pairings(self.home, wait=0.05)
        self.assertEqual((paired, requests, len(stuck)), ([], [], 2))
        self.assertEqual(finish.call_count, 2)
        self.assertGreater(engine.call_count, 1)             # it kept looking for new requests meanwhile

    def admitted(self):
        self.state["pairings"] = {"aaaaaaaaaaaa": self.confirmed(SENDER, JOBS[0], admitted=True)}
        self.home.save(self.state)

    def finish_with(self, context):
        state = self.home.state()
        answer = context if isinstance(context, Exception) else None
        with mock.patch.object(plugin, "engine", return_value={"jobs": []}), \
                mock.patch.object(plugin, "owner_job_context", side_effect=answer,
                                  return_value=None if answer else context):
            return plugin.finish_pairing(self.home, state, "aaaaaaaaaaaa")

    def test_admitted_pairing_completes_when_its_proof_job_ended_unread(self):
        # Admission is saved; the proof claim was lost and the gateway marked the job read, expired it, or purged it.
        for context in ({"state": "completed", "actor_id": SENDER}, {"state": "expired", "actor_id": SENDER}, gone()):
            with self.subTest(context=str(context)):
                self.admitted()
                device = self.finish_with(context)
                self.assertEqual(device["actor_id"], SENDER)
                saved = self.home.state()["pairings"]["aaaaaaaaaaaa"]
                self.assertTrue(saved["proof_finished"])
                self.assertEqual(saved["proof_unread"], "gone" if isinstance(context, Exception) else context["state"])
                self.assertEqual(plugin.pairing_state(saved), "finished")
                self.assertIn(SENDER, self.home.state()["devices"])

    def test_admitted_pairing_waits_while_the_job_is_pending_or_belongs_to_someone_else(self):
        for context in ({"state": "started", "actor_id": SENDER}, {"state": "uncertain", "actor_id": SENDER},
                        {"state": "completed", "actor_id": "act_someoneelse22222"}):
            with self.subTest(context=context):
                self.admitted()
                with self.assertRaises(plugin.MokutoriError):
                    self.finish_with(context)
                self.assertNotIn("proof_finished", self.home.state()["pairings"]["aaaaaaaaaaaa"])

    def test_a_cached_proof_that_does_not_match_never_completes_the_pairing(self):
        self.admitted()
        plugin.C.write_private_json(self.home.claims / (JOBS[0] + ".json"),
                                    {"gateway": GATEWAY, "bot_id": BOT, "job": {"job_id": JOBS[0]}})
        state = self.home.state()
        with mock.patch.object(plugin, "engine", return_value={"jobs": []}), \
                mock.patch.object(plugin, "owner_job_context", return_value={"state": "completed", "actor_id": SENDER}), \
                mock.patch.object(plugin, "receive_job",
                                  side_effect=plugin.MokutoriError("claimed pairing proof does not match")) as receive:
            with self.assertRaises(plugin.MokutoriError):
                plugin.finish_pairing(self.home, state, "aaaaaaaaaaaa")
        self.assertTrue(receive.call_args.kwargs["cached_finished"])
        self.assertNotIn("proof_finished", self.home.state()["pairings"]["aaaaaaaaaaaa"])

    def test_pair_decline_cancels_a_confirmed_pairing_and_takes_back_its_permission(self):
        for admitted, answer in ((True, {"ok": True}), (False, {"ok": True}),
                                 (False, {"ok": False, "error": "not_found", "http_status": 404})):
            with self.subTest(admitted=admitted, answer=answer):
                self.state["pairings"] = {"aaaaaaaaaaaa": self.confirmed(SENDER, JOBS[0], admitted=admitted)}
                self.state["devices"] = {SENDER: {"name": "Study iPad", "e2e_key": KEY}} if admitted else {}
                self.home.save(self.state)
                with mock.patch.object(plugin, "engine", return_value=answer) as engine:
                    code, result = self.run_main("pair-decline", "aaaaaaaaaaaa")
                self.assertEqual((code, result["ok"], result["cancelled"]), (0, True, True))
                self.assertEqual(engine.call_args.args[1:6], ("owner", "contact", BOT, SENDER, "block"))
                saved = self.home.state()
                self.assertEqual(plugin.pairing_state(saved["pairings"]["aaaaaaaaaaaa"]), "revoked")
                self.assertEqual(saved["devices"], {})
                # Nothing is left to retry or to report.
                with mock.patch.object(plugin, "engine", return_value={"inbound": []}), \
                        mock.patch.object(plugin, "finish_pairing") as finish:
                    self.assertEqual(plugin.check_pairings(self.home), ([], [], []))
                finish.assert_not_called()

    def test_pair_decline_revokes_every_confirmed_pairing_of_that_device(self):
        self.state["pairings"] = {"aaaaaaaaaaaa": self.confirmed(SENDER, JOBS[0], admitted=True),
                                  "bbbbbbbbbbbb": self.confirmed(SENDER, JOBS[1])}
        self.state["devices"] = {SENDER: {"name": "Study iPad", "e2e_key": KEY}}
        self.home.save(self.state)
        with mock.patch.object(plugin, "engine", return_value={"ok": True}):
            code, result = self.run_main("pair-decline", "aaaaaaaaaaaa")
        self.assertEqual((code, result["ok"]), (0, True))
        saved = self.home.state()
        self.assertEqual([plugin.pairing_state(saved["pairings"][key]) for key in ("aaaaaaaaaaaa", "bbbbbbbbbbbb")],
                         ["revoked", "revoked"])
        with mock.patch.object(plugin, "engine", return_value={"inbound": []}), \
                mock.patch.object(plugin, "finish_pairing") as finish:
            self.assertEqual(plugin.check_pairings(self.home), ([], [], []))
        finish.assert_not_called()

    def test_pair_decline_changes_nothing_when_the_gateway_does_not_answer(self):
        self.admitted()
        before = self.home.state_file.read_bytes()
        with mock.patch.object(plugin, "engine", return_value={"ok": False, "error": "unreachable"}):
            code, result = self.run_main("pair-decline", "aaaaaaaaaaaa")
        self.assertEqual((code, result["ok"]), (2, False))
        self.assertEqual(self.home.state_file.read_bytes(), before)

    def test_pair_decline_still_refuses_a_pairing_that_is_neither_requested_nor_stuck(self):
        self.state["pairings"] = {"aaaaaaaaaaaa": self.confirmed(SENDER, JOBS[0], proof_finished=True)}
        self.home.save(self.state)
        with mock.patch.object(plugin, "engine") as engine:
            code, result = self.run_main("pair-decline", "aaaaaaaaaaaa")
        self.assertEqual((code, result["ok"]), (2, False))
        engine.assert_not_called()

    def test_pair_accept_that_cannot_finish_says_how_to_go_on(self):
        self.state["pairings"] = {"aaaaaaaaaaaa": self.confirmed(SENDER, JOBS[0], owner_confirmed=False)}
        self.state["devices"] = {}
        self.home.save(self.state)
        with mock.patch.object(plugin, "engine", side_effect=plugin.MokutoriError("review: not_found")):
            code, result = self.run_main("pair-accept", "aaaaaaaaaaaa")
        self.assertEqual((code, result["ok"]), (2, False))
        self.assertEqual(result["pairing_errors"][0]["pairing_id"], "aaaaaaaaaaaa")
        self.assertIn("pair-decline", result["pairing_errors"][0]["next"])
        self.assertEqual(plugin.pairing_state(self.home.state()["pairings"]["aaaaaaaaaaaa"]), "resumable")


class OneBadJob(Fixture):
    """Bug 3: any failure of one job is that job's error; a job the gateway no longer has is closed."""

    def listing(self, count):
        return {"jobs": [{"job_id": job_id, "from": {"principal_id": SENDER}, "claimable": True, "state": "queued"}
                         for job_id in JOBS[:count]]}

    def test_inbox_goes_on_after_every_kind_of_failure(self):
        failures = [plugin.C.ApiError(404, "not_found"), subprocess.TimeoutExpired(["engine", "--home", "/secret"], 120),
                    SystemExit(2), UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte"),
                    KeyError("lease"), TypeError("NoneType"), ValueError("bad base64")]
        received = []

        def receive(home, state, setup, summary, **_):
            if summary["job_id"] in JOBS:
                raise failures[JOBS.index(summary["job_id"])]
            received.append(summary["job_id"])
        listing = self.listing(len(failures))
        listing["jobs"].append({"job_id": "msg_" + "z" * 16, "from": {"principal_id": SENDER}, "claimable": True})
        listing["jobs"].insert(0, {"job_id": "not a job id", "from": {"principal_id": SENDER}})
        listing["jobs"].insert(0, "not even an object")
        with mock.patch.object(plugin, "engine", return_value=listing), \
                mock.patch.object(plugin, "receive_job", side_effect=receive), \
                mock.patch.object(plugin, "finish_received"):
            code, result = self.run_main("inbox", "--no-pair-check")
        self.assertEqual(code, 0, result)
        self.assertEqual(received, ["msg_" + "z" * 16])
        self.assertEqual([error["message_id"] for error in result["errors"]], ["not a job id"] + JOBS[:len(failures)])
        text = json.dumps(result)
        self.assertNotIn("Traceback", text)
        self.assertNotIn("/secret", text)
        self.assertIn("gateway: not_found (HTTP 404)", text)

    def test_a_saved_batch_whose_message_the_gateway_purged_is_closed_not_retried(self):
        for lease_lost in (False, True):
            with self.subTest(lease_lost=lease_lost):
                self.state["received"] = {JOBS[0]: self.batch_receipt(JOBS[0], remote_finished=False,
                                                                      lease_lost=lease_lost)}
                self.home.save(self.state)
                engine_answers = lambda home_dir, *args, **_: {"jobs": []} if args[1] == "list" else {"ok": False}
                with mock.patch.object(plugin, "engine", side_effect=engine_answers), \
                        mock.patch.object(plugin, "owner_job_context", side_effect=gone()) as context:
                    code, result = self.run_main("inbox", "--no-pair-check")
                    self.assertEqual((code, result["errors"]), (0, []))
                    self.assertEqual(context.call_count, 1)
                    receipt = self.home.state()["received"][JOBS[0]]
                    self.assertTrue(receipt["remote_finished"] and receipt["gone_at_gateway"])
                    self.assertEqual([batch["message_id"] for batch in result["batches"]], [JOBS[0]])
                    code, result = self.run_main("inbox", "--no-pair-check")
                    self.assertEqual((code, result["errors"]), (0, []))
                    self.assertEqual(context.call_count, 1)          # never asked again

    def snapshot(self, job_id, sender=SENDER):
        plugin.C.write_private_json(self.home.claims / (job_id + ".json"),
                                    {"gateway": GATEWAY, "bot_id": BOT,
                                     "job": {"job_id": job_id, "from": {"principal_id": sender}}})

    def test_cached_claims_are_recovered_one_by_one_and_a_purged_one_is_reported_once(self):
        self.snapshot(JOBS[0])                       # the gateway no longer has it
        self.snapshot(JOBS[1])                       # the gateway answers with an error
        self.snapshot(JOBS[2])                       # completed: recovered
        (self.home.claims / "msg_not-a-job.json").write_text("{}")
        answers = {JOBS[0]: gone(), JOBS[1]: plugin.C.ApiError(503, "unavailable"),
                   JOBS[2]: {"state": "completed", "actor_id": SENDER}}

        def context(home, setup, job_id):
            if isinstance(answers[job_id], Exception):
                raise answers[job_id]
            return answers[job_id]
        state = self.home.state()
        with mock.patch.object(plugin, "owner_job_context", side_effect=context), \
                mock.patch.object(plugin, "receive_job") as receive:
            errors = plugin.recover_completed_cache(self.home, state, state["setup"], set())
            self.assertEqual(receive.call_args.args[3]["job_id"], JOBS[2])
            self.assertEqual(sorted(error["message_id"] for error in errors), [JOBS[0], JOBS[1], "msg_not-a-job"])
            closed = self.home.state()["received"][JOBS[0]]
            self.assertEqual((closed["ignored"], closed["remote_finished"]), ("gone_at_gateway", True))
            self.assertTrue((self.home.claims / (JOBS[0] + ".json")).exists())      # preserved, not replayed
            again = plugin.recover_completed_cache(self.home, self.home.state(), state["setup"], set())
        self.assertNotIn(JOBS[0], [error["message_id"] for error in again])
        self.assertIn(JOBS[1], [error["message_id"] for error in again])             # a 503 is tried again

    def recover(self, views, *arguments):
        conversations = [{"conversation_id": cid, "actor_id": SENDER, "turns": 3,
                          "created_at": "2026-11-0%dT00:00:00Z" % (index + 1)} for index, cid in enumerate(views)]

        def api_ok(base, method, path, body=None, token=None, timeout=15.0, **_):
            if path.endswith("/conversations"):
                return 200, {"ok": True, "conversations": conversations}
            view = views[path.rsplit("/", 1)[1]]
            if isinstance(view, Exception):
                raise view
            return 200, dict(view, ok=True)
        with mock.patch.object(plugin.C, "api_ok", side_effect=api_ok):
            return self.run_main("recover", *arguments)

    def view(self, cid, *job_ids):
        return {"conversation_id": cid, "actor_id": SENDER, "actor_e2e_key": KEY, "bot_id": BOT,
                "messages": [{"message_id": job_id, "author": "caller", "state": "completed",
                              "created_at": "2026-11-01T00:00:00Z", "content": {"sealed": {}}} for job_id in job_ids]}

    def test_recover_records_a_conversation_it_cannot_read_and_finishes_the_pass(self):
        broken, fine = "cnv_" + "a" * 16, "cnv_" + "b" * 16
        views = {broken: plugin.C.ApiError(500, "internal_error"), fine: self.view(fine, JOBS[0])}
        opened = {"text": "Mokutori 扫描 · 第 1–1 页\n第 1 页：ready", "attachments": []}
        with mock.patch.object(plugin, "open_kept_message", return_value=(opened, None)):
            code, result = self.recover(views, "--seconds", "600")
        self.assertEqual(code, 0, result)
        self.assertEqual(result["remaining_conversations"], 0)
        self.assertEqual(result["restored"], 1)
        device = result["devices"][0]
        self.assertEqual(device["conversations_failed"],
                         [{"conversation_id": broken, "error": "gateway: internal_error (HTTP 500)"}])
        self.assertNotIn("missing_pages", device)
        self.assertNotIn("Every batch", result["next"])
        self.assertIn("conversations_failed", result["next"])
        self.assertEqual([item.get("error") for item in result["checked_now"]],
                         [None, "gateway: internal_error (HTTP 500)"])
        # The next pass starts over and reads it once the gateway answers again.
        views[broken] = self.view(broken)
        with mock.patch.object(plugin, "open_kept_message", return_value=(opened, None)):
            code, result = self.recover(views, "--seconds", "600")
        self.assertEqual(result["devices"][0]["conversations_failed"], [])
        self.assertEqual(result["remaining_conversations"], 0)

    def test_named_recovery_continues_to_older_conversations(self):
        old, new = "cnv_" + "a" * 16, "cnv_" + "b" * 16
        views = {old: self.view(old, JOBS[0]), new: self.view(new)}
        opened = {"text": "saved page", "attachments": []}
        with mock.patch.object(plugin, "open_kept_message", return_value=(opened, None)):
            _, first = self.recover(views, JOBS[0], "--seconds", "0")
            self.assertEqual((first["remaining_conversations"], first["not_checked"], first["not_found"]),
                             (1, [JOBS[0]], []))
            _, second = self.recover(views, JOBS[0], "--seconds", "0")
            self.assertEqual((second["remaining_conversations"], second["not_found"], second["restored"]),
                             (0, [], 1))

    def test_recover_never_stays_on_the_same_failing_conversation(self):
        conversations = ["cnv_" + letter * 16 for letter in "abc"]
        views = {cid: plugin.C.ApiError(None, "unreachable") for cid in conversations}
        for expected in (2, 1, 0):
            code, result = self.recover(views, "--seconds", "0")
            self.assertEqual((code, result["remaining_conversations"]), (0, expected))
            self.assertEqual(len(result["checked_now"]), 1)

    def test_recover_notes_one_message_that_fails_and_saves_the_rest(self):
        cid = "cnv_" + "a" * 16
        views = {cid: self.view(cid, JOBS[0], JOBS[1], JOBS[2])}
        views[cid]["messages"].append({"message_id": "garbage", "author": "caller"})
        opened = {"text": "Mokutori 扫描 · 第 1–1 页", "attachments": []}

        def open_kept(get, reader, base, view, message, device):
            if message["message_id"] == JOBS[0]:
                raise plugin.C.ApiError(404, "not_found")
            if message["message_id"] == JOBS[1]:
                return {"text": "pages", "attachments": [{"name": "page.png", "data": "not base64!"}]}, None
            return opened, None
        with mock.patch.object(plugin, "open_kept_message", side_effect=open_kept):
            code, result = self.recover(views, "--seconds", "600")
        self.assertEqual((code, result["remaining_conversations"], result["restored"]), (0, 0, 1))
        device = result["devices"][0]
        self.assertEqual(device["restored"], [JOBS[2]])
        self.assertEqual([item["message_id"] for item in device["unreadable"]], [JOBS[0], JOBS[1], "garbage"])
        self.assertEqual(device["missing_here"], [item for item in device["missing_here"]
                                                  if item["message_id"] == JOBS[2]])

    def test_a_hanging_engine_is_a_clean_json_failure(self):
        with mock.patch.object(plugin.subprocess, "run",
                               side_effect=subprocess.TimeoutExpired(["python", "/private/home"], 120)):
            code, result = self.run_main("rename", "--name", "Ledger keeper")
        self.assertEqual((code, result["ok"]), (2, False))
        self.assertIn("did not answer", result["error"])
        self.assertNotIn("/private/home", json.dumps(result))
        with mock.patch.dict(plugin.__dict__, {"cmd_devices": mock.Mock(
                side_effect=subprocess.TimeoutExpired(["python", "/private/home"], 120))}):
            code, result = self.run_main("devices")
        self.assertEqual((code, result["ok"]), (2, False))
        self.assertNotIn("/private/home", json.dumps(result))


def data(raw):
    return base64.b64encode(raw).decode("ascii")


class ReceivedFiles(Fixture):
    """Bug 4: binary files are saved and never decoded; unusual names are renamed, not refused."""

    def save(self, attachments, folder=None):
        return plugin.save_batch(folder or self.home.inbox / JOBS[0], JOBS[0], {"name": "Study iPad"},
                                 "Mokutori 扫描 · 第 1–2 页", attachments, "2026-11-01T00:00:00Z")

    def test_every_media_type_the_engine_carries_is_saved_and_none_is_read_as_text(self):
        jpeg = (Path(__file__).parent / "synthetic-page.jpg").read_bytes()
        attachments = [{"name": "page-001.jpg", "data": data(jpeg)}, {"name": "page-001.md", "content": "# 第 1 页"},
                       {"name": "anim.gif", "data": data(GIF)}, {"name": "scan.pdf", "data": data(PDF)},
                       {"name": "memo.mp3", "data": data(MP3)}, {"name": "clip.mov", "data": data(b"\x00\x00\x00\x14ftypqt  ")},
                       {"name": "photo.heif", "data": data(b"\x00\x00\x00\x18ftypmif1")},
                       {"name": "photo.avif", "data": data(b"\x00\x00\x00\x18ftypavif")},
                       {"name": "blob", "data": data(b"\xff\xfe\x00binary")}, {"name": "notes", "content": "plain"}]
        batch = self.save(attachments)
        self.assertEqual([(item["name"], item["kind"]) for item in batch["files"]], [
            ("page-001.jpg", "image"), ("page-001.md", "text"), ("anim.gif", "image"), ("scan.pdf", "pdf"),
            ("memo.mp3", "audio"), ("clip.mov", "video"), ("photo.heif", "image"), ("photo.avif", "image"),
            ("blob", "binary"), ("notes", "text")])
        self.assertEqual(batch["ocr_markdown"], "# 第 1 页\n\nplain")
        self.assertEqual((self.home.inbox / JOBS[0] / "scan.pdf").read_bytes(), PDF)
        self.assertEqual((self.home.inbox / JOBS[0] / "anim.gif").read_bytes(), GIF)
        self.assertNotIn("sent_as", json.dumps(batch))
        self.assertEqual(self.save(attachments), batch)          # a replay finds the same files

    def test_names_the_engine_accepts_are_renamed_safely_and_message_txt_is_kept(self):
        attachments = [{"name": "message.txt", "content": "an attachment, not the summary"},
                       {"name": "領収書 2026.txt", "content": "合計 ￥1,280"},
                       {"name": "Scan page 1.JPG", "data": data(b"\xff\xd8\xff\xe0")},
                       {"name": "scan_page_1.jpg", "data": data(b"\xff\xd8\xff\xe1")},
                       {"name": "MESSAGE.TXT", "content": "another"},
                       {"name": "é" * 100 + ".pdf", "data": data(PDF)}]
        batch = self.save(attachments)
        names = [item["name"] for item in batch["files"]]
        self.assertEqual(names[:5], ["message-2.txt", "領収書_2026.txt", "Scan_page_1.JPG", "scan_page_1-2.jpg",
                                     "MESSAGE-3.TXT"])
        self.assertEqual([item["kind"] for item in batch["files"]], ["text", "text", "image", "image", "text", "pdf"])
        self.assertEqual([item.get("sent_as") for item in batch["files"]], [a["name"] for a in attachments])
        folder = self.home.inbox / JOBS[0]
        self.assertEqual((folder / "message.txt").read_text(encoding="utf-8"), "Mokutori 扫描 · 第 1–2 页")
        self.assertEqual((folder / "message-2.txt").read_text(encoding="utf-8"), "an attachment, not the summary")
        self.assertEqual(len({name.casefold() for name in names} | {"message.txt"}), len(names) + 1)
        for name in names:
            self.assertLessEqual(len(name), 100)
            self.assertLessEqual(len(name.encode("utf-8")), 200)
            self.assertEqual(Path(name).name, name)
            self.assertTrue((folder / name).is_file())
        self.assertEqual(sorted(path.name for path in folder.iterdir()), sorted(names + ["message.txt"]))
        self.assertEqual(self.save(attachments)["files"], batch["files"])

    def test_no_name_leaves_the_batch_folder(self):
        hostile = ["../../escape.txt", "/etc/passwd", "..", ".", "", ".hidden", "-rf", "a/b\\c.txt", "nul\x00.txt",
                   "tab\tname.txt", "\u202egnp.txt", "C:\\autoexec.txt", "~/.ssh/id", "message.txt/..", None, 7,
                   "x" * 5000 + ".txt"]
        batch = self.save([{"name": name, "content": "text %d" % index} for index, name in enumerate(hostile)])
        folder = (self.home.inbox / JOBS[0]).resolve()
        names = [item["name"] for item in batch["files"]]
        self.assertEqual(len(set(names)), len(hostile))
        for item in batch["files"]:
            self.assertRegex(item["name"], r"^[^\W_][\w.-]{0,99}\Z")
            self.assertEqual(Path(item["path"]).resolve().parent, folder)
        self.assertEqual(sorted(path.name for path in folder.iterdir()), sorted(names + ["message.txt"]))
        self.assertEqual(sorted(path.name for path in self.home.inbox.iterdir()), [JOBS[0]])

    def test_links_are_never_followed(self):
        folder = self.home.inbox / JOBS[0]
        folder.mkdir(parents=True)
        outside = Path(self.temp.name) / "outside.txt"
        outside.write_text("untouched")
        (folder / "page.txt").symlink_to(outside)
        with self.assertRaises(plugin.MokutoriError):
            self.save([{"name": "page.txt", "content": "new"}])
        self.assertEqual(outside.read_text(), "untouched")
        linked = self.home.inbox / JOBS[1]
        linked.symlink_to(Path(self.temp.name))
        with self.assertRaises(plugin.MokutoriError):
            self.save([{"name": "page.txt", "content": "new"}], folder=linked)

    def test_malformed_attachments_are_this_jobs_error(self):
        for attachments in ([{"name": "a.png", "data": "not base64!"}], [{"name": "a.txt", "content": 7}],
                            ["not an object"], "not a list", [{"name": "a.txt", "content": "\ud800"}]):
            with self.subTest(attachments=attachments), self.assertRaises(plugin.MokutoriError):
                self.save(attachments)

    def test_an_existing_file_that_differs_is_not_overwritten(self):
        self.save([{"name": "page.gif", "data": data(GIF)}])
        with self.assertRaises(plugin.MokutoriError):
            self.save([{"name": "page.gif", "data": data(GIF + b"changed")}])
        self.assertEqual((self.home.inbox / JOBS[0] / "page.gif").read_bytes(), GIF)


class ClaimsAndHosts(Fixture):
    """Bug 5: a claim cut short by a host's time limit is found again, and no claim starts that late."""

    def summary(self, job_id=JOBS[0]):
        return {"job_id": job_id, "from": {"principal_id": SENDER, "e2e_key": KEY}, "e2e": True, "claimable": True,
                "conversation_id": "cnv_" + "a" * 16, "created_at": "2026-11-01T00:00:00Z"}

    def test_the_job_is_noted_before_its_claim_and_the_note_goes_with_the_receipt(self):
        seen = {}

        def claim(home, setup, job_id):
            seen.update(self.home.state().get("claiming") or {})       # what a killed process would leave
            raise plugin.LostClaim("no answer")

        def owner_copy(home, state, setup, summary, device):
            state.setdefault("received", {})[summary["job_id"]] = self.batch_receipt(summary["job_id"])
            state.get("claiming", {}).pop(summary["job_id"], None)
            home.save(state)
        state = self.home.state()
        with mock.patch.object(plugin, "claim_job", side_effect=claim), \
                mock.patch.object(plugin, "receive_owner_copy", side_effect=owner_copy):
            plugin.receive_job(self.home, state, state["setup"], self.summary())
        self.assertEqual(seen[JOBS[0]]["sender"], SENDER)
        self.assertEqual(seen[JOBS[0]]["conversation_id"], "cnv_" + "a" * 16)
        self.assertEqual(self.home.state().get("claiming"), {})

    def test_a_refused_claim_leaves_no_note(self):
        state = self.home.state()
        with mock.patch.object(plugin, "claim_job", side_effect=plugin.MokutoriError("claim: not_claimable")):
            with self.assertRaises(plugin.MokutoriError):
                plugin.receive_job(self.home, state, state["setup"], self.summary())
        self.assertEqual(self.home.state().get("claiming"), {})

    def noted(self):
        self.state["claiming"] = {JOBS[0]: {"sender": SENDER, "conversation_id": "cnv_" + "a" * 16,
                                            "created_at": "2026-11-01T00:00:00Z", "since": "2026-11-01T00:00:01Z"}}
        self.home.save(self.state)
        return self.home.state()

    def test_inbox_saves_a_job_the_gateway_marked_read_after_a_claim_was_cut_short(self):
        self.noted()

        def owner_copy(home, state, setup, summary, device):
            self.assertEqual((summary["job_id"], summary["conversation_id"], summary["from"]["principal_id"]),
                             (JOBS[0], "cnv_" + "a" * 16, SENDER))
            state.setdefault("received", {})[JOBS[0]] = self.batch_receipt(JOBS[0], remote_finished=False,
                                                                           lease_lost=True)
            state.get("claiming", {}).pop(JOBS[0], None)
            home.save(state)
        with mock.patch.object(plugin, "engine", return_value={"jobs": []}), \
                mock.patch.object(plugin, "owner_job_context", return_value={"state": "completed", "actor_id": SENDER}), \
                mock.patch.object(plugin, "receive_owner_copy", side_effect=owner_copy):
            code, result = self.run_main("inbox", "--no-pair-check")
        self.assertEqual((code, result["errors"]), (0, []))
        self.assertEqual([batch["message_id"] for batch in result["batches"]], [JOBS[0]])
        saved = self.home.state()
        self.assertEqual(saved["claiming"], {})
        self.assertTrue(saved["received"][JOBS[0]]["remote_finished"])

    def test_a_noted_job_still_listed_or_pending_is_left_to_inbox(self):
        state = self.noted()
        with mock.patch.object(plugin, "owner_job_context") as context, \
                mock.patch.object(plugin, "receive_owner_copy") as owner_copy:
            self.assertEqual(plugin.recover_claiming(self.home, state, state["setup"], {JOBS[0]}), [])
            context.assert_not_called()
            context.return_value = {"state": "started", "actor_id": SENDER}
            self.assertEqual(plugin.recover_claiming(self.home, state, state["setup"], set()), [])
        owner_copy.assert_not_called()
        self.assertIn(JOBS[0], self.home.state()["claiming"])

    def test_a_noted_job_that_can_never_be_saved_is_reported_once(self):
        for context, copy_error in ((gone(), None), ({"state": "cancelled", "actor_id": SENDER}, None),
                                    ({"state": "completed", "actor_id": SENDER},
                                     plugin.MokutoriError("the gateway no longer keeps this message's content"))):
            with self.subTest(context=str(context)):
                state = self.noted()
                with mock.patch.object(plugin, "owner_job_context", side_effect=context
                                       if isinstance(context, Exception) else None, return_value=context), \
                        mock.patch.object(plugin, "receive_owner_copy", side_effect=copy_error):
                    errors = plugin.recover_claiming(self.home, state, state["setup"], set())
                    self.assertEqual([error["message_id"] for error in errors], [JOBS[0]])
                    self.assertEqual(self.home.state()["claiming"], {})
                    self.assertEqual(plugin.recover_claiming(self.home, self.home.state(), state["setup"], set()), [])

    def test_no_claim_starts_late_in_a_command(self):
        state = self.home.state()
        plugin.RUN["started"] = time.monotonic() - plugin.LAST_CLAIM_SECONDS - 1
        with mock.patch.object(plugin.B, "request") as request, self.assertRaises(plugin.MokutoriError) as refused:
            plugin.claim_job(self.home, state["setup"], JOBS[0])
        request.assert_not_called()
        self.assertNotIsInstance(refused.exception, plugin.LostClaim)
        # inbox counts such jobs as waiting instead of failing them.
        listing = {"jobs": [self.summary(JOBS[0]), self.summary(JOBS[1])]}
        with mock.patch.object(plugin, "engine", return_value=listing), \
                mock.patch.object(plugin, "receive_job") as receive, \
                mock.patch.object(plugin, "claim_time_left", return_value=False):
            code, result = self.run_main("inbox", "--no-pair-check")
        receive.assert_not_called()
        self.assertEqual((code, result["waiting_at_gateway"], result["errors"]), (0, 2, []))

    def test_a_host_that_waits_host_seconds_never_cuts_a_claim(self):
        longest_claim = max(plugin.CLAIM_TIMEOUT, plugin.C.MAX_TRANSFER_SECONDS)
        self.assertGreater(plugin.HOST_SECONDS, plugin.LAST_CLAIM_SECONDS + longest_claim)
        self.assertGreater(plugin.LAST_CLAIM_SECONDS, plugin.INBOX_SECONDS)


class PairingImage(Fixture):
    """Bug 7: the QR image carries the one-time code."""

    @unittest.skipUnless(os.name == "posix", "POSIX permissions")
    def test_the_qr_image_is_private_whatever_the_umask(self):
        previous = os.umask(0)
        self.addCleanup(os.umask, previous)
        target = Path(self.temp.name) / "pair.png"
        plugin.write_png(target, plugin.qr_matrix("mokutori://pair?v=1"))
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
        self.assertTrue(target.read_bytes().startswith(b"\x89PNG"))
        target.chmod(0o644)                                  # an older, readable image is replaced, not reused
        plugin.write_png(target, plugin.qr_matrix("mokutori://pair?v=1&again"))
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
        self.assertEqual([path.name for path in Path(self.temp.name).glob(".pair.png.*")], [])
        code, result = self.run_main("pair")
        self.assertEqual(code, 0, result)
        self.assertEqual(stat.S_IMODE(Path(result["qr_png"]).stat().st_mode), 0o600)


if __name__ == "__main__":
    unittest.main()
