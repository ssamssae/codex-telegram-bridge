"""Cross-node regressions: final fallback, transport retry, and pane ownership."""
import io
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import urllib.error
import sys
import tempfile
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import codex_repl_bridge as m

def user():
    return {"type":"response_item","payload":{"type":"message","role":"user","content":[{"type":"input_text","text":"request"}]}}

def final():
    return {"type":"response_item","payload":{"type":"message","role":"assistant","phase":"final_answer","content":[{"type":"output_text","text":"finished"}]}}


class CompletionIntegration(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.b = m.Bridge(SimpleNamespace(state_path=Path(self.tmp.name)/"state.json",bridge_kill=False,chat_id="123",node="test",emoji=""), Mock(), Mock())
        for name in ("persist_state","begin_repl_typing","prime_flow_screen_snapshot","start_long_running_progress","stop_telegram_fallback","stop_repl_typing","stop_long_running_progress","emit_sent_directive_card","emit_received_telegram_directive_card","send_pending_reasoning_mirror","resolve_midreport_obligation","warn_incomplete_copy_payload_pair_if_needed"):
            setattr(self.b,name,Mock())
        self.b.request_incomplete_copy_payload_pair_repair_if_needed = Mock(return_value=False)
        self.b.send_answer = Mock(return_value=True)


    def test_idle_startup_can_check_interruption_before_first_user(self):
        self.b.finish_pending_interrupt()
        self.b.telegram.send.assert_not_called()

    def prepare(self):
        self.b.session_path = Path(self.tmp.name) / 'session.jsonl'
        self.b.bridge_state = {'coord_ring': []}
        def persist(offset=None, event_key=None):
            if event_key:
                m.ring_push(self.b.bridge_state, event_key, 100)
        self.b.persist_state = Mock(side_effect=persist)
        self.b.process_line(json.dumps(user()))
        self.b.process_line(json.dumps({'type':'event_msg','payload':{'type':'task_started','turn_id':'turn-a'}}))

    def completion(self, turn='turn-a'):
        return json.dumps({'type':'event_msg','payload':{'type':'task_complete','turn_id':turn,'last_agent_message':'finished'}})

    def test_completion_only_record_delivers(self):
        self.prepare()
        self.assertTrue(self.b.process_line(self.completion()))
        self.b.send_answer.assert_called_once_with('finished')

    def test_final_then_completion_is_delivered_once(self):
        self.prepare()
        self.b.process_line(json.dumps(final()))
        self.b.process_line(self.completion())
        self.b.send_answer.assert_called_once_with('finished')

    def test_previous_turn_completion_cannot_end_current_turn(self):
        self.prepare()
        self.b.process_line(self.completion('old'))
        self.b.send_answer.assert_not_called()
        self.assertFalse(self.b.suppress_until_user)

    def test_failed_final_keeps_flow_and_retries(self):
        self.prepare()
        self.b.close_flow_card = Mock()
        self.b.send_answer.side_effect = [False, True]
        self.assertFalse(self.b.process_line(self.completion()))
        self.b.close_flow_card.assert_not_called()
        self.assertEqual(self.b.bridge_state['coord_ring'], [])
        self.assertTrue(self.b.process_line(self.completion()))
        self.b.close_flow_card.assert_called_once_with('sent')

    def test_metadata_context_does_not_create_user_turn(self):
        record = user()
        record['payload']['content'][0]['text'] = 'injected rules'
        record['payload']['internal_chat_message_metadata_passthrough'] = {'content_item_kinds':['context.environment']}
        self.assertIsNone(m.extract_event(record))
        record['payload']['internal_chat_message_metadata_passthrough']['content_item_kinds'] = ['user.text']
        self.assertEqual(m.extract_event(record), ('user','injected rules'))

    def test_capture_failure_does_not_declare_interrupt(self):
        self.prepare()
        owner = self.b.repl_typing_stop = Mock()
        self.b.close_flow_card = Mock()
        self.b.abort_typing_on_interrupt(owner, 'capture_error')
        self.b.close_flow_card.assert_not_called()
        self.b.telegram.send.assert_not_called()
        self.assertIsNone(self.b.pending_interrupt_notice)

class FloodIntegration(unittest.TestCase):
    def test_full_server_cooldown_and_retry_receipt(self):
        clock = [100.0]
        waits = []
        def sleep(seconds):
            waits.append(seconds)
            clock[0] += seconds
        def response():
            return io.BytesIO(json.dumps({'ok':True,'result':{'message_id':7}}).encode())
        error = urllib.error.HTTPError('https://example.invalid',429,'rate limit',{},io.BytesIO(b'{"parameters":{"retry_after":125}}'))
        with patch.object(m,'assert_codex_egress_chat',return_value=(False,'')), patch.object(m,'mesh_cutover_call',return_value=None), patch.object(m,'mesh_ledger_record'), patch.object(m.time,'monotonic',side_effect=lambda:clock[0]), patch.object(m.time,'sleep',side_effect=sleep), patch.object(m.urllib.request,'urlopen',side_effect=[error,response()]) as request:
            client = m.TelegramClient('test','123','',3500)
            result = client.call('sendMessage',chat_id='123',text='answer')
        self.assertEqual(result['result']['message_id'],7)
        self.assertEqual(request.call_count,2)
        self.assertGreaterEqual(sum(waits),126)
        self.assertLessEqual(max(waits),m.TELEGRAM_FLOOD_WAIT_CAP_SECONDS)

if __name__ == '__main__': unittest.main()


class SuggestedReplyDefaults(unittest.TestCase):
    def test_disabled_hides_plain_classified_and_marker_only_suggestions(self):
        for attrs in ('', ' class="auto-ok"', ' class="hold"'):
            marker = '<추천답변' + attrs + '>next action</추천답변>'
            for surface in ('aniki_dm', 'node'):
                self.assertEqual(m.suggested_reply_messages('answer\n' + marker, False, surface), ['answer'])
                self.assertEqual(m.suggested_reply_messages(marker, False, surface), [''])
        self.assertEqual(m.suggested_reply_messages('ordinary answer', False, 'aniki_dm'), ['ordinary answer'])

    def test_explicit_opt_in_still_displays_suggestion(self):
        self.assertEqual(m.suggested_reply_messages('answer\n<추천답변>next</추천답변>', True, 'aniki_dm'), ['answer', 'next'])

    def test_default_disables_new_and_existing_confirmation_buttons(self):
        b = object.__new__(m.Bridge)
        b.config = SimpleNamespace(chat_id='123', bridge_kill=False)
        b.telegram = Mock()
        b.telegram.send_copy_content.return_value = [45]
        b.repl = Mock()
        with patch.dict(m.os.environ, {}, clear=True):
            b.send_suggested_confirm('next')
            b.telegram.call.assert_not_called()
            b.handle_suggested_confirm({'id':'cb', 'data':m.SUGGESTED_CONFIRM_PREFIX+'old', 'from':{'id':123}, 'message':{'chat':{'id':123}}})
        b.repl.paste_prompt.assert_not_called()
        b.repl._paste_prompt_unlocked.assert_not_called()
        self.assertIn('꺼져', b.telegram.call.call_args.kwargs['text'])
