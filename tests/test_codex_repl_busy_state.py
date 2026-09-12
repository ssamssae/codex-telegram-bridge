#!/usr/bin/env python3
"""Distinguish stale pane chrome after a final from a fresh active turn."""
from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock


class CompletedTurnBusyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        environment = mock.patch.dict(os.environ, {"CRB_CHAT_ID": "1234", "CRB_FLOW_MIRROR": "0"})
        environment.start()
        self.addCleanup(environment.stop)
        scripts = Path(__file__).resolve().parents[1]
        source = scripts / "codex-repl-telegram-bridge.py"
        if not source.exists():
            source = scripts / "codex_repl_bridge.py"
        spec = importlib.util.spec_from_file_location("busy_state_bridge_test", source)
        self.mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = self.mod
        spec.loader.exec_module(self.mod)
        config = replace(
            self.mod.Config.from_env(), state_dir=self.root,
            state_path=self.root / "state.json", chat_id="1234",
            backfill_enabled=False, flow_mirror=False,
            long_running_progress_seconds=0,
        )
        self.session = self.root / "rollout-11111111-2222-3333-4444-555555555555.jsonl"
        self.session.write_text("{}\n", encoding="utf-8")
        self.repl = mock.Mock(spec=[
            "supports_pane_features", "capture_screen", "capture_visible_screen",
            "session_file", "paste_prompt", "clear_composer",
        ], supports_pane_features=True)
        self.repl.session_file.return_value = self.session
        self.repl.capture_screen.return_value = "• Working (1m • esc to interrupt)\n"
        self.repl.capture_visible_screen.return_value = "› Ask Codex to do anything\n"
        self.telegram = mock.Mock()
        self.telegram.send.return_value = True
        self.bridge = self.mod.Bridge(config, self.telegram, self.repl)
        self.bridge.now_fn = lambda: 1000.0
        self.bridge.ensure_session_file()
        for name in (
            "begin_repl_typing", "stop_repl_typing", "stop_long_running_progress",
            "clear_and_paste_prompt", "clear_active_telegram_prompt",
        ):
            setattr(self.bridge, name, mock.Mock())
        sleeper = mock.patch.object(self.mod.time, "sleep")
        sleeper.start()
        self.addCleanup(sleeper.stop)
        self.bridge.mark_repl_turn_finished()

    def working(self, visible):
        self.repl.capture_visible_screen.return_value = visible
        self.repl.capture_screen.return_value = visible

    def request_clear(self):
        self.bridge.process_telegram_update({
            "update_id": 1,
            "message": {"message_id": 2, "chat": {"id": 1234}, "text": "/clear"},
        })

    def test_scrollback_working_does_not_block_idle_visible_pane(self):
        self.assertFalse(self.bridge.repl_is_working())

    def test_completed_turn_ignores_stale_queued_footer(self):
        self.working("답변 완료\n• Queued follow-up inputs\n› Ask Codex to do anything\n")
        self.assertFalse(self.bridge.repl_is_working())

    def test_completed_turn_ignores_working_word_in_answer(self):
        self.working("• Working 표시가 없으면 완료 상태입니다.\n› Ask Codex to do anything\n")
        self.assertFalse(self.bridge.repl_is_working())

    def test_completed_turn_ignores_quoted_interrupt_hint(self):
        self.working('답변: "esc to interrupt" 문구를 확인했습니다.\n› Ask Codex to do anything\n')
        self.assertFalse(self.bridge.repl_is_working())

    def test_idle_clear_is_delivered_after_completed_turn(self):
        self.working("답변 완료\n• Queued follow-up inputs\n› Ask Codex to do anything\n")
        self.request_clear()
        self.bridge.clear_and_paste_prompt.assert_called_once()
        self.assertEqual(self.bridge.clear_and_paste_prompt.call_args.args[0], "/clear")
        self.assertNotIn(self.mod.CLEAR_SLASH_BUSY,
                         [call.args[0] for call in self.telegram.send.call_args_list])

    def test_fresh_native_working_still_blocks_clear_after_old_final(self):
        self.working("• Working (3s • esc to interrupt)\n› Ask Codex to do anything\n")
        self.assertTrue(self.bridge.repl_is_working())
        self.request_clear()
        self.bridge.clear_and_paste_prompt.assert_not_called()
        self.assertIn(self.mod.CLEAR_SLASH_BUSY,
                      [call.args[0] for call in self.telegram.send.call_args_list])

    def test_truncated_or_legacy_live_working_rows_remain_busy(self):
        for row in (
            "• Working (15m 42s • esc to interru…)",
            "• Working (15m 42s • esc to interru…",
            "◦ Working (1m 16s)",
            "Working (1m · esc to interrupt)",
            "• Working (1m • esc to interrupt) · 2 background tasks",
        ):
            with self.subTest(row=row):
                self.working(row + "\n› Ask Codex to do anything\n")
                self.assertTrue(self.bridge.repl_is_working())

    def test_new_pending_prompt_keeps_queued_work_busy(self):
        self.working("• Queued follow-up inputs\n  ↳ 새로운 질문\n› Ask Codex to do anything\n")
        self.bridge.begin_telegram_prompt_tracking("새로운 질문")
        self.assertTrue(self.bridge.repl_is_working())

    def test_active_turn_keeps_queued_work_busy(self):
        self.working("• Queued follow-up inputs\n› Ask Codex to do anything\n")
        self.bridge.suppress_until_user = False
        self.bridge.current_origin = "terminal"
        self.bridge.last_repl_activity_at = 1000.0
        self.assertTrue(self.bridge.repl_is_working())

    def test_legacy_transport_without_visible_capture_remains_supported(self):
        del self.repl.capture_visible_screen
        self.repl.capture_screen.return_value = "Working (1m · esc to interrupt)\n"
        self.assertTrue(self.bridge.repl_is_working())

    def test_paneless_transport_does_not_probe_screen(self):
        self.repl.supports_pane_features = False
        self.assertFalse(self.bridge.repl_is_working())
        self.repl.capture_screen.assert_not_called()
        self.repl.capture_visible_screen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
