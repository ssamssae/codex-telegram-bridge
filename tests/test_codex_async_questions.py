# @mutate-why 화면 위쪽의 오래된 제목만 일치해도 다른 주관식 에디터에 답을 넣는 오작동이 되살아난다
# @mutate scripts/codex_repl_bridge.py | question_title_matches_visible_text(title, "\n".join(title_lines[start:])) | canonical_choice_signature_text(title) in canonical_choice_signature_text(screen)

import importlib.util
import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from codex_async_questions import AsyncQuestions, freeform_editor, PREFIX, TTL

NOW = 1789220000
SESSION = str(Path('/sessions/current.jsonl'))


def request(title='어느 PR인가요?', options=None, name='request_user_input_async'):
    from datetime import datetime, timezone
    return {'type': 'response_item', 'timestamp': datetime.fromtimestamp(NOW, timezone.utc).isoformat(),
            'payload': {'type': 'function_call', 'name': name, 'call_id': 'call-question',
                        'arguments': json.dumps({'questions': [{'title': title, 'options': options if options is not None else ['첫 번째', '둘 다']} ]})}}


def accepted(value=True):
    return {'type': 'response_item', 'payload': {'type': 'function_call_output', 'call_id': 'call-question',
                                              'output': json.dumps({'accepted': value})}}


class QuestionsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'questions.json'
        self.telegram = SimpleNamespace(chat_id='123', call=Mock(return_value={'ok': True, 'result': {'message_id': 55}}), send=Mock())
        self.submit = Mock(return_value=True)
        self.now = NOW
        self.q = AsyncQuestions(self.path, self.telegram, self.submit, clock=lambda: self.now)

    def open(self):
        self.q.observe(request(), SESSION)
        self.q.observe(accepted(), SESSION)
        return next(iter(self.q.items))

    def callback(self, key, **changes):
        data = dict(id='callback-1', data=f'{PREFIX}:{key}:1', message={'message_id': 55, 'chat': {'id': 123}}, **{'from': {'id': 123}})
        data.update(changes)
        return data

    def test_only_accepted_question_creates_buttons_not_tool_progress(self):
        self.q.observe(request(), SESSION)
        self.telegram.call.assert_not_called()
        self.q.observe(accepted(), SESSION)
        kwargs = self.telegram.call.call_args.kwargs
        keys = json.loads(kwargs['reply_markup'])['inline_keyboard']
        self.assertEqual(len(keys), 2)
        self.assertIn('어느 PR인가요?', kwargs['text'])
        self.assertLess(len(keys[0][0]['callback_data'].encode()), 64)

    def test_rejected_question_and_unrelated_function_ignored(self):
        self.q.observe(request(name='some_tool'), SESSION)
        self.assertFalse(self.q.items)
        self.q.observe(request(), SESSION)
        self.q.observe(accepted(False), SESSION)
        self.telegram.call.assert_not_called()

    def test_answer_is_full_quote_once_after_restart(self):
        key = self.open()
        self.q.callback(self.callback(key), SESSION)
        self.submit.assert_called_once_with('> 어느 PR인가요?\n\n둘 다', 55)
        self.q = AsyncQuestions(self.path, self.telegram, self.submit, clock=lambda: NOW)
        self.q.callback(self.callback(key), SESSION)
        self.submit.assert_called_once()

    def test_wrong_user_session_message_and_choice_cannot_submit(self):
        key = self.open()
        for cb, session in [(self.callback(key, **{'from': {'id': 999}}), SESSION),
                            (self.callback(key), '/another'),
                            (self.callback(key, message={'message_id': 999, 'chat': {'id': 123}}), SESSION),
                            (self.callback(key, data=f'{PREFIX}:{key}:99'), SESSION)]:
            self.q.callback(cb, session)
        self.submit.assert_not_called()

    def test_expiry_blocks_button(self):
        key = self.open()
        self.now += TTL + 1
        self.q.callback(self.callback(key), SESSION)
        self.submit.assert_not_called()
        self.assertEqual(self.q.items[key]['status'], 'expired')

    def test_duplicate_event_does_not_duplicate_send(self):
        self.open()
        self.q.observe(request(), SESSION)
        self.q.observe(accepted(), SESSION)
        self.assertEqual(self.telegram.call.call_count, 1)

    def test_restart_between_request_and_accept(self):
        self.q.observe(request(), SESSION)
        self.q = AsyncQuestions(self.path, self.telegram, self.submit, clock=lambda: NOW)
        self.q.observe(accepted(), SESSION)
        self.telegram.call.assert_called_once()

    def test_ambiguous_send_does_not_repeat_on_replay(self):
        self.telegram.call.return_value = None
        self.open()
        self.q.observe(accepted(), SESSION)
        self.telegram.call.assert_called_once()
        self.assertEqual(next(iter(self.q.items.values()))['status'], 'uncertain')

    def test_ambiguous_submission_is_not_repeated(self):
        key = self.open()
        self.submit.side_effect = RuntimeError('input failed')
        self.q.callback(self.callback(key), SESSION)
        self.q.callback(self.callback(key), SESSION)
        self.submit.assert_called_once()
        self.assertEqual(self.q.items[key]['status'], 'uncertain')

    def test_ordinary_quoted_answer_does_not_resolve_native_question(self):
        key = self.open()
        self.q.observe({'type': 'response_item', 'payload': {'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': '> 어느 PR인가요?\n\n직접 답변'}]}}, SESSION)
        self.submit.assert_not_called()
        self.assertEqual(self.q.items[key]['status'], 'open')

    def test_free_text_reply_reserves_question_before_native_input(self):
        key = self.open()
        text = self.q.reply_text({'text': '다른 PR', 'reply_to_message': {'message_id': 55}, 'chat': {'id':123}, 'from': {'id':123}}, SESSION)
        self.assertEqual(text, '> 어느 PR인가요?\n\n다른 PR')
        self.q.callback(self.callback(key), SESSION)
        self.submit.assert_not_called()

    def test_old_event_and_blocking_request_not_replayed(self):
        self.now += TTL + 1
        self.q.observe(request(), SESSION)
        self.now = NOW
        self.q.observe(request(name='request_user_input'), SESSION)
        self.assertFalse(self.q.items)

    @unittest.skipIf(os.name == "nt", "POSIX mode bits; Windows uses inherited ACLs")
    def test_state_file_private(self):
        self.open()
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_multiple_questions_and_freeform_only(self):
        r = request(); args = json.loads(r['payload']['arguments']); args['questions'].append({'title': '설명해주세요'})
        r['payload']['arguments'] = json.dumps(args)
        self.q.observe(r, SESSION); self.q.observe(accepted(), SESSION)
        self.assertEqual(len(self.q.items), 2)
        self.assertEqual(self.telegram.call.call_count, 2)


if __name__ == '__main__':
    unittest.main()

class BridgeIntegrationTest(unittest.TestCase):
    def test_jsonl_callback_submits_matching_native_question_without_composer(self):
        import os
        from unittest.mock import patch
        scripts = Path(__file__).resolve().parents[1]
        spec = importlib.util.spec_from_file_location('async_question_bridge_test', scripts / 'codex_repl_bridge.py')
        m = importlib.util.module_from_spec(spec); sys.modules[spec.name] = m; spec.loader.exec_module(m)
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'CRB_CHAT_ID':'123'}):
            cfg = m.replace(m.Config.from_env(), state_path=Path(tmp)/'bridge.json', bridge_kill=False)
            tg = SimpleNamespace(chat_id='123', call=Mock(return_value={'ok':True,'result':{'message_id':55}}), send=Mock())
            repl = Mock(); repl.session_file.return_value = Path(SESSION)
            from contextlib import nullcontext
            repl.composer_lock = nullcontext
            repl.capture_visible_screen.side_effect = ['? 1 question\nshift + ← to answer', '어느 PR인가요?\n› 1. 첫 번째\n  2. 둘 다\nEnter to select', '› Ask Codex to do anything', '› Ask Codex to do anything']
            b = m.Bridge(cfg, tg, repl)
            b.session_path = Path(SESSION)
            b.handle_flow_event = Mock(); b.persist_state = Mock()
            b.should_queue_telegram_inbound = Mock(return_value=True)
            b.queue_telegram_prompt = Mock(); b.clear_and_paste_prompt = Mock()
            b.get_async_questions().clock = lambda: NOW
            b.process_line(json.dumps(request()), 0, 100)
            tg.call.assert_not_called()
            self.assertIsNone(b.choice_focus_request)
            b.process_line(json.dumps(accepted()), 100, 200)
            key = next(iter(b.get_async_questions().items))
            callback={'id':'q1','data':f'{PREFIX}:{key}:0','message':{'message_id':55,'chat':{'id':123}},'from':{'id':123}}
            self.assertTrue(b.handle_callback_query(callback))
            b.clear_and_paste_prompt.assert_not_called()
            b.queue_telegram_prompt.assert_not_called()
            repl.focus_pending_question.assert_called_once()
            repl.send_choice.assert_called_once()
            self.assertEqual(b.get_async_questions().items[key]['status'], 'answered')
            repl.capture_screen.assert_not_called()


class FreeformTest(unittest.TestCase):
    setUp = QuestionsTest.setUp
    open = QuestionsTest.open
    def reply(self, text='설명', **extra):
        return dict(text=text, reply_to_message={'message_id':55}, chat={'id':123},
                    **{'from': {'id':123}}, **extra)

    def test_owned_stale_replies_never_fall_back(self):
        key = self.open()
        for status in ('submitting', 'uncertain', 'answered', 'expired'):
            self.q.items[key]['status'] = status
            self.q.save()
            reloaded = AsyncQuestions(self.path, self.telegram, self.submit, clock=lambda: NOW)
            self.assertEqual(reloaded.reply_text(self.reply(), SESSION), '')
        self.q.items[key]['status'] = 'open'
        self.assertEqual(self.q.reply_text(self.reply(), '/other'), '')
        self.now += TTL
        self.assertEqual(self.q.reply_text(self.reply(), SESSION), '')
        self.assertEqual(self.q.items[key]['status'], 'expired')

    def test_unrelated_and_unauthorized_replies_do_not_claim(self):
        key = self.open()
        self.assertIsNone(self.q.reply_text({'text':'설명'}, SESSION))
        msg = self.reply(); msg['from']['id'] = 999
        self.assertEqual(self.q.reply_text(msg, SESSION), '')
        self.assertEqual(self.q.items[key]['status'], 'open')
        self.assertEqual(self.q.reply_text(self.reply('\x1btest'), SESSION), '')

    def test_freeform_card_explains_reply(self):
        self.q.observe(request(options=[]), SESSION)
        self.q.observe(accepted(), SESSION)
        self.assertIn('Telegram 답장', self.telegram.call.call_args.kwargs['text'])


def editor_screen(answer='', title='설명해 주세요'):
    return '• Queued follow-up inputs\n' + title + '\n\n' + answer + '\n\nenter submit  ctrl + ] skip  ⌥ + ↓ main prompt\n[tmux status]'


class FreeformEditorTest(unittest.TestCase):
    def test_empty_and_existing_wrapped_title(self):
        self.assertEqual(freeform_editor(editor_screen(), '설명해 주세요'), '')
        self.assertEqual(freeform_editor(editor_screen('ㅇㅋ', '설명해\n 주세요'), '설명해 주세요'), 'ㅇㅋ')

    def test_full_title_with_blank_paragraph_is_consumed(self):
        title = '첫 문단\n\n둘째 문단'
        self.assertEqual(freeform_editor(editor_screen('답변', title), title), '답변')
        self.assertIsNone(freeform_editor(editor_screen('답변', '첫 문단'), title))

    def test_answer_whitespace_is_not_silently_stripped(self):
        for answer in ('  ㅇㅋ  ', 'ㅇㅋ ', '  ㅇㅋ', '   ', '첫 줄\n  둘째 줄'):
            self.assertEqual(freeform_editor(editor_screen(answer), '설명해 주세요'), answer)
        self.assertIsNone(freeform_editor(editor_screen('\nㅇㅋ'), '설명해 주세요'))
        self.assertEqual(freeform_editor(editor_screen('첫 줄\n\n둘째 줄'), '설명해 주세요'), '첫 줄\n\n둘째 줄')
        self.assertEqual(freeform_editor(editor_screen('ㅇㅋ\n'), '설명해 주세요'), 'ㅇㅋ\n')

    def test_common_layout_padding_is_distinct_from_user_spaces(self):
        for answer in ('', 'ㅇㅋ', '  ㅇㅋ  ', '첫 줄\n  둘째 줄'):
            screen = '\n'.join('  ' + line if line else '' for line in editor_screen(answer).split('\n'))
            self.assertEqual(freeform_editor(screen, '설명해 주세요'), answer)
        bad = editor_screen('ㅇㅋ').replace('설명해 주세요', '  설명해 주세요')
        self.assertIsNone(freeform_editor(bad, '설명해 주세요'))

    def test_unknown_or_other_editor_rejected(self):
        for screen in [editor_screen(title='다른 질문'), editor_screen() + '\n• Queued follow-up inputs',
                       editor_screen().replace('⌥ + ↓ main prompt', 'Enter to select'),
                       '› 설명해 주세요\nenter submit', editor_screen('› another composer')]:
            self.assertIsNone(freeform_editor(screen, '설명해 주세요'))


class FreeformBridgeIntegrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        scripts = Path(__file__).resolve().parents[1]
        spec = importlib.util.spec_from_file_location('freeform_bridge_test', scripts / 'codex_repl_bridge.py')
        cls.m = importlib.util.module_from_spec(spec); sys.modules[spec.name] = cls.m; spec.loader.exec_module(cls.m)

    def setUp(self):
        from contextlib import nullcontext
        from unittest.mock import patch
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.env = patch.dict(os.environ, {'CRB_CHAT_ID':'123'}); self.env.start(); self.addCleanup(self.env.stop)
        self.sleep = patch.object(self.m.time, 'sleep'); self.sleep.start(); self.addCleanup(self.sleep.stop)
        cfg = self.m.replace(self.m.Config.from_env(), state_path=Path(self.tmp.name)/'bridge.json', bridge_kill=False)
        tg = SimpleNamespace(chat_id='123', call=Mock(return_value={'ok':True,'result':{'message_id':55}}), send=Mock())
        repl = Mock(); repl.session_file.return_value = Path(SESSION); repl.composer_lock = nullcontext
        self.b = self.m.Bridge(cfg, tg, repl); self.b.session_path = Path(SESSION)
        self.b.get_async_questions().clock = lambda: NOW
        q = self.b.get_async_questions(); q.observe(request(title='설명해 주세요', options=[]), SESSION); q.observe(accepted(), SESSION)
        self.item = next(iter(q.items.values())); self.item['status'] = 'submitting'
        self.repl = repl

    def submit(self, answer='ㅇㅋ'):
        return self.b.submit_async_answer('> 설명해 주세요\n\n' + answer, 55)

    def test_empty_editor_pastes_once_and_requires_two_clear_reads(self):
        self.repl.capture_visible_screen.side_effect = [editor_screen(), '› Ask Codex', editor_screen('ㅇㅋ'), '› Ask Codex', '› Ask Codex']
        self.assertTrue(self.submit())
        self.repl.send_choice_text.assert_called_once_with('ㅇㅋ')
        self.repl.send_key.assert_not_called()
        self.assertEqual(self.repl.capture_visible_screen.call_count, 5)
        self.assertEqual(self.item['status'], 'answered')

    def test_same_existing_answer_enters_once_without_pasting(self):
        self.repl.capture_visible_screen.side_effect = [editor_screen('ㅇㅋ'), '› Ask Codex', '› Ask Codex']
        self.assertTrue(self.submit())
        self.repl.send_key.assert_called_once_with('Enter')
        self.repl.send_choice_text.assert_not_called()

    def test_other_existing_answer_preserved(self):
        self.repl.capture_visible_screen.return_value = editor_screen('아직 작성 중')
        with self.assertRaises(RuntimeError): self.submit()
        self.repl.send_key.assert_not_called(); self.repl.send_choice_text.assert_not_called()

    def test_persistent_editor_is_not_success(self):
        self.repl.capture_visible_screen.return_value = editor_screen()
        with self.assertRaises(RuntimeError): self.submit()
        self.assertEqual(self.item['status'], 'uncertain')
        self.repl.send_choice_text.assert_called_once()

    def test_approval_and_wrong_session_never_submit(self):
        from unittest.mock import patch
        self.repl.capture_visible_screen.return_value = editor_screen()
        with patch.object(self.m, 'parse_approval_prompt', return_value=object()):
            with self.assertRaises(RuntimeError): self.submit()
        self.repl.session_file.return_value = Path('/other')
        with self.assertRaises(RuntimeError): self.submit()
        self.repl.send_key.assert_not_called(); self.repl.send_choice_text.assert_not_called()

    def test_type_your_answer_editor_pastes_once_after_focus(self):
        # Live Codex freeform (T-260913-057 card 2762) uses the same empty
        # editor as choice Other, not the queued-input panel.
        hint = '? 1 question\nshift + ← to answer'
        editor = '설명해 주세요\nType your answer\nenter submit'
        done = 'Working (esc to interrupt)\n› Ask Codex to do anything'
        self.repl.capture_visible_screen.side_effect = [hint, editor, done, done]
        self.assertTrue(self.submit('057 연결 확인'))
        self.repl.focus_pending_question.assert_called_once()
        self.repl.send_choice_text.assert_called_once_with('057 연결 확인')
        self.repl.send_key.assert_not_called()
        self.assertEqual(self.item['status'], 'answered')

    def test_visible_type_your_answer_editor_does_not_need_focus(self):
        editor = '설명해 주세요\nType your answer\nenter submit'
        done = '› Ask Codex to do anything'
        self.repl.capture_visible_screen.side_effect = [editor, done, done]
        self.assertTrue(self.submit())
        self.repl.focus_pending_question.assert_not_called()
        self.repl.send_choice_text.assert_called_once_with('ㅇㅋ')
        self.assertEqual(self.item['status'], 'answered')

    def test_type_your_answer_without_question_title_is_not_the_editor(self):
        self.repl.capture_visible_screen.return_value = '다른 질문\nType your answer\nenter submit'
        with self.assertRaises(RuntimeError):
            self.submit()
        self.repl.send_choice_text.assert_not_called()
        self.repl.send_key.assert_not_called()

    def test_truncated_long_title_type_your_answer_is_recognized(self):
        title = (
            '057 실제 답변 연결 검증입니다. 텔레그램에 도착한 이 질문 카드에 '
            '답장으로 057 연결 확인을 보내주시겠어요?'
        )
        self.item['title'] = title
        visible = title[:32] + '\nType your answer\nenter submit'
        done = '› Ask Codex to do anything'
        self.repl.capture_visible_screen.side_effect = [visible, done, done]
        payload = '> ' + title.replace('\n', '\n> ') + '\n\n057 연결 확인'
        self.assertTrue(self.b.submit_async_answer(payload, 55))
        self.repl.send_choice_text.assert_called_once_with('057 연결 확인')
        self.assertEqual(self.item['status'], 'answered')

    def test_type_your_answer_placeholder_after_paste_still_completes(self):
        editor = '설명해 주세요\nType your answer\nenter submit'
        lingering = (
            '설명해 주세요\nㅇㅋ\nType your answer\nenter submit\n'
            '› Ask Codex to do anything'
        )
        self.repl.capture_visible_screen.side_effect = [editor, lingering, lingering]
        self.assertTrue(self.submit())
        self.repl.send_choice_text.assert_called_once_with('ㅇㅋ')
        self.assertEqual(self.item['status'], 'answered')

    def test_stale_matching_title_above_other_editor_is_not_claimed(self):
        self.repl.capture_visible_screen.return_value = (
            '설명해 주세요\n'
            '이전 질문은 아직 화면 위쪽에 남아 있습니다\n\n'
            '다른 질문\n'
            'Type your answer\n'
            'enter submit'
        )
        with self.assertRaises(RuntimeError):
            self.submit()
        self.repl.send_choice_text.assert_not_called()
        self.repl.send_key.assert_not_called()

    def test_duplicate_owned_reply_consumed_before_normal_composer(self):
        self.b.handle_choice_reply = Mock()
        self.b.prompt_from_telegram_message = Mock()
        self.b.process_telegram_update({'update_id':1, 'message': {'message_id':77, 'text':'ㅇㅋ',
            'chat':{'id':123}, 'from':{'id':123}, 'reply_to_message':{'message_id':55}}})
        self.b.handle_choice_reply.assert_not_called()
        self.b.prompt_from_telegram_message.assert_not_called()


    def test_reply_to_card_routes_to_native_and_persists_completion(self):
        self.item['status'] = 'open'
        self.b.handle_choice_reply = Mock()
        self.b.prompt_from_telegram_message = Mock()
        self.repl.capture_visible_screen.side_effect = [editor_screen(), '› Ask Codex', '› Ask Codex']
        update = {'update_id':1, 'message': {'message_id':77, 'text':'ㅇㅋ',
            'chat':{'id':123}, 'from':{'id':123}, 'reply_to_message':{'message_id':55}}}
        self.b.process_telegram_update(update)
        self.b.process_telegram_update(update)
        self.repl.send_choice_text.assert_called_once_with('ㅇㅋ')
        self.b.prompt_from_telegram_message.assert_not_called()
        self.assertEqual(self.item['status'], 'answered')

    def test_preflight_failed_reply_is_retryable_without_input(self):
        self.item['status'] = 'open'
        self.repl.capture_visible_screen.return_value = editor_screen('다른 입력')
        self.b.process_telegram_update({'update_id':1, 'message': {'message_id':77, 'text':'ㅇㅋ',
            'chat':{'id':123}, 'from':{'id':123}, 'reply_to_message':{'message_id':55}}})
        self.assertEqual(self.item['status'], 'open')
        self.repl.send_choice_text.assert_not_called()

    def test_same_title_ambiguity_blocks_before_input(self):
        self.b.get_async_questions().items['another'] = dict(self.item, message_id=56, status='open')
        self.repl.capture_visible_screen.return_value = editor_screen()
        with self.assertRaises(RuntimeError): self.submit()
        self.repl.send_choice_text.assert_not_called()


    def test_spaced_existing_answer_cannot_enter_unspaced_reply(self):
        self.repl.capture_visible_screen.return_value = editor_screen('  ㅇㅋ  ')
        with self.assertRaises(RuntimeError): self.submit('ㅇㅋ')
        self.repl.send_key.assert_not_called()
        self.repl.send_choice_text.assert_not_called()

    def test_trailing_newline_existing_answer_must_match_exactly(self):
        self.repl.capture_visible_screen.return_value = editor_screen('ㅇㅋ\n')
        with self.assertRaises(RuntimeError): self.submit('ㅇㅋ')
        self.repl.send_key.assert_not_called()
        self.repl.send_choice_text.assert_not_called()
        self.repl.capture_visible_screen.side_effect = [editor_screen('ㅇㅋ\n'), '› Ask Codex', '› Ask Codex']
        self.assertTrue(self.submit('ㅇㅋ\n'))
        self.repl.send_key.assert_called_once_with('Enter')


    def test_padded_panel_preserves_additional_typed_spaces(self):
        def padded(answer):
            return '\n'.join('  ' + line if line else '' for line in editor_screen(answer).split('\n'))
        self.repl.capture_visible_screen.return_value = padded('  ㅇㅋ  ')
        with self.assertRaises(RuntimeError): self.submit('ㅇㅋ')
        self.repl.send_key.assert_not_called()
        self.repl.send_choice_text.assert_not_called()
        self.repl.capture_visible_screen.side_effect = [padded('  ㅇㅋ  '), '› Ask Codex', '› Ask Codex']
        self.assertTrue(self.submit('  ㅇㅋ  '))
        self.repl.send_key.assert_called_once_with('Enter')

    def test_padded_empty_panel_accepts_reply_without_padding(self):
        screen = '\n'.join('  ' + line if line else '' for line in editor_screen().split('\n'))
        self.repl.capture_visible_screen.side_effect = [screen, '› Ask Codex', '› Ask Codex']
        self.assertTrue(self.submit('ㅇㅋ'))
        self.repl.send_choice_text.assert_called_once_with('ㅇㅋ')

class RecoveryTest(unittest.TestCase):
    setUp = QuestionsTest.setUp
    open = QuestionsTest.open
    callback = QuestionsTest.callback
    def test_preflight_rejection_keeps_question_open_and_buttons(self):
        from codex_async_questions import QuestionNotSubmitted
        key = self.open(); self.telegram.call.reset_mock()
        self.submit.side_effect = QuestionNotSubmitted('different_panel')
        self.q.callback(self.callback(key), SESSION)
        self.assertEqual(self.q.items[key]['status'], 'open')
        edits = [c for c in self.telegram.call.call_args_list if c.args[0] == 'editMessageReplyMarkup']
        self.assertTrue(json.loads(edits[-1].kwargs['reply_markup'])['inline_keyboard'])

    def test_uncertain_exposes_recovery_without_repeating_answer(self):
        key = self.open(); self.submit.side_effect = RuntimeError('transport')
        self.q.callback(self.callback(key), SESSION)
        edits = [c for c in self.telegram.call.call_args_list if c.args[0] == 'editMessageReplyMarkup']
        buttons = json.loads(edits[-1].kwargs['reply_markup'])['inline_keyboard']
        self.assertEqual(buttons[0][0]['callback_data'], f'{PREFIX}:{key}:recover')
        self.q.callback(self.callback(key), SESSION)
        self.submit.assert_called_once()

    def test_recovery_requires_fresh_native_panel_and_never_submits(self):
        key = self.open(); self.q.items[key]['status'] = 'uncertain'
        self.q.recover = Mock(return_value=False)
        cb = self.callback(key, data=f'{PREFIX}:{key}:recover')
        self.q.callback(cb, SESSION)
        self.assertEqual(self.q.items[key]['status'], 'uncertain')
        self.q.recover.return_value = True
        self.q.callback(cb, SESSION)
        self.assertEqual(self.q.items[key]['status'], 'open')
        self.submit.assert_not_called()
        self.q.callback(self.callback(key), SESSION)
        self.submit.assert_called_once()

    def test_expired_recovery_cannot_reopen_question(self):
        key = self.open(); self.q.items[key]['status'] = 'uncertain'
        self.q.recover = Mock(return_value=True); self.now += TTL + 1
        self.q.callback(self.callback(key, data=f'{PREFIX}:{key}:recover'), SESSION)
        self.assertEqual(self.q.items[key]['status'], 'expired')
        self.q.recover.assert_not_called()

    def test_failed_freeform_reply_restores_reply_path(self):
        from codex_async_questions import QuestionNotSubmitted
        key = self.open(); self.q.items[key]['status'] = 'submitting'
        self.q.finish_reply(55, False, QuestionNotSubmitted('different_panel'))
        self.assertEqual(self.q.items[key]['status'], 'open')


class NativeRecoveryTest(unittest.TestCase):
    setUpClass = classmethod(FreeformBridgeIntegrationTest.setUpClass.__func__)
    setUp = FreeformBridgeIntegrationTest.setUp

    def panel(self, title='설명해 주세요'):
        self.item['options'] = ['첫 번째', '둘 다']
        return ('• Queued follow-up inputs\n\n' + title +
                '\n\n› 1. 첫 번째\n  2. 둘 다\n  3. Other\n\n'
                'enter submit   ctrl + ] skip   ⌥ + ↓ main prompt')

    def assert_no_input(self):
        self.repl.send_choice.assert_not_called()
        self.repl.send_key.assert_not_called()
        self.repl.send_choice_text.assert_not_called()
        self.repl.focus_pending_question.assert_not_called()

    def test_matching_stable_panel_reopens_without_any_input(self):
        self.repl.capture_visible_screen.return_value = self.panel()
        self.assertTrue(self.b.recover_async_question(self.item))
        self.assert_no_input()

    def test_different_question_even_with_old_title_in_history_is_rejected(self):
        self.repl.capture_visible_screen.return_value = '설명해 주세요\nold answer\n' + self.panel('다른 질문')
        self.assertFalse(self.b.recover_async_question(self.item))
        self.assert_no_input()

    def test_changed_panel_between_reads_is_rejected(self):
        self.repl.capture_visible_screen.side_effect = [self.panel(), self.panel('다른 질문')]
        self.assertFalse(self.b.recover_async_question(self.item))
        self.assert_no_input()

    def test_changed_session_is_rejected(self):
        self.repl.capture_visible_screen.return_value = self.panel()
        self.repl.session_file.return_value = Path('/other')
        self.assertFalse(self.b.recover_async_question(self.item))
        self.assert_no_input()

    def test_existing_freeform_input_is_preserved(self):
        self.repl.capture_visible_screen.return_value = editor_screen('사용자 작성 중')
        self.assertFalse(self.b.recover_async_question(self.item))
        self.assert_no_input()

    def test_actual_transport_failure_remains_uncertain_and_is_not_replayed(self):
        self.item['status'] = 'open'
        self.repl.capture_visible_screen.return_value = self.panel()
        self.repl.send_choice.side_effect = RuntimeError('transport acknowledgement lost')
        q = self.b.get_async_questions(); key = next(iter(q.items))
        cb = dict(id='cb', data=f'{PREFIX}:{key}:0', message={'message_id':55, 'chat':{'id':123}}, **{'from':{'id':123}})
        q.callback(cb, SESSION); q.callback(cb, SESSION)
        self.assertEqual(self.item['status'], 'uncertain')
        self.repl.send_choice.assert_called_once()

    def test_native_submit_rejects_old_title_above_unrelated_panel(self):
        self.repl.capture_visible_screen.return_value = '설명해 주세요\nold answer\n' + self.panel('다른 질문')
        with self.assertRaises(self.m.QuestionNotSubmitted):
            self.b.submit_async_answer('> 설명해 주세요\n\n첫 번째', 55)
        self.assert_no_input()


class CallbackAckTest(unittest.TestCase):
    setUp = QuestionsTest.setUp
    open = QuestionsTest.open
    callback = QuestionsTest.callback

    def test_callback_ack_failure_does_not_strand_submission(self):
        key = self.open()
        def call(method, **kwargs):
            if method == 'answerCallbackQuery':
                raise RuntimeError('callback query expired')
            return {'ok': True}
        self.telegram.call.side_effect = call
        self.q.callback(self.callback(key), SESSION)
        self.assertEqual(self.q.items[key]['status'], 'answered')
        self.submit.assert_called_once()


class ReplyTimeoutRecoveryTest(unittest.TestCase):
    setUp = QuestionsTest.setUp
    open = QuestionsTest.open

    def test_native_timeout_already_marked_uncertain_still_gets_recovery_button(self):
        key = self.open(); self.q.items[key]['status'] = 'uncertain'
        self.telegram.call.reset_mock()
        self.q.finish_reply(55, False, RuntimeError('Native question completion not confirmed'))
        edits = [c for c in self.telegram.call.call_args_list if c.args[0] == 'editMessageReplyMarkup']
        self.assertTrue(edits)
        self.assertEqual(json.loads(edits[-1].kwargs['reply_markup'])['inline_keyboard'][0][0]['callback_data'], f'{PREFIX}:{key}:recover')
