"""Synthetic checks of tap-to-pair links; no HTTP requests or real credentials."""
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock
import urllib.parse
import urllib.request

from test_mokutori_plugin import app, plugin


class PairingLinks(unittest.TestCase):
    def setUp(self):
        self.setup = {"gateway": "https://gateway.example/日本語%20space", "bot_id": "bot_abcdefghijklmnop",
                      "bot_key": "synthetic+key/with=padding"}
        self.pairing_id = "0123456789ab"
        self.secret = "A" * 43

    def test_https_fragment_matches_existing_scheme_payload(self):
        scheme = plugin.pairing_url(self.setup, self.pairing_id, self.secret)
        link = plugin.pairing_link(self.setup, self.pairing_id, self.secret)
        parsed = urllib.parse.urlsplit(link)
        self.assertEqual((parsed.scheme, parsed.netloc, parsed.path, parsed.query),
                         ("https", "mokutori.com", "/pair", ""))
        self.assertEqual(parsed.fragment, urllib.parse.urlsplit(scheme).query)
        self.assertEqual(urllib.parse.parse_qsl(parsed.fragment), [
            ("v", "1"), ("g", self.setup["gateway"]), ("b", self.setup["bot_id"]),
            ("k", self.setup["bot_key"]), ("i", self.pairing_id), ("s", self.secret)])
        self.assertNotIn("&amp;", link)
        self.assertIn("synthetic%2Bkey%2Fwith%3Dpadding", link)
        # Standard HTTP strips the fragment; neither the secret nor routing
        # fields form part of the request path sent to mokutori.com.
        self.assertEqual(urllib.request.Request(link).selector, "/pair")

    def test_cli_returns_both_links_and_keeps_qr_scheme(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = plugin.Home(temporary)
            home.save({"v": 1, "setup": self.setup, "pairings": {}, "devices": {}})
            args = SimpleNamespace(minutes=30, label=None, png=None, terminal=False, wait=0)
            stream = io.StringIO()
            with mock.patch.object(plugin, "qr_matrix", wraps=plugin.qr_matrix) as matrix:
                with redirect_stdout(stream), self.assertRaises(SystemExit) as stopped:
                    plugin.cmd_pair(home, args)
                self.assertEqual(stopped.exception.code, 0)
                result = json.loads(stream.getvalue())
                matrix.assert_called_once_with(result["pair_url"])
            self.assertTrue(Path(result["qr_png"]).read_bytes().startswith(b"\x89PNG"))
            self.assertEqual(app.parse_pairing(result["pair_link"]), app.parse_pairing(result["pair_url"]))
            self.assertEqual(home.state()["pairings"][result["pairing_id"]]["expires_at"], result["expires_at"])
            self.assertEqual(len(home.state()["pairings"]), 1)
            self.assertIn("both the QR PNG and pair_link", result["show_owner"])
            self.assertIn("paired list is nonempty", result["next"])
            self.assertNotIn("paired", result)

    def test_reference_rejects_offsite_or_query_based_https_links(self):
        link = plugin.pairing_link(self.setup, self.pairing_id, self.secret)
        invalid = [link.replace("https:", "http:"), link.replace("mokutori.com", "other.example"),
                   link.replace("/pair#", "/other#"), link.replace("/pair#", "/pair?"),
                   link.replace("/pair#", "/pair?extra=1#"), link.replace("mokutori.com", "mokutori.com:443"),
                   link + "&s=other", link + "&unexpected=1"]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValueError):
                app.parse_pairing(value)


if __name__ == "__main__":
    unittest.main()
