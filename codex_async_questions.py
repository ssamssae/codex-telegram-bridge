"""Mirror question events and track verified native question submission."""
import hashlib
import json
import os
import re
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path

# ─ Codex question panel vocabulary (T-261015-010) ─────────────────────────────
# One place for the native wording of Codex's queued-input / Other / Type-your-
# answer panels. Before 2026-10-15 the header and footer were spelled separately
# here and in codex-repl-telegram-bridge.py and had already drifted: the bridge
# accepted the ● bullet and the Linux "alt" key, this module accepted the · bullet
# and only ⌥/⎇. Both readers now share the union below.
# scripts/tests/test_codex_bridge_question_panel_vocabulary.py fails when a new
# inline copy appears outside this block in either file.
QUEUED_INPUTS_TITLE = 'Queued follow-up inputs'
QUEUED_INPUTS_LEGACY_TITLE = 'Messages to be submitted'
# Header row; the bullet varies by version/terminal (• · ●) or is absent.
QUEUED_INPUTS_HEADER_RE = re.compile(r'\s*(?:[•·●]\s*)?Queued follow-up inputs\s*')
# Footer row; the modifier renders as alt (Linux) or ⌥/⎇ (mac).
QUEUED_INPUTS_FOOTER_RE = re.compile(
    r'\s*enter submit\s+ctrl \+ \] skip\s+(?:alt|⌥|⎇) \+ ↓ main prompt\s*', re.IGNORECASE)
EDITOR_SUBMIT_HINT_RE = re.compile(r'\benter\s+submit\b', re.IGNORECASE)
EDITOR_SUBMIT_SKIP_HINT_RE = re.compile(r'\benter\s+submit\b.*\bskip\b', re.IGNORECASE)
# Placeholder of the empty Type-your-answer editor (English and Korean builds).
EMPTY_ANSWER_HINT_RE = re.compile(r'type (?:your |an? )?answer|type something|답변을 입력', re.IGNORECASE)
OTHER_LABEL = 'Other'
OTHER_LABELS = frozenset({'other', '기타', '직접 입력'})


def is_other_label(label) -> bool:
    """True for the native free-text option label in any observed language."""
    return (label or '').strip().casefold() in OTHER_LABELS
# ─ end Codex question panel vocabulary ────────────────────────────────────────

PREFIX = 'crb_asyncq'
TTL = 3600
FAILURE_REASONS = {
    'unknown': '터미널 질문 상태를 확인하지 못했습니다.',
    'question_ambiguous': '같은 질문을 하나로 확인하지 못했습니다.',
    'session_changed': '질문의 대화가 현재 대화와 다릅니다.',
    'approval_open': '다른 실행 승인 화면이 열려 있습니다.',
    'question_hidden': '답변할 질문 화면을 찾지 못했습니다.',
    'input_conflict': '터미널에 작성 중인 다른 답변이 있습니다.',
    'question_mismatch': '화면의 질문이 카드의 질문과 다릅니다.',
    'options_mismatch': '화면의 선택지가 카드의 선택지와 다릅니다.',
    'selection_unknown': '현재 선택 위치를 확인하지 못했습니다.',
    'invalid_answer': '이 질문에 제출할 수 없는 답변입니다.',
}
# Codex 0.162 drops every pending async question when the live turn ends or a
# new prompt is submitted (tui bottom_pane/async_questions/state.rs
# take_pending_drafts -> clear_pending). Such a card can never be submitted.
CLOSE_REASONS = {
    'turn_ended': 'Codex 작업이 끝나 이 질문은 닫혔습니다.',
    'new_prompt': '새 메시지가 들어와 Codex가 이 질문을 닫았습니다.',
}
# Injected user-role rows that the user did not submit as a prompt.
CONTEXT_PREFIXES = ('<environment_context>', '# AGENTS.md instructions', '<turn_aborted>',
                    '<user_instructions>', '<hook_prompt', '<send_user_message_question_reply>')


def record_time(record):
    try:
        return datetime.fromisoformat(record['timestamp'].replace('Z', '+00:00')).timestamp()
    except (ValueError, KeyError, TypeError, AttributeError):
        return None


def submitted_prompt(payload):
    content = payload.get('content')
    if not isinstance(content, list):
        return False
    text = ''.join(part.get('text', '') for part in content
                   if isinstance(part, dict) and part.get('type') == 'input_text'
                   and isinstance(part.get('text'), str))
    if text.strip():
        return not text.lstrip().startswith(CONTEXT_PREFIXES)
    return any(isinstance(part, dict) and part.get('type') == 'input_image' for part in content)


class QuestionNotSubmitted(RuntimeError):
    """Preflight rejected the answer before any answer key or text was sent."""

    def __init__(self, message='', *, reason='unknown'):
        super().__init__(message)
        self.reason = reason if reason in FAILURE_REASONS else 'unknown'


class UnsupportedChoiceAnswer(QuestionNotSubmitted):
    """A reply to a choice card does not match any supported option."""


class AsyncQuestions:
    def __init__(self, path, telegram, submit, clock=time.time, recover=None, translate=None):
        self.path = Path(path)
        self.telegram = telegram
        self.submit = submit
        self.recover = recover
        self.translate = translate
        self.clock = clock
        self.lock = threading.RLock()
        if self.path.exists():
            self.items = json.loads(self.path.read_text(encoding='utf-8'))
            if not isinstance(self.items, dict):
                raise ValueError('Invalid question state')
        else:
            self.items = {}

    def tr(self, source, **values):
        if self.translate is not None:
            return self.translate(source, **values)
        return source.format(**values) if values else source

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=self.path.name, dir=self.path.parent)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as out:
                json.dump(self.items, out, ensure_ascii=False)
                out.flush()
                os.fsync(out.fileno())
            os.replace(name, self.path)
            # Windows cannot open directories through os.open; file fsync and
            # atomic replacement still apply. POSIX also persists the rename.
            if os.name != 'nt':
                directory = os.open(self.path.parent, os.O_RDONLY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def clear_buttons(self, item):
        if item.get('message_id'):
            try:
                self.telegram.call('editMessageReplyMarkup', chat_id=self.telegram.chat_id,
                                   message_id=item['message_id'],
                                   reply_markup=json.dumps({'inline_keyboard': []}))
            except Exception:
                pass  # Durable state, not a stale Telegram keyboard, controls admission.

    @staticmethod
    def owns_message(item, message_id):
        return (item.get('message_id') == message_id
                or message_id in item.get('input_message_ids', []))

    def choice_buttons(self, key, item):
        buttons = [[{'text': f'{i+1}. {label.strip()}'[:64],
                     'callback_data': f'{PREFIX}:{key}:{i}'}]
                   for i, label in enumerate(item['options'])]
        if item['options']:
            buttons.append([{'text': f'{len(item["options"])+1}. {OTHER_LABEL}',
                             'callback_data': f'{PREFIX}:{key}:other'}])
        if item.get('input_message_id'):
            buttons.append([{'text': self.tr('직접 입력 취소'),
                             'callback_data': f'{PREFIX}:{key}:cancel'}])
        return buttons

    def card_text(self, item):
        answered = item['status'] == 'answered'
        closed = item['status'] == 'closed'
        labels = []
        for i, option in enumerate(item['options']):
            mark = '✅ ' if answered and item.get('selected') == i else ''
            labels.append(f'{mark}{i+1}. {option}')
        if item['options']:
            mark = '✅ ' if answered and item.get('answer_mode') == 'other' else ''
            labels.append(f'{mark}{len(item["options"])+1}. {OTHER_LABEL}')
        heading = (self.tr('✅ 응답 완료\n\n') if answered else
                   self.tr('닫힌 질문\n\n') if closed else self.tr('확인이 필요한 질문\n\n'))
        text = (heading + item['title'] + '\n\n' + '\n'.join(labels))[:3600]
        if answered:
            return text + self.tr('\n\n답변을 Codex에 전달했습니다.')
        if closed:
            reason = CLOSE_REASONS.get(item.get('closed_reason'), CLOSE_REASONS['turn_ended'])
            return text + '\n\n' + self.tr(reason) + self.tr(' 답은 새 메시지로 보내 주세요.')
        text += (self.tr('\n\n선택지를 누르거나 Other를 눌러 답변을 직접 입력해 주세요. 이 카드에 답장으로 입력해도 됩니다.')
                 if item['options'] else self.tr('\n\n이 질문 메시지에 Telegram 답장으로 답변해 주세요.'))
        return text + self.tr(' 질문은 1시간 동안 유효합니다.')

    def complete_card(self, item):
        if item['status'] != 'answered' or not item.get('message_id') or item.get('completion_displayed'):
            return
        try:
            result = self.telegram.call('editMessageText', chat_id=self.telegram.chat_id,
                message_id=item['message_id'], text=self.card_text(item),
                reply_markup=json.dumps({'inline_keyboard': []}))
        except Exception:
            result = None
        if isinstance(result, dict) and result.get('ok') is True:
            item['completion_displayed'] = True
            self.save()
        else:
            self.clear_buttons(item)  # UI failure must never reopen or resend an answer.

    def display_closed(self, item):
        if item['status'] != 'closed' or not item.get('message_id') or item.get('closed_displayed'):
            return
        try:
            result = self.telegram.call('editMessageText', chat_id=self.telegram.chat_id,
                message_id=item['message_id'], text=self.card_text(item),
                reply_markup=json.dumps({'inline_keyboard': []}))
        except Exception:
            result = None
        if isinstance(result, dict) and result.get('ok') is True:
            item['closed_displayed'] = True
            self.save()
        else:
            self.clear_buttons(item)  # A dead native question must never look answerable.

    def close_open_cards(self, session, at, reason):
        """Close cards whose native question Codex already dropped; never submit."""
        closed = []
        for item in self.items.values():
            if item['session'] == session and item['status'] == 'open' and item['created'] < at:
                item.update(status='closed', closed_reason=reason)
                item.pop('input_message_id', None)
                closed.append(item)
        if closed:
            self.save()
            for item in closed:
                self.display_closed(item)

    def refresh_buttons(self, key, item):
        if item['status'] == 'answered':
            self.complete_card(item)
            return
        if item['status'] == 'open':
            buttons = self.choice_buttons(key, item)
        elif item['status'] == 'uncertain':
            buttons = [[{'text': self.tr('터미널 질문 확인 · 선택지 복구'),
                         'callback_data': f'{PREFIX}:{key}:recover'}]]
        else:
            buttons = []
        try:
            self.telegram.call('editMessageReplyMarkup', chat_id=self.telegram.chat_id,
                               message_id=item['message_id'],
                               reply_markup=json.dumps({'inline_keyboard': buttons}, ensure_ascii=False))
        except Exception:
            pass  # Persisted state remains authoritative; never resend an answer.

    def finish_submission(self, key, item, confirmed, error=None):
        if confirmed is True or item['status'] == 'answered':
            item['status'] = 'answered'
            item.pop('input_message_id', None)
            item.pop('failure_stage', None)
            item.pop('failure_reason', None)
        elif isinstance(error, QuestionNotSubmitted):
            item['status'] = 'open'
            item['failure_stage'] = 'before_input'
            item['failure_reason'] = error.reason
        else:
            item['status'] = 'uncertain'
            item['failure_stage'] = 'delivery_unconfirmed'
        # Only fixed classifications are persisted: exception text can contain input.
        self.save()
        self.refresh_buttons(key, item)
        if item['status'] == 'open':
            if isinstance(error, UnsupportedChoiceAnswer):
                self.telegram.send(self.tr('이 질문 카드는 표시된 선택지만 답변할 수 있습니다. 이번 카드 답변은 입력하지 않았습니다. 버튼을 선택하거나, 추가 의견은 카드에 답장하지 않고 새 메시지로 보내주세요.'))
            else:
                notice = self.tr('이 질문 카드의 답변을 제출할 수 있는 터미널 상태를 확인하지 못했습니다. 이번 카드 답변은 입력하지 않았습니다. 질문 화면과 기존 입력을 확인한 뒤 다시 선택하거나 답장해 주세요.')
                if item.get('failure_reason', 'unknown') != 'unknown':
                    notice += '\n\n' + self.tr(FAILURE_REASONS[item['failure_reason']])
                self.telegram.send(notice)
        elif item['status'] == 'uncertain':
            self.telegram.send(self.tr('선택 답변의 전달 여부를 확인하지 못했습니다. 자동 재전송하지 않습니다. 아래 질문 확인 버튼으로 선택지를 복구할 수 있습니다.'))

    def observe(self, record, session):
        if not session or record.get('type') not in {'response_item', 'event_msg'}:
            return
        p = record.get('payload') or {}
        if not isinstance(p, dict):
            return
        with self.lock:
            now = self.clock()
            if record['type'] == 'event_msg':
                ended = record_time(record)
                if p.get('type') in {'task_complete', 'turn_aborted'} and ended is not None:
                    self.close_open_cards(session, ended, 'turn_ended')
                return
            if p.get('type') == 'message' and p.get('role') == 'user':
                self.observe_native_reply(record, session)
                received = record_time(record)
                if submitted_prompt(p) and received is not None:
                    self.close_open_cards(session, received, 'new_prompt')
                return
            if p.get('type') == 'function_call' and str(p.get('name') or '').split('.')[-1] == 'request_user_input_async':
                try:
                    args = json.loads(p['arguments'])
                    created = datetime.fromisoformat(record['timestamp'].replace('Z', '+00:00')).timestamp()
                except (ValueError, KeyError, TypeError):
                    return
                if not isinstance(args, dict) or not p.get('call_id') or not now - TTL < created <= now + 60:
                    return
                questions = args.get('questions')
                if not isinstance(questions, list):
                    return
                for i, q in enumerate(questions[:10]):
                    if not isinstance(q, dict) or not isinstance(q.get('title'), str) or not q['title'].strip():
                        continue
                    options = q.get('options', [])
                    if not isinstance(options, list) or len(options) > 50 or any(not isinstance(o, str) or not o.strip() for o in options):
                        continue
                    key = hashlib.sha256(f'{session}:{p["call_id"]}:{i}'.encode()).hexdigest()[:24]
                    if key not in self.items:
                        self.items[key] = dict(session=session, call_id=p['call_id'], title=q['title'],
                                               options=options, created=created, status='awaiting_accept')
                self.save()
            elif p.get('type') == 'function_call_output':
                try:
                    output = json.loads(p.get('output', ''))
                except (TypeError, ValueError):
                    return
                if not isinstance(output, dict):
                    return
                for key, item in self.items.items():
                    if item['session'] != session or item['call_id'] != p.get('call_id') or item['status'] != 'awaiting_accept':
                        continue
                    if output.get('accepted') is not True or now - item['created'] >= TTL:
                        item['status'] = 'expired'
                        self.save()
                        continue
                    # A crash after send might have delivered; never blindly replay.
                    item['status'] = 'sending'
                    self.save()
                    buttons = self.choice_buttons(key, item)
                    text = self.card_text(item)
                    try:
                        result = self.telegram.call('sendMessage', chat_id=self.telegram.chat_id,
                                                    text=text, reply_markup=json.dumps({'inline_keyboard': buttons}, ensure_ascii=False))
                        mid = ((result or {}).get('result') or {}).get('message_id')
                    except Exception:
                        mid = None
                    item['message_id'] = mid
                    item['status'] = 'open' if type(mid) is int and mid > 0 else 'uncertain'
                    self.save()

    def observe_native_reply(self, record, session):
        """Consume the native question receipt by call/index, never a quoted reply."""
        p = record['payload']
        content = p.get('content')
        metadata = p.get('internal_chat_message_metadata_passthrough')
        if (not isinstance(content, list) or len(content) != 1
                or not isinstance(content[0], dict) or content[0].get('type') != 'input_text'
                or not isinstance(metadata, dict) or not metadata.get('turn_id')):
            return
        text = content[0].get('text')
        if not isinstance(text, str):
            return
        match = re.fullmatch(r'<send_user_message_question_reply>\s*(\[.*\])\s*</send_user_message_question_reply>', text, re.DOTALL)
        if not match:
            return
        try:
            replies = json.loads(match[1])
            received = datetime.fromisoformat(record['timestamp'].replace('Z', '+00:00')).timestamp()
        except (ValueError, KeyError, TypeError):
            return
        if not isinstance(replies, list) or len(replies) > 10:
            return
        for reply in replies:
            if not isinstance(reply, dict):
                continue
            try:
                identity = json.loads(reply.get('questionItemId', ''))
            except (TypeError, ValueError):
                continue
            if (not isinstance(identity, list) or len(identity) != 3
                    or identity[0] != 'request_user_input_async'
                    or not isinstance(identity[1], str)
                    or type(identity[2]) is not int or not 0 <= identity[2] < 10):
                continue
            key = hashlib.sha256(f'{session}:{identity[1]}:{identity[2]}'.encode()).hexdigest()[:24]
            item = self.items.get(key)
            if (not item or item['session'] != session or item['call_id'] != identity[1]
                    or item['status'] not in {'open', 'submitting', 'uncertain', 'answered'}
                    or item.get('completion_displayed')
                    or received < item['created'] or reply.get('question') != item['title']
                    or not isinstance(reply.get('answer'), str) or not reply['answer'].strip()
                    or any(ord(c) < 32 and c != '\n' for c in reply['answer'])
                    or '\x7f' in reply['answer']):
                continue
            item.update(status='answered', answered_at=received, answered_via='native_receipt')
            if reply['answer'] in item['options']:
                item['selected'] = item['options'].index(reply['answer'])
                item.pop('answer_mode', None)
            else:
                item.pop('selected', None)
                item['answer_mode'] = 'other'
            item.pop('input_message_id', None)
            item.pop('failure_stage', None)
            item.pop('failure_reason', None)
            self.save()
            self.complete_card(item)

    def callback(self, callback, session):
        data = callback.get('data', '')
        if not isinstance(data, str) or not data.startswith(PREFIX + ':'):
            return False
        message = callback.get('message') if isinstance(callback.get('message'), dict) else {}
        chat = message.get('chat') if isinstance(message.get('chat'), dict) else {}
        sender = callback.get('from') if isinstance(callback.get('from'), dict) else {}
        # Personal bridge: only the configured user in the configured private chat.
        if str(chat.get('id')) != str(self.telegram.chat_id) or str(sender.get('id')) != str(self.telegram.chat_id):
            return True
        def ack(text):
            if callback.get('id'):
                try:
                    self.telegram.call('answerCallbackQuery', callback_query_id=callback['id'], text=text)
                except Exception:
                    pass  # An expired callback notification must not strand submitting state.
        with self.lock:
            parts = data.split(':')
            if len(parts) != 3 or not (parts[2].isdigit() or parts[2] in {'recover', 'other', 'cancel'}):
                ack(self.tr('유효하지 않은 선택입니다.'))
                return True
            item = self.items.get(parts[1])
            if not item or item.get('message_id') != message.get('message_id') or item['session'] != session:
                ack(self.tr('현재 대화의 질문이 아닙니다.'))
                return True
            if item['status'] == 'closed':
                self.display_closed(item)
                ack(self.tr('닫힌 질문입니다. 답은 새 메시지로 보내 주세요.'))
                return True
            if self.clock() - item['created'] >= TTL:
                item['status'] = 'expired'
                self.save()
                self.clear_buttons(item)
                ack(self.tr('만료된 질문입니다. 메시지로 다시 알려주세요.'))
                return True
            if parts[2] == 'recover':
                if item['status'] != 'uncertain' or not callable(self.recover):
                    ack(self.tr('복구할 수 없는 질문입니다.'))
                    return True
                try:
                    ready = self.recover(item) is True
                except Exception:
                    ready = False
                if ready:
                    item['status'] = 'open'
                    self.save()
                    self.refresh_buttons(parts[1], item)
                    ack(self.tr('같은 질문을 확인했습니다. 답변을 다시 선택하거나 답장해 주세요.'))
                else:
                    ack(self.tr('터미널에서 같은 질문을 확인하지 못했습니다. 답변은 전송하지 않았습니다.'))
                return True
            if item['status'] != 'open':
                if item['status'] == 'answered':
                    self.complete_card(item)
                ack(self.tr('이미 선택했거나 더 이상 사용할 수 없는 질문입니다.'))
                return True
            if parts[2] == 'cancel':
                item.pop('input_message_id', None)
                self.save()
                self.refresh_buttons(parts[1], item)
                ack(self.tr('직접 입력을 취소했습니다. 다른 선택지를 고를 수 있습니다.'))
                return True
            if parts[2] == 'other':
                if not item['options']:
                    ack(self.tr('유효하지 않은 선택입니다.'))
                    return True
                if item.get('input_message_id'):
                    ack(self.tr('답변 입력 요청 메시지에 답장해 주세요.'))
                    return True
                ack(self.tr('직접 입력할 답변을 요청합니다.'))
                try:
                    result = self.telegram.call('sendMessage', chat_id=self.telegram.chat_id,
                        text=self.tr('직접 입력 · Other\n\n{title}\n\n이 메시지에 답장으로 내용을 보내주세요. 입력을 그만두려면 원래 카드에서 직접 입력 취소를 누르세요.', title=item['title'][:3000]),
                        reply_markup=json.dumps({'force_reply': True, 'selective': True}))
                    mid = ((result or {}).get('result') or {}).get('message_id')
                except Exception:
                    mid = None
                if type(mid) is int and mid > 0 and mid != item.get('message_id'):
                    item.setdefault('input_message_ids', []).append(mid)
                    item['input_message_id'] = mid
                    self.save()
                    self.refresh_buttons(parts[1], item)
                else:
                    self.telegram.send(self.tr('답변 입력 요청을 확인하지 못했습니다. 원래 질문 카드에 답장으로 내용을 보내주세요.'))
                return True
            index = int(parts[2])
            if index >= len(item['options']):
                ack(self.tr('이미 선택했거나 더 이상 사용할 수 없는 질문입니다.'))
                return True
            text = '> ' + item['title'].replace('\n', '\n> ') + '\n\n' + item['options'][index]
            item.update(status='submitting', selected=index)
            item.pop('input_message_id', None)
            item.pop('answer_mode', None)
            self.save()  # Crash or ambiguous input failure must never repeat submission.
            ack(self.tr('선택한 답변을 Codex에 전달합니다.'))
            confirmed, error = False, None
            try:
                confirmed = self.submit(text, item['message_id'])
            except Exception as exc:
                error = exc
            finally:
                self.finish_submission(parts[1], item, confirmed, error)
            return True

    def owns_title(self, title, session):
        normalized = ''.join(title.split()).casefold()
        with self.lock:
            return any(item['session'] == session and
                       ''.join(item['title'].split()).casefold() == normalized and
                       self.clock() - item['created'] < TTL
                       for item in self.items.values())

    def finish_reply(self, message_id, confirmed, error=None):
        with self.lock:
            for key, item in self.items.items():
                if self.owns_message(item, message_id) and item['status'] in {'submitting', 'uncertain', 'answered'}:
                    self.finish_submission(key, item, confirmed, error)

    def reply_text(self, message, session):
        """None means unrelated; empty text means owned but inadmissible.

        Retain ownership even after expiry/restart so stale replies never become
        ordinary prompts. Only an explicit reply to this card can claim it.
        """
        reply = message.get('reply_to_message') or {}
        if not isinstance(reply, dict) or not isinstance(reply.get('message_id'), int):
            return None
        with self.lock:
            owned = [item for item in self.items.values()
                     if self.owns_message(item, reply['message_id'])]
            if not owned:
                return None
            sender = message.get('from') or {}
            chat = message.get('chat') or {}
            if (len(owned) != 1 or not isinstance(sender, dict) or not isinstance(chat, dict)
                    or str(sender.get('id')) != str(self.telegram.chat_id)
                    or str(chat.get('id')) != str(self.telegram.chat_id)):
                return ''
            item = owned[0]
            if item['session'] != session or item['status'] != 'open':
                return ''
            if (reply['message_id'] != item.get('message_id')
                    and reply['message_id'] != item.get('input_message_id')):
                return ''  # Cancelled/replaced input requests remain owned, never normal prompts.
            if self.clock() - item['created'] >= TTL:
                item['status'] = 'expired'
                self.save()
                self.clear_buttons(item)
                return ''
            answer = message.get('text')
            if (not isinstance(answer, str) or not answer.strip()
                    or any(ord(c) < 32 and c != '\n' for c in answer) or '\x7f' in answer):
                return ''
            item['status'] = 'submitting'
            if reply['message_id'] == item.get('input_message_id'):
                item['answer_mode'] = 'other'
            else:
                item.pop('answer_mode', None)
            self.save()
            self.clear_buttons(item)
            return '> ' + item['title'].replace('\n', '\n> ') + '\n\n' + answer


def freeform_editor(screen, title):
    """Read only the observed queued-input editor, never a main composer.

    The title and answer are separated by a blank row. Missing/truncated or
    multiple panels are ambiguous. Preserve answer contents, including newlines.
    """
    lines = screen.splitlines()
    heads = [i for i, line in enumerate(lines) if QUEUED_INPUTS_HEADER_RE.fullmatch(line)]
    feet = [i for i, line in enumerate(lines) if QUEUED_INPUTS_FOOTER_RE.fullmatch(line)]
    if len(heads) != 1 or len(feet) != 1 or feet[0] <= heads[0]:
        return None
    body = lines[heads[0] + 1:feet[0]]
    while body and not body[0].strip():
        body.pop(0)
    if not body:
        return None
    # Infer layout padding only from two independent fixed panel elements.
    # Never infer it from the answer: extra spaces there belong to the user.
    title_padding = re.match(r' *', body[0]).group()
    footer_padding = re.match(r' *', lines[feet[0]]).group()
    if title_padding != footer_padding:
        return None
    if any(line and not line.startswith(title_padding) for line in body):
        return None
    body = [line[len(title_padding):] if line else '' for line in body]
    # A title may contain blank paragraphs and may wrap on the terminal.
    # Consume its entire normalized text; an internal blank is not the editor.
    normalized_title = ''.join(title.split())
    boundaries = [i for i in range(1, len(body))
                  if body[i] == '' and body[i - 1].strip()
                  and ''.join(''.join(body[:i]).split()) == normalized_title]
    if len(boundaries) != 1 or not body or body[-1] != '':
        return None
    # The observed layout has exactly one blank spacer before the footer.
    # Remove only that row, never strip answer text or arbitrary blank rows.
    editor = body[boundaries[0] + 1:-1]
    if not editor:
        return None
    if editor[0] == '' and any(editor[1:]):
        # A leading answer newline is indistinguishable from extra layout rows.
        return None
    if any(line.startswith(('›', '•')) for line in editor):
        return None
    return '\n'.join(editor)
