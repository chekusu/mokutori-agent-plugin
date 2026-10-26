#!/usr/bin/env python3
"""Reference of the iPad app's agent relay protocol, used to test the Mokutori agent plugin.

The separately maintained iPad app implements the same steps: parse the pairing QR, enroll a pseudonymous caller at the
gateway, prove the pairing with a sealed chat message, then send each batch of finished pages as one sealed chat message
(page images plus OCR markdown). Sealing uses Yobiru's reference bot_e2e.py (bundled with the plugin engine).
"""
import base64
import hashlib
import hmac
import json
import os
import secrets
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts" / "engine"))
import bot_e2e as E  # noqa: E402

PAIR_PREFIX = "mokutori-pair/1"
CTX = "yobiru-e2e/1"
MAX_FILES = 8
MAX_FILE_BYTES = 4 * 1024 * 1024
MAX_TOTAL_BYTES = 7 * 1024 * 1024 + 512 * 1024
MAX_TEXT_FILE_BYTES = 16000
MAX_TEXT_CHARS = 4000
MAX_PAGES = 5


def canonical(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def b64u(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def call(base, method, path, body=None, token=None):
    request = urllib.request.Request(base + path, method=method, data=canonical(body) if body is not None else None)
    request.add_header("Accept", "application/json")
    if body is not None:
        request.add_header("Content-Type", "application/json")
    if token:
        request.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        with error:
            return error.code, json.loads(error.read() or b"{}")


def parse_pairing(url):
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme == "mokutori" and parsed.netloc == "pair" and not parsed.path and not parsed.fragment:
        parameters = parsed.query
    elif (parsed.scheme == "https" and parsed.netloc == "mokutori.com"
          and parsed.path == "/pair" and not parsed.query):
        parameters = parsed.fragment
    else:
        raise ValueError("not a Mokutori pairing code")
    pairs = urllib.parse.parse_qsl(parameters, strict_parsing=True)
    query = dict(pairs)
    if len(query) != len(pairs):
        raise ValueError("duplicate pairing parameter")
    if set(query) != set("vgbkis") or query.get("v") != "1" or not all(query.get(k) for k in "gbkis"):
        raise ValueError("incomplete pairing code")
    return {"gateway": query["g"].rstrip("/"), "bot_id": query["b"], "bot_key": query["k"],
            "pairing_id": query["i"], "secret": query["s"]}


def proof(secret, pairing_id, bot_id, actor_id):
    raw = base64.urlsafe_b64decode(secret + "=" * (-len(secret) % 4))
    message = ("%s|%s|%s|%s" % (PAIR_PREFIX, pairing_id, bot_id, actor_id)).encode("ascii")
    return b64u(hmac.new(raw, message, hashlib.sha256).digest())


class App:
    def __init__(self, state=None):
        self.state = state or {}

    def pair(self, url, device_name="Mokutori iPad"):
        code = parse_pairing(url)
        status, card = call(code["gateway"], "GET", "/@" + code["bot_id"])
        if status != 200 or (card.get("e2e") or {}).get("key") != code["bot_key"]:
            raise ValueError("the gateway shows another key than the QR code")
        token = "yba_" + b64u(os.urandom(32))
        secret, public = E.generate()
        status, actor = call(code["gateway"], "POST", "/v1/actors", {"display_name": device_name, "e2e_key": public},
                             token)
        if status not in (200, 201):
            raise ValueError("enrollment refused: %s" % actor)
        self.state = {"gateway": code["gateway"], "bot_id": code["bot_id"], "bot_key": code["bot_key"],
                      "token": token, "secret": secret, "public": public, "actor_id": actor["actor_id"]}
        text = "%s %s %s" % (PAIR_PREFIX, code["pairing_id"],
                             proof(code["secret"], code["pairing_id"], code["bot_id"], actor["actor_id"]))
        return self.message(text, [])

    def conversation(self):
        if not self.state.get("conversation_id"):
            status, obj = call(self.state["gateway"], "POST", "/v1/bots/%s/conversations" % self.state["bot_id"],
                               {"op_key": secrets.token_hex(12)}, self.state["token"])
            if status not in (200, 201):
                raise ValueError("conversation refused: %s" % obj)
            self.state["conversation_id"] = obj["conversation_id"]
        return self.state["conversation_id"]

    def message(self, text, attachments):
        cid = self.conversation()
        bind = {"bot_id": self.state["bot_id"], "caller": self.state["actor_id"], "conversation_id": cid,
                "field": "msg"}
        recipients = [self.state["bot_key"], self.state["public"]]
        body = {"kind": "chat", "op_key": secrets.token_hex(12),
                "sealed": E.seal(canonical({"v": 1, "bind": bind, "text": text}), self.state["secret"], recipients,
                                 CTX)}
        if attachments:
            body["sealed_files"] = E.seal(canonical({"v": 1, "bind": dict(bind, field="files"),
                                                     "attachments": attachments}),
                                          self.state["secret"], recipients, CTX)
            body["files"] = {"count": len(attachments), "bytes": sum(
                len(base64.b64decode(a["data"])) if "data" in a else len(a["content"].encode("utf-8"))
                for a in attachments)}
        self.last_message = (cid, body)
        return self.retry_last_message()

    def retry_last_message(self):
        cid, body = self.last_message
        return call(self.state["gateway"], "POST", "/v1/conversations/%s/messages" % cid, body, self.state["token"])


def page_markdown(page):
    flags = page["status"] + ("（%s）" % "、".join(page["warnings"]) if page.get("warnings") else "")
    return "## 第 %d 页 · %s · %s\n\n%s\n" % (page["number"], page["id"][:8], flags, page["text"].strip())


def text_files(pages):
    """OCR markdown split into files of at most MAX_TEXT_FILE_BYTES, at page boundaries where possible."""
    chunks, current = [], ""
    for page in pages:
        section = page_markdown(page)
        while len(section.encode("utf-8")) > MAX_TEXT_FILE_BYTES:
            cut = MAX_TEXT_FILE_BYTES
            head = section.encode("utf-8")[:cut].decode("utf-8", "ignore")
            if current:
                chunks.append(current)
                current = ""
            chunks.append(head)
            section = section[len(head):]
        if len((current + "\n" + section).encode("utf-8")) > MAX_TEXT_FILE_BYTES:
            chunks.append(current)
            current = section
        else:
            current = current + "\n" + section if current else section
    if current:
        chunks.append(current)
    first, last = pages[0]["number"], pages[-1]["number"]
    if len(chunks) == 1:
        return [{"name": "ocr-%03d-%03d.md" % (first, last), "content": chunks[0]}]
    return [{"name": "ocr-%03d-%03d-%d.md" % (first, last, i + 1), "content": c} for i, c in enumerate(chunks)]


def pack(pages):
    """-> (pages in this batch, attachments, summary text). Pages carry number, id, status, warnings, text, jpeg."""
    chosen = []
    for page in pages[:MAX_PAGES]:
        trial = chosen + [page]
        files = text_files(trial)
        images = sum(len(p["jpeg"]) for p in trial)
        texts = sum(len(f["content"].encode("utf-8")) for f in files)
        if chosen and (len(trial) + len(files) > MAX_FILES or images + texts > MAX_TOTAL_BYTES):
            break
        chosen = trial
    attachments = [{"name": "page-%03d.jpg" % p["number"], "data": base64.b64encode(p["jpeg"]).decode("ascii")}
                   for p in chosen] + text_files(chosen)
    lines = ["Mokutori 扫描 · 第 %d–%d 页" % (chosen[0]["number"], chosen[-1]["number"])]
    for p in chosen:
        flags = "、".join(p.get("warnings") or [])
        lines.append("第 %d 页：%s%s" % (p["number"], p["status"], "（%s）" % flags if flags else ""))
    lines.append("OCR 全文在 ocr-*.md 附件里，图片是矫正后的页面。")
    return chosen, attachments, "\n".join(lines)[:MAX_TEXT_CHARS]
