"""Locale behavior at the command and Telegram rendering boundaries; no live I/O."""
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
from types import SimpleNamespace
from string import Formatter

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from bridge_i18n import Language, MESSAGES, translate


def load_bridge():
    path = ROOT / 'codex-repl-telegram-bridge.py'
    if not path.exists():
        path = ROOT / 'codex_repl_bridge.py'
    spec = importlib.util.spec_from_file_location('i18n_repl', path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


class LanguageTests(unittest.TestCase):
    def test_catalog_has_matching_placeholders_in_both_languages(self):
        for entry in MESSAGES.values():
            fields = lambda value: {name for _, name, _, _ in Formatter().parse(value) if name is not None}
            self.assertEqual(fields(entry['en']), fields(entry['ko']), entry['en'])

    def test_environment_region_and_unsupported_fallback(self):
        with patch.dict(os.environ, {'TAB_LANGUAGE': 'ko-KR'}, clear=True):
            self.assertEqual(Language().code, 'ko')
        with patch.dict(os.environ, {'CRB_LANGUAGE': 'en_US', 'TAB_LANGUAGE': 'ko'}, clear=True):
            self.assertEqual(Language().code, 'en')
        with patch.dict(os.environ, {'CRB_LANGUAGE': 'invalid'}, clear=True):
            self.assertEqual(Language().code, 'en')

    def test_explicit_selection_survives_restart_and_overrides_env(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'TAB_LANGUAGE': 'en'}, clear=True):
            language = Language(Path(tmp))
            self.assertIn('한국어', language.command('/language ko'))
            self.assertEqual(Language(Path(tmp)).code, 'ko')
            before = (Path(tmp) / 'bridge-language.json').read_bytes()
            self.assertIn('/language en', language.command('/language ja'))
            self.assertEqual(language.code, 'ko')
            self.assertEqual((Path(tmp) / 'bridge-language.json').read_bytes(), before)
            self.assertIn('한국어', language.command('/language'))
            self.assertIn('English', language.command('/language@my_bot en'))
            self.assertIsNone(language.command('translate /language en'))

    def test_corrupt_state_falls_back_and_write_failure_is_not_success(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {}, clear=True):
            (Path(tmp) / 'bridge-language.json').write_text('{broken')
            language = Language(Path(tmp))
            self.assertEqual(language.code, 'en')
            with patch('bridge_i18n.os.replace', side_effect=OSError('read-only')):
                self.assertIn('Could not save', language.command('/language ko'))
            self.assertEqual(language.code, 'en')

    def test_translation_formats_values_once_without_translating_them(self):
        value = '한국어 {literal} /language en'
        self.assertEqual(translate('Reason:', 'ko'), '이유:')
        self.assertEqual(translate('codex failed: {detail}', 'ko', detail=value), 'Codex 실행 실패: ' + value)
        self.assertEqual(translate('Unknown diagnostic {detail}', 'xx', detail=value), 'Unknown diagnostic ' + value)


class ReplLanguageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.m = load_bridge()

    def test_installation_default_and_selected_override(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {}, clear=True):
            cfg = SimpleNamespace(state_path=Path(tmp)/'state.json', bridge_kill=False, chat_id='123', node='test', emoji='')
            bridge = self.m.Bridge(cfg, Mock(), Mock())
            expected = 'ko' if (ROOT / 'codex-repl-telegram-bridge.py').exists() else 'en'
            self.assertEqual(bridge.language.code, expected)
            self.assertEqual(bridge.telegram.language, bridge.language)

    def test_language_command_is_allowlisted_and_never_reaches_codex(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {}, clear=True):
            cfg = SimpleNamespace(state_path=Path(tmp)/'state.json', bridge_kill=False, chat_id='123', node='test', emoji='')
            bridge = self.m.Bridge(cfg, Mock(), Mock())
            bridge.handle_choice_reply = Mock(return_value=False)
            bridge.get_async_questions = Mock()
            bridge.get_async_questions.return_value.reply_text.return_value = None
            bridge.prompt_from_telegram_message = Mock()
            for chat in ('999', '123'):
                bridge.process_telegram_update({'update_id': 1, 'message': {'chat': {'id': chat}, 'text': '/language en'}})
                if chat == '999':
                    bridge.telegram.send.assert_not_called()
                    self.assertFalse((Path(tmp) / 'bridge-language.json').exists())
            self.assertIn('English', bridge.telegram.send.call_args.args[0])
            bridge.prompt_from_telegram_message.assert_not_called()
            bridge.repl.send_key.assert_not_called()
            self.assertEqual(Language(Path(tmp)).code, 'en')

    def test_card_localizes_chrome_but_preserves_source_and_callbacks(self):
        with patch.dict(os.environ, {}, clear=True):
            client = self.m.TelegramClient('dummy', '123', '', 4096)
            client.language = Language(default='ko')
            client.call = Mock(return_value={'ok': True, 'result': {'message_id': 1}})
            prompt = self.m.ApprovalPrompt('id', 'echo "keep {raw}"', 'Original reason', (self.m.ApprovalOption('1', 'Yes', 'y'),))
            client.send_approval_prompt(prompt)
            params = client.call.call_args.kwargs
            self.assertIn('명령:', params['text'])
            self.assertIn('echo "keep {raw}"', params['text'])
            self.assertIn('Original reason', params['text'])
            self.assertEqual(json.loads(params['reply_markup'])['inline_keyboard'][0][0]['callback_data'], f'{self.m.APPROVAL_CALLBACK_PREFIX}:id:1')
            client.send('Codex is waiting for command approval. 사용자 답변')
            self.assertEqual(client.call.call_args.kwargs['text'], 'Codex is waiting for command approval. 사용자 답변')

    def test_expired_typed_answer_is_consumed_in_either_language(self):
        bridge = object.__new__(self.m.Bridge)
        bridge.telegram = Mock()
        for prefix in ('Codex 선택 답변 · ', 'Codex typed answer · '):
            self.assertTrue(bridge.handle_choice_reply({'text': 'answer', 'reply_to_message': {'text': prefix + 'old'}}))

    def test_clear_result_uses_selected_language_without_resetting_session(self):
        bridge = object.__new__(self.m.Bridge)
        bridge.telegram = Mock()
        with patch.dict(os.environ, {}, clear=True):
            bridge.language = Language(default='en')
            bridge.send_clear_notice(self.m.CLEAR_SLASH_COMPLETE, 'sent', 'failed')
            bridge.telegram.send.assert_called_once_with('Session cleared.')

    def test_async_question_preserves_original_options_and_callback_values(self):
        from codex_async_questions import AsyncQuestions, PREFIX
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {}, clear=True):
            telegram = Mock(chat_id='123')
            telegram.language = Language(default='en')
            questions = AsyncQuestions(Path(tmp) / 'questions.json', telegram, Mock(), translate=telegram.language.text)
            item = {'status': 'open', 'options': ['원문 선택', 'Other'], 'message_id': 7}
            questions.refresh_buttons('question-key', item)
            markup = json.loads(telegram.call.call_args.kwargs['reply_markup'])
            self.assertEqual(markup['inline_keyboard'][0][0], {'text': '1. 원문 선택', 'callback_data': f'{PREFIX}:question-key:0'})
            item['status'] = 'uncertain'
            questions.refresh_buttons('question-key', item)
            self.assertIn('Restore choices', telegram.call.call_args.kwargs['reply_markup'])


class ExecLanguageTests(unittest.TestCase):
    def test_legacy_console_encoding_does_not_block_original_telegram_text(self):
        package_root = ROOT.parent / 'packaging' / 'codex-telegram-bridge'
        if package_root.exists():
            sys.path.insert(0, str(package_root))
        import telegram_agent_bridge as tab
        for encoding in ('cp1252', 'ascii', 'utf-8'):
            with self.subTest(encoding=encoding), tempfile.TemporaryDirectory() as tmp:
                config = SimpleNamespace(state_dir=Path(tmp), chat_id='123',
                                         prefix='', prefix_line=False, telegram_chunk=3500)
                telegram = Mock()
                bridge = tab.Bridge(config, SimpleNamespace(name='codex'), telegram)
                bridge.language.code = 'ko'
                raw = io.BytesIO()
                with io.TextIOWrapper(raw, encoding=encoding, errors='strict') as output:
                    with patch('sys.stdout', output):
                        bridge.handle_message_text('/ping')
                        status = telegram.call.call_args.kwargs['text']
                        bridge.mirror_prompt(tab.BridgeJob('telegram', '원문 입력'))
                        bridge.mirror_answer(tab.BridgeJob('telegram', 'question'), '원문 답변 🤖')
                    self.assertIn('실행 중', status)
                    self.assertEqual(telegram.call.call_args.kwargs['text'], '원문 답변 🤖')
                    self.assertTrue(bridge.jobs.empty())
                    self.assertTrue(raw.getvalue())
                    if encoding == 'utf-8':
                        self.assertIn('원문 답변 🤖', raw.getvalue().decode(encoding))

    def test_chat_command_does_not_enqueue_and_answer_remains_original(self):
        package_root = ROOT.parent / 'packaging' / 'codex-telegram-bridge'
        if package_root.exists():
            sys.path.insert(0, str(package_root))
        import telegram_agent_bridge as tab
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {}, clear=True):
            cfg = SimpleNamespace(state_dir=Path(tmp), chat_id='123', prefix='', prefix_line=False, telegram_chunk=4096)
            telegram = Mock()
            bridge = tab.Bridge(cfg, SimpleNamespace(name='codex'), telegram)
            bridge.handle_update({'message': {'chat': {'id': '999'}, 'text': '/language ko'}})
            self.assertEqual(bridge.language.code, 'en')
            bridge.handle_update({'message': {'chat': {'id': '123'}, 'text': '/language ko'}})
            self.assertTrue(bridge.jobs.empty())
            bridge.handle_message_text('/ping')
            self.assertIn('실행 중', telegram.call.call_args.kwargs['text'])
            bridge.handle_message_text('raw 사용자 prompt')
            self.assertEqual(bridge.jobs.get_nowait().text, 'raw 사용자 prompt')
            bridge.mirror_answer(tab.BridgeJob('telegram', 'question'), 'codex REPL bridge running')
            self.assertEqual(telegram.call.call_args.kwargs['text'], 'codex REPL bridge running')

    def test_setup_language_option_validates_and_writes_private_config(self):
        package_root = ROOT.parent / 'packaging' / 'codex-telegram-bridge'
        if package_root.exists():
            sys.path.insert(0, str(package_root))
        import bridge_setup as setup
        args = setup.build_parser().parse_args(['setup', '--language', 'ko'])
        self.assertEqual(args.language, 'ko')
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'bridge.env'
            setup.write_env_config(path, mode='exec', token='test', chat_id='123', agent='codex', agent_cmd='codex', workdir=Path(tmp), prefix='', prefix_line=False, state_dir=Path(tmp), local_input=Path(tmp)/'input', dangerous_bypass=False, tmux_socket='codex', tmux_session='codex', submit_key='Tab', audio_transcribe_cmd='', language='ko')
            self.assertEqual(setup.load_env_file(path)['TAB_LANGUAGE'], 'ko')
            if os.name != 'nt':
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            api = Mock(return_value={'ok': True})
            self.assertTrue(setup.send_test_message('test', '123', api_call=api, language='ko', service_status_text='active'))
            self.assertIn('브릿지 설정을 완료', api.call_args.kwargs['text'])


if __name__ == '__main__':
    unittest.main()
