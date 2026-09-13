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
from codex_async_questions import AsyncQuestions, PREFIX, TTL

NOW = 1789220000
SESSION = '/sessions/current.jsonl'


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
        text = self.q.reply_text({'text': '다른 PR', 'reply_to_message': {'message_id': 55}}, SESSION)
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
