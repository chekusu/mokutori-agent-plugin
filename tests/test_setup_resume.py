"""`setup` continues after a step failed or the process ended, and never saves a setup without the bot's key.

Synthetic state only: the engine CLI and the card request are replaced by recorders; no network, no real
credentials.
"""
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import mokutori as plugin

GATEWAY = "https://gateway.invalid"
BOT = "bot_abcdefghijklmnop"
KEY = plugin.E.generate()[1]
CARD = {"ok": True, "bot_id": BOT, "handle": "@study", "display_name": "Study assistant", "e2e": {"key": KEY}}


class Gateway:
    """What the engine CLI and the card request answer, and a record of what was asked."""

    def __init__(self, test):
        self.test, self.calls, self.failing, self.card = test, [], set(), CARD
        self.enrolled = {"ok": True, "bot_id": BOT}

    def engine(self, home_dir, *args, check=True):
        step = " ".join(args[:2]) if args[0] in ("bot", "worker") else args[0]
        self.calls.append((step, args))
        if step in self.failing:
            if not check:
                return {"ok": False, "error": "unavailable"}
            raise plugin.MokutoriError("%s: unavailable" % args[0])
        if step == "enroll-bot":
            return self.enrolled
        if step == "bot create":
            return {"ok": True, "bot_id": BOT}
        if step == "worker bind":
            self.test.assertFalse(Path(args[args.index("--token-file") + 1]).exists())
        return {"ok": True}

    def api_ok(self, base, method, path, body=None, token=None, timeout=15.0, accept=(200, 201, 202)):
        self.calls.append(("card", (path,)))
        if isinstance(self.card, Exception):
            raise self.card
        return 200, self.card

    def steps(self):
        steps = [step for step, _ in self.calls]
        del self.calls[:]
        return steps


class SetupResume(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = plugin.Home(Path(self.temp.name) / "data")
        self.gateway = Gateway(self)

    def run_main(self, *arguments):
        stream = io.StringIO()
        with mock.patch.object(plugin, "engine", side_effect=self.gateway.engine), \
                mock.patch.object(plugin.C, "api_ok", side_effect=self.gateway.api_ok), \
                redirect_stdout(stream), self.assertRaises(SystemExit) as done:
            plugin.main(["--home", str(self.home.path)] + list(arguments))
        return done.exception.code, json.loads(stream.getvalue())

    def setup(self, *extra):
        return self.run_main("setup", "--gateway", GATEWAY, "--name", "Study assistant", "--agent", "codex", *extra)

    def test_an_uninterrupted_setup_is_unchanged(self):
        code, result = self.setup()
        self.assertEqual((code, result["ok"]), (0, True))
        self.assertEqual(self.gateway.steps(), ["enroll-bot", "bot publish", "worker bind", "worker login", "card"])
        self.assertNotIn("resumed", result)
        saved = self.home.state()
        self.assertEqual((saved["setup"]["bot_id"], saved["setup"]["bot_key"], saved["setup"]["agent"]),
                         (BOT, KEY, "codex"))
        self.assertNotIn("setup_pending", saved)

    def test_setup_continues_from_the_step_that_failed_without_enrolling_again(self):
        # The retry is the same command, with or without --force.
        for failing, retry in (("worker bind", ()), ("worker login", ()), ("worker bind", ("--force",))):
            with self.subTest(failing=failing, retry=retry):
                self.home = plugin.Home(Path(self.temp.name) / (failing.replace(" ", "-") + "".join(retry)))
                self.gateway.failing = {failing}
                code, result = self.setup()
                self.assertEqual((code, result["ok"]), (2, False))
                self.assertIn("Run the same `setup` command again", result["error"])
                self.assertEqual(self.gateway.steps()[0], "enroll-bot")
                saved = self.home.state()
                self.assertNotIn("setup", saved)
                self.assertEqual(saved["setup_pending"]["bot_id"], BOT)
                self.assertFalse((self.home.path / "reader.token").exists())
                # `status` does not call a half-finished setup set up.
                self.assertIsNone(self.run_main("status")[1]["bot_id"])
                self.gateway.steps()
                self.gateway.failing = set()
                code, result = self.setup(*retry)
                self.assertEqual((code, result["ok"]), (0, True), result)
                self.assertEqual(self.gateway.steps(), ["bot publish", "worker bind", "worker login", "card"])
                self.assertTrue(result["resumed"])
                self.assertEqual((result["bot_id"], result["agent"]), (BOT, "codex"))
                saved = self.home.state()
                self.assertEqual(saved["setup"]["bot_key"], KEY)
                self.assertNotIn("setup_pending", saved)

    def test_the_binding_of_the_interrupted_run_is_replaced_and_a_finished_one_is_kept(self):
        self.gateway.failing = {"worker login"}
        self.setup()
        self.gateway.failing = set()
        self.gateway.card = plugin.C.ApiError(503, "unavailable")
        self.gateway.steps()
        self.setup()
        bind = next(args for step, args in self.gateway.calls if step == "worker bind")
        self.assertIn("--replace", bind)          # the first binding's credential never reached the reader
        self.gateway.steps()
        # The reader now holds its credential and key (the engine's `worker login` saves them): no third binding.
        reader = self.home.reader / "public" / "credentials.json"
        reader.parent.mkdir(parents=True, exist_ok=True)
        plugin.C.write_private_json(reader, {"v": 1, "gateways": {GATEWAY: {
            "workers": {BOT: {"token": "ybw_synthetic"}}, "bot_keys": {BOT: [{"public": KEY}]}}}})
        self.gateway.card = CARD
        code, result = self.setup()
        self.assertEqual((code, result["ok"]), (0, True))
        self.assertEqual(self.gateway.steps(), ["bot publish", "card"])

    def owner_credentials(self, entry):
        path = self.home.default_owner / "public" / "credentials.json"
        path.parent.mkdir(parents=True)
        plugin.C.write_private_json(path, {"v": 1, "gateways": {GATEWAY: entry}})

    def test_a_setup_ended_right_after_enrollment_is_found_in_the_owner_home(self):
        # The engine saved the credential; the process ended before Mokutori wrote anything.
        for entry, credential in (
                ({"owner": {"token": "ybo_x", "self_enrolled": True},
                  "sources": {BOT: {"token": "ybs_x"}}, "default_identity": BOT}, "owner"),
                ({"owner": {"token": "ybm_x", "self_enrolled": True, "scope_bot_id": BOT},
                  "sources": {BOT: {"token": "ybs_x"}}}, "manager")):
            with self.subTest(credential=credential):
                self.home = plugin.Home(Path(self.temp.name) / credential)
                self.owner_credentials(entry)
                code, result = self.setup()
                self.assertEqual((code, result["ok"], result["resumed"]), (0, True, True))
                self.assertEqual(self.gateway.steps(), ["bot publish", "worker bind", "worker login", "card"])
                self.assertEqual((result["bot_id"], result["credential"]), (BOT, credential))
                self.assertEqual(self.home.state()["setup"].get("credential"),
                                 "manager" if credential == "manager" else None)

    def test_a_credential_that_does_not_name_one_bot_is_not_taken_for_an_interrupted_setup(self):
        other = "bot_bbbbbbbbbbbbbbbb"
        entries = [{"owner": {"token": "ybo_x"}, "sources": {BOT: {}}},                          # owner login
                   {"owner": {"token": "ybo_x", "self_enrolled": True}, "sources": {BOT: {}, other: {}}},
                   {"owner": {"token": "ybo_x", "self_enrolled": True}, "sources": {}},
                   {"pending_bot": {"owner_token": "ybo_x"}},                                    # answer still unknown
                   {"owner": {"token": "ybo_x", "self_enrolled": True}, "sources": {"../bot": {}}}]
        for index, entry in enumerate(entries):
            with self.subTest(entry=entry):
                self.home = plugin.Home(Path(self.temp.name) / str(index))
                self.owner_credentials(entry)
                self.setup()
                self.assertEqual(self.gateway.steps()[0], "enroll-bot")

    def test_the_email_step_is_not_sent_again_for_a_bot_that_already_exists(self):
        self.gateway.failing = {"worker bind"}
        self.setup("--email-code", "123456")
        self.gateway.failing = set()
        self.gateway.steps()
        code, result = self.setup("--email", "owner@example.invalid")
        self.assertEqual((code, result["ok"], result["resumed"]), (0, True, True))
        self.assertNotIn("enroll-bot", self.gateway.steps())

    def test_setup_fails_and_stays_resumable_when_the_card_or_its_key_cannot_be_read(self):
        cards = (plugin.C.ApiError(404, "not_found"), plugin.C.ApiError(429, "rate_limited"),
                 plugin.C.ApiError(502, "http_502"), plugin.C.ApiError(None, "unreachable"),
                 dict(CARD, e2e=None), dict(CARD, e2e={"key": None}), dict(CARD, e2e={"key": "not a key"}),
                 dict(CARD, e2e={"key": plugin.b64u(bytes(32))}), dict(CARD, bot_id="bot_bbbbbbbbbbbbbbbb"),
                 {"ok": True})
        for card in cards:
            with self.subTest(card=str(card)):
                self.gateway.card = card
                code, result = self.setup()
                self.assertEqual((code, result["ok"]), (2, False))
                self.assertIn("card", result["error"])
                saved = self.home.state()
                self.assertNotIn("setup", saved)
                self.assertEqual(saved["setup_pending"]["bot_id"], BOT)
                self.assertEqual(self.run_main("pair")[0], 2)            # no code without a finished setup
        self.gateway.steps()
        self.gateway.card = CARD
        code, result = self.setup()
        self.assertEqual((code, result["ok"], result["resumed"]), (0, True, True))
        self.assertNotIn("enroll-bot", self.gateway.steps())
        self.assertEqual(self.home.state()["setup"]["bot_key"], KEY)

    def test_a_publish_failure_is_named_when_the_card_is_then_missing(self):
        self.gateway.failing = {"bot publish"}
        self.gateway.card = plugin.C.ApiError(404, "not_found")
        code, result = self.setup()
        self.assertEqual(code, 2)
        self.assertIn("publishing the bot failed", result["error"])
        self.assertNotIn("setup", self.home.state())
        # A bot that is published all the same (its card is readable) is a finished setup.
        self.gateway.card = CARD
        self.assertEqual(self.setup()[0], 0)

    def test_setup_with_an_existing_owner_home_keeps_its_place_too(self):
        owner = Path(self.temp.name) / "yobiru"
        (owner / "public").mkdir(parents=True)
        token = plugin.C.new_token("owner")
        plugin.C.write_private_json(owner / "public" / "credentials.json",
                                    {"v": 1, "gateways": {GATEWAY: {"owner": {"token": token}}}})
        self.gateway.failing = {"worker bind"}
        self.assertEqual(self.setup("--owner-home", str(owner))[0], 2)
        self.assertEqual(self.gateway.steps()[0], "bot create")
        self.gateway.failing = set()
        code, result = self.setup("--owner-home", str(owner))
        self.assertEqual((code, result["ok"], result["resumed"]), (0, True, True))
        self.assertNotIn("bot create", self.gateway.steps())
        self.assertEqual(self.home.state()["setup"]["owner_home"], str(owner))


class PairNeedsTheBotKey(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = plugin.Home(Path(self.temp.name) / "data")
        # What 0.3.6 saved, with ok:true, when the card request answered with an error.
        self.home.save({"v": 1, "setup": {"gateway": GATEWAY, "bot_id": BOT, "handle": None, "name": None,
                                         "bot_key": None, "agent": "codex"}, "devices": {}, "pairings": {}})

    def pair(self, card):
        stream = io.StringIO()
        answer = mock.Mock(side_effect=card) if isinstance(card, Exception) else mock.Mock(return_value=(200, card))
        with mock.patch.object(plugin.C, "api_ok", answer), redirect_stdout(stream), \
                self.assertRaises(SystemExit) as done:
            plugin.main(["--home", str(self.home.path), "pair"])
        return done.exception.code, json.loads(stream.getvalue())

    def test_pair_refuses_a_code_without_a_usable_bot_key(self):
        for card in (plugin.C.ApiError(404, "not_found"), dict(CARD, e2e={"key": "None"})):
            with self.subTest(card=str(card)):
                code, result = self.pair(card)
                self.assertEqual((code, result["ok"]), (2, False))
                self.assertIn("no pairing code", result["error"])
                self.assertNotIn("pair_url", result)
                self.assertEqual(self.home.state()["pairings"], {})
                self.assertEqual(list(self.home.path.glob("pair-*.png")), [])

    def test_pair_reads_the_key_once_the_card_is_there(self):
        code, result = self.pair(CARD)
        self.assertEqual((code, result["ok"]), (0, True))
        self.assertIn("k=" + KEY, result["pair_url"])
        self.assertNotIn("k=None", result["pair_url"] + result["pair_link"])
        saved = self.home.state()["setup"]
        self.assertEqual((saved["bot_key"], saved["handle"], saved["name"]), (KEY, "@study", "Study assistant"))


if __name__ == "__main__":
    unittest.main()
