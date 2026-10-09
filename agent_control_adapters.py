"""Adapters for existing bridge transports; no new Telegram poller or model process."""
from __future__ import annotations

import atexit
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import sys
import threading
import time

from agent_control_protocol import ControlError, Controller, validate_current, write_json


def fingerprint(*parts):
    return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()[:32]


def history_rows(path, max_bytes=262144):
    """Read complete JSONL records from the same bounded transcript tail."""
    if not path:
        return []
    path = Path(path).expanduser()
    # Session paths come from an authenticated bridge, never from an HTTP caller.
    if not path.is_file() or path.is_symlink():
        return []
    with path.open('rb') as stream:
        size = stream.seek(0, 2)
        stream.seek(max(0, size - max_bytes))
        if size > max_bytes:
            stream.readline()
        lines = stream.read().decode('utf-8', errors='replace').splitlines()
    rows = []
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def native_turn(path, engine):
    """A native turn boundary, never inferred from the displayed card or idle time."""
    for row in reversed(history_rows(path, max_bytes=8 * 1024 * 1024)):
        if engine == 'codex':
            payload = row.get('payload') or {}
            if row.get('type') != 'event_msg' or not isinstance(payload, dict):
                continue
            if payload.get('type') in ('task_complete', 'turn_aborted'):
                return ''
            if payload.get('type') == 'task_started':
                return str(payload.get('turn_id') or '')
        else:
            if row.get('isSidechain') or row.get('isMeta') or row.get('isCompactSummary'):
                continue
            if row.get('type') == 'system' and row.get('subtype') == 'turn_duration':
                return ''
            message = row.get('message') or {}
            if not isinstance(message, dict):
                continue
            content = message.get('content')
            blocks = content if isinstance(content, list) else []
            text = content if isinstance(content, str) else '\n'.join(
                str(b.get('text', '')) for b in blocks
                if isinstance(b, dict) and b.get('type') == 'text')
            if row.get('interruptedMessageId'):
                return ''
            if message.get('role') == 'assistant' and message.get('stop_reason') == 'end_turn' and text.strip():
                return ''
            if message.get('role') == 'user' and text.strip() and not any(
                    isinstance(b, dict) and b.get('type') == 'tool_result' for b in blocks):
                return str(row.get('uuid') or '')
    return ''


def codex_turn_activity(path):
    """Only the bound session's native records can resolve an ambiguous pane."""
    for row in reversed(history_rows(path, max_bytes=8 * 1024 * 1024)):
        payload = row.get('payload')
        if not isinstance(payload, dict):
            continue
        if row.get('type') == 'event_msg':
            if payload.get('type') in ('task_complete', 'turn_aborted'):
                return 'idle'
            if payload.get('type') in ('task_started', 'user_message'):
                return 'busy'
        elif (row.get('type') == 'response_item' and payload.get('type') == 'message'
              and payload.get('role') == 'user'):
            # Input may arrive before task_started; an older completion must
            # not reopen the composer during that handoff.
            return 'busy'
    return None


def claude_prompt_anchor(path, anchor):
    """Sidecar receipts point at attachments; only follow their prompt ancestry."""
    rows = {row.get('uuid'): row for row in history_rows(path, max_bytes=8 * 1024 * 1024)
            if row.get('uuid')}
    for _ in range(64):
        row = rows.get(anchor)
        if not row or row.get('isSidechain') or row.get('isMeta') or row.get('isCompactSummary'):
            return ''
        if row.get('type') == 'attachment':
            anchor = row.get('parentUuid')
            continue
        message = row.get('message') or {}
        if row.get('type') != 'user' or message.get('role') != 'user' or row.get('interruptedMessageId'):
            return ''
        content = message.get('content')
        if isinstance(content, list) and any(isinstance(b, dict) and b.get('type') == 'tool_result'
                                            for b in content):
            return ''
        return str(anchor)
    return ''


def claude_turn_activity(path):
    """Claude may hide its spinner while streaming. Use native turn records too."""
    for row in reversed(history_rows(path)):
        if row.get('isSidechain'):
            continue
        if row.get('type') == 'system' and row.get('subtype') == 'turn_duration':
            return 'idle'
        message = row.get('message')
        if not isinstance(message, dict):
            continue
        content = message.get('content')
        blocks = content if isinstance(content, list) else []
        text = content if isinstance(content, str) else '\n'.join(
            str(block.get('text', '')) for block in blocks
            if isinstance(block, dict) and block.get('type') == 'text')
        if message.get('role') == 'assistant':
            tool_use = any(isinstance(block, dict) and block.get('type') == 'tool_use' for block in blocks)
            # A thinking-only end_turn is emitted before the actual text stream.
            if message.get('stop_reason') == 'end_turn' and text.strip() and not tool_use:
                return 'idle'
            if content:
                return 'busy'
        elif message.get('role') == 'user':
            # Native cancellation retains promptId in Claude 2.3.0. Its
            # interruptedMessageId distinguishes it from a typed marker.
            if (not row.get('promptId') or row.get('interruptedMessageId')) and text.strip() in (
                    '[Request interrupted by user]', '[Request interrupted by user for tool use]'):
                return 'idle'
            if content and not row.get('isMeta') and not row.get('isCompactSummary'):
                return 'busy'
    return None


def transcript(path, limit=40, row_adapter=None):
    """Only visible user/assistant text. Never tools, reasoning, or system messages."""
    result = []
    for row in history_rows(path):
        if row_adapter:
            row = row_adapter(row)
            if not isinstance(row, dict):
                continue
        if row.get('type') == 'response_item':
            row = row.get('payload', {})
            if row.get('type') != 'message':
                continue
        elif isinstance(row.get('message'), dict):
            row = row['message']
        role = row.get('role')
        if role not in ('user', 'assistant') or row.get('channel') in ('analysis', 'justify', 'confidence'):
            continue
        content = row.get('content', '')
        if isinstance(content, list):
            text = '\n'.join(str(block.get('text', '')) for block in content
                             if isinstance(block, dict) and block.get('type') in
                             ('text', 'input_text', 'output_text'))
        else:
            text = content if isinstance(content, str) else ''
        if text.strip():
            result.append({'role': role, 'text': text[-12000:]})
    return result[-limit:]


class TerminalAdapter:
    def __init__(self, engine, bridge, module=None):
        self.engine, self.bridge = engine, bridge
        self.g = module or vars(sys.modules[type(bridge).__module__])
        self.prompt = None

    def snapshot(self, *, include_turn=False):
        b, g, engine = self.bridge, self.g, self.engine
        self.prompt = None
        if not getattr(b.repl, 'supports_pane_features', True):
            raise ControlError('unsupported_transport')
        pid = b.repl.pane_pid()
        if engine == 'codex':
            # Do NOT use session_file's newest-session fallback for input targeting.
            path = g['session_file_from_descendants'](pid)
            if not path:
                raise ControlError('session_unbound')
            screen = b.repl.capture_visible_screen()
            approval = g['parse_approval_prompt'](screen)
            choice = g['parse_choice_prompt'](screen)
            self.prompt = approval or choice
            question = None
            if self.prompt:
                p = self.prompt
                options = ([{'value': x.number, 'label': x.label} for x in p.options] if approval
                           else [{'value': x.value, 'label': x.label} for x in p.options
                                 if x.label.strip().casefold() not in ('other', '기타', '직접 입력')])
                question = {'id': p.signature, 'title': (p.command or p.reason) if approval else p.title,
                            'options': options}
            activity = g['repl_pane_activity_from_screen'](screen, 20)
            prompts = [line.lstrip()[1:].strip() for line in screen.splitlines()
                       if line.lstrip().startswith('›')]
            # Codex paints braille animation cells around its empty placeholder.
            # Do not strip arbitrary draft text or accept a missing composer.
            empty = bool(prompts and re.fullmatch(
                r'[\s\u2800-\u28ff]*(?:Ask Codex to do anything|Ask a follow-up question)[\s\u2800-\u28ff]*',
                prompts[-1]))
            draft = not empty and not question
            native_activity = codex_turn_activity(path)
            if question:
                status = 'waiting'
            elif activity == 'interstitial':
                status = 'unknown'
            elif activity == 'alive' or native_activity == 'busy':
                status = 'busy'
            elif activity == 'idle':
                status = 'idle'
            elif activity in ('', 'interrupt') and empty and native_activity == 'idle':
                # Old interruption text remains visible after the task ends.
                # Require both a native end and an empty composer to recover.
                status = 'idle'
            else:
                status = 'unknown'
            capabilities = [] if b.config.bridge_kill else ['send', 'stop', 'answer']
        else:
            resolver = getattr(b.binder, 'resolve_for_health_check', b.binder.resolve)
            binding = resolver()
            if binding.pane_pid != pid:
                raise ControlError('session_unbound')
            path = binding.transcript_path
            screen = b.repl.capture_pane(80)
            parsed = g['parse_pane_choice'](screen) or g['parse_ask_question'](screen)
            self.prompt = parsed
            question = None
            if parsed:
                question = {'id': parsed['signature'], 'title': parsed['title'],
                            'context': '\n'.join(parsed.get('context') or []),
                            'options': [{'value': str(n), 'label': label} for n, label in parsed['options']]}
            busy = g['screen_has_active_work'](screen) or claude_turn_activity(path) == 'busy'
            visible = g['pane_composer_visible'](screen.splitlines())
            draft = bool(g['composer_residual_text'](screen)) if visible else not bool(question)
            status = 'waiting' if question else 'busy' if busy else 'idle' if visible else 'unknown'
            capabilities = ['send', 'stop']
            if getattr(b, 'pending', None) or getattr(b, 'clear_watch', None):
                capabilities.remove('send')
            if question and g['choice_buttons_allowed'](parsed['kind']):
                capabilities.append('answer')
            if g['screen_has_usage_limit'](screen):
                status, capabilities = 'limited', []
        if question:
            # Same labels in a later turn must not revive an old choice button.
            question['id'] = fingerprint(question['id'], path, Path(path).stat().st_size)
        turn_id = native_turn(path, engine) if include_turn else ''
        turn = ''
        if turn_id:
            info = Path(path).stat()
            turn = fingerprint(path, info.st_dev, info.st_ino, turn_id)
        return {'session': fingerprint(engine, pid, path), 'session_label': Path(path).stem[-12:],
                'turn': turn, 'turn_id': turn_id,
                'status': status, 'draft': draft, 'question': question,
                'capabilities': capabilities, 'messages': transcript(path),
                'mode': 'current-conversation'}

    def perform(self, request):
        # This is the very same lock used by Telegram input and native selection.
        with self.bridge.repl.composer_lock():
            validate_current(request, self.snapshot(include_turn='turn' in request))
            transport, action = self.bridge.repl, request['action']
            if action == 'send':
                transport._paste_prompt_unlocked(request['text'])
            elif action == 'stop':
                if self.engine == 'codex':
                    transport.send_key('Escape')
                else:
                    transport._submit_prompt_unlocked('Escape')
            elif self.engine == 'claude':
                # Existing choice policy was rechecked above. A single digit is
                # the native Claude menu selector, without an extra Enter.
                transport._submit_prompt_unlocked(request['option'])
            elif hasattr(self.prompt, 'command'):
                option = next(x for x in self.prompt.options if x.number == request['option'])
                transport.send_key(option.key)
                self.bridge.resolved_approval_ids.add(self.prompt.signature)
            else:
                option = next(x for x in self.prompt.options if x.value == request['option'])
                transport.send_choice_option(self.prompt, option)
                self.bridge.resolved_choice_ids.add(self.prompt.short_signature)
        return {'status': 'accepted'}


class TelegramStopButtons:
    """Bind one progress card to one native turn; persist intent before Escape."""
    prefix = 'turnstop:'

    def __init__(self, node, engine, bridge, module):
        self.bridge, self.engine = bridge, engine
        self.root = Path(bridge.config.state_path).parent / ('telegram-stop-' + engine)
        adapter = TerminalAdapter(engine, bridge, module)
        # This local callback uses the configured bot's node. The web/socket
        # controller keeps its existing fleet allowlist.
        self.control = Controller(node, engine, self.root,
                                  lambda: adapter.snapshot(include_turn=True), adapter.perform,
                                  allowed_nodes=(node,))
        self.lock = threading.RLock()
        self.path = self.root / 'card.json'

    def _read(self):
        try:
            card = json.loads(self.path.read_text())
            return card if isinstance(card, dict) else {}
        except (OSError, ValueError):
            return {}

    def _owner(self):
        b = self.bridge
        if self.engine == 'claude':
            active = getattr(b, 'active_turn', None)
            if not active or active.flow_closed:
                return '', 0, ''
            expected = active.user_uuid or ''
            binding = getattr(b, 'session_binding', None)
            if expected and binding:
                expected = claude_prompt_anchor(binding.transcript_path, expected)
            return active.nonce, active.flow_message_id, expected
        if getattr(b, 'flow_closing', False):
            return '', 0, ''
        state = getattr(b, 'bridge_state', None) or {}
        turn = state.get('active_turn') or {}
        expected = str(turn.get('id') or '')
        owner = fingerprint(b.flow_scope, expected)
        card = self._read()
        # Earlier cards included the process-local flow generation in their
        # owner. Keep only an already bound, still-current legacy card. Its
        # original native session/turn request is revalidated before Escape.
        if ('owner_version' not in card and not card.get('closed')
                and not card.get('attempted') and not card.get('pending')
                and b.flow_message_id and card.get('message_id') == b.flow_message_id
                and expected and card.get('expected') == expected
                and isinstance(card.get('request'), dict)
                and card['request'].get('session') and card['request'].get('turn')):
            owner = str(card.get('owner') or '')
        return owner, b.flow_message_id, expected

    def prepare(self):
        """Only a newly created card may acquire a new native target."""
        with self.lock:
            write_json(self.path, {'closed': True})
            owner, _, expected = self._owner()
            if not owner or not expected:
                return {}
            card = dict(owner=owner, expected=expected, message_id=0,
                        pending=True, attempted=False, closed=False)
            if self.engine == 'codex':
                card['owner_version'] = 2
            write_json(self.path, card)
            return self._bind(card)

    def _bind(self, card):
        """Allow delayed native confirmation only for the card's original turn."""
        try:
            current = self.control.snapshot()
            if current.get('turn_id') != card['expected'] or not current.get('turn'):
                return {}
            request = dict(node=self.control.node, engine=self.engine, action='stop',
                           id='stop_' + fingerprint(current['session'], current['turn']),
                           session=current['session'], turn=current['turn'])
            validate_current(request, current)
            # A new card (including overflow/restart) cannot replay an old stop.
            if (self.root / (request['id'] + '.json')).exists():
                card['closed'] = True
                write_json(self.path, card)
                return {}
            card.update(token=secrets.token_hex(12), request=request, pending=False)
            write_json(self.path, card)
            return self._markup(card)
        except Exception:
            # Progress/final delivery continues while the native state is unavailable.
            return {}

    def sent(self, message_id):
        with self.lock:
            card = self._read()
            owner, _, _ = self._owner()
            if card.get('owner') == owner and message_id:
                card['message_id'] = int(message_id)
                write_json(self.path, card)

    def _markup(self, card):
        keyboard = [] if card.get('attempted') or card.get('closed') else [[{
            'text': self.bridge.tr('Stop'), 'callback_data': self.prefix + card['token']}]]
        return {'reply_markup': json.dumps({'inline_keyboard': keyboard}, ensure_ascii=False)}

    def markup(self, *, close=False):
        with self.lock:
            card = self._read()
            owner, mid, _ = self._owner()
            if not card or card.get('owner') != owner or card.get('message_id') != mid:
                return {}
            if close:
                card['closed'] = True
                write_json(self.path, card)
            elif card.get('pending') and not card.get('closed'):
                return self._bind(card)
            return self._markup(card)

    def retire(self, owner, message_id):
        """Close the stored card after its turn detached, without touching a newer one."""
        with self.lock:
            card = self._read()
            if (owner and message_id and card.get('owner') == owner
                    and card.get('message_id') == message_id):
                card['closed'] = True
                write_json(self.path, card)

    def callback(self, callback):
        data = str(callback.get('data') or '')
        if not data.startswith(self.prefix):
            return False
        b = self.bridge

        def answer(text):
            if callback.get('id'):
                b.telegram.call('answerCallbackQuery', callback_query_id=callback['id'], text=b.tr(text))

        message = callback.get('message') or {}
        chat = message.get('chat') or {}
        sender = callback.get('from') or {}
        allowed = str(b.config.chat_id)
        if str(chat.get('id')) != allowed or str(sender.get('id')) != allowed:
            answer('This button is not for this chat.')
            return True
        with self.lock:
            card = self._read()
            owner, mid, _ = self._owner()
            if (not card or not owner or card.get('closed')
                    or card.get('owner') != owner or card.get('message_id') != mid
                    or str(message.get('message_id')) != str(mid)
                    or data != self.prefix + str(card.get('token'))):
                answer('This request has already ended.')
                return True
            if card.get('attempted'):
                answer('Stop was already requested. Check the result before trying again.')
                return True
            result = {}
            try:
                validate_current(card['request'], self.control.snapshot())
                card['attempted'] = True
                write_json(self.path, card)
                result = self.control.execute(card['request'])
            except ControlError:
                result = {'status': 'rejected'}
            except Exception:
                result = {'status': 'uncertain'}
            # Retire the button after a checked click, even when delivery is
            # uncertain. A later click must never become a retry of Escape.
            card['closed'] = True
            try:
                write_json(self.path, card)
            except Exception:
                pass
            try:
                b.telegram.call('editMessageReplyMarkup', chat_id=b.config.chat_id,
                                message_id=mid, reply_markup=json.dumps({'inline_keyboard': []}))
            except Exception:
                pass
            if result.get('status') == 'accepted':
                answer('Stop requested. Waiting for confirmation.')
            elif result.get('status') == 'rejected':
                answer('The running task changed. Check the current card or local screen.')
            else:
                answer('Stop delivery is unconfirmed. Check the local screen; do not retry.')
        return True


def start_control(*args, **kwargs):
    return None
