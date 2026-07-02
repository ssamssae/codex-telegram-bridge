import importlib.util
import os
import sys
import unittest
from pathlib import Path
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


if __name__ == "__main__":
    unittest.main()
