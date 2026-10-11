# @mutate-why Other 버튼이 빠지면 사용자가 직접 입력을 시작할 수 없다
# @mutate scripts/codex_async_questions.py | buttons.append([{'text': f'{len(item["options"])+1}. Other', | buttons.extend([]); unused = ([{'text': f'{len(item["options"])+1}. Other',
# @mutate-why 취소한 입력 요청을 다시 받으면 오래된 답장이 현재 질문을 제출한다
# @mutate scripts/codex_async_questions.py | and reply['message_id'] != item.get('input_message_id')): | and False):
# @mutate-why 화면의 Other 확인을 없애면 다른 선택지를 승인하고 답변을 입력할 수 있다
# @mutate scripts/codex-repl-telegram-bridge.py | if len(others) != 1 or others[0].label.strip().casefold() not in {'other', '기타', '직접 입력'}: | if False:
# @mutate-why 버튼만 지우는 동작으로 돌아가면 승인 전달 뒤 원본 카드에 확인 표시가 없다
# @mutate scripts/codex_async_questions.py | self.complete_card(item) | self.clear_buttons(item)
# @mutate-why 전송 확인 없이 성공 처리하면 실패한 답변에도 완료 표시가 붙는다
# @mutate scripts/codex_async_questions.py | if confirmed is True or item['status'] == 'answered': | if True:
# @mutate-why 선택된 Other 화면을 별도 편집기로만 처리하면 실제 0.154 답변이 전송되지 않는다
# @mutate scripts/codex-repl-telegram-bridge.py | if inline_other: | if False and inline_other:
# @mutate-why 붙여넣기 직전 화면을 다시 확인하지 않으면 변경된 질문이나 초안을 덮어쓴다
# @mutate scripts/codex-repl-telegram-bridge.py | if not empty_inline_other_editor(screen, item['title'], item['options']): | if False:

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_codex_async_questions as fixtures
from test_codex_async_questions import AsyncQuestions, PREFIX, SESSION, TTL, accepted, editor_screen, request


class OtherCardTest(unittest.TestCase):
    setUp = fixtures.QuestionsTest.setUp
    open = fixtures.QuestionsTest.open
    callback = fixtures.QuestionsTest.callback

    def other(self, key, action='other'):
        return self.callback(key, data=f'{PREFIX}:{key}:{action}')

    def request_input(self):
        key = self.open()
        self.telegram.call.return_value = {'ok': True, 'result': {'message_id': 56}}
        self.q.callback(self.other(key), SESSION)
        return key

    def reply(self, mid=56, text='  직접 쓴 답변\n둘째 줄  '):
        return {'message_id': 80, 'text': text, 'reply_to_message': {'message_id': mid},
                'chat': {'id': 123}, 'from': {'id': 123}}

    def test_all_three_options_and_fourth_other_are_shown(self):
        self.q.observe(request(options=['아직 확인하지 못함', '정상적으로 제출됨', '여전히 제출되지 않음']), SESSION)
        self.q.observe(accepted(), SESSION)
        call = self.telegram.call.call_args.kwargs
        buttons = json.loads(call['reply_markup'])['inline_keyboard']
        self.assertEqual([r[0]['text'] for r in buttons],
                         ['1. 아직 확인하지 못함', '2. 정상적으로 제출됨', '3. 여전히 제출되지 않음', '4. Other'])
        self.assertIn('4. Other', call['text'])
        self.assertIn('Other', call['text'])
        self.assertNotIn('같은 답만', call['text'])

    def test_other_opens_force_reply_without_submitting_an_answer(self):
        key = self.request_input()
        sends = [c for c in self.telegram.call.call_args_list if c.args[0] == 'sendMessage']
        self.assertEqual(len(sends), 2)
        self.assertTrue(json.loads(sends[-1].kwargs['reply_markup'])['force_reply'])
        self.assertIn('어느 PR인가요?', sends[-1].kwargs['text'])
        self.assertEqual(self.q.items[key]['input_message_id'], 56)
        self.assertEqual(self.q.items[key]['status'], 'open')
        self.submit.assert_not_called()
        self.q.callback(self.other(key), SESSION)
        self.assertEqual(len([c for c in self.telegram.call.call_args_list if c.args[0] == 'sendMessage']), 2)

    def test_reply_binding_and_exact_contents_survive_restart(self):
        key = self.request_input()
        self.q = AsyncQuestions(self.path, self.telegram, self.submit, clock=lambda: self.now)
        msg = self.reply()
        self.assertEqual(self.q.reply_text(msg, SESSION), '> 어느 PR인가요?\n\n' + msg['text'])
        self.assertEqual(self.q.items[key]['answer_mode'], 'other')
        self.q.finish_reply(56, True)
        self.assertEqual(self.q.items[key]['status'], 'answered')
        self.assertEqual(self.q.reply_text(msg, SESSION), '')

    def test_cancelled_or_replaced_input_remains_owned_but_cannot_submit(self):
        key = self.request_input()
        self.q.callback(self.other(key, 'cancel'), SESSION)
        self.assertEqual(self.q.items[key]['status'], 'open')
        self.assertEqual(self.q.reply_text(self.reply(), SESSION), '')
        self.telegram.call.return_value = {'ok': True, 'result': {'message_id': 57}}
        self.q.callback(self.other(key), SESSION)
        self.assertEqual(self.q.reply_text(self.reply(), SESSION), '')
        self.assertEqual(self.q.reply_text(self.reply(57), SESSION), '> 어느 PR인가요?\n\n' + self.reply()['text'])
        self.submit.assert_not_called()

    def test_selecting_normal_option_invalidates_prior_other_request(self):
        key = self.request_input()
        self.q.callback(self.callback(key), SESSION)
        self.submit.assert_called_once_with('> 어느 PR인가요?\n\n둘 다', 55)
        self.assertEqual(self.q.reply_text(self.reply(), SESSION), '')

    def test_wrong_user_chat_session_expiry_and_controls_cannot_claim_input(self):
        key = self.request_input()
        for section in ('from', 'chat'):
            msg = self.reply(); msg[section]['id'] = 999
            self.assertEqual(self.q.reply_text(msg, SESSION), '')
        self.assertEqual(self.q.reply_text(self.reply(), '/another'), '')
        for text in (' ', '\x1bunsafe', 'unsafe\x7f', 'unsafe\ranswer'):
            self.assertEqual(self.q.reply_text(self.reply(text=text), SESSION), '')
        self.assertEqual(self.q.items[key]['status'], 'open')
        self.now += TTL
        self.assertEqual(self.q.reply_text(self.reply(), SESSION), '')
        self.assertEqual(self.q.items[key]['status'], 'expired')
        self.submit.assert_not_called()

    def test_unbound_message_stays_an_ordinary_message_after_other(self):
        self.request_input()
        msg = self.reply(); msg.pop('reply_to_message')
        self.assertIsNone(self.q.reply_text(msg, SESSION))

    def test_late_other_button_cannot_open_input_after_session_change_or_answer(self):
        key = self.open()
        self.telegram.call.reset_mock()
        self.q.callback(self.other(key), '/another')
        self.q.items[key]['status'] = 'answered'
        self.q.callback(self.other(key), SESSION)
        self.assertFalse([c for c in self.telegram.call.call_args_list if c.args[0] == 'sendMessage'])
        self.submit.assert_not_called()

    def test_unconfirmed_reply_does_not_accept_repeated_other_input(self):
        key = self.request_input()
        self.q.reply_text(self.reply(), SESSION)
        self.q.finish_reply(56, False, RuntimeError('transport receipt missing'))
        self.assertEqual(self.q.items[key]['status'], 'uncertain')
        self.assertEqual(self.q.reply_text(self.reply(), SESSION), '')
        self.q.callback(self.other(key), SESSION)
        self.submit.assert_not_called()

    def test_request_delivery_failure_keeps_original_reply_path(self):
        key = self.open()
        self.telegram.call.return_value = {'ok': True, 'result': {'message_id': 0}}
        self.q.callback(self.other(key), SESSION)
        self.assertNotIn('input_message_id', self.q.items[key])
        self.assertEqual(self.q.items[key]['status'], 'open')
        self.submit.assert_not_called()
        self.assertTrue(self.q.reply_text(self.reply(55), SESSION))

    def test_long_question_still_sends_a_valid_force_reply(self):
        self.q.observe(request(title='긴 질문 ' * 1200), SESSION)
        self.q.observe(accepted(), SESSION)
        key = next(iter(self.q.items))
        self.telegram.call.return_value = {'ok': True, 'result': {'message_id': 56}}
        self.q.callback(self.other(key), SESSION)
        sends = [c for c in self.telegram.call.call_args_list if c.args[0] == 'sendMessage']
        self.assertLess(len(sends[-1].kwargs['text']), 4096)
        self.assertTrue(json.loads(sends[-1].kwargs['reply_markup'])['force_reply'])

    def test_confirmed_option_marks_original_card_and_selected_option_once(self):
        key = self.open()
        self.q.callback(self.callback(key), SESSION)
        self.q.callback(self.callback(key), SESSION)
        edits = [c for c in self.telegram.call.call_args_list if c.args[0] == 'editMessageText']
        self.assertEqual(len(edits), 1)
        self.assertEqual(edits[0].kwargs['message_id'], 55)
        self.assertIn('✅ 응답 완료', edits[0].kwargs['text'])
        self.assertIn('✅ 2. 둘 다', edits[0].kwargs['text'])
        self.assertNotIn('유효합니다', edits[0].kwargs['text'])
        self.assertEqual(json.loads(edits[0].kwargs['reply_markup']), {'inline_keyboard': []})
        self.submit.assert_called_once()

    def test_unconfirmed_or_rejected_submission_never_displays_completion(self):
        from codex_async_questions import QuestionNotSubmitted
        key = self.open()
        self.submit.return_value = False
        self.q.callback(self.callback(key), SESSION)
        self.assertEqual(self.q.items[key]['status'], 'uncertain')
        self.q.items[key]['status'] = 'open'
        self.submit.side_effect = QuestionNotSubmitted('no native question')
        self.q.callback(self.callback(key), SESSION)
        self.assertEqual(self.q.items[key]['status'], 'open')
        self.assertFalse([c for c in self.telegram.call.call_args_list if c.args[0] == 'editMessageText'])

    def test_display_failure_retries_only_card_edit_after_restart(self):
        key = self.open()
        def fail_edit(method, **kwargs):
            if method == 'editMessageText':
                raise RuntimeError('display failed')
            return {'ok': True}
        self.telegram.call.side_effect = fail_edit
        self.q.callback(self.callback(key), SESSION)
        self.assertEqual(self.q.items[key]['status'], 'answered')
        self.assertNotIn('completion_displayed', self.q.items[key])
        self.q = AsyncQuestions(self.path, self.telegram, self.submit, clock=lambda: self.now)
        self.telegram.call.side_effect = None
        self.q.callback(self.callback(key), SESSION)
        self.assertTrue(self.q.items[key]['completion_displayed'])
        self.submit.assert_called_once()

    def test_native_receipt_repairs_an_answered_card_without_completion_display(self):
        key = self.open(); item = self.q.items[key]
        item['status'] = 'answered'; self.q.save()
        self.q = AsyncQuestions(self.path, self.telegram, self.submit, clock=lambda: self.now)
        record = fixtures.NativeQuestionReceiptTest.receipt(self, item)
        self.q.observe(record, SESSION)
        self.assertTrue(self.q.items[key]['completion_displayed'])
        self.assertIn('✅ 1. 첫 번째', self.telegram.call.call_args.kwargs['text'])
        self.submit.assert_not_called()


class OtherNativeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixtures.FreeformBridgeIntegrationTest.setUpClass.__func__(cls)

    setUp = fixtures.FreeformBridgeIntegrationTest.setUp
    submit = fixtures.FreeformBridgeIntegrationTest.submit

    def panel(self, title='설명해 주세요', other='Other'):
        return f'{title}\n› 1. 첫 번째\n  2. 둘 다\n  3. {other}\nEnter to select'

    def prepare(self):
        self.item['options'] = ['첫 번째', '둘 다']
        return '설명해 주세요\nType your answer\nenter submit'

    def inline_panel(self, selected=3):
        rows = ['• Queued follow-up inputs', '', '  설명해 주세요', '']
        for index, label in enumerate(['첫 번째', '둘 다', 'Other'], 1):
            rows.append(f'  {"›" if index == selected else " "} {index}. {label}')
        return '\n'.join(rows + ['', '  enter submit   ctrl + ] skip   alt + ↓ main prompt', ''])

    def test_live_inline_other_pastes_exact_multiline_answer_once(self):
        self.prepare()
        answer = 'T-261011-038 자동 검증 입력입니다.\n한글 두 줄과  공백을 보존합니다.'
        panel = self.inline_panel()
        self.repl.capture_visible_screen.side_effect = [panel, panel] + ['› Ask Codex'] * 12
        self.assertTrue(self.submit(answer))
        self.repl.send_choice.assert_not_called()
        self.repl.send_choice_text.assert_called_once_with(answer)
        self.repl.paste_prompt.assert_not_called()
        self.assertEqual(self.item['status'], 'answered')

    def test_navigation_can_reach_inline_other_without_separate_editor(self):
        self.prepare()
        panel = self.inline_panel()
        self.repl.capture_visible_screen.side_effect = [self.inline_panel(1), panel, panel] + ['› Ask Codex'] * 12
        self.assertTrue(self.submit('직접 답변'))
        self.repl.send_choice.assert_called_once()
        self.assertEqual(self.repl.send_choice.call_args.args[0].index, 2)
        self.repl.send_choice_text.assert_called_once_with('직접 답변')

    def test_inline_other_rechecks_draft_question_options_and_session_before_paste(self):
        self.prepare()
        panel = self.inline_panel()
        changed = [panel.replace('설명해 주세요', '다른 질문'),
                   panel.replace('첫 번째', '다른 항목'),
                   panel.replace('3. Other', '3. Other 작성 중'),
                   panel.replace('3. Other\n', '3. Other\n  작성 중인 초안\n'),
                   self.inline_panel(1), panel + '\n› Ask Codex to do anything']
        for screen in changed:
            with self.subTest(screen=screen):
                self.repl.capture_visible_screen.side_effect = [panel, screen]
                with self.assertRaises(self.m.QuestionNotSubmitted):
                    self.submit('직접 답변')
                self.repl.send_choice.assert_not_called()
                self.repl.send_choice_text.assert_not_called()
        self.repl.capture_visible_screen.side_effect = [panel, panel]
        self.repl.session_file.side_effect = [Path(SESSION), Path('/other')]
        with self.assertRaises(self.m.QuestionNotSubmitted):
            self.submit('직접 답변')
        self.repl.send_choice_text.assert_not_called()

    def test_inline_other_requires_complete_current_empty_panel(self):
        self.prepare()
        panel = self.inline_panel()
        invalid = [self.inline_panel(1), panel.replace('3. Other', '3. Approve'),
                   panel.replace('3. Other', '3. Other 작성 중'),
                   panel.replace('3. Other\n', '3. Other\n  작성 중인 초안\n'),
                   panel.replace('첫 번째', '다른 항목'),
                   panel.replace('  설명해 주세요', '  다른 질문'),
                   panel.replace('    2. 둘 다\n', ''),
                   panel.replace('• Queued follow-up inputs\n', ''),
                   panel.replace('alt + ↓ main prompt', 'Enter to select'),
                   panel + '\n› Ask Codex to do anything', panel + panel]
        for screen in invalid:
            with self.subTest(screen=screen):
                self.assertFalse(self.m.empty_inline_other_editor(
                    screen, self.item['title'], self.item['options']))

    def test_observed_three_options_and_wrapped_title(self):
        title = ('Other 입력 검증에 사용할 답변을 알려주세요. 별도 문구가 없으면 제가 '
                 '‘T-261011-038 자동 검증 입력입니다.’와 ‘한글 두 줄과  공백을 보존합니다.’를 두 줄로 입력하겠습니다.')
        panel = ('• Queued follow-up inputs\n \n  '
                 + title.replace('한글 두 줄과', '한글 두\n  줄과')
                 + '\n \n    1. 기본 검증 문구 사용\n    2. 한글 한 줄로 검증\n'
                 '    3. 영문 한 줄로 검증\n  › 4. Other\n \n'
                 '  enter submit   ctrl + ] skip   alt + ↓ main prompt\n \n')
        self.assertTrue(self.m.empty_inline_other_editor(panel, title,
            ['기본 검증 문구 사용', '한글 한 줄로 검증', '영문 한 줄로 검증']))

    def test_full_telegram_inline_other_reply_completes_original_card_once(self):
        self.prepare(); self.item['status'] = 'open'
        q = self.b.get_async_questions(); key = next(iter(q.items))
        self.b.telegram.call.return_value = {'ok': True, 'result': {'message_id': 56}}
        self.b.handle_callback_query({'id': 'other-click', 'data': f'{PREFIX}:{key}:other',
            'message': {'message_id': 55, 'chat': {'id': 123}}, 'from': {'id': 123}})
        answer = 'T-261011-038 자동 검증 입력입니다.\n한글 두 줄과  공백을 보존합니다.'
        panel = self.inline_panel()
        self.repl.capture_visible_screen.side_effect = [panel, panel] + ['› Ask Codex'] * 12
        update = {'update_id': 1, 'message': {'message_id': 77, 'text': answer,
            'chat': {'id': 123}, 'from': {'id': 123}, 'reply_to_message': {'message_id': 56}}}
        self.b.process_telegram_update(update); self.b.process_telegram_update(update)
        self.repl.send_choice_text.assert_called_once_with(answer)
        self.assertEqual(self.item['status'], 'answered')
        edits = [c for c in self.b.telegram.call.call_args_list if c.args[0] == 'editMessageText']
        self.assertEqual(len(edits), 1)
        self.assertIn('✅ 응답 완료', edits[0].kwargs['text'])
        self.assertIn('✅ 3. Other', edits[0].kwargs['text'])

    def test_other_navigation_then_empty_editor_then_exact_text_once(self):
        editor = self.prepare()
        answer = '  선택지 밖의 한글 답변\n둘째 줄  '
        self.repl.capture_visible_screen.side_effect = [self.panel(), editor, '› Ask Codex', '› Ask Codex']
        self.assertTrue(self.submit(answer))
        self.repl.send_choice.assert_called_once()
        self.assertEqual(self.repl.send_choice.call_args.args[0].index, 2)
        self.repl.send_choice_text.assert_called_once_with(answer)
        self.repl.paste_prompt.assert_not_called()
        self.assertEqual(self.item['status'], 'answered')

    def test_other_requires_exact_question_options_and_native_other(self):
        self.prepare()
        for panel in (self.panel('다른 질문'), self.panel().replace('첫 번째', '다른 항목'),
                      self.panel(other='Approve'), self.panel().replace('  3. Other\n', '')):
            with self.subTest(panel=panel):
                self.repl.capture_visible_screen.return_value = panel
                with self.assertRaises(self.m.QuestionNotSubmitted):
                    self.submit('직접 답변')
                self.repl.send_choice.assert_not_called()
                self.repl.send_choice_text.assert_not_called()

    def test_open_existing_other_editor_without_reselecting(self):
        editor = self.prepare()
        self.repl.capture_visible_screen.side_effect = [editor, '› Ask Codex', '› Ask Codex']
        self.assertTrue(self.submit('직접 답변'))
        self.repl.send_choice.assert_not_called()
        self.repl.send_choice_text.assert_called_once_with('직접 답변')

    def test_changed_session_after_other_navigation_never_types(self):
        editor = self.prepare()
        self.repl.capture_visible_screen.side_effect = [self.panel(), editor]
        self.repl.session_file.side_effect = [Path(SESSION), Path('/other')]
        with self.assertRaises(self.m.QuestionNotSubmitted):
            self.submit('직접 답변')
        self.repl.send_choice_text.assert_not_called()

    def test_nonempty_or_unrelated_editor_preserves_draft(self):
        self.prepare()
        for editor in (editor_screen('작성 중인 초안'), '다른 질문\nType your answer\nenter submit'):
            with self.subTest(editor=editor):
                self.repl.capture_visible_screen.side_effect = [self.panel()] + [editor] * 10
                with self.assertRaises(self.m.QuestionNotSubmitted):
                    self.submit('직접 답변')
                self.repl.send_choice_text.assert_not_called()
                self.repl.send_key.assert_not_called()

    def test_full_telegram_other_click_reply_and_duplicate(self):
        editor = self.prepare(); self.item['status'] = 'open'
        q = self.b.get_async_questions(); key = next(iter(q.items))
        self.b.telegram.call.return_value = {'ok': True, 'result': {'message_id': 56}}
        cb = {'id': 'other-click', 'data': f'{PREFIX}:{key}:other',
              'message': {'message_id': 55, 'chat': {'id': 123}}, 'from': {'id': 123}}
        self.b.handle_callback_query(cb)
        self.repl.capture_visible_screen.assert_not_called()
        self.b.handle_choice_reply = Mock(); self.b.prompt_from_telegram_message = Mock()
        # Choosing Other explicitly must preserve the user's literal contents,
        # even when those contents happen to equal a suggested option.
        answer = '첫 번째'
        self.repl.capture_visible_screen.side_effect = [self.panel(), editor, '› Ask Codex', '› Ask Codex']
        update = {'update_id': 1, 'message': {'message_id': 77, 'text': answer,
                  'chat': {'id': 123}, 'from': {'id': 123}, 'reply_to_message': {'message_id': 56}}}
        self.b.process_telegram_update(update); self.b.process_telegram_update(update)
        self.repl.send_choice_text.assert_called_once_with(answer)
        self.assertEqual(self.repl.send_choice.call_args.args[0].index, 2)
        self.assertEqual(self.item['status'], 'answered')
        self.b.handle_choice_reply.assert_not_called()
        self.b.prompt_from_telegram_message.assert_not_called()
        edits = [c for c in self.b.telegram.call.call_args_list if c.args[0] == 'editMessageText']
        self.assertEqual(len(edits), 1)
        self.assertIn('✅ 응답 완료', edits[0].kwargs['text'])
        self.assertIn('✅ 3. Other', edits[0].kwargs['text'])

    def test_bridge_pause_keeps_reply_retryable_without_native_input(self):
        self.prepare(); self.item['status'] = 'open'
        self.b.config = self.m.replace(self.b.config, bridge_kill=True)
        self.b.process_telegram_update({'update_id': 1, 'message': {'message_id': 77,
            'text': '직접 답변', 'chat': {'id': 123}, 'from': {'id': 123},
            'reply_to_message': {'message_id': 55}}})
        self.assertEqual(self.item['status'], 'open')
        self.repl.capture_visible_screen.assert_not_called()
        self.repl.send_choice.assert_not_called()
        self.repl.send_choice_text.assert_not_called()


if __name__ == '__main__':
    unittest.main()
