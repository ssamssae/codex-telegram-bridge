#!/usr/bin/env python3
"""No live terminal keys or Telegram calls: exercise the bridge delivery boundary."""
import importlib.util
import sys
import threading
import tempfile
import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('choice_delivery_bridge', SCRIPTS / 'codex_repl_bridge.py')
m = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = m
spec.loader.exec_module(m)
SCREEN = '''새 디자인과 검수 이미지가 담긴 PR을 배포할까?

› 1. 승인 — 머지·배포
  2. 보류 — 미리보기 검토
  3. Other

enter submit     ctrl + ] skip     main prompt
'''
OTHER = SCREEN.replace('› 1.', '  1.').replace('  2.', '› 2.')
DONE = 'Working (esc to interrupt)\n› Ask Codex to do anything'

class ChoiceDeliveryTests(unittest.TestCase):
    def bridge(self, screens):
        b = object.__new__(m.Bridge)
        b.config = SimpleNamespace(chat_id='1', bridge_kill=False, approval_ttl_seconds=300)
        b.choice_lock = threading.RLock()
        b.choice_submit_lock = threading.Lock()
        b.pending_choice = m.parse_choice_prompt(SCREEN)
        b.pending_choice_message_id = 42
        b.resolved_choice_ids = set()
        b.choice_focus_request = None
        b.choice_focus_attempt_at = 0.0
        b.choice_focus_seen = set()
        b.session_path = Path('/tmp/test-choice-session')
        b.telegram = Mock()
        b.repl = Mock()
        b.repl.capture_visible_screen.side_effect = screens
        b.repl.session_file.return_value = b.session_path
        b.repl.composer_lock.return_value = threading.RLock()
        return b

    def test_footer_is_not_part_of_other_option(self):
        self.assertEqual(m.parse_choice_prompt(SCREEN).options[-1].label, 'Other')

    def test_stale_screen_sends_no_key(self):
        b = self.bridge([SCREEN.replace('새 디자인', '완전히 다른 질문')] * 2)
        b.handle_choice_choice('1', callback_query_id='cb')
        b.repl.send_choice.assert_not_called()

    def test_selection_uses_fresh_cursor_not_old_card_cursor(self):
        b = self.bridge([OTHER, OTHER, DONE, DONE])
        with patch.object(m.time, 'sleep'):
            b.handle_choice_choice('1', callback_query_id='cb')
        self.assertEqual(b.repl.send_choice.call_args.args[0].selected_index, 1)
        self.assertIn('제출 확인', b.telegram.update_choice_prompt.call_args.args[2])

    def test_unchanged_screen_is_not_success_and_not_sent_twice(self):
        b = self.bridge([SCREEN] * 50)
        with patch.object(m.time, 'sleep'):
            b.handle_choice_choice('1', callback_query_id='cb')
            b.handle_choice_choice('1', callback_query_id='cb2')
        self.assertEqual(b.repl.send_choice.call_count, 1)
        self.assertNotIn('✅', b.telegram.update_choice_prompt.call_args.args[2])
        self.assertIn('확인되지', b.telegram.update_choice_prompt.call_args.args[2])

    def test_blank_redraw_does_not_confirm_submission(self):
        b = self.bridge([SCREEN, SCREEN] + [''] * 50)
        with patch.object(m.time, 'sleep'):
            b.handle_choice_choice('1', callback_query_id='cb')
        self.assertNotIn('✅', b.telegram.update_choice_prompt.call_args.args[2])

    def test_kill_switch_sends_no_key(self):
        b = self.bridge([SCREEN])
        b.config.bridge_kill = True
        b.handle_choice_choice('1', callback_query_id='cb')
        b.repl.send_choice.assert_not_called()

    def test_blocking_event_arms_focus_once_without_submitting(self):
        b = self.bridge([SCREEN])
        record = {'type':'response_item','payload':{'type':'function_call','name':'request_user_input','call_id':'call-1','arguments':'{"questions":[{"title":"배포할까?","options":["승인","보류"]}]}'}}
        b.observe_choice_request(record)
        self.assertIsNotNone(b.choice_focus_request)
        b.repl.send_choice.assert_not_called()
        b.choice_focus_request = None
        b.observe_choice_request(record)
        self.assertIsNone(b.choice_focus_request)

    def test_hidden_question_is_focused_without_enter(self):
        b = self.bridge([SCREEN])
        b.choice_focus_request = {'titles':[m.parse_choice_prompt(SCREEN).title], 'attempts':0}
        with patch.object(m.time, 'sleep'):
            screen = b.choice_screen()
        self.assertIsNotNone(m.parse_choice_prompt(screen))
        # Already visible: no navigation or selection required.
        b.repl.focus_pending_question.assert_not_called()
        b.repl.send_choice.assert_not_called()

    def test_other_transition_is_input_wait_not_submission(self):
        b = self.bridge([SCREEN, SCREEN] + ['새 디자인과 검수 이미지가 담긴 PR을 배포할까?\nType your answer\nenter submit'] * 50)
        with patch.object(m.time, 'sleep'):
            b.handle_choice_choice('3', callback_query_id='cb', other_text='답변')
        self.assertNotIn('✅', b.telegram.update_choice_prompt.call_args.args[2])

class ChoiceBoundaryTests(unittest.TestCase):
    bridge = ChoiceDeliveryTests.bridge

    def test_hidden_async_question_opens_before_card_detection(self):
        b = self.bridge(['› Ask Codex to do anything', SCREEN])
        b.choice_focus_request = {'titles':[m.parse_choice_prompt(SCREEN).title], 'attempts':0}
        with patch.object(m.time, 'sleep'):
            shown = b.choice_screen()
        self.assertEqual(m.parse_choice_prompt(shown).title, m.parse_choice_prompt(SCREEN).title)
        b.repl.focus_pending_question.assert_called_once_with()
        b.repl.send_choice.assert_not_called()

    def test_focus_navigation_does_not_send_enter(self):
        transport = object.__new__(m.TmuxTransport)
        transport.config = SimpleNamespace(pane_target='test:0.0')
        transport.tmux = Mock()
        transport.focus_pending_question()
        transport.tmux.assert_called_once_with('send-keys', '-t', 'test:0.0', 'S-Left')

    def test_stale_session_prevents_even_focus_navigation(self):
        b = self.bridge([SCREEN])
        b.pending_choice = m.replace(b.pending_choice, session_path='/tmp/old-session')
        b.handle_choice_choice('1', callback_query_id='cb')
        b.repl.focus_pending_question.assert_not_called()
        b.repl.send_choice.assert_not_called()

    def test_old_instance_of_identical_question_is_rejected(self):
        b = self.bridge([SCREEN])
        b.pending_choice = m.replace(b.pending_choice, instance_id='new-instance')
        b.handle_choice_choice('1', signature='old-instance', callback_query_id='cb')
        b.repl.send_choice.assert_not_called()

    def test_transport_exception_is_ambiguous_and_not_retried(self):
        b = self.bridge([SCREEN, SCREEN])
        b.repl.send_choice.side_effect = RuntimeError('transport failed after Enter')
        b.handle_choice_choice('1', callback_query_id='cb')
        b.handle_choice_choice('1', callback_query_id='cb2')
        self.assertEqual(b.repl.send_choice.call_count, 1)
        self.assertNotIn('✅', b.telegram.update_choice_prompt.call_args.args[2])

    def test_other_chat_callback_cannot_submit(self):
        b = self.bridge([SCREEN])
        b.handle_choice_callback_query({'id':'cb','data':f'{m.CHOICE_CALLBACK_PREFIX}:{b.pending_choice.short_signature}:1',
            'message':{'chat':{'id':2}}})
        b.repl.send_choice.assert_not_called()

    def test_restart_recovers_hint_without_replaying_answer(self):
        b = self.bridge([SCREEN])
        record = {'type':'response_item','payload':{'type':'function_call','name':'request_user_input','call_id':'pending',
            'arguments':json.dumps({'questions':[{'title':'배포할까?','options':['승인','보류']}]})}}
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'session.jsonl'
            path.write_text(json.dumps(record) + '\n' + 'partial JSON')
            b.recover_choice_request(path)
        self.assertEqual(b.choice_focus_request['titles'], ['배포할까?'])
        b.repl.send_choice.assert_not_called()

    def test_real_process_line_observes_async_tool_record(self):
        b = self.bridge([SCREEN])
        b.persist_state = Mock()
        record = {'type':'response_item','payload':{'type':'function_call','name':'request_user_input','call_id':'pending',
            'arguments':json.dumps({'questions':[{'title':'배포할까?','options':['승인','보류']}]})}}
        with patch.object(m, 'extract_event', return_value=None):
            b.process_line(json.dumps(record), line_end=100)
        self.assertIsNotNone(b.choice_focus_request)

    def test_choice_card_callback_binds_instance_not_content(self):
        tg = object.__new__(m.TelegramClient)
        tg.chat_id = '1'
        tg.with_emoji_prefix = lambda text:text
        tg.call = Mock(return_value={'result':{'message_id':42}})
        prompt = m.replace(m.parse_choice_prompt(SCREEN), instance_id='instance-a')
        tg.send_choice_prompt(prompt)
        markup = json.loads(tg.call.call_args.kwargs['reply_markup'])
        self.assertEqual(markup['inline_keyboard'][0][0]['callback_data'], f'{m.CHOICE_CALLBACK_PREFIX}:instance-a:1')

class ChoiceFreeTextTests(unittest.TestCase):
    bridge = ChoiceDeliveryTests.bridge

    def test_other_button_requests_reply_without_submitting(self):
        b = self.bridge([SCREEN])
        b.telegram.call.return_value = {'result':{'message_id':77}}
        b.handle_choice_choice('3', callback_query_id='cb')
        b.repl.send_choice.assert_not_called()
        self.assertEqual(b.choice_text_request['message_id'], 77)

    def test_reply_is_bound_to_message_and_question(self):
        b = self.bridge([SCREEN])
        b.choice_text_request = {'message_id':77,'signature':'old','choice':'3'}
        self.assertFalse(b.handle_choice_reply({'text':'다른 답변','reply_to_message':{'message_id':78}}))
        self.assertTrue(b.handle_choice_reply({'text':'다른 답변','reply_to_message':{'message_id':77}}))
        b.repl.send_choice.assert_not_called()

    def test_free_text_submits_only_in_verified_empty_answer_editor(self):
        editor = '새 디자인과 검수 이미지가 담긴 PR을 배포할까?\nType your answer\nenter submit'
        b = self.bridge([SCREEN, SCREEN, editor, DONE, DONE])
        with patch.object(m.time, 'sleep'):
            b.handle_choice_choice('3', callback_query_id='cb', other_text='미리보기 주소를 알려줘')
        b.repl.send_choice_text.assert_called_once_with('미리보기 주소를 알려줘')
        self.assertIn('제출 확인', b.telegram.update_choice_prompt.call_args.args[2])

    def test_free_text_never_pastes_into_main_composer(self):
        b = self.bridge([SCREEN, SCREEN] + [DONE] * 20)
        with patch.object(m.time, 'sleep'):
            b.handle_choice_choice('3', callback_query_id='cb', other_text='미리보기 주소를 알려줘')
        b.repl.send_choice_text.assert_not_called()
        self.assertNotIn('✅', b.telegram.update_choice_prompt.call_args.args[2])

class ChoiceObserverTests(unittest.TestCase):
    bridge = ChoiceDeliveryTests.bridge

    def observe(self, b, ticks=1):
        b.stop_event = Mock()
        b.stop_event.is_set.side_effect = [False] * ticks + [True]
        b.approval_lock = threading.Lock()
        b.pending_approval = None
        b.pending_approval_message_id = None
        b.resolved_approval_ids = set()
        b.pending_choice_send_attempt_at = 0.0
        b.maybe_notify_fast_mode = Mock()
        b.telegram.send_choice_prompt.return_value = 42
        b.approval_loop()

    def test_observer_sends_nonce_card_and_keeps_it_on_redraw(self):
        b = self.bridge([SCREEN, OTHER])
        b.pending_choice = None
        self.observe(b, ticks=2)
        b.telegram.send_choice_prompt.assert_called_once()
        sent = b.telegram.send_choice_prompt.call_args.args[0]
        self.assertEqual(len(sent.instance_id), 16)
        self.assertTrue(sent.session_path)
        self.assertGreater(sent.expires_at, m.time.time())
        self.assertEqual(sent.short_signature, b.pending_choice.short_signature)

    def test_hidden_card_is_not_invalidated_by_main_composer(self):
        b = self.bridge([DONE])
        b.pending_choice = m.replace(b.pending_choice, expires_at=m.time.time()+300)
        self.observe(b)
        self.assertIsNotNone(b.pending_choice)

    def test_stale_force_reply_after_restart_is_consumed(self):
        b = self.bridge([SCREEN])
        b.choice_text_request = None
        self.assertTrue(b.handle_choice_reply({'text':'승인', 'reply_to_message':{
            'message_id':77, 'text':'Codex 선택 답변 · old'}}))
        b.repl.send_choice.assert_not_called()

    def test_lost_pending_question_reply_never_becomes_main_input(self):
        b = self.bridge([SCREEN])
        b.pending_choice = None
        b.choice_text_request = {'message_id':77, 'signature':'old', 'choice':'3'}
        self.assertTrue(b.handle_choice_reply({'text':'답변', 'reply_to_message':{'message_id':77}}))
        b.repl.send_choice.assert_not_called()

class ChoiceAcknowledgementTests(unittest.TestCase):
    bridge = ChoiceDeliveryTests.bridge

    def pending(self):
        b = self.bridge([SCREEN] * 50)
        with patch.object(m.time, 'sleep'):
            b.handle_choice_choice('1', callback_query_id='cb')
        return b

    def answer(self, title=None, answer='승인 — 머지·배포'):
        title = title or m.parse_choice_prompt(SCREEN).title
        return {'type':'response_item','timestamp':m.datetime.now(m.timezone.utc).isoformat(),'payload':{'type':'message','role':'user',
            'content':[{'type':'input_text','text':f'> {title}\n\n{answer}'}],
            'internal_chat_message_metadata_passthrough':{'content_item_kinds':['user.text']}}}

    def test_delayed_real_answer_confirms_card_after_screen_timeout(self):
        b = self.pending()
        b.confirm_choice_answer(self.answer())
        self.assertIn('✅ 제출 확인', b.telegram.update_choice_prompt.call_args.args[2])
        self.assertIn('Codex 응답 수신', b.telegram.update_choice_prompt.call_args.args[2])

    def test_native_literal_newline_separator_confirms(self):
        b = self.pending()
        record = self.answer()
        record['payload']['content'][0]['text'] = record['payload']['content'][0]['text'].replace('\n', r'\n')
        b.confirm_choice_answer(record)
        self.assertIn('✅ 제출 확인', b.telegram.update_choice_prompt.call_args.args[2])

    def test_observed_live_answer_envelope(self):
        fixture = json.loads((SCRIPTS / 'tests/fixtures/codex_async_choice_answer.json').read_text())
        b = self.bridge([SCREEN])
        prompt = m.replace(b.pending_choice, title=fixture['question']['title'])
        option = m.ChoiceOption(value='1', label=fixture['question']['options'][0])
        b.choice_confirmations = {'live-receipt': {'prompt':prompt, 'option':option,
            'answer':option.label, 'message_id':42, 'session_path':str(b.session_path),
            'sent_at':m.parse_event_timestamp(fixture['answer_record'])-14,
            'deadline':m.time.monotonic()+60}}
        b.confirm_choice_answer(fixture['answer_record'])
        self.assertIn('✅ 제출 확인', b.telegram.update_choice_prompt.call_args.args[2])

    def test_unrelated_or_wrong_answer_cannot_confirm(self):
        b = self.pending()
        b.confirm_choice_answer(self.answer(title='다른 질문'))
        b.confirm_choice_answer(self.answer(answer='다른 선택'))
        self.assertNotIn('✅', b.telegram.update_choice_prompt.call_args.args[2])

    def test_duplicate_native_answer_updates_once(self):
        b = self.pending()
        b.confirm_choice_answer(self.answer())
        count = b.telegram.update_choice_prompt.call_count
        b.confirm_choice_answer(self.answer())
        self.assertEqual(b.telegram.update_choice_prompt.call_count, count)

    def test_answer_in_different_session_cannot_confirm(self):
        b = self.pending()
        b.session_path = Path('/tmp/replaced-session')
        b.confirm_choice_answer(self.answer())
        self.assertNotIn('✅', b.telegram.update_choice_prompt.call_args.args[2])

class ChoiceAcknowledgementAgeTests(unittest.TestCase):
    bridge = ChoiceDeliveryTests.bridge
    pending = ChoiceAcknowledgementTests.pending
    answer = ChoiceAcknowledgementTests.answer

    def test_older_identical_answer_cannot_confirm(self):
        b = self.pending()
        record = self.answer()
        record['timestamp'] = '2000-01-01T00:00:00Z'
        b.confirm_choice_answer(record)
        self.assertNotIn('✅', b.telegram.update_choice_prompt.call_args.args[2])

    def test_confirmation_deadline_removes_pending_receipt(self):
        b = self.pending()
        for receipt in b.choice_confirmations.values(): receipt['deadline'] = 0
        b.expire_choice_confirmations()
        self.assertFalse(b.choice_confirmations)
        self.assertIn('제한 시간', b.telegram.update_choice_prompt.call_args.args[2])

if __name__ == '__main__': unittest.main()
