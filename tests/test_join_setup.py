"""A second agent of the same owner sets up with the owner's email (Yobiru 0.7.20 join).

The official gateway lets one email address register one owner. When the owner's address already owns a bot (a
Grok Bot set up first), `setup --email-code` on a new agent (Muse) adds a new bot to that owner with the same code,
and the new data home keeps only a manager credential for that one bot: never the owner credential. Every later
command that needs administration (pair-accept, pair-decline, rename, wake, unpair, recovery) uses that manager
credential, and none of them can reach the first agent's bot.

A real local Friends gateway with email verification on (from $YOBIRU_SOURCE or ../yobiru, pinned like
test_mokutori_plugin); the code is read from the gateway's outbox, so no mail is sent. Synthetic identities only.
"""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

PLUGIN = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN / "tests"))
import app_protocol_reference as app  # noqa: E402
from test_mokutori_plugin import YOBIRU, free_port  # noqa: E402

EMAIL = "synthetic.owner+agents@example.com"


@unittest.skipIf(YOBIRU is None, "needs a Yobiru source checkout for the local gateway (set YOBIRU_SOURCE)")
class JoinSetup(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        pin = json.loads((PLUGIN / "scripts" / "engine" / "SOURCE.json").read_text())
        digest = hashlib.sha256((YOBIRU / pin["test_gateway"]["path"]).read_bytes()).hexdigest()
        if digest != pin["test_gateway"]["sha256"]:
            raise RuntimeError("test gateway differs from the pinned Yobiru source; use tests/run_e2e.py")
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        gateway = [sys.executable, str(YOBIRU / "public_bots" / "bot_gateway.py"), "--gateway-home",
                   str(cls.root / "gw")]
        subprocess.run(gateway + ["init", "--port", str(free_port())], check=True, capture_output=True)
        config = cls.root / "gw" / "gateway.json"
        settings = json.loads(config.read_text())
        settings["enrollment"] = {"visitors": True, "bots": True, "self_enrolled_publish": True,
                                  "email_verification": True}
        settings.setdefault("limits", {}).update(bot_enroll_per_hour_per_ip=100, enroll_per_hour_per_ip=100,
                                                 email_codes_per_hour_per_ip=100,
                                                 email_codes_per_hour_per_address=100,
                                                 hosted_wake_private_hosts=1)
        config.write_text(json.dumps(settings))
        os.chmod(str(config), 0o600)
        cls.server = subprocess.Popen(gateway + ["serve"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                      text=True)
        cls.url = json.loads(cls.server.stdout.readline())["listening"]
        cls.grok = cls.root / "grok"
        cls.muse = cls.root / "muse"
        cls.set_up(cls.grok, "Grok 账本管家", "grok")
        cls.muse_setup = cls.set_up(cls.muse, "Muse 扫描", "muse")

    @classmethod
    def tearDownClass(cls):
        cls.server.terminate()
        cls.server.wait(timeout=10)
        cls.server.stdout.close()
        cls.temp.cleanup()

    @classmethod
    def run_mokutori(cls, home, *args):
        process = subprocess.run([sys.executable, str(PLUGIN / "scripts" / "mokutori.py"), "--home", str(home)]
                                 + list(args), capture_output=True, text=True, timeout=180)
        return json.loads(process.stdout) if process.stdout.strip() else {"ok": False, "stderr": process.stderr}

    @classmethod
    def code(cls):
        db = sqlite3.connect(str(cls.root / "gw" / "gateway.sqlite3"))
        try:
            return db.execute("SELECT code FROM mail_outbox WHERE code IS NOT NULL ORDER BY created_ts DESC, rowid "
                              "DESC LIMIT 1").fetchone()[0]
        finally:
            db.close()

    @classmethod
    def set_up(cls, home, name, agent):
        """The documented two steps, with the same email for both agents."""
        sent = cls.run_mokutori(home, "setup", "--gateway", cls.url, "--email", EMAIL, "--name", name,
                                "--agent", agent)
        assert sent.get("step") == "email_sent", sent
        done = cls.run_mokutori(home, "setup", "--gateway", cls.url, "--email-code", cls.code(), "--name", name,
                                "--agent", agent)
        assert done.get("ok"), done
        return done

    def mokutori(self, *args, home=None, expect_ok=True):
        result = self.run_mokutori(home or self.muse, *args)
        if expect_ok:
            self.assertTrue(result.get("ok"), result)
        return result

    def credentials(self, home):
        return json.loads((home / "owner" / "public" / "credentials.json").read_text())["gateways"][self.url]

    def grok_bot(self):
        return self.mokutori("status", home=self.grok)["bot_id"]

    def test_the_second_agent_joins_the_owner_with_a_bot_credential_only(self):
        grok, muse = self.mokutori("status", home=self.grok), self.mokutori("status")
        self.assertEqual("owner", grok["credential"])
        self.assertEqual("manager", muse["credential"])
        self.assertNotEqual(grok["bot_id"], muse["bot_id"])
        owner = self.credentials(self.muse)["owner"]
        self.assertTrue(owner["token"].startswith("ybm_"), "a manager credential, never the owner credential")
        self.assertEqual(muse["bot_id"], owner["scope_bot_id"])
        self.assertEqual(self.credentials(self.grok)["owner"]["owner_id"], owner["owner_id"], "the same owner")
        for path in self.muse.rglob("*"):
            if path.is_file():
                self.assertNotIn(b"ybo_", path.read_bytes(), path)
        self.assertNotIn(EMAIL, json.dumps(self.muse_setup))

    def test_pairing_pages_and_owner_commands_run_on_the_manager_credential(self):
        pairing = self.mokutori("pair")
        ipad = app.App()
        status, receipt = ipad.pair(pairing["pair_url"])
        self.assertEqual(202, status, receipt)
        requests = self.mokutori("pair-check")["pair_requests"]
        self.assertEqual(1, len(requests), requests)
        paired = self.mokutori("pair-accept", requests[0]["pairing_id"])["paired"]
        self.assertEqual(1, len(paired))
        jpeg = (Path(__file__).parent / "synthetic-page.jpg").read_bytes()
        _, attachments, text = app.pack([{"number": 1, "id": "00000001-0000-4000-8000-000000000000",
                                          "status": "ready", "warnings": [], "text": "第 1 页", "jpeg": jpeg}])
        status, sent = ipad.message(text, attachments)
        self.assertEqual(202, status, sent)
        batches = self.mokutori("inbox")["batches"]
        self.assertEqual(1, len(batches), batches)
        self.mokutori("rename", "--name", "Muse 书房")
        card = app.call(self.url, "GET", "/@" + self.mokutori("status")["bot_id"])[1]
        self.assertEqual("Muse 书房", card["display_name"])
        files = {}
        for name, value in (("url", "http://127.0.0.1:9/routine"), ("key", "synthetic-routine-key-0123")):
            path = self.root / ("wake-" + name)
            path.write_text(value + "\n")
            os.chmod(str(path), 0o600)
            files[name] = str(path)
        wake = self.mokutori("wake", "set", "--url-file", files["url"], "--secret-file", files["key"], "--no-test")
        self.assertTrue(wake["wake"]["configured"])
        self.assertTrue(self.mokutori("wake", "status")["wake"]["configured"])
        self.mokutori("wake", "clear")
        actor = paired[0]["actor_id"]
        self.mokutori("unpair", actor)
        self.assertNotIn(actor, [d["actor_id"] for d in self.mokutori("devices")["devices"]])

    def test_a_declined_request_ends_at_the_gateway(self):
        pairing = self.mokutori("pair")
        ipad = app.App()
        status, receipt = ipad.pair(pairing["pair_url"])
        self.assertEqual(202, status, receipt)
        requests = self.mokutori("pair-check")["pair_requests"]
        self.mokutori("pair-decline", requests[0]["pairing_id"])
        message = app.call(self.url, "GET", "/v1/messages/" + receipt["message_id"], token=ipad.state["token"])[1]
        self.assertEqual("declined", message["state"], message)

    def test_the_bot_credential_cannot_reach_the_first_agents_bot(self):
        engine = PLUGIN / "scripts" / "engine" / "bot_client.py"
        grok_bot = self.grok_bot()
        for args in (["bot", "edit", grok_bot, "--name", "taken"],
                     ["owner", "contacts", grok_bot],
                     ["bot", "create", "--handle", "mokutori-extra", "--name", "Extra"]):
            process = subprocess.run([sys.executable, str(engine), "--home", str(self.muse / "owner")] + args
                                     + ["--gateway", self.url], capture_output=True, text=True, timeout=60)
            self.assertNotEqual(0, process.returncode, (args, process.stdout))
        bots = subprocess.run([sys.executable, str(engine), "--home", str(self.muse / "owner"), "owner", "bots",
                               "--gateway", self.url], capture_output=True, text=True, timeout=60)
        self.assertEqual([self.mokutori("status")["bot_id"]], [b["bot_id"] for b in json.loads(bots.stdout)["bots"]])
        self.assertEqual("Grok 账本管家", app.call(self.url, "GET", "/@" + grok_bot)[1]["display_name"])


if __name__ == "__main__":
    unittest.main()
