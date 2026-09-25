import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import voice_input_provenance as voice

class VoiceProvenanceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.session = self.root/'session.jsonl'
        self.session.write_text('')
        self.req = dict(id=str(uuid.uuid4()), origin='microphone', engine='codex',
                        node='laptop', text='승인\n본문 그대로')
        self.record = voice.record_voice_input(self.req, self.session, directory=self.root/'receipts', now=100)

    def match(self, **kwargs):
        args = dict(text=self.req['text'], session_path=self.session, event_offset=0,
                    node='laptop', directory=self.root/'receipts', now=101)
        args.update(kwargs)
        return voice.match_voice_input(**args)

    def test_matches_one_event_and_allows_delivery_retry(self):
        self.assertTrue(self.match())
        self.assertTrue(self.match())
        self.assertFalse(self.match(event_offset=25))

    def test_no_body_stored_or_prompt_modified(self):
        self.assertNotIn(self.req['text'], self.record.read_text())
        data = json.loads(self.record.read_text())
        self.assertEqual(data['sha256'], hashlib.sha256(self.req['text'].encode()).hexdigest())
        self.assertEqual(self.req['text'], '승인\n본문 그대로')
        if os.name != "nt":
            self.assertEqual(self.record.stat().st_mode & 0o777, 0o600)

    def test_wrong_body_session_target_offset_and_time_rejected(self):
        for changes in [dict(text='승인'), dict(session_path=self.root/'other'),
                        dict(node='node'), dict(event_offset=-1), dict(event_offset=None),
                        dict(now=99), dict(now=221)]:
            with self.subTest(changes=changes): self.assertFalse(self.match(**changes))

    def test_unrelated_user_prevents_later_same_phrase_from_matching(self):
        self.session.write_text(json.dumps({'payload':{'role':'user','content':'different'}})+'\n')
        self.assertFalse(self.match(event_offset=self.session.stat().st_size))

    def test_ambiguous_or_malformed_receipts_fail_closed(self):
        (self.record.parent/'broken.json').write_text('null')
        req = dict(self.req, id=str(uuid.uuid4()))
        voice.record_voice_input(req, self.session, directory=self.record.parent, now=100)
        self.assertFalse(self.match())

    def test_only_microphone_codex_creates_receipt(self):
        for changes in [dict(origin='synthetic'), dict(engine='cursor')]:
            self.assertIsNone(voice.record_voice_input(dict(self.req, **changes), self.session, directory=self.record.parent))

    def test_nest_capture_metadata_is_display_only_and_bound_to_receipt(self):
        request = dict(self.req, start_source='google-home-matter', capture_node='node')
        voice.record_voice_input(request, self.session, directory=self.record.parent, now=100)
        data = self.match(details=True)
        self.assertEqual(data['start_source'], 'google-home-matter')
        self.assertEqual(data['capture_node'], 'node')
        self.assertFalse(self.match(text='different', details=True))

    def test_unknown_metadata_is_not_echoed(self):
        request = dict(self.req, start_source='untrusted label', capture_node=None)
        voice.record_voice_input(request, self.session, directory=self.record.parent, now=100)
        data = self.match(details=True)
        self.assertEqual(data['start_source'], 'microphone')
        self.assertIsNone(data['capture_node'])

    def test_fresh_session_receipt_requires_same_live_pane_and_session(self):
        self.record.unlink()
        voice.record_voice_input(self.req, None, directory=self.record.parent, now=100, pane_pid=23)
        self.assertFalse(self.match())
        self.assertFalse(self.match(pane_pid=24, resolve_session=lambda pid:self.session))
        self.assertFalse(self.match(pane_pid=23, resolve_session=lambda pid:self.root/'wrong.jsonl'))
        self.assertFalse(self.match(pane_pid=23, resolve_session=lambda pid:None))
        self.assertTrue(self.match(pane_pid=23, resolve_session=lambda pid:self.session))
        self.assertTrue(self.match())

    def test_fresh_receipt_does_not_match_an_existing_prior_user_event(self):
        self.record.unlink()
        voice.record_voice_input(self.req, None, directory=self.record.parent, now=100, pane_pid=23)
        self.session.write_text(json.dumps({'payload': {'role': 'user', 'content': 'previous'}})+'\n')
        self.assertFalse(self.match(event_offset=self.session.stat().st_size, pane_pid=23,
                                    resolve_session=lambda pid:self.session))

    def test_receiver_binds_new_session_before_file_descriptor_closes(self):
        self.record.unlink()
        path = voice.record_voice_input(self.req, None, directory=self.record.parent, now=100, pane_pid=23)
        context = json.dumps({'payload': {'role': 'user', 'content': 'initial context'}})+'\n'
        self.session.write_bytes((context + json.dumps({'payload': {'role': 'user', 'content': self.req['text']}})+'\n').encode())
        offset = len(context.encode())
        voice.bind_voice_input_session(path, self.session)
        self.assertTrue(self.match(event_offset=offset))
        voice.bind_voice_input_session(path, self.root/'different')
        self.assertTrue(self.match(event_offset=offset))

    def clear_race(self, stamp='1970-01-01T00:01:40.500Z'):
        old = self.root/'old.jsonl'
        old.write_text(' ' * 75000)
        request = dict(self.req, start_source='google-home-matter', capture_node='node')
        voice.record_voice_input(request, old, directory=self.record.parent, now=100, pane_pid=23)
        context = json.dumps({'payload': {'role': 'user', 'content': 'initial context'}})+'\n'
        event = {'timestamp': stamp, 'payload': {'role': 'user', 'content': self.req['text']}}
        self.session.write_bytes((context + json.dumps(event)+'\n').encode())
        return len(context.encode())

    def test_clear_stale_session_rebinds_before_smaller_offset_check(self):
        offset = self.clear_race()
        data = self.match(event_offset=offset, pane_pid=23,
                          resolve_session=lambda pid:self.session, details=True)
        self.assertEqual(data['session'], str(self.session.resolve()))
        self.assertEqual(data['after_offset'], offset)
        self.assertEqual(data['start_source'], 'google-home-matter')
        self.assertTrue(self.match(event_offset=offset))

    def test_clear_rebind_requires_live_pane_and_post_injection_event(self):
        for stamp in ['1970-01-01T00:01:39Z', '1970-01-01T00:03:41Z', 'bad']:
            offset = self.clear_race(stamp)
            self.assertFalse(self.match(event_offset=offset, pane_pid=23,
                                        resolve_session=lambda pid:self.session))
        offset = self.clear_race()
        self.assertFalse(self.match(event_offset=offset, pane_pid=24, resolve_session=lambda pid:self.session))
        self.assertFalse(self.match(event_offset=offset, pane_pid=23, resolve_session=lambda pid:self.root/'other'))

    def test_receiver_rebinds_stale_session_without_replaying_text(self):
        offset = self.clear_race()
        voice.bind_voice_input_session(self.record, self.session)
        self.assertTrue(self.match(event_offset=offset))

    def test_clear_race_real_card_keeps_nest_source(self):
        offset = self.clear_race()
        path = Path(__file__).resolve().parents[1]/'codex_repl_bridge.py'
        spec = importlib.util.spec_from_file_location('clear_voice_bridge', path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)
        bridge = mod.Bridge.__new__(mod.Bridge)
        bridge.config = SimpleNamespace(bridge_kill=False, node='laptop')
        bridge.repl = SimpleNamespace(pane_pid=lambda:23)
        bridge.session_path = self.session
        bridge.input_event_offset = offset
        bridge.bridge_state = {}
        bridge.persist_state = lambda **kw: mod.ring_push(bridge.bridge_state, kw['event_key'], 100)
        sent = []
        bridge.telegram = SimpleNamespace(send=lambda body, **kw: sent.append(body) or True)
        with patch.object(mod, 'session_file_from_descendants', return_value=self.session), patch.object(voice, 'receipt_dir', return_value=self.record.parent), patch.object(voice.time, 'time', return_value=101):
            bridge.emit_sent_directive_card(self.req['text'])
        self.assertEqual(len(sent), 1)
        self.assertIn('네스트로 시작 · node 음성 입력 · 자비스 전달', sent[0])
        self.assertNotIn('미확인', sent[0])

    def test_completed_first_turn_attach_emits_voice_card_once(self):
        path = Path(__file__).resolve().parents[1]/'codex_repl_bridge.py'
        spec = importlib.util.spec_from_file_location('voice_attach_bridge', path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)
        bridge = mod.Bridge.__new__(mod.Bridge)
        bridge.config = SimpleNamespace(bridge_kill=False, node='laptop', tail_scan_bytes=100000)
        bridge.session_path = self.session
        bridge.bridge_state = {}
        bridge.persist_state = lambda **kw: mod.ring_push(bridge.bridge_state, kw['event_key'], 100)
        sent = []
        bridge.telegram = SimpleNamespace(send=lambda body, **kw: sent.append(body) or True)
        events = [SimpleNamespace(kind='user', text=self.req['text'], start=0),
                  SimpleNamespace(kind='assistant', text='reply', start=100)]
        with patch.object(mod, 'read_tail_jsonl_events', return_value=events), patch.object(voice, 'receipt_dir', return_value=self.record.parent), patch.object(voice.time, 'time', return_value=101):
            bridge.bind_preexisting_session_user(self.session)
            bridge.bind_preexisting_session_user(self.session)
        self.assertEqual(len(sent), 1)
        self.assertIn('받은 음성 입력', sent[0])
        self.assertTrue(sent[0].endswith(self.req['text']))

    def test_real_bridge_card_uses_metadata_only_for_matched_event(self):
        path = Path(__file__).resolve().parents[1]/'codex_repl_bridge.py'
        spec = importlib.util.spec_from_file_location('voice_card_bridge', path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)
        sent = []
        bridge = mod.Bridge.__new__(mod.Bridge)
        bridge.config = SimpleNamespace(bridge_kill=False, node='laptop')
        bridge.bridge_state = {}
        bridge.persist_state = lambda **kw: mod.ring_push(bridge.bridge_state, kw["event_key"], 100)
        bridge.session_path = self.session
        bridge.input_event_offset = 0
        bridge.telegram = SimpleNamespace(send=lambda body, **kw: sent.append(body) or True)
        with patch.object(voice, 'receipt_dir', return_value=self.record.parent), patch.object(voice.time, 'time', return_value=101):
            bridge.emit_sent_directive_card(self.req['text'])
            bridge.bridge_state = {}
            request = dict(self.req, start_source='google-home-matter', capture_node='node')
            voice.record_voice_input(request, self.session, directory=self.record.parent, now=100)
            bridge.emit_sent_directive_card(self.req['text'])
            bridge.input_event_offset = 10
            bridge.emit_sent_directive_card(self.req['text'])
        self.assertIn('🎙 받은 음성 입력 — 자비스', sent[0])
        self.assertTrue(sent[0].endswith(self.req['text']))
        self.assertIn('네스트로 시작 · node 음성 입력 · 자비스 전달', sent[1])
        self.assertIn('전달 프로그램 미확인', sent[2])
        bridge.input_event_offset = 0
        with patch.object(voice, 'receipt_dir', return_value=self.record.parent), patch.object(voice.time, 'time', return_value=101):
            bridge.emit_sent_directive_card(self.req['text'], voice_only=True)
            bridge.emit_sent_directive_card('unmatched', voice_only=True)
        self.assertEqual(len(sent), 3)

if __name__ == '__main__': unittest.main()
