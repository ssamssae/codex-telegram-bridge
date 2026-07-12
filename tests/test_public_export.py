import importlib.util
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


class PublicExportTest(unittest.TestCase):
    def test_imports_public_bridge_and_defaults(self):
        path = Path(__file__).resolve().parents[1] / "codex_repl_bridge.py"
        spec = importlib.util.spec_from_file_location("codex_repl_bridge", path)
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)
        self.assertEqual(mod.node_defaults()[0], "codex")
        with mock.patch.dict(
            os.environ,
            {
                "CRB_CHAT_ID": "123456789",
                "CRB_TOKEN_FILE": str(Path.home() / ".config/codex-telegram-bridge/token.json"),
            },
            clear=True,
        ):
            cfg = mod.Config.from_env()
        self.assertEqual(cfg.chat_id, "123456789")
        self.assertTrue(str(cfg.state_dir).endswith(".local/state/codex-telegram-bridge"))
        self.assertTrue(str(cfg.directive_signal_path).endswith("received-directive.jsonl"))
        self.assertEqual(mod.extract_codex_context_text("Model: gpt-5"), "Codex context not visible yet.")
        progress = mod.format_long_running_progress_message(
            "deploy bridge",
            630,
            task_id="T-260624-11",
            recent_progress="checks running",
        )
        self.assertTrue(progress.startswith("Progress update\n\n✓ "))
        self.assertIn("10 min", progress)
        self.assertIn("final answer", progress)
        self.assertIn("checks running", progress)
        source = path.read_text(encoding="utf-8")
        self.assertNotIn("asc-release-hold", source)
        self.assertIsNone(mod.release_hold_response("출시 멈춰 memoyo"))
        # T-260701-68: stripped mesh layer must leave working no-op stubs
        self.assertIsNone(mod.mesh_cutover_call("sendMessage", {}))
        self.assertIsNone(mod.mesh_ledger_record())

    def test_private_dm_removes_leading_decoration_and_group_keeps_context(self):
        path = Path(__file__).resolve().parents[1] / "codex_repl_bridge.py"
        spec = importlib.util.spec_from_file_location("codex_repl_bridge_behavior", path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)

        private = mod.TelegramClient("token", "1234", "BOT", 4096)
        private_calls = []
        private.call = lambda method, **params: private_calls.append((method, params)) or {"ok": True}
        self.assertEqual(private.with_emoji_prefix("🙂😄👋 hello"), "hello")
        self.assertEqual(private.with_emoji_prefix("🍎"), "🍎")
        private.send("answer", reply_to_message_id=42)
        self.assertEqual(private_calls[-1][1]["reply_to_message_id"], 42)

        group = mod.TelegramClient("token", "-1234", "BOT", 4096)
        group_calls = []
        group.call = lambda method, **params: group_calls.append((method, params)) or {"ok": True}
        group.send("answer", reply_to_message_id=42)
        self.assertEqual(group_calls[-1][1]["text"], "BOT\nanswer")
        self.assertEqual(group_calls[-1][1]["reply_to_message_id"], 42)

    def test_exec_bridge_private_emoji_only_is_not_emptied(self):
        path = Path(__file__).resolve().parents[1] / "telegram_agent_bridge.py"
        spec = importlib.util.spec_from_file_location("telegram_agent_bridge_behavior", path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = mod
        sys.path.insert(0, str(path.parent))
        try:
            spec.loader.exec_module(mod)
        finally:
            sys.path.pop(0)
        bridge = mod.Bridge.__new__(mod.Bridge)
        bridge.config = SimpleNamespace(
            chat_id="1234",
            prefix="BOT",
            prefix_line=False,
            telegram_chunk=4096,
        )

        self.assertEqual(bridge.telegram_chunks("🍎"), ["🍎"])


if __name__ == "__main__":
    unittest.main()
