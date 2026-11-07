# @mutate-why 주기 처리에서 빠지면 새 로그가 없는 동안 고정·만료 해제가 실행되지 않는다.
# @mutate scripts/codex_repl_bridge.py | self.get_async_questions().sync_pins(str(self.session_path or "")) | pass
# @mutate-why 응답한 질문의 고정을 유지하면 미응답 질문이 남았다는 표시가 흐려진다.
# @mutate scripts/codex_async_questions.py | and item['status'] in {'open', 'submitting', 'uncertain'} | and item['status'] in {'open', 'submitting', 'uncertain', 'answered'}
"""Keep unanswered native questions reachable without duplicating their cards."""
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from codex_async_questions import AsyncQuestions, PREFIX, TTL

NOW = 1789220000
SESSION = '/sessions/current.jsonl'


def request():
    return {'type': 'response_item',
            'timestamp': datetime.fromtimestamp(NOW, timezone.utc).isoformat(),
            'payload': {'type': 'function_call', 'name': 'request_user_input_async',
                        'call_id': 'call-question',
                        'arguments': json.dumps({'questions': [
                            {'title': '승인 대상', 'options': ['승인', '보류']}]})}}


def accepted():
    return {'type': 'response_item', 'payload': {'type': 'function_call_output',
            'call_id': 'call-question', 'output': json.dumps({'accepted': True})}}


class Telegram:
    chat_id = '123'

    def __init__(self):
        self.calls = []
        self.budgets = []
        self.pinned = {999}  # A user's unrelated pinned message.
        self.next_message = 55
        self.fail = None

    def call(self, method, *, _attempts=3, _request_timeout=60.0, **kwargs):
        self.calls.append((method, kwargs))
        self.budgets.append((method, _attempts, _request_timeout))
        if method == 'sendMessage':
            self.next_message += 1
            return {'ok': True, 'result': {'message_id': self.next_message}}
        if method == 'pinChatMessage':
            self.pinned.add(kwargs['message_id'])
        if method == 'unpinChatMessage':
            self.pinned.discard(kwargs['message_id'])
        if method == self.fail:
            raise TimeoutError('response lost after the operation')
        return {'ok': True, 'result': True}

    def send(self, text):
        return self.call('sendMessage', text=text)

    def calls_for(self, method):
        return [kwargs for name, kwargs in self.calls if name == method]


class QuestionPinsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'questions.json'
        self.telegram = Telegram()
        self.submit = Mock(return_value=True)
        self.now = NOW
        self.restart()

    def restart(self):
        self.q = AsyncQuestions(self.path, self.telegram, self.submit,
                                clock=lambda: self.now)

    def open(self, count=1):
        event = request()
        event['payload']['arguments'] = json.dumps({'questions': [
            {'title': f'승인 대상 {i+1}', 'options': ['승인', '보류']}
            for i in range(count)
        ]})
        self.q.observe(event, SESSION)
        self.q.observe(accepted(), SESSION)
        return list(self.q.items)

    def tick(self, count=1, session=SESSION):
        for _ in range(count):
            self.q.sync_pins(session)

    def answer(self, key):
        self.q.callback({
            'id': 'callback', 'data': f'{PREFIX}:{key}:0',
            'from': {'id': 123},
            'message': {'message_id': self.q.items[key]['message_id'],
                        'chat': {'id': 123}},
        }, SESSION)

    def test_new_progress_does_not_bury_two_questions_or_duplicate_cards(self):
        self.open(2)
        self.tick(2)
        for _ in range(20):
            self.q.observe({'type': 'event_msg', 'payload': {'type': 'agent_message'}}, SESSION)
            self.tick()
        self.assertEqual(self.telegram.pinned, {999, 56, 57})
        self.assertEqual(len(self.telegram.calls_for('sendMessage')), 2)
        self.assertEqual(len(self.telegram.calls_for('pinChatMessage')), 2)
        self.assertTrue(all(c['disable_notification'] is True
                            for c in self.telegram.calls_for('pinChatMessage')))
        self.submit.assert_not_called()

    def test_answer_removes_only_that_pin_and_keeps_the_other_question(self):
        first, second = self.open(2)
        self.tick(2)
        self.answer(first)
        self.tick(3)
        self.assertEqual(self.telegram.pinned, {999, 57})
        self.assertEqual(self.q.items[second]['status'], 'open')
        self.answer(second)
        self.tick(3)
        self.assertEqual(self.telegram.pinned, {999})
        self.assertEqual(self.submit.call_count, 2)
        self.assertEqual(self.telegram.calls_for('unpinChatMessage'), [
            {'chat_id': '123', 'message_id': 56},
            {'chat_id': '123', 'message_id': 57},
        ])

    def test_native_receipt_unpins_without_resubmitting(self):
        key, = self.open()
        self.tick()
        event = request()
        event['payload'] = {'type': 'message', 'role': 'user',
            'internal_chat_message_metadata_passthrough': {'turn_id': 'native-turn'},
            'content': [{'type': 'input_text', 'text':
                '<send_user_message_question_reply>' + json.dumps([{
                    'questionItemId': json.dumps(['request_user_input_async', 'call-question', 0]),
                    'question': self.q.items[key]['title'], 'answer': '승인',
                }]) + '</send_user_message_question_reply>'}]}
        self.q.observe(event, SESSION)
        self.tick()
        self.assertEqual(self.telegram.pinned, {999})
        self.submit.assert_not_called()

    def test_unconfirmed_submission_keeps_visible_question_without_resending(self):
        key, = self.open()
        self.tick()
        self.submit.side_effect = TimeoutError('delivery unconfirmed')
        self.answer(key)
        self.tick(5)
        self.assertEqual(self.q.items[key]['status'], 'uncertain')
        self.assertEqual(self.telegram.pinned, {999, 56})
        self.submit.assert_called_once()

    def test_native_turn_end_unpins(self):
        self.open(2)
        self.tick(2)
        self.q.close_open_cards(SESSION, NOW + 1, 'turn_ended')
        self.tick(2)
        self.assertEqual(self.telegram.pinned, {999})
        self.submit.assert_not_called()

    def test_expiry_unpins_even_with_no_new_messages(self):
        self.open()
        self.tick()
        self.now += TTL
        self.tick()
        self.assertEqual(self.telegram.pinned, {999})
        self.submit.assert_not_called()

    def test_session_rotation_clears_owned_pin_but_missing_session_preserves_it(self):
        self.open()
        self.tick()
        self.tick(session='')
        self.assertEqual(self.telegram.pinned, {999, 56})
        self.tick(session='/sessions/other.jsonl')
        self.assertEqual(self.telegram.pinned, {999})
        self.submit.assert_not_called()

    def test_restart_keeps_confirmed_pin_without_repinning_or_resending(self):
        self.open()
        self.tick()
        self.restart()
        self.tick(5)
        self.assertEqual(len(self.telegram.calls_for('pinChatMessage')), 1)
        self.assertEqual(len(self.telegram.calls_for('sendMessage')), 1)

    def test_ambiguous_pin_is_owned_before_call_and_cleared_after_restart(self):
        key, = self.open()
        original = self.telegram.call

        def inspect_journal(method, **kwargs):
            if method == 'pinChatMessage':
                # The journal is UTF-8; Windows reads default to the ANSI code page.
                saved = json.loads(self.path.read_text(encoding='utf-8'))[key]
                self.assertTrue(saved['pin_managed'])
            return original(method, **kwargs)

        self.telegram.call = inspect_journal
        self.telegram.fail = 'pinChatMessage'
        self.tick()
        self.assertEqual(self.telegram.pinned, {999, 56})
        self.restart()
        self.answer(key)
        self.tick()
        self.assertEqual(self.telegram.pinned, {999})
        self.assertEqual(len(self.telegram.calls_for('sendMessage')), 1)

    def test_pin_failures_are_bounded_across_restart_and_never_block_answer(self):
        key, = self.open()
        self.telegram.fail = 'pinChatMessage'
        self.tick(50)
        self.assertEqual(len(self.telegram.calls_for('pinChatMessage')), 1)
        for _ in range(5):
            self.now += 60
            self.restart()
            self.tick()
        self.assertEqual(len(self.telegram.calls_for('pinChatMessage')), 3)
        self.assertEqual(self.q.items[key]['status'], 'open')
        self.answer(key)
        self.tick()
        self.assertEqual(self.telegram.pinned, {999})
        self.submit.assert_called_once()

    def test_unpin_failure_retries_without_reopening_or_resubmitting(self):
        key, = self.open()
        self.tick()
        self.telegram.fail = 'unpinChatMessage'
        self.answer(key)
        self.tick(20)
        self.assertEqual(len(self.telegram.calls_for('unpinChatMessage')), 1)
        self.telegram.fail = None
        self.now += 60
        self.restart()
        self.tick(20)
        self.assertEqual(len(self.telegram.calls_for('unpinChatMessage')), 2)
        self.assertEqual(self.q.items[key]['status'], 'answered')
        self.submit.assert_called_once()

    def test_no_pin_for_unaccepted_invalid_id_expired_foreign_or_group_cards(self):
        self.q.observe(request(), SESSION)
        self.tick()
        self.assertFalse(self.telegram.calls_for('pinChatMessage'))
        self.q.observe(accepted(), SESSION)
        key = next(iter(self.q.items))
        for bad in (None, 0, -1, True, '56'):
            self.q.items[key]['message_id'] = bad
            self.tick()
        self.q.items[key]['message_id'] = 56
        self.tick(session='/sessions/other.jsonl')
        self.telegram.chat_id = '-123'
        self.tick()
        self.telegram.chat_id = '123'
        self.now += TTL
        self.tick()
        self.assertFalse(self.telegram.calls_for('pinChatMessage'))
        self.assertFalse(self.telegram.calls_for('unpinChatMessage'))

    def test_one_network_operation_per_tick_and_no_global_unpin(self):
        self.open(3)
        self.tick()
        self.assertEqual(len(self.telegram.calls_for('pinChatMessage')), 1)
        self.tick(2)
        self.now += TTL
        self.tick()
        self.assertEqual(len(self.telegram.calls_for('unpinChatMessage')), 1)
        self.assertFalse(self.telegram.calls_for('unpinAllChatMessages'))
        self.assertIn(999, self.telegram.pinned)
        self.assertTrue(all(attempts == 1 and timeout == 5.0
                            for method, attempts, timeout in self.telegram.budgets
                            if method in {'pinChatMessage', 'unpinChatMessage'}))

    def test_chat_binding_is_required_and_config_change_does_not_touch_other_chat(self):
        key, = self.open()
        self.q.items[key].pop('message_chat_id')
        self.tick()
        self.assertFalse(self.telegram.calls_for('pinChatMessage'))
        self.q.items[key]['message_chat_id'] = '123'
        self.tick()
        self.telegram.chat_id = '456'
        self.now += TTL
        self.tick()
        self.assertFalse(self.telegram.calls_for('unpinChatMessage'))

    def test_answer_during_pin_request_is_unpinned_on_next_tick(self):
        key, = self.open()
        original = self.telegram.call

        def answer_in_flight(method, **kwargs):
            if method == 'pinChatMessage':
                self.answer(key)
            return original(method, **kwargs)

        self.telegram.call = answer_in_flight
        self.tick(2)
        self.assertEqual(self.q.items[key]['status'], 'answered')
        self.assertEqual(self.telegram.pinned, {999})
        self.submit.assert_called_once()

    def test_unsuccessful_telegram_response_is_not_recorded_as_pinned(self):
        key, = self.open()
        original = self.telegram.call

        def reject_pin(method, **kwargs):
            if method == 'pinChatMessage':
                return {'ok': False, 'description': 'cannot pin'}
            return original(method, **kwargs)

        self.telegram.call = reject_pin
        self.tick()
        self.assertIsNone(self.q.items[key]['pin_confirmed'])
        self.assertEqual(self.q.items[key]['status'], 'open')


class BridgePinIntegrationTest(unittest.TestCase):
    def test_idle_watch_pins_and_expires_without_more_jsonl_events(self):
        scripts = Path(__file__).resolve().parents[1]
        spec = importlib.util.spec_from_file_location(
            'question_pin_bridge_test', scripts / 'codex_repl_bridge.py')
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'CRB_CHAT_ID': '123'}):
            config = module.replace(module.Config.from_env(),
                                    state_path=Path(tmp) / 'bridge.json', bridge_kill=False)
            telegram = Telegram()
            bridge = module.Bridge(config, telegram, Mock())
            bridge.session_path = Path(SESSION)
            # The bridge keys questions by str(session_path), which uses
            # backslashes on Windows; observe under that same key.
            session = str(bridge.session_path)
            bridge.choice_screen = Mock(return_value='› Ask Codex to do anything')
            bridge.maybe_notify_fast_mode = Mock()
            questions = bridge.get_async_questions()
            questions.clock = lambda: NOW
            questions.observe(request(), session)
            questions.observe(accepted(), session)
            bridge.stop_event.wait = lambda _: bridge.stop_event.set()
            bridge.approval_loop()
            self.assertEqual(telegram.pinned, {999, 56})
            questions.clock = lambda: NOW + TTL
            bridge.stop_event.clear()
            bridge.approval_loop()
            self.assertEqual(telegram.pinned, {999})


if __name__ == '__main__':
    unittest.main()
