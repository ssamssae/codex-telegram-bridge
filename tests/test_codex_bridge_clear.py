#!/usr/bin/env python3
"""Clear acknowledgements against both the source and exported bridge."""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock


class ClearAcknowledgementTest(unittest.TestCase):
    # The original file-identity cases exercise the pane-less ConPTY transport.
    # Native tmux cases below require a current visible reset receipt.
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        env = mock.patch.dict(os.environ, {
            "HOME": str(self.root), "USERPROFILE": str(self.root),
            "CRB_CHAT_ID": "1234", "CRB_FLOW_MIRROR": "0",
        })
        env.start()
        self.addCleanup(env.stop)
        base = Path(__file__).resolve().parents[1]
        source = base / "codex-repl-telegram-bridge.py"
        if not source.exists():
            source = base / "codex_repl_bridge.py"
        spec = importlib.util.spec_from_file_location("clear_bridge_under_test", source)
        self.mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = self.mod
        spec.loader.exec_module(self.mod)
        self.old = self.root / "old.jsonl"
        self.new = self.root / "new.jsonl"
        self.old.write_text("{}\n", encoding="utf-8")
        self.new.write_text("{}\n", encoding="utf-8")
        self.config = replace(
            self.mod.Config.from_env(), state_dir=self.root,
            state_path=self.root / "state.json", backfill_enabled=False,
            flow_mirror=False, long_running_progress_seconds=0,
        )
        self.repl = mock.Mock(spec=[
            "supports_pane_features", "session_file", "capture_screen",
            "paste_prompt", "clear_composer",
        ], supports_pane_features=False)
        self.repl.session_file.return_value = self.old
        self.repl.capture_screen.return_value = "› Ask a question\n"
        self.telegram = mock.Mock()
        self.telegram.send.return_value = True
        self.clock = 1000.0
        self.bridge = self.make_bridge()
        self.bridge.ensure_session_file()
        self.sleep = mock.patch.object(self.mod.time, "sleep")
        self.sleep.start()
        self.addCleanup(self.sleep.stop)

    def make_bridge(self):
        bridge = self.mod.Bridge(self.config, self.telegram, self.repl)
        bridge.now_fn = lambda: self.clock
        for name in [
            "begin_repl_typing", "begin_telegram_prompt_tracking",
            "stop_repl_typing", "stop_long_running_progress",
            "clear_active_telegram_prompt",
        ]:
            setattr(bridge, name, mock.Mock())
        bridge.repl_is_working = mock.Mock(return_value=False)
        return bridge

    def request(self, text="/clear"):
        self.bridge.process_telegram_update({
            "update_id": 1,
            "message": {"message_id": 2, "chat": {"id": 1234}, "text": text},
        })

    def sent(self):
        return [call.args[0] for call in self.telegram.send.call_args_list]

    def state(self):
        return json.loads(self.config.state_path.read_text(encoding="utf-8"))

    def switch_session(self, *_args):
        self.repl.session_file.return_value = self.new

    def test_immediate_clear_reports_completion_once(self):
        self.repl.paste_prompt.side_effect = self.switch_session
        self.request()
        self.bridge.ensure_session_file()
        self.assertEqual(self.sent(), ["세션을 클리어했습니다."])
        self.assertNotIn("pending_clear_watch", self.state())

    def test_delayed_session_has_pending_notice_then_completion_without_new_input(self):
        self.request()
        self.assertNotIn("세션을 클리어했습니다.", self.sent())
        self.assertIn("pending_clear_watch", self.state())
        self.switch_session()
        self.bridge.ensure_session_file()
        self.bridge.ensure_session_file()
        self.assertEqual(self.sent().count("세션을 클리어했습니다."), 1)
        self.assertEqual(self.repl.paste_prompt.call_count, 1)

    def test_unchanged_session_never_reports_success_from_elapsed_time(self):
        self.request()
        self.clock += 3600
        self.bridge.ensure_session_file()
        self.assertNotIn("세션을 클리어했습니다.", self.sent())

    def test_failed_completion_retries_without_another_session_change(self):
        self.request()
        self.switch_session()
        self.telegram.send.return_value = False
        self.bridge.ensure_session_file()
        self.assertIn("pending_clear_watch", self.state())
        attempts = self.telegram.send.call_count
        self.bridge.ensure_session_file()
        self.assertEqual(self.telegram.send.call_count, attempts)
        self.clock += 5
        self.telegram.send.return_value = True
        self.bridge.ensure_session_file()
        self.assertEqual(self.telegram.send.call_count, attempts + 1)
        self.assertNotIn("pending_clear_watch", self.state())

    def test_immediate_send_exception_survives_restart(self):
        self.repl.paste_prompt.side_effect = self.switch_session
        self.telegram.send.side_effect = OSError("temporary send failure")
        self.request()
        self.assertIn("pending_clear_watch", self.state())
        self.clock += 5
        self.telegram.send.side_effect = None
        resumed = self.make_bridge()
        resumed.ensure_session_file()
        self.assertNotIn("pending_clear_watch", self.state())
        self.assertEqual(self.sent().count("세션을 클리어했습니다."), 2)

    def test_pending_confirmation_survives_restart(self):
        self.request()
        self.switch_session()
        resumed = self.make_bridge()
        resumed.ensure_session_file()
        resumed.ensure_session_file()
        self.assertEqual(self.sent().count("세션을 클리어했습니다."), 1)

    def test_busy_clear_is_refused_without_pasting_or_waiting(self):
        self.bridge.repl_is_working.return_value = True
        self.request()
        self.repl.paste_prompt.assert_not_called()
        self.assertIn("실행하지 않았습니다", self.sent()[0])
        self.assertNotIn("pending_clear_watch", self.state())

    def test_bot_suffix_is_removed_and_chat_title_preserved(self):
        self.repl.paste_prompt.side_effect = self.switch_session
        self.request("/clear@my_bot release prep")
        self.repl.paste_prompt.assert_called_once_with("/clear release prep")
        self.assertEqual(self.sent(), ["세션을 클리어했습니다."])

    def test_unsupported_command_cancels_pending_confirmation(self):
        self.bridge.handle_slash_command_result = mock.Mock(return_value=True)
        self.request()
        self.switch_session()
        self.bridge.ensure_session_file()
        self.assertNotIn("세션을 클리어했습니다.", self.sent())
        self.assertNotIn("pending_clear_watch", self.state())

    def test_failed_paste_cannot_acknowledge_a_later_unrelated_switch(self):
        self.repl.paste_prompt.side_effect = RuntimeError("paste unavailable")
        self.request()
        self.switch_session()
        self.bridge.ensure_session_file()
        self.assertNotIn("세션을 클리어했습니다.", self.sent())
        self.assertNotIn("pending_clear_watch", self.state())

    def test_no_pending_request_means_no_clear_notice_for_session_switch(self):
        self.switch_session()
        self.bridge.ensure_session_file()
        self.assertEqual(self.sent(), [])

    def test_atomic_replacement_of_session_file_is_detected(self):
        self.request()
        self.new.replace(self.old)
        self.bridge.ensure_session_file()
        self.assertEqual(self.sent().count("세션을 클리어했습니다."), 1)

    def test_parallel_observers_send_one_completion(self):
        self.request()
        self.switch_session()
        barrier = threading.Barrier(3)
        def observe():
            barrier.wait()
            self.bridge.maybe_confirm_pending_clear()
        threads = [threading.Thread(target=observe) for _ in range(2)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(timeout=2)
            self.assertFalse(thread.is_alive())
        self.assertEqual(self.sent().count("세션을 클리어했습니다."), 1)


    SID = '01a08f32-bc03-75b3-ada9-d7f87e484d6c'

    def native(self):
        previous = self.root / f'rollout-2026-09-11T12-00-00-{self.SID}.jsonl'
        self.old.rename(previous)
        self.old = previous
        self.repl.session_file.return_value = previous
        self.repl.supports_pane_features = True
        self.screen = '› Ask Codex to do anything\n'
        self.repl.capture_visible_screen = mock.Mock(side_effect=lambda: self.screen)
        self.bridge.ensure_session_file()

    def receipt(self, sid=None):
        return (f'To continue this session, run codex resume, then\n'
                f'select 영상 자막 확인 ({sid or self.SID})\n'
                '› Ask Codex to do anything\n')

    def test_review_native_receipt_completes_before_new_rollout_exists(self):
        self.native()
        self.repl.paste_prompt.side_effect = lambda *_: setattr(self, 'screen', self.receipt())
        self.request()
        self.assertIn('세션을 클리어했습니다.', self.sent())
        self.assertEqual(self.repl.session_file(), self.old)

    def test_review_unrelated_rollout_switch_is_not_clear_completion(self):
        self.native()
        self.request()
        self.switch_session()
        self.bridge.ensure_session_file()
        self.assertNotIn('세션을 클리어했습니다.', self.sent())

    def test_review_expired_unconfirmed_request_gets_terminal_notice(self):
        self.native()
        self.request()
        self.clock += 91
        self.bridge.ensure_session_file()
        self.assertIn('세션 전환을 확인하지 못했습니다. 완료로 표시하지 않습니다.', self.sent())
        self.assertNotIn('pending_clear_watch', self.state())

    def test_review_disabled_clear_does_not_wait_for_unrelated_switch(self):
        self.native()
        self.repl.paste_prompt.side_effect = lambda *_: setattr(
            self, 'screen', "'/clear' is disabled while a task is in progress\n› Ask Codex to do anything\n")
        self.request()
        self.switch_session()
        self.bridge.ensure_session_file()
        self.assertNotIn('세션을 클리어했습니다.', self.sent())
        self.assertNotIn('pending_clear_watch', self.state())

    def test_review_stale_visible_receipt_is_not_completion(self):
        self.native()
        self.screen = self.receipt()
        self.request()
        self.switch_session()
        self.bridge.ensure_session_file()
        self.assertNotIn('세션을 클리어했습니다.', self.sent())

    def test_review_wrong_session_receipt_is_not_completion(self):
        self.native()
        self.request()
        self.screen = self.receipt('01a08f32-bc03-75b3-ada9-000000000000')
        self.switch_session()
        self.bridge.ensure_session_file()
        self.assertNotIn('세션을 클리어했습니다.', self.sent())

    def test_review_unconfirmed_restart_does_not_turn_other_rollout_into_success(self):
        self.native()
        self.request()
        self.switch_session()
        resumed = self.make_bridge()
        resumed.ensure_session_file()
        self.assertNotIn('세션을 클리어했습니다.', self.sent())

    def test_native_wrapped_receipt_matches_previous_session(self):
        self.native()
        self.request()
        self.screen = ('To continue this session, run codex resume, then\n'
                       'select 이전 작업의 긴 한국어 제목 (01a\n'
                       '08f32-bc03-75b3-ada9-d7f87e484d6c)\n'
                       '› Ask Codex to do anything\n')
        self.bridge.ensure_session_file()
        self.assertEqual(self.sent().count('세션을 클리어했습니다.'), 1)

    def test_native_quoted_receipt_never_confirms(self):
        self.native()
        self.request()
        self.screen = f'> To continue this session, run codex resume {self.SID}\n› Ask Codex to do anything\n'
        self.bridge.ensure_session_file()
        self.assertNotIn('세션을 클리어했습니다.', self.sent())

    def test_native_completion_retries_after_receipt_leaves_view(self):
        self.native()
        self.request()
        self.screen = self.receipt()
        self.telegram.send.return_value = False
        self.bridge.ensure_session_file()
        self.assertEqual(self.state()['pending_clear_watch']['outcome'], 'complete')
        self.clock += 5
        self.screen = '› Ask Codex to do anything\n'
        self.telegram.send.return_value = True
        self.bridge.ensure_session_file()
        self.assertEqual(self.sent().count('세션을 클리어했습니다.'), 2)
        self.assertNotIn('pending_clear_watch', self.state())

    def test_native_receipt_confirmation_survives_restart_without_new_rollout(self):
        self.native()
        self.request()
        self.screen = self.receipt()
        resumed = self.make_bridge()
        resumed.ensure_session_file()
        resumed.ensure_session_file()
        self.assertEqual(self.sent().count('세션을 클리어했습니다.'), 1)

    def test_timeout_retry_never_changes_into_success(self):
        self.native()
        self.request()
        self.clock += 91
        self.telegram.send.return_value = False
        self.bridge.ensure_session_file()
        self.assertEqual(self.state()['pending_clear_watch']['outcome'], 'timeout')
        self.clock += 5
        self.screen = self.receipt()
        self.switch_session()
        self.telegram.send.return_value = True
        resumed = self.make_bridge()
        resumed.ensure_session_file()
        self.assertNotIn('세션을 클리어했습니다.', self.sent())
        self.assertNotIn('pending_clear_watch', self.state())

    def test_native_side_refusal_is_reported_and_does_not_remain_pending(self):
        self.native()
        self.repl.paste_prompt.side_effect = lambda *_: setattr(
            self, 'screen', "'/clear' is unavailable in side conversations\n")
        self.request()
        self.assertTrue(any('보조 대화' in line for line in self.sent()))
        self.assertNotIn('pending_clear_watch', self.state())

    def test_repeated_request_preserves_pending_confirmation_and_does_not_reclear(self):
        self.native()
        self.request()
        previous = self.state()['pending_clear_watch']
        self.request()
        self.assertEqual(self.repl.paste_prompt.call_count, 1)
        self.assertEqual(self.state()['pending_clear_watch'], previous)

    def test_native_capture_excludes_scrollback(self):
        transport = object.__new__(self.mod.TmuxTransport)
        transport.config = self.config
        transport.tmux = mock.Mock(return_value=mock.Mock(stdout='visible receipt'))
        self.assertEqual(transport.capture_visible_screen(), 'visible receipt')
        self.assertNotIn('-S', transport.tmux.call_args.args)


    def test_native_receipt_survives_session_lookup_failure(self):
        self.native()
        self.request()
        previous = self.state()
        self.screen = self.receipt()
        self.repl.session_file.side_effect = RuntimeError('new rollout not created yet')
        with self.assertRaises(RuntimeError):
            self.bridge.ensure_session_file()
        self.assertEqual(self.sent().count('세션을 클리어했습니다.'), 1)
        self.assertNotIn('pending_clear_watch', self.state())
        self.assertEqual(self.state()['offset'], previous['offset'])

    def test_restart_receipt_without_rollout_bind_is_persisted_once(self):
        self.native()
        self.request()
        previous = self.state()
        self.screen = self.receipt()
        self.repl.session_file.side_effect = RuntimeError('new rollout not created yet')
        resumed = self.make_bridge()
        with self.assertRaises(RuntimeError):
            resumed.ensure_session_file()
        again = self.make_bridge()
        with self.assertRaises(RuntimeError):
            again.ensure_session_file()
        self.assertEqual(self.sent().count('세션을 클리어했습니다.'), 1)
        self.assertNotIn('pending_clear_watch', self.state())
        self.assertEqual(self.state(), {k: v for k, v in previous.items() if k != 'pending_clear_watch'})

    def test_restart_timeout_runs_without_rollout_bind(self):
        self.native()
        self.request()
        previous = self.state()
        self.clock += 91
        self.repl.session_file.side_effect = RuntimeError('rollout unavailable')
        resumed = self.make_bridge()
        with self.assertRaises(RuntimeError):
            resumed.ensure_session_file()
        self.assertIn('세션 전환을 확인하지 못했습니다. 완료로 표시하지 않습니다.', self.sent())
        self.assertNotIn('pending_clear_watch', self.state())
        self.assertEqual(self.state()['offset'], previous['offset'])


if __name__ == "__main__":
    unittest.main()
