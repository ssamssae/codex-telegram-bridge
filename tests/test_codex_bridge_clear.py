#!/usr/bin/env python3
"""Clear acknowledgements against both the source and exported bridge."""
from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
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
            "CODEX_HOME": str(self.root / ".codex"),
            "CRB_CHAT_ID": "1234", "CRB_FLOW_MIRROR": "0", "CRB_LANGUAGE": "ko",
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

    def log_transport(self):
        self.native()
        self.transport = object.__new__(self.mod.TmuxTransport)
        self.transport.pane_pid = mock.Mock(return_value=120)
        self.process_tree = {120: {120, 121}, 121: {121}}
        patch = mock.patch.object(self.mod, "descendants", side_effect=lambda pid: self.process_tree.get(pid, {pid}))
        patch.start()
        self.addCleanup(patch.stop)
        self.log_path = self.root / ".codex" / "logs_2.sqlite"
        self.log_path.parent.mkdir()
        self.db = sqlite3.connect(self.log_path)
        self.addCleanup(self.db.close)
        self.db.execute("CREATE TABLE logs (id INTEGER PRIMARY KEY, target TEXT, process_uuid TEXT, feedback_log_body TEXT, ts INTEGER, ts_nanos INTEGER)")
        self.owner = "pid:121:11111111-1111-1111-1111-111111111111"
        self.db.execute("INSERT INTO logs (id,target,process_uuid,feedback_log_body) VALUES (1, 'codex_tui::chatwidget', ?, 'idle')", (self.owner,))
        self.db.commit()
        self.repl.clear_log_cursor = self.transport.clear_log_cursor
        self.repl.clear_log_confirms_reset = self.transport.clear_log_confirms_reset
        self.screen = "OpenAI Codex (v0.155.1)\n› Ask Codex to do anything\nContext 0% used\n"

    def start_receipt(self, owner=None, method="thread/start", client="codex-tui", at_ns=None, request_id="7", connection_id="1"):
        now = time.time_ns() if at_ns is None else at_ns
        self.db.execute("INSERT INTO logs (target, process_uuid, feedback_log_body, ts, ts_nanos) VALUES (?, ?, ?, ?, ?)", (
            "codex_app_server::outgoing_message", owner or self.owner,
            f'app_server.request{{rpc.method="{method}" app_server.client_name="{client}" rpc.request_id={request_id} app_server.connection_id={connection_id}}}: '
            'app-server event: thread/started targeted_connections=0', now // 1_000_000_000, now % 1_000_000_000))
        self.db.commit()

    NEW_SID = '01a08f32-bc04-75b3-ada9-d7f87e484d6c'

    def identity_transport(self):
        from contextlib import nullcontext
        self.log_transport()
        self.transport.composer_lock = lambda: nullcontext()
        self.active_id = self.SID
        self.status_count = 0
        self.history = ''
        self.screen = '› Ask Codex to do anything\nGPT-6-Astra max · ~ · Context 0% used\n? for shortcuts\n'
        self.transport.capture_visible_screen = lambda: self.screen
        self.transport.capture_screen = lambda *_: self.history + self.screen
        self.commands = []
        self.keys = []
        self.transport.config = self.config
        self.alternate = False

        def tmux(*args, **kwargs):
            if args[0] == 'display-message':
                return mock.Mock(stdout='1' if self.alternate else '0')
            self.keys.append(args[-1])
            if args[-1] == 'C-l':
                self.history = ''
        self.transport.tmux = tmux
        self.emit_start = True
        self.explicit_receipt_id = ''

        def paste(text):
            self.commands.append(text)
            if text == '/status':
                self.status_count += 1
                self.history += ('› /status\n╭────────────────────╮\n'
                                 '│ OpenAI Codex (v0.162.1) │\n'
                                 f'│ Session: {self.active_id} │\n'
                                 '╰────────────────────╯\n')
            elif text.startswith('/clear'):
                self.active_id = self.NEW_SID
                self.history = ''
                if self.emit_start:
                    self.start_receipt()
                    if self.explicit_receipt_id:
                        self.db.execute("UPDATE logs SET feedback_log_body=feedback_log_body || ? WHERE id=(SELECT max(id) FROM logs)",
                                        (f' thread_id="{self.explicit_receipt_id}"',))
                        self.db.commit()
        self.transport._paste_prompt_unlocked = paste
        self.repl.submit_clear_with_identity = self.transport.submit_clear_with_identity
        self.repl.clear_identity_confirms_reset = self.transport.clear_identity_confirms_reset

    def test_identity_clear_matches_live_status_without_rollout_creation(self):
        self.identity_transport()
        self.request()
        self.assertEqual(self.commands, ['/status', '/clear', '/status'])
        self.assertEqual(self.sent(), [self.mod.CLEAR_SLASH_COMPLETE])
        self.assertEqual(self.repl.session_file(), self.old)

    def cropped_status_transport(self):
        self.identity_transport()
        self.alternate = True
        self.transport.capture_visible_screen = lambda: self.history + self.screen
        paste = self.transport._paste_prompt_unlocked

        def cropped(text):
            paste(text)
            if text == '/status':
                # Observed 55x23 alternate screen: no command/title/scrollback.
                # Codex 0.162.1 always renders Agents.md (tui/src/status/card.rs).
                self.history = ('  Model: GPT-6-Astra (reasoning max)\n'
                                '  Directory: /\n  Permissions: read only\n'
                                '  Agents.md: <none>\n'
                                f'  Session:\n    {self.active_id[:32]}\n'
                                f'    {self.active_id[32:]}\n\n'
                                '  Context window: 96% left\n')
                if self.active_id == self.NEW_SID:
                    # Empty threads omit context usage, exposing the end of
                    # the native usage-link introduction above Model.
                    self.history = ('  settings/usage for up-to-date\n'
                                    '  information on rate limits and credits\n' + self.history)
        self.transport._paste_prompt_unlocked = cropped

    def test_cropped_alternate_status_completes_without_command_echo(self):
        self.cropped_status_transport()
        self.request()
        self.assertEqual(self.sent(), [self.mod.CLEAR_SLASH_COMPLETE])
        self.assertEqual(self.commands, ['/status', '/clear', '/status'])
        self.assertEqual(self.history, '')

    def test_ineffective_view_clear_cannot_adopt_old_cropped_status(self):
        self.cropped_status_transport()
        self.transport._paste_prompt_unlocked('/status')
        tmux = self.transport.tmux
        self.transport.tmux = lambda *args, **kwargs: (
            self.keys.append('C-l') if args[-1] == 'C-l' else tmux(*args, **kwargs))
        self.transport._paste_prompt_unlocked = self.commands.append
        self.commands.clear()
        self.request()
        self.assertEqual(self.commands, [])
        self.assertNotIn(self.mod.CLEAR_SLASH_COMPLETE, self.sent())

    def test_cropped_status_requires_fresh_probe_and_unambiguous_native_fields(self):
        self.cropped_status_transport()
        self.transport._paste_prompt_unlocked('/status')
        valid = self.history
        self.assertEqual(self.mod.clear_status_session_id(valid), '')
        self.assertEqual(self.mod.clear_status_session_id(valid, allow_cropped=True), self.SID)
        for invalid in (
            valid.replace('  Directory: /\n', ''),
            valid.replace('  Model: GPT-6-Astra (reasoning max)\n', 'some prose\n'),
            valid.replace('  Session:\n', '  Session:\n\n'),
            valid + f'  Session: {self.NEW_SID}\n',
        ):
            with self.subTest(invalid=invalid):
                self.assertEqual(self.mod.clear_status_session_id(invalid, allow_cropped=True), '')

    def test_cropped_invalid_id_cleans_own_display_without_submitting_clear(self):
        self.cropped_status_transport()
        paste = self.transport._paste_prompt_unlocked

        def invalid(text):
            paste(text)
            self.history = self.history.replace(self.active_id[:32], 'unavailable')
        self.transport._paste_prompt_unlocked = invalid
        self.request()
        self.assertEqual(self.commands, ['/status'])
        self.assertEqual(self.history, '')
        self.assertEqual(self.keys, ['C-l', 'C-l'])
        self.assertNotIn(self.mod.CLEAR_SLASH_COMPLETE, self.sent())

    # Exact live pane capture (55x23, alternate_on=1) after the probe's /status
    # on 2026-10-14 03:48 KST. A wrapped thread name and session value made the
    # native card taller than the screen: the title and Model scrolled off and
    # only Model's value continuation remained on the first row. Account,
    # thread name and IDs are replaced; columns and wraps are unchanged.
    LIVE_TALL_FOOTER = ('› Ask Codex to do anything\n\n'
                        '  GPT-6-Astra max · / · Context 71% used · weekly 64% …\n'
                        '  ? for shortcuts\n')

    def live_tall_status(self, sid):
        return ('                       summaries auto)\n'
                '  Model provider:      openai\n'
                '  Directory:           /\n'
                '  Permissions:         Full Access\n'
                '  Agents.md:           Users/user/.codex/AGENTS.md\n'
                '  Account:             user@example.com (Plus)\n'
                '  Thread name:         긴 시험 대화 제목이 화면 폭을 넘\n'
                '                       겨\n'
                '  Collaboration mode:  Default\n'
                '  Session:\n'
                f'                       {sid[:32]}\n'
                f'                       {sid[32:]}\n'
                '\n'
                '  Context window:      29% left (187K used / 258K)\n'
                '  Weekly limit:        [█████████████░░░░░░░] 64% left\n'
                '                       (resets 2:04 PM on 20 Oct)\n'
                '  Credits:             15596 credits\n'
                '\n\n')

    def test_cropped_status_reads_live_card_after_model_scrolled_off(self):
        screen = self.live_tall_status(self.SID) + self.LIVE_TALL_FOOTER
        self.assertEqual(len(screen.splitlines()), 23)
        self.assertTrue(self.mod.clear_identity_probe_ready(screen))
        self.assertEqual(self.mod.clear_status_session_id(screen, allow_cropped=True), self.SID)
        # A cropped card stays untrusted unless the probe saw its own reset view.
        self.assertEqual(self.mod.clear_status_session_id(screen), '')

    def test_cropped_tall_status_completes_clear_and_cleans_its_display(self):
        self.cropped_status_transport()
        paste = self.transport._paste_prompt_unlocked

        def tall(text):
            paste(text)
            if text == '/status' and self.active_id == self.SID:
                self.history = self.live_tall_status(self.active_id)
        self.transport._paste_prompt_unlocked = tall
        self.request()
        self.assertEqual(self.sent(), [self.mod.CLEAR_SLASH_COMPLETE])
        self.assertEqual(self.commands, ['/status', '/clear', '/status'])
        # Each probe resets the view, then removes its own status display.
        self.assertEqual(self.keys, ['C-l', 'C-l', 'C-l', 'C-l'])
        self.assertEqual(self.history, '')

    def test_cropped_status_reads_every_top_crop_with_two_native_fields(self):
        rows = self.live_tall_status(self.SID).splitlines(keepends=True)
        session = rows.index('  Session:\n')
        for top in range(session + 1):
            screen = ''.join(rows[top:]) + self.LIVE_TALL_FOOTER
            fields = sum(1 for row in rows[top:session] if row.strip() and not row.startswith('   '))
            with self.subTest(top=top, fields=fields):
                self.assertEqual(self.mod.clear_status_session_id(screen, allow_cropped=True),
                                 self.SID if fields >= 2 else '')

    def test_cropped_status_rejects_rows_the_screen_top_cannot_explain(self):
        valid = self.live_tall_status(self.SID) + self.LIVE_TALL_FOOTER
        remnant = '                       summaries auto)\n'
        directory = '  Directory:           /\n'
        agents = '  Agents.md:           Users/user/.codex/AGENTS.md\n'
        account = '  Account:             user@example.com (Plus)\n'
        self.assertEqual(self.mod.clear_status_session_id(valid, allow_cropped=True), self.SID)
        rejected = {
            # Only the cut-off continuation of an earlier value may sit above
            # the first visible field. Other rows mean the card start was not
            # cropped, so nothing explains the missing Model.
            'prose_at_top': valid.replace(remnant, 'previous reply\n'),
            'blank_at_top': valid.replace(remnant, remnant + '\n'),
            # A field the card always renders cannot vanish below a visible one.
            'missing_always_rendered': valid.replace(agents, ''),
            # Native fields keep their render order and appear once.
            'out_of_order': valid.replace(directory, '').replace(account, account + directory),
            'repeated': valid.replace(account, account + account),
            'identity_below_session': valid.replace('  Credits:', directory + '  Credits:'),
            # Nothing but field rows may interrupt the card above Session.
            'transcript_inside': valid.replace(account, account + 'previous reply\n'),
            'blank_inside': valid.replace(account, account + '\n'),
            'second_session': valid.replace('  Credits:', f'  Session: {self.NEW_SID}\n  Credits:'),
        }
        for name, screen in rejected.items():
            with self.subTest(screen=name):
                self.assertEqual(self.mod.clear_status_session_id(screen, allow_cropped=True), '')

    def test_cropped_status_accepts_other_native_rows_and_newer_fields(self):
        tall = self.live_tall_status(self.SID)
        tail = f'                       {self.SID[32:]}\n'
        # Codex 0.162.1 (tui/src/status/card.rs) renders Forked from below
        # Session and, for a daemon connection, Server plus a blank row above
        # Model. The short card is the empty-thread layout without thread name
        # and context usage.
        forked = tall.replace(tail, tail + '  Forked from:\n'
                              f'                       {self.NEW_SID[:32]}\n'
                              f'                       {self.NEW_SID[32:]}\n')
        short = ('  Server:              Local background server\n\n'
                 '  Model:               GPT-6-Astra (reasoning max,\n'
                 + tall.replace('  Thread name:         긴 시험 대화 제목이 화면 폭을 넘\n'
                                '                       겨\n', '')
                 .replace('  Context window:      29% left (187K used / 258K)\n', ''))
        # A field a newer Codex adds above Session must not break /clear again.
        newer = '  Reasoning:           max\n'
        screens = {
            'forked_from_below_session': forked,
            'server_above_model': short,
            'newer_field_on_short_card': short.replace('  Directory:', newer + '  Directory:'),
            'newer_field_on_cropped_card': tall.replace('  Directory:', newer + '  Directory:'),
        }
        for name, card in screens.items():
            with self.subTest(screen=name):
                self.assertEqual(self.mod.clear_status_session_id(
                    card + self.LIVE_TALL_FOOTER, allow_cropped=True), self.SID)

    def test_view_reset_rejects_unmatched_directory_and_unknown_transcript(self):
        self.identity_transport()
        banner = '  >_ OpenAI Codex (v0.162.1)\n     ~\n'
        self.assertTrue(self.mod.clear_probe_view_is_reset(banner + self.screen))
        self.assertFalse(self.mod.clear_probe_view_is_reset(banner.replace('~', '/elsewhere') + self.screen))
        self.assertFalse(self.mod.clear_probe_view_is_reset('previous reply\n' + self.screen))
        self.assertFalse(self.mod.clear_probe_view_is_reset(self.screen.replace('Ask Codex to do anything', 'draft')))

    def test_view_reset_accepts_native_braille_logo_but_not_status_or_transcript(self):
        self.identity_transport()
        # Observed 119x61 Codex 0.162.1 reset view; narrow panes omit the logo.
        banner = ('  >_ OpenAI Codex (v0.162.1)\n     ~\n\n'
                  '                 ⣀⣤⣤⣤⣀⣀\n'
                  '            ⣾⣿⣿⠃ ⢸⣿⣿⡇ ⢸⣿⣿⠋⠁  ⠈⠙⢻⣿⣿⣿⣶⣤⡀ ⠹⢿⣿⣷⡄\n'
                  '                    ⠈⠙⠛⠿⠿⠿⠿⠛⠉\n')
        self.assertTrue(self.mod.clear_probe_view_is_reset(banner + self.screen))
        for extra in ('previous reply\n', f'Session: {self.SID}\n',
                      'Model: GPT-6-Astra\n', '› /status\n', '⣿ text ⣿\n'):
            with self.subTest(extra=extra):
                self.assertFalse(self.mod.clear_probe_view_is_reset(banner + extra + self.screen))
        self.assertFalse(self.mod.clear_probe_view_is_reset(
            banner + self.screen.replace('Ask Codex to do anything', 'unfinished draft')))

    def test_alternate_logo_after_view_clear_allows_fresh_identity_probe(self):
        self.cropped_status_transport()
        tmux = self.transport.tmux
        banner = '  >_ OpenAI Codex (v0.162.1)\n     ~\n\n     ⣾⣿⣿⠃ ⢸⣿⣿⡇\n'

        def reset_with_logo(*args, **kwargs):
            result = tmux(*args, **kwargs)
            if args[-1] == 'C-l':
                self.history = banner
            return result
        self.transport.tmux = reset_with_logo
        self.request()
        self.assertEqual(self.commands, ['/status', '/clear', '/status'])
        self.assertEqual(self.sent(), [self.mod.CLEAR_SLASH_COMPLETE])
        self.assertEqual(self.history, banner)

    def test_view_reset_accepts_yolo_permissions_line_but_not_other_chrome(self):
        self.identity_transport()
        # Observed 2026-10-13 on live 55x23 alternate screens: with
        # approval_policy "never" and sandbox "danger-full-access", Codex
        # 0.162.1 (tui history_cell/session.rs) keeps one permissions line
        # under the title/cwd after Ctrl+L. Only that exact line may precede
        # the optional logo; status fields, drafts and transcript stay rejected.
        banner = '  >_ OpenAI Codex (v0.162.1)\n     ~\n  permissions: YOLO mode\n'
        self.assertTrue(self.mod.clear_probe_view_is_reset(banner + self.screen))
        self.assertTrue(self.mod.clear_probe_view_is_reset(
            banner + '\n     ⣾⣿⣿⠃ ⢸⣿⣿⡇\n' + self.screen))
        rejected = {
            'no_title': '  permissions: YOLO mode\n' + self.screen,
            'before_cwd': '  >_ OpenAI Codex (v0.162.1)\n  permissions: YOLO mode\n     ~\n' + self.screen,
            'other_mode': banner.replace('YOLO mode', 'read only') + self.screen,
            'status_field': banner.replace('permissions:', 'Permissions:') + self.screen,
            'twice': banner + '  permissions: YOLO mode\n' + self.screen,
            'transcript': banner + 'previous reply\n' + self.screen,
            'session': banner + f'Session: {self.SID}\n' + self.screen,
            'model': banner + 'Model: GPT-6-Astra\n' + self.screen,
            'command': banner + '› /status\n' + self.screen,
            'draft': banner + self.screen.replace('Ask Codex to do anything', 'unfinished draft'),
        }
        for name, view in rejected.items():
            with self.subTest(view=name):
                self.assertFalse(self.mod.clear_probe_view_is_reset(view))

    def test_view_reset_recognizes_observed_yolo_alternate_screen(self):
        self.identity_transport()
        # Exact live pane capture (55x23, alternate_on=1) after the probe's
        # Ctrl+L on 2026-10-13 20:39 KST, cwd "/" and a truncated weekly field.
        screen = ('\n  >_ OpenAI Codex (v0.162.1)\n     /\n  permissions: YOLO mode\n'
                  + '\n' * 15
                  + '› Ask Codex to do anything\n\n'
                  '  GPT-6-Astra max · / · Context 4% used · weekly 80% l…\n'
                  '  ? for shortcuts\n')
        self.assertTrue(self.mod.clear_identity_probe_ready(screen))
        self.assertTrue(self.mod.clear_probe_view_is_reset(screen))

    def test_alternate_yolo_banner_after_view_clear_allows_fresh_identity_probe(self):
        self.cropped_status_transport()
        tmux = self.transport.tmux
        banner = '  >_ OpenAI Codex (v0.162.1)\n     ~\n  permissions: YOLO mode\n'

        def reset_with_permissions(*args, **kwargs):
            result = tmux(*args, **kwargs)
            if args[-1] == 'C-l':
                self.history = banner
            return result
        self.transport.tmux = reset_with_permissions
        self.request()
        self.assertEqual(self.commands, ['/status', '/clear', '/status'])
        self.assertEqual(self.sent(), [self.mod.CLEAR_SLASH_COMPLETE])
        self.assertEqual(self.history, banner)

    def test_identity_explicit_receipt_must_match_active_id(self):
        self.identity_transport()
        self.explicit_receipt_id = self.SID
        self.request()
        self.assertNotIn(self.mod.CLEAR_SLASH_COMPLETE, self.sent())

    def test_identity_status_change_without_start_receipt_is_not_complete(self):
        self.identity_transport()
        self.emit_start = False
        self.request()
        self.assertNotIn(self.mod.CLEAR_SLASH_COMPLETE, self.sent())
        self.assertEqual(self.commands, ['/status', '/clear'])

    def test_identity_other_process_receipt_is_not_complete(self):
        self.identity_transport()
        self.emit_start = False
        self.request()
        self.start_receipt(owner='pid:999:55555555-5555-5555-5555-555555555555')
        self.bridge.maybe_confirm_pending_clear()
        self.assertNotIn(self.mod.CLEAR_SLASH_COMPLETE, self.sent())

    def test_identity_unchanged_active_id_is_not_complete(self):
        self.identity_transport()
        self.NEW_SID = self.SID
        self.request()
        self.assertNotIn(self.mod.CLEAR_SLASH_COMPLETE, self.sent())

    def test_identity_duplicate_request_does_not_clear_twice(self):
        self.identity_transport()
        self.emit_start = False
        self.request()
        self.request()
        self.assertEqual(self.commands.count('/clear'), 1)
        self.start_receipt()
        self.bridge.maybe_confirm_pending_clear()
        self.assertEqual(self.sent().count(self.mod.CLEAR_SLASH_COMPLETE), 1)

    def test_identity_consecutive_requests_use_latest_status_id(self):
        self.identity_transport()
        self.request()
        self.NEW_SID = '01a08f32-bc05-75b3-ada9-d7f87e484d6c'
        self.request()
        self.assertEqual(self.commands.count('/clear'), 2)
        # Remove per-probe throttle to model a later poll without sleeping.
        self.transport._clear_identity_probe_after = 0
        self.bridge.maybe_confirm_pending_clear()
        self.assertEqual(self.sent().count(self.mod.CLEAR_SLASH_COMPLETE), 2)

    def test_identity_multiple_fresh_starts_are_ambiguous(self):
        self.identity_transport()
        self.emit_start = False
        self.request()
        self.start_receipt()
        self.start_receipt(request_id='8')
        self.bridge.maybe_confirm_pending_clear()
        self.assertNotIn(self.mod.CLEAR_SLASH_COMPLETE, self.sent())

    def test_identity_resume_or_fork_without_second_start_is_not_complete(self):
        self.identity_transport()
        self.emit_start = False
        self.request()
        self.start_receipt()
        for method in ('thread/resume', 'thread/fork'):
            with self.subTest(method=method):
                self.db.execute("DELETE FROM logs WHERE target='codex_app_server::message_processor'")
                self.db.execute("INSERT INTO logs (target,process_uuid,feedback_log_body) VALUES (?, ?, ?)",
                                ('codex_app_server::message_processor', self.owner,
                                 f'app-server request: {method} connection_id=ConnectionId(1)'))
                self.db.commit()
                self.bridge.maybe_confirm_pending_clear()
                self.assertNotIn(self.mod.CLEAR_SLASH_COMPLETE, self.sent())
        self.assertEqual(self.commands, ['/status', '/clear'])

    def test_identity_resume_during_status_query_is_not_complete(self):
        self.identity_transport()
        paste = self.transport._paste_prompt_unlocked

        def race(text):
            paste(text)
            if text == '/status' and self.active_id == self.NEW_SID:
                self.db.execute("INSERT INTO logs (target,process_uuid,feedback_log_body) VALUES (?, ?, ?)",
                                ('codex_app_server::message_processor', self.owner,
                                 'app-server request: thread/resume connection_id=ConnectionId(1)'))
                self.db.commit()
        self.transport._paste_prompt_unlocked = race
        self.request()
        self.assertNotIn(self.mod.CLEAR_SLASH_COMPLETE, self.sent())

    def test_identity_persisted_watch_survives_restart(self):
        self.identity_transport()
        self.emit_start = False
        self.request()
        self.assertEqual(self.state()['pending_clear_watch']['log_cursor']['before_session_id'], self.SID)
        self.bridge = self.make_bridge()
        self.start_receipt()
        self.bridge.maybe_confirm_pending_clear()
        self.assertEqual(self.sent().count(self.mod.CLEAR_SLASH_COMPLETE), 1)

    def test_identity_draft_blocks_probe_and_clear(self):
        self.identity_transport()
        self.screen = self.screen.replace('Ask Codex to do anything', 'user draft')
        self.request()
        self.assertEqual(self.commands, [])
        self.assertNotIn(self.mod.CLEAR_SLASH_COMPLETE, self.sent())

    def test_identity_draft_after_submission_blocks_status_probe(self):
        self.identity_transport()
        self.emit_start = False
        self.request()
        self.screen = self.screen.replace('Ask Codex to do anything', 'user draft')
        self.start_receipt()
        self.bridge.maybe_confirm_pending_clear()
        self.assertEqual(self.commands, ['/status', '/clear'])
        self.assertNotIn(self.mod.CLEAR_SLASH_COMPLETE, self.sent())

    def test_identity_stale_status_does_not_submit_clear(self):
        self.identity_transport()
        self.transport._paste_prompt_unlocked('/status')
        self.transport._paste_prompt_unlocked = self.commands.append
        self.commands.clear()
        self.request()
        self.assertEqual(self.commands, ['/status'])
        self.assertNotIn(self.mod.CLEAR_SLASH_COMPLETE, self.sent())

    def test_identity_old_visible_reset_receipt_cannot_bypass_id_match(self):
        self.identity_transport()
        self.emit_start = False
        self.request()
        self.screen = self.receipt()
        self.bridge.maybe_confirm_pending_clear()
        self.assertNotIn(self.mod.CLEAR_SLASH_COMPLETE, self.sent())

    def test_idle_process_identity_survives_other_panes_log_volume(self):
        self.log_transport()
        self.db.execute("INSERT INTO logs (id,target,process_uuid) VALUES (30000, 'other', 'pid:999:55555555-5555-5555-5555-555555555555')")
        self.db.commit()
        self.assertEqual(self.transport.clear_log_cursor()['process_uuid'], self.owner)

    def test_status_id_parser_rejects_prose_partial_and_ambiguous_box(self):
        self.identity_transport()
        self.transport._paste_prompt_unlocked('/status')
        self.assertEqual(self.mod.clear_status_session_id(self.history), self.SID)
        for invalid in (f'Session: {self.SID}', self.history.replace('╰', ''),
                        self.history.replace('╰', f'│ Session: {self.NEW_SID} │\n╰')):
            self.assertEqual(self.mod.clear_status_session_id(invalid), '')

    def test_native_0162_unboxed_status_without_prompt_prefix(self):
        self.identity_transport()
        paste = self.transport._paste_prompt_unlocked

        def unboxed(text):
            paste(text)
            if text == '/status':
                self.history = self.history.replace('› /status', '/status').replace(
                    '╭────────────────────╮\n│ OpenAI Codex (v0.162.1) │',
                    '  >_ OpenAI Codex (v0.162.1)').replace('│ Session:', '  Session:').replace(
                    ' │\n╰────────────────────╯', '')
        self.transport._paste_prompt_unlocked = unboxed
        self.request()
        self.assertEqual(self.sent(), [self.mod.CLEAR_SLASH_COMPLETE])

    def test_narrow_status_wraps_session_value_and_clears_probe_display(self):
        self.identity_transport()
        paste = self.transport._paste_prompt_unlocked

        def wrapped(text):
            paste(text)
            if text == '/status':
                self.history = self.history.replace(
                    f'│ Session: {self.active_id} │',
                    f'│ Session: │\n│ {self.active_id[:30]} │\n│ {self.active_id[30:]} │')
        self.transport._paste_prompt_unlocked = wrapped
        self.request()
        self.assertEqual(self.sent(), [self.mod.CLEAR_SLASH_COMPLETE])
        self.assertEqual(self.commands, ['/status', '/clear', '/status'])
        self.assertEqual(self.keys, ['C-l', 'C-l'])
        self.assertEqual(self.history, '')

    def test_wrapped_status_parser_rejects_unrelated_or_ambiguous_uuid(self):
        header = '  >_ OpenAI Codex (v0.162.1)\n'
        wrapped = f'  Session:\n    {self.SID[:30]}\n    {self.SID[30:]}\n'
        self.assertEqual(self.mod.clear_status_session_id(header + wrapped), self.SID)
        for invalid in (
            wrapped.replace('Session:\n', 'Session:\n\n'),
            wrapped + f'  Session: {self.NEW_SID}\n',
            f'  Session: {self.SID[:30]}\n  Note: {self.SID[30:]}\n',
            f'  Session: {self.SID}\n  Session: invalid\n',
        ):
            with self.subTest(invalid=invalid):
                self.assertEqual(self.mod.clear_status_session_id(header + invalid), '')

    def test_failed_identity_probe_cleans_only_its_own_idle_status(self):
        self.identity_transport()
        paste = self.transport._paste_prompt_unlocked

        def invalid_id(text):
            paste(text)
            self.history = self.history.replace(self.active_id, 'unavailable')
        self.transport._paste_prompt_unlocked = invalid_id
        self.request()
        self.assertEqual(self.commands, ['/status'])
        self.assertEqual(self.keys, ['C-l'])
        self.assertNotIn(self.mod.CLEAR_SLASH_COMPLETE, self.sent())

    def test_status_cleanup_preserves_a_new_draft(self):
        self.identity_transport()
        paste = self.transport._paste_prompt_unlocked

        def draft(text):
            paste(text)
            self.screen = self.screen.replace('Ask Codex to do anything', 'unfinished draft')
        self.transport._paste_prompt_unlocked = draft
        self.request()
        self.assertEqual(self.keys, [])
        self.assertEqual(self.commands, ['/status'])

    def test_status_cleanup_preserves_an_intervening_command(self):
        self.identity_transport()
        paste = self.transport._paste_prompt_unlocked

        def another_command(text):
            paste(text)
            self.history += '› /help\ncommand help\n'
        self.transport._paste_prompt_unlocked = another_command
        self.request()
        self.assertEqual(self.keys, [])
        self.assertEqual(self.commands, ['/status'])

    def test_footer_change_cannot_make_old_status_fresh(self):
        self.identity_transport()
        self.transport._paste_prompt_unlocked('/status')

        def only_footer(text):
            self.commands.append(text)
            self.screen = self.screen.replace('max', 'medium')
        self.transport._paste_prompt_unlocked = only_footer
        self.commands.clear()
        self.request()
        self.assertEqual(self.commands, ['/status'])
        self.assertNotIn(self.mod.CLEAR_SLASH_COMPLETE, self.sent())

    def test_log_receipt_completes_without_banner_or_new_rollout(self):
        self.log_transport()
        self.repl.paste_prompt.side_effect = lambda *_: self.start_receipt()
        self.request()
        self.assertEqual(self.sent(), [self.mod.CLEAR_SLASH_COMPLETE])
        self.assertEqual(self.repl.session_file(), self.old)

    def split_server(self):
        self.log_transport()
        self.server_owner = "pid:123:33333333-3333-3333-3333-333333333333"
        self.process_tree = {120: {120, 121, 122, 123}, 121: {121, 122, 123}}
        self.db.execute("INSERT INTO logs (target,process_uuid,feedback_log_body) VALUES ('codex_app_server::message_processor', ?, 'app-server request: account/rateLimits/read connection_id=ConnectionId(1)')", (self.server_owner,))
        self.db.commit()
        self.screen = "› Ask Codex to do anything\n\nGPT-6-Astra medium · ~ · Context 0% used · weekly 69% left\n? for shortcuts\n"

    def test_descendant_server_receipt_without_product_title_completes_once(self):
        self.split_server()
        self.repl.paste_prompt.side_effect = lambda *_: self.start_receipt(owner=self.server_owner)
        self.request()
        self.bridge.maybe_confirm_pending_clear()
        self.assertEqual(self.sent(), [self.mod.CLEAR_SLASH_COMPLETE])
        self.assertEqual(self.repl.session_file(), self.old)

    def test_agent_navigation_footer_completes_once_after_fresh_receipt(self):
        self.split_server()
        self.screen = (
            '  >_ OpenAI Codex (v0.162.0)\n\n'
            '› Ask Codex to do anything\n\n'
            '  GPT-6-Astra medium · ~ · Context 0% used · weekly 68% left\n'
            '  ← for agents · ? for shortcuts\n')
        self.request()
        self.assertNotIn(self.mod.CLEAR_SLASH_COMPLETE, self.sent())
        self.start_receipt(owner=self.server_owner)
        self.bridge.maybe_confirm_pending_clear()
        self.bridge.maybe_confirm_pending_clear()
        self.assertEqual(self.sent().count(self.mod.CLEAR_SLASH_COMPLETE), 1)
        self.assertNotIn('pending_clear_watch', self.state())
        self.assertEqual(self.repl.session_file(), self.old)

    def test_agent_navigation_footer_with_warnings_completes(self):
        self.split_server()
        self.screen = self.screen.replace(
            '? for shortcuts', '← for agents · ? for shortcuts ⚠️ 2 warnings · f2 to view')
        self.repl.paste_prompt.side_effect = lambda *_: self.start_receipt(owner=self.server_owner)
        self.request()
        self.assertEqual(self.sent(), [self.mod.CLEAR_SLASH_COMPLETE])

    def test_agent_footer_does_not_accept_arbitrary_prefix_or_suffix(self):
        self.split_server()
        self.request()
        self.start_receipt(owner=self.server_owner)
        for footer in ('unrecognized modal · ? for shortcuts',
                       '← for agents · ? for shortcuts · unsent text',
                       '← for agents · ? for shortcuts\nadditional modal'):
            with self.subTest(footer=footer):
                self.screen = ('› Ask Codex to do anything\nContext 0% used\n' + footer)
                self.assertFalse(self.bridge._pending_clear_reset_confirmed(self.bridge.pending_clear_watch))

    def test_split_server_watermark_survives_bridge_restart(self):
        self.split_server()
        self.request()
        self.bridge = self.make_bridge()
        self.start_receipt(owner=self.server_owner)
        self.bridge.maybe_confirm_pending_clear()
        self.assertEqual(self.sent().count(self.mod.CLEAR_SLASH_COMPLETE), 1)

    def test_sibling_server_is_not_bound_to_this_tui(self):
        self.split_server()
        self.process_tree[121] = {121}
        cursor = self.transport.clear_log_cursor()
        self.start_receipt(owner=self.server_owner)
        self.assertFalse(self.transport.clear_log_confirms_reset(cursor))

    def test_restarted_or_reparented_server_does_not_confirm(self):
        self.split_server()
        cursor = self.transport.clear_log_cursor()
        self.start_receipt(owner="pid:123:44444444-4444-4444-4444-444444444444")
        self.assertFalse(self.transport.clear_log_confirms_reset(cursor))
        self.start_receipt(owner=self.server_owner)
        self.process_tree[121] = {121}
        self.assertFalse(self.transport.clear_log_confirms_reset(cursor))

    def test_embedded_server_is_not_replaced_by_a_descendant_app(self):
        self.split_server()
        self.db.execute("INSERT INTO logs (target,process_uuid,feedback_log_body) VALUES ('codex_app_server::outgoing_message', ?, 'app-server event: thread/tokenUsage/updated')", (self.owner,))
        self.db.execute("INSERT INTO logs (target,process_uuid) VALUES ('codex_app_server::message_processor', 'pid:122:44444444-4444-4444-4444-444444444444')")
        self.db.commit()
        cursor = self.transport.clear_log_cursor()
        self.assertIsNotNone(cursor)
        self.assertEqual(cursor['receipt_process_uuid'], self.owner)
        self.start_receipt(owner=self.server_owner)
        self.assertFalse(self.transport.clear_log_confirms_reset(cursor))
        self.start_receipt()
        self.assertTrue(self.transport.clear_log_confirms_reset(cursor))

    def test_ambiguous_descendant_servers_fail_closed(self):
        self.split_server()
        self.db.execute("INSERT INTO logs (target,process_uuid) VALUES ('codex_app_server::message_processor', 'pid:122:44444444-4444-4444-4444-444444444444')")
        self.db.commit()
        self.assertIsNone(self.transport.clear_log_cursor())

    def test_split_server_ignores_other_sessions_and_temporary_requests(self):
        self.split_server()
        cursor = self.transport.clear_log_cursor()
        self.start_receipt(owner=self.server_owner, request_id="temporary-structured-example")
        self.start_receipt(owner="pid:999:55555555-5555-5555-5555-555555555555")
        self.assertFalse(self.transport.clear_log_confirms_reset(cursor))

    def test_other_client_on_same_daemon_is_not_clear_completion(self):
        self.split_server()
        cursor = self.transport.clear_log_cursor()
        self.start_receipt(owner=self.server_owner, connection_id="2")
        self.assertFalse(self.transport.clear_log_confirms_reset(cursor))

    def test_shared_or_unknown_daemon_connection_fails_closed(self):
        self.split_server()
        self.db.execute("INSERT INTO logs (target,process_uuid,feedback_log_body) VALUES ('codex_app_server::message_processor', ?, 'app-server request: account/rateLimits/read connection_id=ConnectionId(2)')", (self.server_owner,))
        self.db.commit()
        self.assertIsNone(self.transport.clear_log_cursor())
        self.db.execute("UPDATE logs SET feedback_log_body='unknown' WHERE target='codex_app_server::message_processor'")
        self.db.commit()
        self.assertIsNone(self.transport.clear_log_cursor())

    def test_initialize_only_diagnostic_client_does_not_mask_tui_connection(self):
        self.split_server()
        self.db.execute("INSERT INTO logs (target,process_uuid,feedback_log_body) VALUES ('codex_app_server::message_processor', ?, 'app-server request: initialize connection_id=ConnectionId(40) request_id=Integer(1)')", (self.server_owner,))
        self.db.commit()
        cursor = self.transport.clear_log_cursor()
        self.assertEqual(cursor["receipt_connection_id"], "1")
        self.start_receipt(owner=self.server_owner, connection_id="40")
        self.assertFalse(self.transport.clear_log_confirms_reset(cursor))
        cursor = self.transport.clear_log_cursor()
        self.start_receipt(owner=self.server_owner)
        self.assertTrue(self.transport.clear_log_confirms_reset(cursor))

    def test_initialize_alone_does_not_establish_tui_connection(self):
        self.split_server()
        self.db.execute("UPDATE logs SET feedback_log_body='app-server request: initialize connection_id=ConnectionId(1)' WHERE target='codex_app_server::message_processor'")
        self.db.commit()
        self.assertIsNone(self.transport.clear_log_cursor())

    def test_title_free_screen_still_requires_empty_composer_and_zero_context(self):
        self.split_server()
        self.request()
        self.start_receipt(owner=self.server_owner)
        for screen in ("› existing draft\nContext 0% used\n",
                       "› Ask Codex to do anything\nContext 12% used\n",
                       "quoted Ask Codex to do anything\nContext 0% used\n",
                       "› Ask Codex to do anything\nexisting draft\nContext 0% used\n",
                       "› Ask Codex to do anything\nContext 0% used\n› unsent user draft\nGPT-6 · Context 12% used\n? for shortcuts\n",
                       "› Ask Codex to do anything\nContext 0% used\nunknown modal\n"):
            with self.subTest(screen=screen):
                self.screen = screen
                self.assertFalse(self.bridge._pending_clear_reset_confirmed(self.bridge.pending_clear_watch))

    def test_legacy_persisted_log_cursor_remains_supported(self):
        self.log_transport()
        cursor = self.transport.clear_log_cursor()
        cursor.pop("receipt_pid")
        cursor.pop("receipt_process_uuid")
        self.start_receipt()
        self.assertTrue(self.transport.clear_log_confirms_reset(cursor))

    def test_log_watermark_survives_bridge_restart_and_ack_is_once(self):
        self.log_transport()
        self.request()
        self.assertIn("log_cursor", self.state()["pending_clear_watch"])
        self.bridge = self.make_bridge()
        self.start_receipt()
        self.bridge.maybe_confirm_pending_clear()
        self.bridge.maybe_confirm_pending_clear()
        self.assertEqual(self.sent().count(self.mod.CLEAR_SLASH_COMPLETE), 1)

    def test_old_log_and_empty_screen_do_not_complete(self):
        self.log_transport()
        self.start_receipt()
        self.request()
        self.assertNotIn(self.mod.CLEAR_SLASH_COMPLETE, self.sent())

    def test_other_process_and_non_tui_or_resume_events_do_not_complete(self):
        self.log_transport()
        self.request()
        self.start_receipt(owner="pid:999:11111111-1111-1111-1111-111111111111")
        self.start_receipt(client="codex-exec")
        self.start_receipt(method="thread/resume")
        self.bridge.maybe_confirm_pending_clear()
        self.assertNotIn(self.mod.CLEAR_SLASH_COMPLETE, self.sent())

    def test_process_restart_and_multiple_receipts_fail_closed(self):
        self.log_transport()
        cursor = self.transport.clear_log_cursor()
        self.start_receipt(owner="pid:121:22222222-2222-2222-2222-222222222222")
        self.assertFalse(self.transport.clear_log_confirms_reset(cursor))
        self.start_receipt()
        self.start_receipt()
        self.assertFalse(self.transport.clear_log_confirms_reset(cursor))

    def test_delayed_old_start_log_is_not_a_fresh_receipt(self):
        self.log_transport()
        cursor = self.transport.clear_log_cursor()
        self.start_receipt(at_ns=cursor["at_ns"] - 1)
        self.assertFalse(self.transport.clear_log_confirms_reset(cursor))
        self.start_receipt()
        self.assertTrue(self.transport.clear_log_confirms_reset(cursor))

    def test_temporary_background_thread_is_not_clear_completion(self):
        self.log_transport()
        cursor = self.transport.clear_log_cursor()
        self.start_receipt(request_id="temporary-structured-example")
        self.assertFalse(self.transport.clear_log_confirms_reset(cursor))

    def test_detached_process_is_not_completion(self):
        self.log_transport()
        cursor = self.transport.clear_log_cursor()
        self.start_receipt()
        with mock.patch.object(self.mod, "descendants", return_value={777}):
            self.assertFalse(self.transport.clear_log_confirms_reset(cursor))

    def test_log_receipt_requires_idle_empty_main_screen(self):
        self.log_transport()
        self.request()
        self.start_receipt()
        for screen in ("", "OpenAI Codex\n› Ask Codex to do anything\nContext 20% used",
                       self.screen + "\nWorking (esc to interrupt)",
                       self.screen + "\n'/clear' is disabled while a task is in progress"):
            self.screen = screen
            self.assertFalse(self.bridge._pending_clear_reset_confirmed(self.bridge.pending_clear_watch))

    def test_missing_or_changed_log_schema_fails_closed(self):
        self.log_transport()
        cursor = self.transport.clear_log_cursor()
        self.db.execute("DROP TABLE logs")
        self.db.commit()
        self.assertIsNone(self.transport.clear_log_cursor())
        self.assertFalse(self.transport.clear_log_confirms_reset(cursor))
        self.db.close()  # Windows cannot unlink an open SQLite database.
        self.log_path.unlink()
        self.assertIsNone(self.transport.clear_log_cursor())
        self.assertFalse(self.log_path.exists())

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
        self.assertIn(self.mod.CLEAR_SLASH_TIMEOUT, self.sent())
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
        self.assertIn(self.mod.CLEAR_SLASH_TIMEOUT, self.sent())
        self.assertNotIn('pending_clear_watch', self.state())
        self.assertEqual(self.state()['offset'], previous['offset'])


if __name__ == "__main__":
    unittest.main()
