import json
import pathlib
import sys
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))
from muse_connector import ConnectorError, HTTPServer, MCPServer, Service, MAX_IMAGE_BYTES
import mokutori as cli


class FakeService:
    def status(self):
        return {"configured": True, "gateway": "https://gateway.example", "bot_id": "bot_test",
                "handle": "@mokutori", "devices": [], "pending_pairings": 0}

    def list_scans(self, limit):
        return {"limit_seen": limit, "batches": [], "ignored_from_unpaired_senders": 0,
                "acknowledged": False, "content_trust": "untrusted"}

    def get_scan_image(self, notice_id, name):
        return {"notice_id": notice_id, "name": name, "media_type": "image/png", "bytes": 3}, [
            {"type": "image", "data": "YWJj", "mimeType": "image/png"}]

    def acknowledge(self, notice_ids):
        return {"acknowledged": list(notice_ids), "refused": []}


class MCPDispatchTests(unittest.TestCase):
    def setUp(self):
        self.server = MCPServer(FakeService(), "a" * 32)

    def ready(self):
        initialized = self.server.dispatch({"jsonrpc": "2.0", "id": 0, "method": "initialize",
                                            "params": {"protocolVersion": "2025-11-25"}})
        self.assertEqual(initialized["result"]["protocolVersion"], "2025-11-25")
        self.assertIsNone(self.server.dispatch({"jsonrpc": "2.0", "method": "notifications/initialized"}))

    def test_initialize_and_tools(self):
        response = self.server.dispatch({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                         "params": {"protocolVersion": "2025-06-18"}})
        self.assertEqual(response["result"]["protocolVersion"], "2025-06-18")
        self.assertEqual(response["result"]["serverInfo"]["version"], cli.VERSION)
        self.assertIsNone(self.server.dispatch({"jsonrpc": "2.0", "method": "notifications/initialized"}))
        tools = self.server.dispatch({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        names = {tool["name"] for tool in tools["result"]["tools"]}
        self.assertEqual(names, {"mokutori_status", "mokutori_list_scans", "mokutori_get_scan_image",
                                 "mokutori_acknowledge_scans"})
        ack = next(tool for tool in tools["result"]["tools"] if tool["name"] == "mokutori_acknowledge_scans")
        self.assertFalse(ack["annotations"]["readOnlyHint"])
        ingest = next(tool for tool in tools["result"]["tools"] if tool["name"] == "mokutori_list_scans")
        self.assertFalse(ingest["annotations"]["readOnlyHint"])

    def test_tool_calls_and_validation(self):
        self.ready()
        status = self.server.dispatch({"jsonrpc": "2.0", "id": "s", "method": "tools/call",
                                       "params": {"name": "mokutori_status", "arguments": {}}})
        self.assertTrue(status["result"]["structuredContent"]["configured"])
        image = self.server.dispatch({"jsonrpc": "2.0", "id": "i", "method": "tools/call",
                                       "params": {"name": "mokutori_get_scan_image",
                                                   "arguments": {"notice_id": "msg_1", "name": "page.png"}}})
        self.assertEqual(image["result"]["content"][1]["type"], "image")
        invalid = self.server.dispatch({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                                        "params": {"name": "mokutori_status", "arguments": {"secret": "x"}}})
        self.assertTrue(invalid["result"]["isError"])

    def test_lifecycle_rejects_operations_before_ready_and_duplicate_initialize(self):
        before = self.server.dispatch({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
        self.assertEqual(before["error"]["code"], -32002)
        self.ready()
        duplicate = self.server.dispatch({"jsonrpc": "2.0", "id": 2, "method": "initialize", "params": {}})
        self.assertEqual(duplicate["error"]["code"], -32600)

    def test_malformed_json_rpc_is_an_error_response(self):
        for message in (None, 7, "request"):
            response = self.server.dispatch(message)
            self.assertEqual(response["error"]["code"], -32600)
        self.ready()
        invalid_name = self.server.dispatch({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                                             "params": {"name": [], "arguments": {}}})
        self.assertEqual(invalid_name["error"]["code"], -32602)

    def test_failed_service_response_is_sanitized(self):
        class BrokenService(FakeService):
            def list_scans(self, limit):
                raise RuntimeError("/private/credentials.json should never be returned")

        server = MCPServer(BrokenService(), "a" * 32)
        server.dispatch({"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {}})
        server.dispatch({"jsonrpc": "2.0", "method": "notifications/initialized"})
        response = server.dispatch({"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                                    "params": {"name": "mokutori_list_scans", "arguments": {}}})
        self.assertTrue(response["result"]["isError"])
        self.assertEqual(response["result"]["structuredContent"], {"error": "connector_operation_failed"})

    def test_auth_and_http_transport(self):
        http = HTTPServer(("127.0.0.1", 0), self.server)
        thread = threading.Thread(target=http.serve_forever, daemon=True)
        thread.start()
        try:
            url = "http://127.0.0.1:%d/mcp" % http.server_address[1]
            body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping", "params": {}}).encode()
            headers = {"Content-Type": "application/json",
                       "Accept": "application/json, text/event-stream",
                       "Authorization": "Bearer " + "a" * 32}
            with self.assertRaises(HTTPError) as refused:
                urlopen(Request(url, data=body, headers={**headers, "Authorization": "Bearer wrong"}))
            self.assertEqual(refused.exception.code, 401)
            refused.exception.close()
            initialize = json.dumps({"jsonrpc": "2.0", "id": 0, "method": "initialize",
                                     "params": {"protocolVersion": "2025-11-25"}}).encode()
            with urlopen(Request(url, data=initialize, headers=headers)) as response:
                self.assertEqual(json.loads(response.read())["result"]["protocolVersion"], "2025-11-25")
                session_id = response.headers["MCP-Session-Id"]
            notification = json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}).encode()
            session_headers = {**headers, "MCP-Protocol-Version": "2025-11-25", "MCP-Session-Id": session_id}
            with urlopen(Request(url, data=notification, headers=session_headers)):
                pass
            request = Request(url, data=body, headers=session_headers)
            with urlopen(request) as response:
                self.assertEqual(json.loads(response.read())["result"], {})
            with self.assertRaises(HTTPError) as wrong_version:
                urlopen(Request(url, data=body, headers={**session_headers, "MCP-Protocol-Version": "future"}))
            self.assertEqual(wrong_version.exception.code, 400)
            wrong_version.exception.close()
            with self.assertRaises(HTTPError) as wrong_origin:
                urlopen(Request(url, data=body, headers={**session_headers, "Origin": "https://evil.example"}))
            self.assertEqual(wrong_origin.exception.code, 403)
            wrong_origin.exception.close()
            with self.assertRaises(HTTPError) as get_refused:
                urlopen(Request(url, headers={"Accept": "text/event-stream"}))
            self.assertEqual(get_refused.exception.code, 405)
            get_refused.exception.close()
        finally:
            http.shutdown()
            http.server_close()
            thread.join(timeout=2)

    def test_muse_setup_uses_generic_engine_host_label(self):
        self.assertEqual(cli.binding_host("muse"), "other")
        self.assertEqual(cli.binding_host("grok"), "grok")


class ServiceSafetyTests(unittest.TestCase):
    def test_inbox_failure_does_not_return_subprocess_traceback(self):
        with tempfile.TemporaryDirectory() as temp:
            service = Service(pathlib.Path(temp))
            process = type("Process", (), {"returncode": 0, "stdout": "not-json", "stderr": "/private/credentials.json"})()
            with mock.patch("muse_connector.subprocess.run", return_value=process):
                with self.assertRaisesRegex(ConnectorError, "inbox_unavailable"):
                    service._run_inbox(1)

    def test_rejected_batch_is_marked_unavailable(self):
        with tempfile.TemporaryDirectory() as temp:
            service = Service(pathlib.Path(temp))
            service._run_inbox = lambda limit: {
                "batches": [{"notice_id": "notice_bad", "error": "sender key changed since pairing"}],
                "ignored_from_unpaired_senders": 0}
            batch = service.list_scans(5)["batches"][0]
        self.assertEqual(batch["error"], "scan_not_available")
        self.assertEqual(batch["files"], [])

    def test_acknowledge_only_allows_saved_batch(self):
        with tempfile.TemporaryDirectory() as temp:
            service = Service(pathlib.Path(temp))
            service.home.inbox.joinpath("notice_saved").mkdir(parents=True)
            service.home.inbox.joinpath("notice_saved", "message.txt").write_text("page")
            state = service.home.state()
            state.setdefault("received", {})["notice_saved"] = {
                "sender": "actor", "batch": {"summary": "fixture"}, "remote_finished": True,
                "acknowledged": False}
            service.home.save(state)
            with mock.patch.object(cli.C, "api_call", return_value=(200, {"ok": True})) as call:
                result = service.acknowledge(["notice_saved", "notice_never_read"])
            self.assertEqual(result["acknowledged"], ["notice_saved"])
            self.assertEqual(result["refused"], [{"notice_id": "notice_never_read", "error": "not_read"}])
            call.assert_not_called()
            self.assertTrue(service.home.state()["received"]["notice_saved"]["acknowledged"])

    def test_acknowledge_rejects_forged_notice_file(self):
        with tempfile.TemporaryDirectory() as temp:
            service = Service(pathlib.Path(temp))
            folder = service.home.inbox / "notice_forged"
            folder.mkdir(parents=True)
            (folder / "message.txt").write_text("forged")
            with mock.patch.object(cli.C, "api_call") as call:
                result = service.acknowledge(["notice_forged"])
            self.assertEqual(result["acknowledged"], [])
            self.assertEqual(result["refused"], [{"notice_id": "notice_forged", "error": "not_read"}])
            call.assert_not_called()

    def test_scan_image_is_bounded_and_validated(self):
        with tempfile.TemporaryDirectory() as temp:
            service = Service(pathlib.Path(temp))
            folder = service.home.inbox / "2026-10-26" / "notice_img"
            folder.mkdir(parents=True)
            (folder / "page.png").write_bytes(b"not-an-image")
            with self.assertRaisesRegex(ConnectorError, "validation"):
                service.get_scan_image("notice_img", "page.png")
            (folder / "large.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * MAX_IMAGE_BYTES)
            with self.assertRaisesRegex(ConnectorError, "too large"):
                service.get_scan_image("notice_img", "large.png")


if __name__ == "__main__":
    unittest.main()
