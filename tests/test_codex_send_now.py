import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest import mock

if os.name == 'nt':
    raise unittest.SkipTest('Send-now controls require the POSIX tmux transport')

ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / 'codex_repl_bridge.py'
if not BRIDGE.exists():
    BRIDGE = ROOT / 'codex_repl_bridge.py'
sys.path.insert(0, str(ROOT))
from codex_send_now import SendNowButtons, pending_previews, matches_preview


def load_bridge():
    spec = importlib.util.spec_from_file_location('codex_send_now_bridge_test', BRIDGE)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def screen(*messages):
    # Native 0.155.1 pending_input_preview snapshots: pending steers and ordinary
    # Tab queues are separate sections; only the first offers immediate send.
    return ('• Working (15s • esc to interrupt)\n\n'
            '• Messages to be submitted after next tool call\n'
            '  (press esc to interrupt and send immediately)\n'
            + ''.join('  ↳ ' + text + '\n' for text in messages)
            + '\n› Ask Codex to do anything\n  Main [test] · Context 10% used\n')


class PreviewTests(unittest.TestCase):
    def test_native_wrapped_hint_and_multiline_preview(self):
        self.assertEqual(pending_previews(screen('첫 메시지', '둘째\n    줄')), ['첫 메시지', '둘째 줄'])
        self.assertTrue(matches_preview('첫 줄 긴\n    메시지 …', '첫 줄 긴 메시지 나머지'))
        self.assertFalse(matches_preview('first', 'first but not the same'))

    def test_not_a_tab_queue_or_remapped_escape_or_draft(self):
        for value in [screen('next').replace('next tool call', 'end of turn'),
                      screen('next').replace('Messages to be submitted after next tool call', 'Queued follow-up inputs'),
                      screen('next').replace('press esc', 'press f12'),
                      screen('next').replace('› Ask Codex to do anything', '› my draft'),
                      'Quoted header only\n↳ next',
                      screen('next').replace('  (press esc to interrupt and send immediately)\n', '')]:
            with self.subTest(value=value):
                self.assertEqual(pending_previews(value), [])

    def test_different_queue_does_not_join_pending_steers(self):
        value = screen('first').replace('\n›', '\n• Queued follow-up inputs\n  ↳ later\n›')
        self.assertEqual(pending_previews(value), ['first'])


class SendNowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.m = load_bridge()

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.session = self.root / 'session.jsonl'
        self.session.write_text(json.dumps({'type': 'event_msg', 'payload':
                                           {'type': 'task_started', 'turn_id': 'turn-a'}}) + '\n')
        cfg = SimpleNamespace(state_path=self.root / 'state.json', node='test-node', emoji='🤖',
                              chat_id='123', bridge_kill=False, flow_mirror=True,
                              pane_target='=codex:', typing_liveness_seconds=0)
        self.repl = mock.Mock(supports_pane_features=True)
        self.repl.composer_lock.return_value = threading.RLock()
        self.repl.pane_pid.return_value = 12345
        self.repl.capture_visible_screen.return_value = screen('first', 'second')
        self.telegram = mock.Mock()
        self.telegram.call.return_value = {'ok': True}
        self.b = self.m.Bridge(cfg, self.telegram, self.repl)
        self.b.language.code = 'ko'
        self.b.session_path = self.session
        self.b.session_identity = self.m.session_identity(self.session)
        self.b.bridge_state = {'active_turn': {'id': 'turn-a'}}
        self.b.flow_message_id = 91
        self.b.flow_scope = 'original request'
        self.b.flow_body = 'Working'
        self.b.queued_telegram = [('first', 21), ('second', 22)]
        self.b.persist_telegram_inbox = mock.Mock()
        self.b.stop_buttons.prepare()
        self.b.stop_buttons.sent(91)
        patcher = mock.patch.object(self.m, 'session_file_from_descendants', return_value=self.session)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.buttons = self.b.send_now_buttons
        self.assertIsNotNone(self.buttons)

    def show(self):
        self.assertEqual(self.buttons.markup(), [])
        state = self.buttons.read()
        state['card']['since'] -= 11
        self.buttons.path.write_text(json.dumps(state))
        rows = self.buttons.markup()
        self.assertTrue(rows)
        self.data = rows[0][0]['callback_data']
        return rows

    def callback(self, **changes):
        value = {'id': 'cb1', 'data': self.data, 'from': {'id': 123},
                 'message': {'message_id': 91, 'chat': {'id': 123}}}
        value.update(changes)
        return value

    def test_delayed_button_and_duplicate_click_restart_never_resends(self):
        rows = self.show()
        self.assertIn('2개', rows[0][0]['text'])
        self.b.handle_callback_query(self.callback())
        self.b.handle_callback_query(self.callback())
        SendNowButtons(self.b, vars(self.m)).callback(self.callback())
        self.repl.tmux.assert_called_once_with('send-keys', '-t', '=codex:', 'Escape')
        self.assertEqual(self.b.queued_telegram, [('first', 21), ('second', 22)])
        self.assertEqual(self.buttons.read()['attempt']['status'], 'requested')
        self.assertEqual(self.buttons.markup(), [])

    def test_click_includes_new_pending_messages_and_future_messages_stay_queued(self):
        self.show()
        self.b.queued_telegram.append(('third', 23))
        self.repl.capture_visible_screen.return_value = screen('first', 'second', 'third')
        self.buttons.callback(self.callback())
        self.b.queued_telegram.append(('later', 24))
        start = self.session.stat().st_size
        self.assertEqual(self.buttons.consume_receipt('first\nsecond\nthird', start), 23)
        self.assertEqual(self.b.queued_telegram, [('later', 24)])
        self.assertEqual(self.buttons.read()['attempt']['status'], 'confirmed')
        self.assertIsNone(self.buttons.consume_receipt('first\nsecond\nthird', start))

    def test_receipt_is_fresh_exact_and_session_bound(self):
        self.show()
        self.buttons.callback(self.callback())
        offset = self.session.stat().st_size
        for text, position in [('first\nsecond', offset - 1), ('unrelated', offset), ('first', offset)]:
            self.assertIsNone(self.buttons.consume_receipt(text, position))
        self.b.session_path = self.root / 'other.jsonl'
        self.assertIsNone(self.buttons.consume_receipt('first\nsecond', offset))
        self.assertEqual(len(self.b.queued_telegram), 2)

    def test_native_merging_a_later_normal_input_preserves_its_reply_anchor(self):
        self.show()
        self.buttons.callback(self.callback())
        self.b.queued_telegram.extend([('third', 23), ('still waiting', 24)])
        self.assertEqual(self.buttons.consume_receipt('first\nsecond\nthird', self.session.stat().st_size), 23)
        self.assertEqual(self.b.queued_telegram, [('still waiting', 24)])
        self.assertEqual(self.repl.tmux.call_count, 1)

    def test_merged_receipt_uses_last_message_reply_anchor(self):
        self.show()
        self.buttons.callback(self.callback())
        self.b.input_event_offset = self.session.stat().st_size
        self.b.active_telegram_message_id = 20
        self.b.begin_telegram_prompt_tracking = mock.Mock()
        self.b.begin_repl_typing = mock.Mock()
        self.b.prime_flow_screen_snapshot = mock.Mock()
        self.b.handle_user_event('first\nsecond')
        self.b.begin_telegram_prompt_tracking.assert_called_once_with(
            'first\nsecond', message_id=22, expect_jsonl_echo=False)
        self.assertEqual(self.b.queued_telegram, [])

    def test_wrong_chat_sender_card_and_token(self):
        self.show()
        for changes in [{'from': {'id': 456}}, {'data': 'codexnow:forged'},
                        {'message': {'message_id': 92, 'chat': {'id': 123}}},
                        {'message': {'message_id': 91, 'chat': {'id': 456}}}]:
            self.buttons.callback(self.callback(**changes))
        self.repl.tmux.assert_not_called()

    def test_consumed_queue_new_turn_draft_kill_and_other_pane_are_rejected(self):
        self.show()
        original = self.repl.capture_visible_screen.return_value
        for changed in ['consumed', 'turn', 'draft', 'kill', 'side', 'unbound', 'question']:
            with self.subTest(changed=changed):
                state = self.buttons.read()
                state['card']['closed'] = False
                self.buttons.path.write_text(json.dumps(state))
                self.b.config.bridge_kill = changed == 'kill'
                self.b.bridge_state['active_turn']['id'] = 'turn-b' if changed == 'turn' else 'turn-a'
                self.repl.capture_visible_screen.return_value = (
                    original.replace('› Ask Codex to do anything', '› draft') if changed == 'draft'
                    else '› Ask Codex to do anything' if changed == 'consumed' else original)
                with mock.patch.object(self.m, 'is_side_screen', return_value=changed == 'side'), \
                     mock.patch.object(self.m, 'session_file_from_descendants', return_value=None if changed == 'unbound' else self.session), \
                     mock.patch.object(self.buttons.adapter, 'snapshot', wraps=self.buttons.adapter.snapshot) as snapshot:
                    if changed == 'question':
                        snapshot.return_value = dict(status='waiting', draft=False, question={'id': 'approval'})
                    self.buttons.callback(self.callback())
                self.repl.tmux.assert_not_called()

    def test_ambiguous_or_local_previews_do_not_offer_button(self):
        for inbox in [[('unrelated', 21)], [('first', 21), ('first', 22)], [('first', 0)]]:
            self.b.queued_telegram = inbox
            self.repl.capture_visible_screen.return_value = screen('first')
            self.assertIsNone(self.buttons.snapshot())

    def test_send_error_is_durable_and_never_retried(self):
        self.show()
        self.repl.tmux.side_effect = RuntimeError('uncertain delivery')
        self.buttons.callback(self.callback())
        SendNowButtons(self.b, vars(self.m)).callback(self.callback())
        self.assertEqual(self.repl.tmux.call_count, 1)
        self.assertEqual(len(self.b.queued_telegram), 2)
        self.assertEqual(self.buttons.read()['attempt']['status'], 'requested')

    def test_preview_disappearing_during_click_never_sends_escape(self):
        self.show()
        snapshot = self.buttons.snapshot()
        with mock.patch.object(self.buttons, 'snapshot', side_effect=[snapshot, None]):
            self.buttons.callback(self.callback())
        self.repl.tmux.assert_not_called()
        self.assertEqual(self.buttons.read()['attempt']['status'], 'not_sent')

    def test_restart_keeps_binding_and_concurrent_clicks_send_once(self):
        from concurrent.futures import ThreadPoolExecutor
        self.show()
        self.b.send_now_buttons = SendNowButtons(self.b, vars(self.m))
        self.assertEqual(self.b.send_now_buttons.markup()[0][0]['callback_data'], self.data)
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(self.b.send_now_buttons.callback, [self.callback(), self.callback()]))
        self.assertEqual(self.repl.tmux.call_count, 1)

    def test_receipt_persistence_failure_keeps_queue_for_retry(self):
        self.show()
        self.buttons.callback(self.callback())
        self.b.persist_telegram_inbox.side_effect = OSError('disk unavailable')
        with self.assertRaises(OSError):
            self.buttons.consume_receipt('first\nsecond', self.session.stat().st_size)
        self.assertEqual(len(self.b.queued_telegram), 2)
        self.b.persist_telegram_inbox.side_effect = None
        self.assertEqual(self.buttons.consume_receipt('first\nsecond', self.session.stat().st_size), 22)

    def test_unconfirmed_markup_uses_short_request_and_cooldown(self):
        self.show()
        self.telegram.call.return_value = None
        self.buttons.poll()
        self.buttons.last_poll = 0
        self.buttons.poll()
        self.assertEqual(self.telegram.call.call_count, 1)
        self.assertEqual(self.telegram.call.call_args.kwargs['_attempts'], 1)

    def test_unchanged_keyboard_is_success_not_an_endless_retry(self):
        client = self.m.TelegramClient('test-token', '123', '🤖', 4000)
        body = json.dumps({'ok': False, 'error_code': 400,
                           'description': 'Bad Request: message is not modified'}).encode()
        error = self.m.urllib.error.HTTPError('https://example.invalid', 400, 'unchanged', {}, io.BytesIO(body))
        with mock.patch.object(self.m, 'mesh_cutover_call', return_value=None), \
             mock.patch.object(self.m, 'mesh_ledger_record'), \
             mock.patch.object(self.m.urllib.request, 'urlopen', side_effect=error) as request:
            self.assertTrue(client.call('editMessageReplyMarkup', chat_id='123', message_id=91,
                                        reply_markup='{}', _attempts=1)['ok'])
        self.assertEqual(request.call_count, 1)

    def test_progress_markup_preserves_stop_and_poll_removes_only_now(self):
        # Bind the stop card after the resolver patch, then combine both rows.
        self.b.stop_buttons.prepare()
        self.b.stop_buttons.sent(91)
        self.show()
        keyboard = json.loads(self.b.stop_button_kwargs()['reply_markup'])['inline_keyboard']
        self.assertEqual(len(keyboard), 2)
        self.assertTrue(keyboard[1][0]['callback_data'].startswith('turnstop:'))
        self.repl.capture_visible_screen.return_value = '• Working (20s • esc to interrupt)\n› Ask Codex to do anything'
        self.buttons.poll()
        edits = [call.kwargs for call in self.telegram.call.call_args_list if call.args[0] == 'editMessageReplyMarkup']
        rows = json.loads(edits[-1]['reply_markup'])['inline_keyboard']
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0][0]['callback_data'].startswith('turnstop:'))

    def test_mirror_off_and_native_end_disable_button(self):
        self.show()
        with mock.patch.object(self.m, 'flow_mirror_enabled', return_value=False):
            self.assertIsNone(self.buttons.snapshot())
        with self.session.open('a') as stream:
            stream.write(json.dumps({'type': 'event_msg', 'payload': {'type': 'task_complete'}}) + '\n')
        self.buttons.callback(self.callback())
        self.repl.tmux.assert_not_called()

    def test_english_card_and_requested_receipt(self):
        self.b.language.code = 'en'
        self.assertIn('Apply now', self.show()[0][0]['text'])
        self.buttons.callback(self.callback())
        answers = [call.kwargs['text'] for call in self.telegram.call.call_args_list
                   if call.args[0] == 'answerCallbackQuery']
        self.assertEqual(answers[-1], 'Apply now requested. Waiting for Codex confirmation.')


if __name__ == '__main__':
    unittest.main()
