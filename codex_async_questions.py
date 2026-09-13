"""Mirror question events and track verified native question submission."""
import hashlib
import json
import os
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path

PREFIX = 'crb_asyncq'
TTL = 3600


class AsyncQuestions:
    def __init__(self, path, telegram, submit, clock=time.time):
        self.path = Path(path)
        self.telegram = telegram
        self.submit = submit
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
                    text += '\n\n버튼으로 선택해 주세요. 직접 입력 답변은 현재 터미널에서 제출해 주세요. 선택지는 1시간 동안 유효합니다.'
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
                self.telegram.call('answerCallbackQuery', callback_query_id=callback['id'], text=text)
        with self.lock:
            parts = data.split(':')
            if len(parts) != 3 or not parts[2].isdigit():
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
            index = int(parts[2])
            if item['status'] != 'open' or index >= len(item['options']):
                ack('이미 선택했거나 더 이상 사용할 수 없는 질문입니다.')
                return True
            text = '> ' + item['title'].replace('\n', '\n> ') + '\n\n' + item['options'][index]
            item.update(status='submitting', selected=index)
            self.save()  # Crash or ambiguous input failure must never repeat submission.
            ack('선택한 답변을 Codex에 전달합니다.')
            try:
                confirmed = self.submit(text, item['message_id'])
                item['status'] = 'answered' if confirmed is True else 'uncertain'
            except Exception:
                item['status'] = 'uncertain'
                self.telegram.send('선택 답변의 전달 여부를 확인하지 못했습니다. 중복 전송은 하지 않습니다.')
            finally:
                self.save()
                self.clear_buttons(item)
            return True

    def owns_title(self, title, session):
        normalized = ''.join(title.split()).casefold()
        with self.lock:
            return any(item['session'] == session and
                       ''.join(item['title'].split()).casefold() == normalized and
                       self.clock() - item['created'] < TTL
                       for item in self.items.values())

    def reply_text(self, message, session):
        """Attach full question context to a free-text reply to the question card."""
        reply = message.get('reply_to_message') or {}
        with self.lock:
            for item in self.items.values():
                if item['session'] == session and item.get('message_id') == reply.get('message_id') and item['status'] == 'open':
                    if self.clock() - item['created'] < TTL and isinstance(message.get('text'), str):
                        item['status'] = 'submitting'
                        self.save()
                        self.clear_buttons(item)
                        return '> ' + item['title'].replace('\n', '\n> ') + '\n\n' + message['text']
        return None
