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

PREFIX = 'crb_asyncq'
TTL = 3600


class QuestionNotSubmitted(RuntimeError):
    """Preflight rejected the answer before any answer key or text was sent."""


class AsyncQuestions:
    def __init__(self, path, telegram, submit, clock=time.time, recover=None):
        self.path = Path(path)
        self.telegram = telegram
        self.submit = submit
        self.recover = recover
        self.clock = clock
        self.lock = threading.RLock()
        if self.path.exists():
            self.items = json.loads(self.path.read_text(encoding='utf-8'))
            if not isinstance(self.items, dict):
                raise ValueError('Invalid question state')
        else:
            self.items = {}

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

    def refresh_buttons(self, key, item):
        if item['status'] == 'open':
            buttons = [[{'text': f'{i+1}. {label.strip()}'[:64],
                         'callback_data': f'{PREFIX}:{key}:{i}'}]
                       for i, label in enumerate(item['options'])]
        elif item['status'] == 'uncertain':
            buttons = [[{'text': '터미널 질문 확인 · 선택지 복구',
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
        if confirmed is True:
            item['status'] = 'answered'
        elif isinstance(error, QuestionNotSubmitted):
            item['status'] = 'open'
            item['failure_stage'] = 'before_input'
        else:
            item['status'] = 'uncertain'
            item['failure_stage'] = 'delivery_unconfirmed'
        # Only fixed classifications are persisted: exception text can contain input.
        self.save()
        self.refresh_buttons(key, item)
        if item['status'] == 'open':
            self.telegram.send('터미널의 해당 질문을 확인하지 못해 답변을 입력하지 않았습니다. 질문 화면을 연 뒤 다시 선택하거나 답장해 주세요.')
        elif item['status'] == 'uncertain':
            self.telegram.send('선택 답변의 전달 여부를 확인하지 못했습니다. 자동 재전송하지 않습니다. 아래 질문 확인 버튼으로 선택지를 복구할 수 있습니다.')

    def observe(self, record, session):
        if not session or record.get('type') != 'response_item':
            return
        p = record.get('payload') or {}
        if not isinstance(p, dict):
            return
        with self.lock:
            now = self.clock()
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
                    buttons = [[{'text': f'{i+1}. {label.strip()}'[:64],
                                 'callback_data': f'{PREFIX}:{key}:{i}'}]
                               for i, label in enumerate(item['options'])]
                    labels = '\n'.join(f'{i+1}. {o}' for i, o in enumerate(item['options']))
                    text = ('확인이 필요한 질문\n\n' + item['title'] + '\n\n' + labels)[:3600]
                    text += ('\n\n버튼으로 선택해 주세요. 직접 입력은 표시된 선택지와 같은 답만 지원합니다.' if buttons else '\n\n이 질문 메시지에 Telegram 답장으로 답변해 주세요.')
                    text += ' 질문은 1시간 동안 유효합니다.'
                    try:
                        result = self.telegram.call('sendMessage', chat_id=self.telegram.chat_id,
                                                    text=text, reply_markup=json.dumps({'inline_keyboard': buttons}, ensure_ascii=False))
                        mid = ((result or {}).get('result') or {}).get('message_id')
                    except Exception:
                        mid = None
                    item['message_id'] = mid
                    item['status'] = 'open' if isinstance(mid, int) else 'uncertain'
                    self.save()

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
            if len(parts) != 3 or not (parts[2].isdigit() or parts[2] == 'recover'):
                ack('유효하지 않은 선택입니다.')
                return True
            item = self.items.get(parts[1])
            if not item or item.get('message_id') != message.get('message_id') or item['session'] != session:
                ack('현재 대화의 질문이 아닙니다.')
                return True
            if self.clock() - item['created'] >= TTL:
                item['status'] = 'expired'
                self.save()
                self.clear_buttons(item)
                ack('만료된 질문입니다. 메시지로 다시 알려주세요.')
                return True
            if parts[2] == 'recover':
                if item['status'] != 'uncertain' or not callable(self.recover):
                    ack('복구할 수 없는 질문입니다.')
                    return True
                try:
                    ready = self.recover(item) is True
                except Exception:
                    ready = False
                if ready:
                    item['status'] = 'open'
                    self.save()
                    self.refresh_buttons(parts[1], item)
                    ack('같은 질문을 확인했습니다. 답변을 다시 선택하거나 답장해 주세요.')
                else:
                    ack('터미널에서 같은 질문을 확인하지 못했습니다. 답변은 전송하지 않았습니다.')
                return True
            index = int(parts[2])
            if item['status'] != 'open' or index >= len(item['options']):
                ack('이미 선택했거나 더 이상 사용할 수 없는 질문입니다.')
                return True
            text = '> ' + item['title'].replace('\n', '\n> ') + '\n\n' + item['options'][index]
            item.update(status='submitting', selected=index)
            self.save()  # Crash or ambiguous input failure must never repeat submission.
            ack('선택한 답변을 Codex에 전달합니다.')
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
                if item.get('message_id') == message_id and item['status'] in {'submitting', 'uncertain'}:
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
                     if item.get('message_id') == reply['message_id']]
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
            self.save()
            self.clear_buttons(item)
            return '> ' + item['title'].replace('\n', '\n> ') + '\n\n' + answer


def freeform_editor(screen, title):
    """Read only the observed queued-input editor, never a main composer.

    The title and answer are separated by a blank row. Missing/truncated or
    multiple panels are ambiguous. Preserve answer contents, including newlines.
    """
    lines = screen.splitlines()
    heads = [i for i, line in enumerate(lines)
             if re.fullmatch(r'\s*[•·]?\s*Queued follow-up inputs\s*', line)]
    feet = [i for i, line in enumerate(lines) if re.fullmatch(
        r'\s*enter submit\s+ctrl \+ \] skip\s+[⌥⎇] \+ ↓ main prompt\s*', line, re.I)]
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
