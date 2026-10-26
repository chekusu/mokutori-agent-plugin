"""End-to-end test of the Mokutori agent plugin against a real local Friends gateway.

The gateway comes from a Yobiru source checkout ($YOBIRU_SOURCE, default ../yobiru next to this repository or
/home/user/chekusu/yobiru); the test is skipped without one. The iPad side is the Python reference of the app
protocol in tests/app_protocol_reference.py.
"""
import json
import base64
from contextlib import closing
import hashlib
import importlib.util
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[1]
# Keep the public test suite self-contained: the app-side reference is copied
# into this repository so a fresh clone never needs the private iPad checkout.
sys.path.insert(0, str(PLUGIN / "tests"))
import app_protocol_reference as app  # noqa: E402

spec = importlib.util.spec_from_file_location("mokutori_plugin", PLUGIN / "scripts" / "mokutori.py")
plugin = importlib.util.module_from_spec(spec)
spec.loader.exec_module(plugin)

REPO = PLUGIN
CANDIDATES = [os.environ.get("YOBIRU_SOURCE"), str(REPO.parent / "yobiru"), "/home/user/chekusu/yobiru"]
YOBIRU = next((Path(c) for c in CANDIDATES if c and (Path(c) / "public_bots" / "bot_gateway.py").exists()), None)


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@unittest.skipIf(YOBIRU is None, "needs a Yobiru source checkout for the local gateway (set YOBIRU_SOURCE)")
class PluginFlow(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        pin = json.loads((PLUGIN / "scripts" / "engine" / "SOURCE.json").read_text())
        digest = hashlib.sha256((YOBIRU / pin["test_gateway"]["path"]).read_bytes()).hexdigest()
        if digest != pin["test_gateway"]["sha256"]:
            raise RuntimeError("test gateway differs from the pinned Yobiru source; use tests/run_e2e.py")
        cls.temp = tempfile.TemporaryDirectory()
        root = Path(cls.temp.name)
        port = free_port()
        gateway = [sys.executable, str(YOBIRU / "public_bots" / "bot_gateway.py"), "--gateway-home", str(root / "gw")]
        subprocess.run(gateway + ["init", "--port", str(port)], check=True, capture_output=True)
        config = root / "gw" / "gateway.json"
        settings = json.loads(config.read_text())
        settings.setdefault("enrollment", {})["self_enrolled_publish"] = True
        # This loopback-only fixture creates independent synthetic identities for every test.
        settings.setdefault("limits", {}).update(bot_enroll_per_hour_per_ip=100, enroll_per_hour_per_ip=100)
        config.write_text(json.dumps(settings))
        cls.server = subprocess.Popen(gateway + ["serve"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        cls.url = json.loads(cls.server.stdout.readline())["listening"]
        cls.root = root

    def setUp(self):
        self.home = self.root / self._testMethodName

    def setup_agent(self, agent="other"):
        return self.mokutori("setup", "--gateway", self.url,
                            "--handle", "mokutori-" + hashlib.sha256(self._testMethodName.encode()).hexdigest()[:8],
                            "--name", "Mokutori", "--agent", agent)

    @classmethod
    def tearDownClass(cls):
        cls.server.terminate()
        cls.server.wait(timeout=10)
        cls.server.stdout.close()
        cls.temp.cleanup()

    def mokutori(self, *args, expect_ok=True):
        process = subprocess.run([sys.executable, str(PLUGIN / "scripts" / "mokutori.py"), "--home", str(self.home)]
                                 + list(args), capture_output=True, text=True, timeout=180)
        self.assertTrue(process.stdout.strip(), process.stderr)
        result = json.loads(process.stdout)
        if expect_ok:
            self.assertTrue(result.get("ok"), result)
        return result

    def pages(self, count, start=1):
        # Downsampled from the repository's synthetic-flat.png, never a real user's scan.
        jpeg = (Path(__file__).parent / "synthetic-page.jpg").read_bytes()
        return [{"number": start + i, "id": "%08d-0000-4000-8000-000000000000" % (start + i), "status": "ready",
                 "warnings": [] if i else ["lowContrast"], "text": "第 %d 页 領収書 合計 ￥1,280" % (start + i),
                 "jpeg": jpeg} for i in range(count)]

    def paired_device(self, agent="other"):
        setup = self.setup_agent(agent)
        pairing = self.mokutori("pair")
        ipad = app.App()
        status, receipt = ipad.pair(pairing["pair_url"])
        self.assertEqual(status, 202, receipt)
        paired = self.mokutori("pair-check")
        self.assertEqual(len(paired["paired"]), 1, paired)
        return setup, ipad, receipt

    def status_of(self, ipad, message_id):
        status, result = app.call(self.url, "GET", "/v1/messages/" + message_id, token=ipad.state["token"])
        self.assertEqual(status, 200, result)
        return result

    def send_page(self, ipad):
        _, attachments, text = app.pack(self.pages(1))
        status, receipt = ipad.message(text, attachments)
        self.assertEqual(status, 202, receipt)
        return receipt["message_id"]

    def test_pair_send_and_receive(self):
        setup = self.setup_agent()
        self.assertTrue(setup["bot_key_fingerprint"])
        pairing = self.mokutori("pair", "--terminal")
        self.assertTrue(pairing["pair_url"].startswith("mokutori://pair?"))
        self.assertTrue(Path(pairing["qr_png"]).read_bytes().startswith(b"\x89PNG"))
        self.assertIn("█", pairing["qr_text"])

        ipad = app.App()
        status, receipt = ipad.pair(pairing["pair_url"], "书房 iPad")
        self.assertEqual(status, 202, receipt)
        self.assertEqual(self.mokutori("pair-check")["paired"][0]["actor_id"], ipad.state["actor_id"])

        inbox = self.mokutori("inbox")
        self.assertEqual(inbox["batches"], [], inbox)
        self.assertEqual(inbox["errors"], [], inbox)
        self.assertEqual(self.status_of(ipad, receipt["message_id"])["code"], "read")

        # The same QR code cannot pair a second device, and a forged proof is ignored.
        intruder = app.App()
        status, _ = intruder.pair(pairing["pair_url"], "intruder")
        self.assertEqual(status, 202)
        forged = app.App()
        code = app.parse_pairing(self.mokutori("pair")["pair_url"])
        forged.state = dict(intruder.state)
        forged.state.pop("conversation_id", None)
        forged.message("%s %s %s" % (app.PAIR_PREFIX, code["pairing_id"], "A" * 43), [])
        self.assertEqual(self.mokutori("pair-check")["paired"], [])
        self.assertEqual(len(self.mokutori("devices")["devices"]), 1)

        chosen, attachments, text = app.pack(self.pages(3))
        self.assertEqual(len(chosen), 3)
        status, receipt = ipad.message(text, attachments)
        self.assertEqual(status, 202, receipt)
        status, duplicate = ipad.retry_last_message()
        self.assertEqual(status, 200, duplicate)
        self.assertTrue(duplicate["duplicate"])
        self.assertEqual(duplicate["message_id"], receipt["message_id"])
        status, _ = intruder.message("not paired", [])
        self.assertEqual(status, 202)

        inbox = self.mokutori("inbox")
        self.assertEqual(len(inbox["batches"]), 1, inbox)
        batch = inbox["batches"][0]
        self.assertEqual(batch["from_device"], "书房 iPad")
        self.assertIn("第 1–3 页", batch["summary"])
        self.assertIn("領収書", batch["ocr_markdown"])
        images = [f for f in batch["files"] if f["kind"] == "image"]
        self.assertEqual(len(images), 3)
        self.assertEqual(Path(images[0]["path"]).read_bytes(), chosen[0]["jpeg"])
        self.assertEqual(oct(Path(images[0]["path"]).stat().st_mode & 0o777), "0o600")
        self.assertEqual(self.status_of(ipad, receipt["message_id"])["code"], "read")
        before = Path(images[0]["path"]).stat().st_mtime_ns
        self.assertEqual(self.mokutori("inbox")["batches"], inbox["batches"])
        self.assertEqual(Path(images[0]["path"]).stat().st_mtime_ns, before)
        self.assertGreaterEqual(inbox["ignored_from_unpaired_senders"], 1)  # unpaired jobs remain unclaimed

        self.assertEqual(len(self.mokutori("inbox", "--ack")["batches"]), 1)
        self.assertEqual(self.mokutori("inbox")["batches"], [])

        self.mokutori("unpair", ipad.state["actor_id"])
        status, _ = ipad.message("after unpair", [])
        self.assertGreaterEqual(status, 400)

    def test_upgrade_reuses_external_owner_for_pair_receive_recovery_and_unpair(self):
        setup = self.setup_agent()
        home = plugin.Home(self.home)
        external = self.root / (self._testMethodName + "-yobiru")
        home.owner.rename(external)  # Move synthetic credentials, never duplicate them.
        original_credentials = json.loads((external / "public" / "credentials.json").read_text())
        state = home.state()
        state["setup"]["owner_home"] = str(external)
        state["pairings"]["0123456789ab"] = {"used_by": "actor_legacy", "expires_at": "2026-10-25T00:00:00Z"}
        home.save(state)
        status = self.mokutori("status")
        self.assertEqual(status["owner_home"], str(external))
        self.assertEqual(status["bot_id"], setup["bot_id"])
        self.assertEqual(status["handle"], setup["handle"])
        code = self.mokutori("pair")
        ipad = app.App()
        _, receipt = ipad.pair(code["pair_url"])
        self.assertEqual(self.mokutori("pair-check")["paired"][0]["actor_id"], ipad.state["actor_id"])
        # This direct metadata query exercises the non-CLI owner credential path.
        self.assertEqual(plugin.owner_job_context(home, home.state()["setup"], receipt["message_id"])["state"], "completed")
        message_id = self.send_page(ipad)
        inbox = self.mokutori("inbox")
        self.assertEqual(len(inbox["batches"]), 1)
        self.assertEqual(inbox["errors"], [])
        self.assertEqual(self.status_of(ipad, message_id)["code"], "read")
        self.mokutori("inbox", "--ack")
        self.assertEqual(self.mokutori("inbox")["batches"], [])
        self.mokutori("unpair", ipad.state["actor_id"])
        # The engine may pin newly seen peer keys in this file, but the original
        # owner identity and credentials must remain the same.
        current_credentials = json.loads((external / "public" / "credentials.json").read_text())
        self.assertEqual(current_credentials["gateways"][self.url]["owner"],
                         original_credentials["gateways"][self.url]["owner"])
        self.assertFalse((home.default_owner / "public" / "credentials.json").exists())
        self.assertEqual(home.state()["pairings"]["0123456789ab"], state["pairings"]["0123456789ab"])

    def test_new_setup_can_reuse_an_existing_owner_without_email_or_copy(self):
        original = self.setup_agent()
        owner = self.home / "owner"
        self.home = self.root / (self._testMethodName + "-second")
        setup = self.mokutori("setup", "--gateway", self.url, "--owner-home", str(owner), "--agent", "grok")
        self.assertNotEqual(setup["bot_id"], original["bot_id"])
        self.assertEqual(setup["owner_home"], str(owner))
        self.assertFalse((self.home / "owner" / "public" / "credentials.json").exists())
        self.assertTrue(self.mokutori("setup", "--owner-home", str(owner))["already_set_up"])
        code = self.mokutori("pair")
        ipad = app.App()
        ipad.pair(code["pair_url"])
        self.assertEqual(len(self.mokutori("pair-check")["paired"]), 1)

    def test_muse_receives_image_and_acknowledges_without_deleting(self):
        sys.path.insert(0, str(PLUGIN))
        from muse_connector import MCPServer, Service

        _, ipad, _ = self.paired_device(agent="muse")
        message_id = self.send_page(ipad)
        server = MCPServer(Service(self.home), "synthetic-test-key-not-a-production-key")
        server.dispatch({"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {}})
        server.dispatch({"jsonrpc": "2.0", "method": "notifications/initialized"})

        def call_tool(name, arguments):
            response = server.dispatch({"jsonrpc": "2.0", "id": name, "method": "tools/call",
                                        "params": {"name": name, "arguments": arguments}})
            self.assertNotIn("error", response)
            self.assertFalse(response["result"]["isError"], response)
            return response["result"]

        inbox = call_tool("mokutori_list_scans", {"limit": 1})
        self.assertNotIn(str(self.home), json.dumps(inbox))
        self.assertEqual(inbox["structuredContent"]["ingestion_error_count"], 0)
        batch = inbox["structuredContent"]["batches"][0]
        self.assertEqual(batch["notice_id"], message_id)
        self.assertIn("領収書", batch["ocr_markdown"])
        image_file = next(item for item in batch["files"] if item["kind"] == "image")
        image = call_tool("mokutori_get_scan_image", {"notice_id": message_id, "name": image_file["name"]})
        self.assertEqual(base64.b64decode(image["content"][1]["data"]), self.pages(1)[0]["jpeg"])
        ack = call_tool("mokutori_acknowledge_scans", {"notice_ids": [message_id]})
        self.assertEqual(ack["structuredContent"]["acknowledged"], [message_id])
        self.assertEqual(call_tool("mokutori_list_scans", {})["structuredContent"]["batches"], [])
        self.assertEqual(call_tool("mokutori_get_scan_image", {"notice_id": message_id,
                                                               "name": image_file["name"]})["content"], image["content"])
        self.assertEqual(self.status_of(ipad, message_id)["code"], "read")

    def test_resume_saved_claim_uses_start_without_reclaim(self):
        setup, ipad, _ = self.paired_device()
        message_id = self.send_page(ipad)
        home = plugin.Home(self.home)
        state = home.state()
        setup = state["setup"]
        plugin.reader_job(home, setup, "claim", message_id, "--lease-seconds", "600")
        summary = next(item for item in plugin.reader_job(home, setup, "list")["jobs"] if item["job_id"] == message_id)
        with mock.patch.object(plugin, "reader_job", wraps=plugin.reader_job) as gateway:
            plugin.receive_job(home, state, setup, summary)
            plugin.finish_received(home, state, setup, message_id)
        operations = [item.args[2] for item in gateway.call_args_list]
        self.assertIn("heartbeat", operations)  # /start revalidates the saved lease
        self.assertNotIn("claim", operations)
        self.assertEqual(self.status_of(ipad, message_id)["code"], "read")
        self.assertEqual(len(self.mokutori("inbox")["batches"]), 1)

    def test_finish_recovery_before_and_after_lost_local_receipt(self):
        _, ipad, _ = self.paired_device()
        for remotely_finished in (False, True):
            with self.subTest(remotely_finished=remotely_finished):
                message_id = self.send_page(ipad)
                home = plugin.Home(self.home)
                state = home.state()
                setup = state["setup"]
                summary = next(item for item in plugin.reader_job(home, setup, "list")["jobs"] if item["job_id"] == message_id)
                plugin.receive_job(home, state, setup, summary)
                before = state["received"][message_id]["batch"]
                if remotely_finished:
                    plugin.reader_job(home, setup, "read", message_id)
                    # The process died before its receipt recorded that finish succeeded.
                result = self.mokutori("inbox")
                self.assertEqual(result["errors"], [], result)
                self.assertEqual(result["batches"], [before])
                self.assertEqual(self.status_of(ipad, message_id)["code"], "read")
                self.assertTrue(home.state()["received"][message_id]["remote_finished"])
                self.mokutori("inbox", "--ack")
                self.assertEqual(self.mokutori("inbox", "--ack")["batches"], [])

    def test_verified_pairing_resumes_after_admission_interruption(self):
        self.setup_agent()
        pairing = self.mokutori("pair")
        ipad = app.App()
        _, receipt = ipad.pair(pairing["pair_url"])
        original = plugin.engine
        def interrupt_accept(home_dir, *args, **kwargs):
            if args[:2] == ("review", "accept"):
                raise plugin.MokutoriError("simulated interruption before admission")
            return original(home_dir, *args, **kwargs)
        home = plugin.Home(self.home)
        with mock.patch.object(plugin, "engine", side_effect=interrupt_accept):
            with self.assertRaises(plugin.MokutoriError):
                plugin.check_pairings(home)
        self.assertEqual(home.state()["devices"], {})
        intruder = app.App()
        intruder.pair(pairing["pair_url"])
        result = self.mokutori("pair-check")
        self.assertEqual([device["actor_id"] for device in result["paired"]], [ipad.state["actor_id"]])
        self.assertEqual(self.status_of(ipad, receipt["message_id"])["code"], "read")
        self.assertEqual(len(self.mokutori("devices")["devices"]), 1)

    def test_unpair_cancels_unreceived_pages(self):
        _, ipad, _ = self.paired_device()
        self.send_page(ipad)
        self.mokutori("unpair", ipad.state["actor_id"])
        result = self.mokutori("inbox")
        self.assertEqual(result["batches"], [], result)
        self.assertEqual(list((self.home / "inbox").iterdir()), [])
        status, _ = ipad.message("new pages after disconnect", [])
        self.assertGreaterEqual(status, 400)
        self.assertEqual(self.mokutori("pair-check")["paired"], [])

    def test_expired_pairing_proof_cache_is_finished(self):
        self.setup_agent()
        pairing = self.mokutori("pair")
        ipad = app.App()
        _, receipt = ipad.pair(pairing["pair_url"])
        home = plugin.Home(self.home)
        def interrupt_after_claim(home, state, setup, summary, **kwargs):
            plugin.reader_job(home, setup, "claim", summary["job_id"], "--lease-seconds", "600")
            raise plugin.MokutoriError("simulated process exit after proof claim")
        with mock.patch.object(plugin, "receive_job", side_effect=interrupt_after_claim):
            with self.assertRaises(plugin.MokutoriError):
                plugin.check_pairings(home)
        with closing(sqlite3.connect(str(self.root / "gw" / "gateway.sqlite3"))) as database:
            database.execute("UPDATE messages SET lease_expires=? WHERE id=?", (time.time() - 1, receipt["message_id"]))
            database.commit()
        paired = self.mokutori("pair-check")
        self.assertEqual([item["actor_id"] for item in paired["paired"]], [ipad.state["actor_id"]])
        self.assertTrue(home.state()["pairings"][pairing["pairing_id"]]["proof_finished"])
        self.assertEqual(self.mokutori("inbox")["batches"], [])

    def test_changed_encryption_sender_is_not_saved(self):
        _, ipad, _ = self.paired_device()
        forged = app.App(dict(ipad.state))
        forged.state["secret"], forged.state["public"] = app.E.generate()
        self.send_page(forged)
        result = self.mokutori("inbox")
        self.assertEqual(result["batches"], [], result)
        self.assertEqual(len(result["errors"]), 1, result)
        self.assertEqual(list((self.home / "inbox").iterdir()), [])

    def test_uncertain_claim_preserves_data_and_resumes_only_after_owner_retry(self):
        _, ipad, _ = self.paired_device()
        for saved in (False, True):
            with self.subTest(saved=saved):
                home = plugin.Home(self.home)
                state = home.state()
                setup = state["setup"]
                plugin.engine(home.owner, "owner", "contact", setup["bot_id"], ipad.state["actor_id"], "untrust", "--gateway", self.url)
                message_id = self.send_page(ipad)
                plugin.engine(home.owner, "review", "approve-inbound", setup["bot_id"], message_id, "--gateway", self.url)
                if saved:
                    summary = next(item for item in plugin.reader_job(home, setup, "list")["jobs"] if item["job_id"] == message_id)
                    plugin.receive_job(home, state, setup, summary)
                    state["received"][message_id]["acknowledged"] = True
                    home.save(state)
                    page = Path(state["received"][message_id]["batch"]["files"][0]["path"])
                    original = (page.read_bytes(), page.stat().st_mtime_ns)
                else:
                    plugin.reader_job(home, setup, "claim", message_id, "--lease-seconds", "600")
                # A supervised claim stays non-direct even if the owner subsequently accepts its sender.
                plugin.engine(home.owner, "owner", "contact", setup["bot_id"], ipad.state["actor_id"], "trust", "--gateway", self.url)
                # Advance only this synthetic lease in the isolated test gateway past its 600s expiry.
                with closing(sqlite3.connect(str(self.root / "gw" / "gateway.sqlite3"))) as database:
                    database.execute("UPDATE messages SET lease_expires=? WHERE id=?", (time.time() - 1, message_id))
                    database.commit()
                result = self.mokutori("inbox")
                self.assertTrue(any(item["message_id"] == message_id for item in result["errors"]), result)
                error = next(item for item in result["errors"] if item["message_id"] == message_id)
                self.assertTrue(error["recovery"]["requires_owner_decision"])
                self.assertEqual(self.status_of(ipad, message_id)["state"], "uncertain")
                # Simulate the owner's explicit retry decision, never the plugin deciding to replay.
                process = subprocess.run(error["recovery"]["retry_command"], capture_output=True, text=True, check=True)
                self.assertTrue(json.loads(process.stdout)["ok"])
                resumed = self.mokutori("inbox", "--ack")
                self.assertEqual(resumed["errors"], [], resumed)
                self.assertEqual(len(resumed["batches"]), 0 if saved else 1, resumed)
                self.assertEqual(self.status_of(ipad, message_id)["code"], "read")
                if saved:
                    self.assertEqual((page.read_bytes(), page.stat().st_mtime_ns), original)
                    self.assertTrue(home.state()["received"][message_id]["acknowledged"])

    def test_lease_expiring_between_list_and_heartbeat_keeps_unique_cache(self):
        _, ipad, _ = self.paired_device()
        for interrupted in (False, True):
            with self.subTest(interrupted=interrupted):
                message_id = self.send_page(ipad)
                home = plugin.Home(self.home)
                state = home.state()
                setup = state["setup"]
                plugin.reader_job(home, setup, "claim", message_id, "--lease-seconds", "600")
                summary = next(item for item in plugin.reader_job(home, setup, "list")["jobs"] if item["job_id"] == message_id)
                original_reader_job = plugin.reader_job
                def expire_before_start(home, setup, operation, *args, **kwargs):
                    if operation == "heartbeat":
                        with closing(sqlite3.connect(str(self.root / "gw" / "gateway.sqlite3"))) as database:
                            database.execute("UPDATE messages SET lease_expires=? WHERE id=?", (time.time() - 1, message_id))
                            database.commit()
                    return original_reader_job(home, setup, operation, *args, **kwargs)
                with mock.patch.object(plugin, "reader_job", side_effect=expire_before_start):
                    if interrupted:
                        with mock.patch.object(plugin, "owner_job_context", side_effect=plugin.MokutoriError("simulated exit after lease cleanup")):
                            with self.assertRaises(plugin.MokutoriError):
                                plugin.receive_job(home, state, setup, summary)
                        self.assertTrue((home.claims / (message_id + ".json")).is_file())
                        self.assertNotIn(message_id, plugin.B.State(home.reader).load("leases.json")["leases"])
                    else:
                        plugin.receive_job(home, state, setup, summary)
                result = self.mokutori("inbox", "--ack")
                self.assertEqual(result["errors"], [], result)
                self.assertEqual(len(result["batches"]), 1, result)
                self.assertEqual(result["batches"][0]["message_id"], message_id)
                self.assertEqual(self.status_of(ipad, message_id)["code"], "read")
                self.assertFalse((home.claims / (message_id + ".json")).exists())
                self.assertEqual(self.mokutori("inbox")["batches"], [])

    def test_expired_direct_claim_recovers_cached_pages_after_gateway_marks_read(self):
        _, ipad, _ = self.paired_device()
        for saved in (False, True):
            with self.subTest(saved=saved):
                message_id = self.send_page(ipad)
                home = plugin.Home(self.home)
                state = home.state()
                setup = state["setup"]
                if saved:
                    summary = next(item for item in plugin.reader_job(home, setup, "list")["jobs"] if item["job_id"] == message_id)
                    plugin.receive_job(home, state, setup, summary)
                else:
                    plugin.reader_job(home, setup, "claim", message_id, "--lease-seconds", "600")
                with closing(sqlite3.connect(str(self.root / "gw" / "gateway.sqlite3"))) as database:
                    database.execute("UPDATE messages SET lease_expires=? WHERE id=?", (time.time() - 1, message_id))
                    database.commit()
                result = self.mokutori("inbox", "--ack")
                self.assertEqual(result["errors"], [], result)
                self.assertEqual(len(result["batches"]), 1, result)
                self.assertEqual(result["batches"][0]["message_id"], message_id)
                self.assertEqual(self.status_of(ipad, message_id)["code"], "read")
                self.assertTrue(home.state()["received"][message_id]["remote_finished"])
                self.assertEqual(self.mokutori("inbox")["batches"], [])

    def test_two_queued_batches_arrive_in_one_inbox(self):
        _, ipad, _ = self.paired_device()
        first, second = self.send_page(ipad), self.send_page(ipad)
        result = self.mokutori("inbox")
        self.assertEqual(result["errors"], [], result)
        self.assertEqual({batch["message_id"] for batch in result["batches"]}, {first, second})

    def test_pairing_rejects_other_prefix_and_wrong_prefix_mac(self):
        self.setup_agent()
        pairing = self.mokutori("pair")
        code = app.parse_pairing(pairing["pair_url"])
        ipad = app.App()
        # Even a valid HMAC for another protocol prefix must not authorize pairing.
        with mock.patch.object(app, "PAIR_PREFIX", "mitori-pair/1"):
            status, receipt = ipad.pair(pairing["pair_url"], "prefix mismatch iPad")
            wrong_mac = app.proof(code["secret"], code["pairing_id"], code["bot_id"], ipad.state["actor_id"])
        self.assertEqual(status, 202, receipt)
        self.assertEqual(self.mokutori("pair-check")["paired"], [])
        self.assertEqual(self.mokutori("devices")["devices"], [])

        # Relabelling that HMAC as the expected prefix must also fail.
        status, receipt = ipad.message("%s %s %s" % (app.PAIR_PREFIX, code["pairing_id"], wrong_mac), [])
        self.assertEqual(status, 202, receipt)
        self.assertEqual(self.mokutori("pair-check")["paired"], [])

        # Rejected attempts do not consume the QR; the correctly bound proof works.
        correct_mac = app.proof(code["secret"], code["pairing_id"], code["bot_id"], ipad.state["actor_id"])
        status, receipt = ipad.message("%s %s %s" % (app.PAIR_PREFIX, code["pairing_id"], correct_mac), [])
        self.assertEqual(status, 202, receipt)
        self.assertEqual(self.mokutori("pair-check")["paired"][0]["actor_id"], ipad.state["actor_id"])


class PairingRules(unittest.TestCase):
    def test_unsafe_job_id_and_symlink_are_rejected_before_engine_calls(self):
        with tempfile.TemporaryDirectory() as directory:
            home = plugin.Home(Path(directory) / "home")
            home.ensure()
            with mock.patch.object(plugin, "reader_job") as gateway:
                for job_id in ("../outside", "/tmp/outside", "msg_" + "a" * 16 + "/../outside"):
                    with self.assertRaises(plugin.MokutoriError):
                        plugin.receive_job(home, {}, {}, {"job_id": job_id})
                target = Path(directory) / "outside"
                target.mkdir()
                safe_id = "msg_" + "a" * 16
                (home.inbox / safe_id).symlink_to(target, target_is_directory=True)
                with self.assertRaises(plugin.MokutoriError):
                    plugin.receive_job(home, {}, {}, {"job_id": safe_id})
                gateway.assert_not_called()
                self.assertEqual(list(target.iterdir()), [])

    def test_engine_matches_recorded_source_hashes(self):
        engine = PLUGIN / "scripts" / "engine"
        pin = json.loads((engine / "SOURCE.json").read_text())
        for name, expected in pin["files"].items():
            self.assertEqual(hashlib.sha256((engine / name).read_bytes()).hexdigest(), expected, name)

    def test_rejects_unsupported_scheme(self):
        vectors = json.loads((REPO / "tests" / "seal_vectors.json").read_text())
        self.assertEqual(app.parse_pairing(vectors["pair_url"])["gateway"], "https://bots.yobiru.com")
        with self.assertRaises(ValueError):
            app.parse_pairing(vectors["pair_url"].replace("mokutori://", "mitori://", 1))


class PackingRules(unittest.TestCase):
    def page(self, number, text="x", jpeg_bytes=600_000):
        return {"number": number, "id": "%08d-0000-4000-8000-000000000000" % number, "status": "ready",
                "warnings": [], "text": text, "jpeg": b"\xff\xd8" + b"\x00" * jpeg_bytes}

    def test_batches_respect_gateway_limits(self):
        chosen, attachments, text = app.pack([self.page(n) for n in range(1, 9)])
        self.assertEqual(len(chosen), app.MAX_PAGES)
        self.assertLessEqual(len(attachments), app.MAX_FILES)
        big = [self.page(n, jpeg_bytes=3_000_000) for n in range(1, 4)]
        chosen, attachments, _ = app.pack(big)
        self.assertEqual(len(chosen), 2)
        long_text = [self.page(n, text="字" * 9000, jpeg_bytes=1000) for n in range(1, 6)]
        chosen, attachments, text = app.pack(long_text)
        self.assertLessEqual(len(attachments), app.MAX_FILES)
        for item in attachments:
            if "content" in item:
                self.assertLessEqual(len(item["content"].encode("utf-8")), app.MAX_TEXT_FILE_BYTES)
        self.assertLessEqual(len(text), app.MAX_TEXT_CHARS)


if __name__ == "__main__":
    unittest.main()
