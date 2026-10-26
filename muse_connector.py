#!/usr/bin/env python3
"""A small, least-privilege MCP connector for Muse.

The existing ``mokutori`` command is the setup and local-agent client.  This
module is the optional HTTP bridge used by Muse Connector Platform.  It keeps
the Yobiru credentials in ``MOKUTORI_AGENT_HOME`` and exposes only the
read/acknowledge operations needed to read pages from a paired iPad.

The server is deliberately bound to localhost by default.  A production
deployment must put it behind an HTTPS reverse proxy and set a random
``MOKUTORI_MUSE_API_KEY``.  The API key is an installation credential, not a
Yobiru credential, and is never returned by a tool.
"""

from __future__ import annotations

import argparse
import base64
import hmac
import http.server
import json
import os
import re
import secrets
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Tuple

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE / "scripts"
sys.path.insert(0, str(SCRIPTS))
import mokutori as core  # noqa: E402


PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26")
LATEST_PROTOCOL = PROTOCOL_VERSIONS[0]
MAX_BODY_BYTES = 12 * 1024 * 1024
MAX_IMAGE_BYTES = getattr(core.C, "MEDIA_MAX_BYTES", 4 * 1024 * 1024)
MAX_BATCHES = 10
MAX_ACK_IDS = 50
API_KEY_ENV = "MOKUTORI_MUSE_API_KEY"
ALLOWED_ORIGINS_ENV = "MOKUTORI_MUSE_ALLOWED_ORIGINS"
FIXTURE_ENV = "MOKUTORI_MUSE_FIXTURE_ONLY"
NOTICE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,160}$\Z")
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,120}\Z")
IMAGE_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
               ".webp": "image/webp", ".heic": "image/heic", ".gif": "image/gif"}


class ConnectorError(Exception):
    """An expected connector failure that can be shown to Muse."""


def _valid_id(value: Any) -> bool:
    return isinstance(value, str) or type(value) is int


def _result(request_id: Any, value: Dict[str, Any]) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": value}


def _error(request_id: Any, code: int, message: str) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id,
            "error": {"code": code, "message": message}}


def _tool_result(value: Dict[str, Any], blocks: Optional[list] = None, is_error: bool = False) -> Dict[str, Any]:
    content = [{"type": "text", "text": json.dumps(value, ensure_ascii=False, sort_keys=True)}]
    if blocks:
        content.extend(blocks)
    return {"content": content, "structuredContent": value, "isError": is_error}


def _read_only() -> Dict[str, bool]:
    return {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}


def _write() -> Dict[str, bool]:
    return {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}


TOOLS = [
    {
        "name": "mokutori_status",
        "title": "Mokutori connection status",
        "description": "Read the paired Mokutori iPad connection and pending pairing count. This never returns credentials.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "outputSchema": {"type": "object"}, "annotations": _read_only(),
    },
    {
        "name": "mokutori_list_scans",
        "title": "List new Mokutori scans",
        "description": "Fetch and locally cache newly received scans from the paired iPad, marking their gateway messages read after saving. Returns locally unread OCR and safe file metadata; page images are fetched with mokutori_get_scan_image.",
        "inputSchema": {"type": "object", "properties": {
            "limit": {"type": "integer", "minimum": 1, "maximum": MAX_BATCHES},
        }, "additionalProperties": False},
        "outputSchema": {"type": "object"}, "annotations": _write(),
    },
    {
        "name": "mokutori_get_scan_image",
        "title": "Get a scanned page image",
        "description": "Read one image from a batch returned by mokutori_list_scans. The image is returned as an MCP image block; no filesystem path is exposed.",
        "inputSchema": {"type": "object", "properties": {
            "notice_id": {"type": "string", "minLength": 1, "maxLength": 160},
            "name": {"type": "string", "minLength": 1, "maxLength": 120},
        }, "required": ["notice_id", "name"], "additionalProperties": False},
        "outputSchema": {"type": "object"}, "annotations": _read_only(),
    },
    {
        "name": "mokutori_acknowledge_scans",
        "title": "Acknowledge received scans",
        "description": "Clear the local unread marker for specific received scan batches. Their gateway messages were marked read when saved. This does not delete any saved pages.",
        "inputSchema": {"type": "object", "properties": {
            "notice_ids": {"type": "array", "minItems": 1, "maxItems": MAX_ACK_IDS,
                           "items": {"type": "string", "minLength": 1, "maxLength": 160}},
        }, "required": ["notice_ids"], "additionalProperties": False},
        "outputSchema": {"type": "object"}, "annotations": _write(),
    },
]
TOOL_INDEX = {tool["name"]: tool for tool in TOOLS}


def _schema_error(schema: Dict[str, Any], value: Any, path: str = "arguments") -> Optional[str]:
    types = schema.get("type")
    if types is not None:
        names = types if isinstance(types, list) else [types]
        checks = {"object": isinstance(value, dict), "array": isinstance(value, list),
                  "string": isinstance(value, str), "integer": type(value) is int,
                  "boolean": type(value) is bool, "null": value is None}
        if not any(checks.get(item, False) for item in names):
            return "%s must be %s" % (path, " or ".join(names))
    if isinstance(value, str):
        if len(value) < schema.get("minLength", 0) or len(value) > schema.get("maxLength", len(value)):
            return "%s has an invalid length" % path
    if type(value) is int:
        if value < schema.get("minimum", value) or value > schema.get("maximum", value):
            return "%s is outside the allowed range" % path
    if isinstance(value, list):
        if len(value) < schema.get("minItems", 0) or len(value) > schema.get("maxItems", len(value)):
            return "%s has an invalid item count" % path
        for index, item in enumerate(value):
            error = _schema_error(schema.get("items", {}), item, "%s[%d]" % (path, index))
            if error:
                return error
    if isinstance(value, dict):
        properties = schema.get("properties", {})
        for key in schema.get("required", []):
            if key not in value:
                return "%s.%s is required" % (path, key)
        if schema.get("additionalProperties") is False:
            extra = sorted(set(value) - set(properties))
            if extra:
                return "%s has unknown field(s) %s" % (path, ", ".join(extra))
        for key, item in value.items():
            if key in properties:
                error = _schema_error(properties[key], item, "%s.%s" % (path, key))
                if error:
                    return error
    return None


def _safe_name(value: Any) -> str:
    if not isinstance(value, str) or not NAME_RE.fullmatch(value):
        raise ConnectorError("invalid file name")
    return value


def _safe_notice_id(value: Any) -> str:
    if not isinstance(value, str) or not NOTICE_ID_RE.fullmatch(value):
        raise ConnectorError("invalid notice_id")
    return value


def _inside(root: Path, child: Path) -> bool:
    try:
        return os.path.commonpath([str(root.resolve()), str(child.resolve())]) == str(root.resolve())
    except ValueError:
        return False


class Service:
    """Adapter around the existing CLI's private, already-paired data home."""

    def __init__(self, home: Path):
        self.home = core.Home(home)
        self._operation_lock = threading.RLock()

    def status(self) -> Dict[str, Any]:
        state = self.home.state()
        view = core.status_view(self.home, state)
        # Keep paths and all credential-bearing details out of connector output.
        return {"configured": bool(view.get("gateway")), "gateway": view.get("gateway"),
                "bot_id": view.get("bot_id"), "handle": view.get("handle"),
                "devices": [{"actor_id": item.get("actor_id"), "name": item.get("name"),
                              "paired_at": item.get("paired_at")} for item in view.get("devices", [])],
                "pending_pairings": view.get("pending_pairings", 0),
                "synthetic_reviewer_fixture": os.environ.get(FIXTURE_ENV) == "1"}

    def _run_inbox(self, limit: int) -> Dict[str, Any]:
        """Use the tested CLI ingestion path; it performs E2E opening and safe file writes."""
        command = [sys.executable, str(SCRIPTS / "mokutori.py"), "--home", str(self.home.path),
                   "inbox", "--no-pair-check", "--limit", str(limit)]
        try:
            process = subprocess.run(command, capture_output=True, text=True, timeout=120)
        except subprocess.TimeoutExpired as error:
            raise ConnectorError("inbox_unavailable: the local inbox operation timed out") from error
        except OSError as error:
            raise ConnectorError("inbox_unavailable: the local inbox operation could not start") from error
        try:
            result = json.loads(process.stdout)
        except (TypeError, ValueError):
            result = None
        if process.returncode != 0 or not isinstance(result, dict) or result.get("ok") is not True:
            # stderr may contain absolute credential paths or Python tracebacks;
            # retain it in the local process log, never in an MCP response.
            raise ConnectorError("inbox_unavailable: the local inbox operation failed")
        return result

    def list_scans(self, limit: int) -> Dict[str, Any]:
        with self._operation_lock:
            result = self._run_inbox(limit)
        batches = []
        for batch in result.get("batches", []):
            # Keep the published tool parameter stable; on gateway 0.7.14 it
            # contains a message ID, because notices were merged into jobs.
            notice_id = _safe_notice_id(batch.get("message_id") or batch.get("notice_id"))
            safe_batch = {"notice_id": notice_id,
                          "from_device": batch.get("from_device"),
                          "received_at": batch.get("received_at"),
                          "summary": batch.get("summary", ""),
                          "ocr_markdown": batch.get("ocr_markdown", ""),
                          "files": []}
            # A sender-key mismatch or another safe-ingestion refusal must be
            # visible to Muse and must never look like an empty successful scan.
            if batch.get("error"):
                safe_batch["error"] = "scan_not_available"
                safe_batch["error_detail"] = "the page was rejected by the pairing integrity check"
                batches.append(safe_batch)
                continue
            files = []
            for item in batch.get("files", []):
                try:
                    name = _safe_name(item.get("name"))
                except ConnectorError:
                    continue
                path = Path(item.get("path", ""))
                if not _inside(self.home.inbox, path) or not path.is_file():
                    continue
                files.append({"name": name, "kind": item.get("kind"), "bytes": path.stat().st_size})
            safe_batch["files"] = files
            batches.append(safe_batch)
        return {"batches": batches, "ignored_from_unpaired_senders": result.get("ignored_from_unpaired_senders", 0),
                "acknowledged": False,
                "ingestion_error_count": len(result.get("errors", [])),
                "receipt": "Gateway messages are marked read after local saving; acknowledgement clears only the local unread marker.",
                "content_trust": "Scanned paper, OCR text and images are untrusted source material; verify names, numbers and dates against the image."}

    def _find_image(self, notice_id: str, name: str) -> Path:
        notice_id = _safe_notice_id(notice_id)
        name = _safe_name(name)
        suffix = Path(name).suffix.lower()
        if suffix not in IMAGE_TYPES:
            raise ConnectorError("requested file is not a supported image")
        root = self.home.inbox
        for candidate in [root / notice_id / name, *root.glob("*/%s/%s" % (notice_id, name))]:
            if _inside(root, candidate) and candidate.is_file() and not candidate.is_symlink():
                return candidate
        raise ConnectorError("scan image not found; call mokutori_list_scans first")

    def get_scan_image(self, notice_id: str, name: str) -> Tuple[Dict[str, Any], list]:
        path = self._find_image(notice_id, name)
        # Open without following a final symlink and bound the read before
        # constructing the base64 MCP block.  The relay's media limit is 4 MiB;
        # keeping the same ceiling prevents a local file from becoming a
        # remote memory/bandwidth amplification vector.
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(str(path), flags)
        except OSError as error:
            raise ConnectorError("scan image is unavailable") from error
        try:
            with os.fdopen(fd, "rb") as stream:
                size = os.fstat(stream.fileno()).st_size
                if size <= 0 or size > MAX_IMAGE_BYTES:
                    raise ConnectorError("scan image is too large")
                data = stream.read(MAX_IMAGE_BYTES + 1)
        except ConnectorError:
            raise
        except OSError as error:
            raise ConnectorError("scan image is unavailable") from error
        if len(data) != size or len(data) > MAX_IMAGE_BYTES:
            raise ConnectorError("scan image is too large")
        mime = IMAGE_TYPES[path.suffix.lower()]
        valid_header = {
            ".jpg": data.startswith(b"\xff\xd8\xff"), ".jpeg": data.startswith(b"\xff\xd8\xff"),
            ".png": data.startswith(b"\x89PNG\r\n\x1a\n"),
            ".webp": len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP",
            ".gif": data.startswith((b"GIF87a", b"GIF89a")),
            # ISO-BMFF still images carry an ftyp box at byte 4.  Brand
            # variants differ between HEIC/HEIF cameras, so do not overfit it.
            ".heic": len(data) >= 12 and data[4:8] == b"ftyp",
        }[path.suffix.lower()]
        if not valid_header:
            raise ConnectorError("scan image content failed validation")
        value = {"notice_id": notice_id, "name": path.name, "media_type": mime, "bytes": len(data)}
        return value, [{"type": "image", "data": base64.b64encode(data).decode("ascii"), "mimeType": mime}]

    def acknowledge(self, notice_ids: Iterable[str]) -> Dict[str, Any]:
        with self._operation_lock, core.C.FileLock(self.home.path / ".command.lock", blocking=False):
            state = self.home.state()
            acknowledged, refused = [], []
            for raw in notice_ids:
                notice_id = _safe_notice_id(raw)
                record = (state.get("received") or {}).get(notice_id)
                saved = self.home.inbox / notice_id / "message.txt"
                if (not isinstance(record, dict) or not record.get("batch")
                        or not record.get("remote_finished") or not _inside(self.home.inbox, saved)
                        or not saved.is_file() or saved.is_symlink()):
                    refused.append({"notice_id": notice_id, "error": "not_read"})
                    continue
                record["acknowledged"] = True
                acknowledged.append(notice_id)
            if acknowledged:
                self.home.save(state)
            return {"acknowledged": acknowledged, "refused": refused}


class MCPServer:
    def __init__(self, service: Service, api_key: str):
        if not api_key or len(api_key) < 20:
            raise ValueError("MOKUTORI_MUSE_API_KEY must be a random key of at least 20 characters")
        self.service, self.api_key = service, api_key
        self._phase_lock = threading.Lock()
        self._default_state = {"phase": "new", "protocol_version": None}
        self._sessions = {}

    def create_session(self) -> str:
        session_id = secrets.token_urlsafe(24)
        with self._phase_lock:
            self._sessions[session_id] = {"phase": "new", "protocol_version": None}
        return session_id

    def has_session(self, session_id: Optional[str]) -> bool:
        with self._phase_lock:
            return isinstance(session_id, str) and session_id in self._sessions

    def _state(self, session_id: Optional[str]) -> Optional[Dict[str, Any]]:
        with self._phase_lock:
            return self._default_state if session_id is None else self._sessions.get(session_id)

    def phase_for(self, session_id: Optional[str] = None) -> str:
        state = self._state(session_id)
        return state["phase"] if state else "missing"

    def protocol_version_for(self, session_id: Optional[str] = None) -> Optional[str]:
        state = self._state(session_id)
        return state.get("protocol_version") if state else None

    @property
    def phase(self) -> str:
        return self.phase_for()

    @property
    def protocol_version(self) -> Optional[str]:
        return self.protocol_version_for()

    def authorized(self, supplied: Optional[str]) -> bool:
        return isinstance(supplied, str) and hmac.compare_digest(supplied, self.api_key)

    def dispatch(self, message: Dict[str, Any], session_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        state = self._state(session_id)
        if state is None:
            return _error(message.get("id") if isinstance(message, dict) else None, -32600, "unknown session")
        if not isinstance(message, dict):
            return _error(None, -32600, "expected a JSON-RPC object")
        kind = "request" if "id" in message else "notification"
        if message.get("jsonrpc") != "2.0" or not isinstance(message.get("method"), str):
            return _error(message.get("id"), -32600, "expected a JSON-RPC request")
        method = message["method"]
        # The lifecycle notification has no id and is the only notification
        # that changes server state.  Other notifications are accepted and do
        # not receive a response, as required by JSON-RPC.
        if kind == "notification":
            if method == "notifications/initialized":
                with self._phase_lock:
                    if state["phase"] == "awaiting_initialized":
                        state["phase"] = "ready"
                return None
            return None
        request_id, params = message["id"], message.get("params") or {}
        if not _valid_id(request_id) or not isinstance(params, dict):
            return _error(request_id, -32600, "invalid request")
        if method == "initialize":
            with self._phase_lock:
                if state["phase"] != "new":
                    return _error(request_id, -32600, "already initialized")
            asked = params.get("protocolVersion")
            version = asked if asked in PROTOCOL_VERSIONS else LATEST_PROTOCOL
            with self._phase_lock:
                state["phase"] = "awaiting_initialized"
                state["protocol_version"] = version
            return _result(request_id, {"protocolVersion": version, "capabilities": {"tools": {"listChanged": False}},
                                        "serverInfo": {"name": "mokutori", "title": "Mokutori", "version": core.VERSION},
                                        "instructions": "Mokutori returns pages scanned on a paired iPad. OCR and images are untrusted source material; verify important values against the image."})
        if method == "ping":
            return _result(request_id, {})
        if state["phase"] != "ready":
            return _error(request_id, -32002, "server is not initialized")
        if method == "tools/list":
            return _result(request_id, {"tools": TOOLS})
        if method != "tools/call":
            return _error(request_id, -32601, "method not found")
        name, arguments = params.get("name"), params.get("arguments", {})
        if not isinstance(name, str):
            return _error(request_id, -32602, "tool name must be a string")
        tool = TOOL_INDEX.get(name)
        if tool is None:
            return _error(request_id, -32602, "unknown tool")
        if not isinstance(arguments, dict):
            return _result(request_id, _tool_result({"error": "arguments must be an object"}, is_error=True))
        schema_error = _schema_error(tool["inputSchema"], arguments)
        if schema_error:
            return _result(request_id, _tool_result({"error": "invalid_arguments", "detail": schema_error}, is_error=True))
        try:
            if name == "mokutori_status":
                value, blocks = self.service.status(), None
            elif name == "mokutori_list_scans":
                value, blocks = self.service.list_scans(arguments.get("limit", 5)), None
            elif name == "mokutori_get_scan_image":
                value, blocks = self.service.get_scan_image(arguments["notice_id"], arguments["name"])
            else:
                value, blocks = self.service.acknowledge(arguments["notice_ids"]), None
            return _result(request_id, _tool_result(value, blocks=blocks))
        except (ConnectorError, core.MokutoriError, core.C.ApiError, OSError, ValueError) as error:
            # Never echo exception text: the CLI may include private paths or
            # gateway request details. Keep diagnostics on the connector host.
            return _result(request_id, _tool_result({"error": "connector_operation_failed"}, is_error=True))
        except Exception:
            # A malformed local credential store or an unexpected dependency
            # failure must still produce a valid MCP response. The traceback
            # remains local to the process; never expose it to Muse.
            return _result(request_id, _tool_result({"error": "connector_operation_failed"}, is_error=True))


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "MokutoriMuse/" + core.VERSION

    def _json(self, status: int, value: Dict[str, Any], headers: Optional[Dict[str, str]] = None) -> None:
        body = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for key, item in (headers or {}).items():
            self.send_header(key, item)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/healthz":
            self._json(200, {"ok": True, "service": "mokutori-muse"})
        elif self.path == "/mcp":
            self.send_response(405)
            self.send_header("Allow", "POST")
            self.send_header("Content-Length", "0")
            self.end_headers()
        else:
            self._json(404, {"error": "not_found"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/mcp":
            self._json(404, {"error": "not_found"})
            return
        supplied = (self.headers.get("Authorization") or "")
        supplied = supplied[7:] if supplied.startswith("Bearer ") else None
        if not self.server.mcp.authorized(supplied):
            self._json(401, {"error": "unauthorized"})
            return
        origin = self.headers.get("Origin")
        if origin:
            allowed = {item.strip() for item in os.environ.get(ALLOWED_ORIGINS_ENV, "").split(",") if item.strip()}
            if origin not in allowed:
                self._json(403, {"error": "origin_not_allowed"})
                return
        accept = self.headers.get("Accept", "")
        if "application/json" not in accept or "text/event-stream" not in accept:
            self._json(406, {"error": "accept_must_include_json_and_event_stream"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > MAX_BODY_BYTES:
                raise ValueError("invalid content length")
            message = json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as error:
            self._json(400, _error(None, -32700, "invalid JSON: %s" % error))
            return
        protocol = self.headers.get("MCP-Protocol-Version")
        if protocol and protocol not in PROTOCOL_VERSIONS:
            self._json(400, {"error": "unsupported_mcp_protocol_version"})
            return
        session_id = self.headers.get("MCP-Session-Id")
        method = message.get("method") if isinstance(message, dict) else None
        if session_id and not self.server.mcp.has_session(session_id):
            self._json(404, {"error": "unknown_mcp_session"})
            return
        if not session_id and method != "initialize":
            self._json(400, {"error": "missing_mcp_session_id"})
            return
        if method == "initialize" and not session_id:
            session_id = self.server.mcp.create_session()
        # The lifecycle spec requires the negotiated version on every request
        # after initialize.  The initialize request itself is the one
        # exception because negotiation has not happened yet.
        if session_id and self.server.mcp.phase_for(session_id) != "new":
            if not protocol:
                self._json(400, {"error": "missing_mcp_protocol_version"})
                return
            if protocol != self.server.mcp.protocol_version_for(session_id):
                self._json(400, {"error": "mcp_protocol_version_mismatch"})
                return
        response = self.server.mcp.dispatch(message, session_id=session_id)
        if response is None:
            self.send_response(202)
            self.send_header("Content-Length", "0")
            self.end_headers()
        else:
            extra = {"MCP-Session-Id": session_id} if method == "initialize" and session_id else None
            self._json(200, response, headers=extra)

    def log_message(self, *_args: Any) -> None:
        return


class HTTPServer(http.server.ThreadingHTTPServer):
    def __init__(self, address: Tuple[str, int], mcp: MCPServer):
        super().__init__(address, Handler)
        self.mcp = mcp


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("--home", help="Mokutori agent home (default: $MOKUTORI_AGENT_HOME or ~/.mokutori-agent)")
    parser.add_argument("--host", default="127.0.0.1", help="bind address; keep localhost unless behind HTTPS")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--api-key", default=os.environ.get(API_KEY_ENV), help="connector API key (prefer env var)")
    args = parser.parse_args(argv)
    try:
        mcp = MCPServer(Service(Path(args.home).expanduser() if args.home else core.default_home()), args.api_key or "")
    except ValueError as error:
        parser.error(str(error))
    server = HTTPServer((args.host, args.port), mcp)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
