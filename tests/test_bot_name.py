"""Synthetic checks of the bot name rules and setup's agent record; no HTTP requests or real credentials."""
from contextlib import redirect_stdout
import io
import json
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from test_mokutori_plugin import plugin


class BotName(unittest.TestCase):
    def test_name_is_required_and_identifies_the_agent(self):
        self.assertEqual(plugin.bot_name("  书房   助手 "), "书房 助手")
        self.assertEqual(plugin.bot_name("x" * 80), "x" * 80)
        for value in (None, "", "   ", "x" * 81, "Mokutori", " mokutori ", "line\nbreak"):
            with self.subTest(value=value), self.assertRaises(plugin.MokutoriError):
                plugin.bot_name(value)
        with self.assertRaisesRegex(plugin.MokutoriError, "--name is required"):
            plugin.bot_name(None)

    def test_new_setup_without_name_stops_before_the_gateway(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = plugin.Home(temporary)
            args = SimpleNamespace(name=None, agent="grok", force=False, gateway=None, owner_home=None,
                                   email="owner@example.invalid", email_code=None, handle=None, lang=None)
            with mock.patch.object(plugin, "engine") as engine, self.assertRaisesRegex(plugin.MokutoriError, "--name"):
                plugin.cmd_setup(home, args)
            engine.assert_not_called()

    def test_existing_setup_records_a_missing_agent_once(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = plugin.Home(temporary)
            home.save({"v": 1, "setup": {"gateway": "https://gateway.invalid", "bot_id": "bot_fixture"},
                       "pairings": {}, "devices": {}})
            args = SimpleNamespace(name=None, agent="grok", force=False)
            with mock.patch.object(plugin, "engine") as engine:
                with redirect_stdout(io.StringIO()) as stream, self.assertRaises(SystemExit):
                    plugin.cmd_setup(home, args)
                self.assertTrue(json.loads(stream.getvalue())["already_set_up"])
                self.assertEqual(home.state()["setup"]["agent"], "grok")
                with redirect_stdout(io.StringIO()), self.assertRaises(SystemExit):
                    plugin.cmd_setup(home, SimpleNamespace(name=None, agent=None, force=False))
                with self.assertRaisesRegex(plugin.MokutoriError, "new setup"):
                    plugin.cmd_setup(home, SimpleNamespace(name=None, agent="muse", force=False))
            engine.assert_not_called()
            self.assertEqual(home.state()["setup"]["agent"], "grok")


if __name__ == "__main__":
    unittest.main()
