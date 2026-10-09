"""Telegram access to Codex's native pending-steer interrupt-and-send action.

Codex 0.155.1: chatwidget/interaction.rs sets
submit_pending_steers_after_interrupt; input_restore.rs submits those inputs.
Never copy a prompt back into the composer or apply Escape to a Tab queue.
"""
from __future__ import annotations

import json
from pathlib import Path
import re
import secrets
import threading
import time

from agent_control_adapters import TerminalAdapter, fingerprint
from agent_control_protocol import write_json


PREFIX = "codexnow:"
DELAY = 10.0
HEADER = "• Messages to be submitted after next tool call"
HINT = "(press esc to interrupt and send immediately)"
EMPTY_COMPOSER = re.compile(
    r"[\s\u2800-\u28ff]*(?:Ask Codex to do anything|Ask a follow-up question)[\s\u2800-\u28ff]*"
)


def pending_previews(screen):
    """Read only the native section immediately above an empty visible composer.

The displayed Esc hint is required: remapped/unbound shortcuts, cropped
sections, popups, and rejected/Tab queues must not become stop operations.
"""
    lines = screen.splitlines()
    composers = [i for i, line in enumerate(lines) if line.lstrip().startswith("›")]
    if not composers:
        return []
    end = composers[-1]
    if not EMPTY_COMPOSER.fullmatch(lines[end].lstrip()[1:].strip()):
        return []
    starts = [i for i, line in enumerate(lines[:end]) if line.strip().startswith(HEADER)]
    if not starts:
        return []
    section = lines[starts[-1]:end]
    # The header wraps on narrow panes, but its native wording stays identical.
    first = next((i for i, line in enumerate(section) if line.strip().startswith("↳")), None)
    if first is None or " ".join(line.strip() for line in section[:first]) != HEADER + " " + HINT:
        return []
    previews = []
    for line in section[first:]:
        text = line.strip()
        if not text or re.fullmatch(r"[─━\s]+", text):
            continue
        if text.startswith("• "):
            break  # Other native queues have different submission semantics.
        if text.startswith("↳ "):
            previews.append(text[2:])
        elif previews and line.startswith(" "):
            previews[-1] += " " + text
        else:
            return []
    return previews


def matches_preview(preview, text):
    # Native previews wrap and truncate at three lines. Compare the visible
    # prefix, and let the caller reject ambiguous matches in the bridge inbox.
    compact = lambda value: "".join(value.split())
    visible = compact(preview)
    expected = compact(text)
    return bool(visible) and (visible == expected or
                             (visible.endswith("…") and expected.startswith(visible[:-1])))


class SendNowButtons:
    def __init__(self, bridge, module):
        self.b, self.g = bridge, module
        self.path = Path(bridge.config.state_path).with_suffix(".send-now.json")
        self.adapter = TerminalAdapter("codex", bridge, module)
        self.lock = threading.RLock()
        self.last_poll = 0.0
        self.last_markup = None
        self.failed_markup = None
        self.markup_retry_at = 0.0

    def read(self):
        try:
            value = json.loads(self.path.read_text())
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            return {}

    def inbox(self):
        with self.b.lock:
            return list(self.b.pending_telegram) + list(self.b.queued_telegram)

    def snapshot(self):
        b = self.b
        if (b.config.bridge_kill or b.flow_closing or not b.flow_message_id
                or not b.session_path or not self.inbox()):
            return None
        if not self.g['flow_mirror_enabled'](bool(getattr(b.config, 'flow_mirror', False))):
            return None
        screen = b.repl.capture_visible_screen()
        if self.g["is_side_screen"](screen):
            return None
        previews = pending_previews(screen)
        if not previews:
            return None
        inbox = self.inbox()
        items = []
        for preview in previews:
            matches = [(text, mid) for text, mid in inbox if mid > 0
                       and not text.lstrip().startswith(("/", "!"))
                       and matches_preview(preview, text)]
            if len(matches) != 1 or matches[0] in items:
                return None
            items.append(matches[0])
        current = self.adapter.snapshot(include_turn=True)
        if (current["status"] != "busy" or current["draft"] or current["question"]
                or not current["turn"] or "stop" not in current["capabilities"]
                or current["session"] != fingerprint("codex", b.repl.pane_pid(), b.session_path)
                or current["turn_id"] != ((b.bridge_state or {}).get("active_turn") or {}).get("id")):
            return None
        stop = getattr(b, "stop_buttons", None)
        stopped = stop._read() if stop else {}
        if stopped.get("message_id") == b.flow_message_id and stopped.get("attempted"):
            return None
        return {"session": current["session"], "turn": current["turn"],
                "items": [[mid, fingerprint(text)] for text, mid in items],
                "receipt": fingerprint(self.g["normalize_prompt"]("\n".join(text for text, _ in items)))}

    def refresh(self):
        state = self.read()
        old = state.get("card") or {}
        current = self.snapshot()
        if not current:
            if old and not old.get("closed"):
                old["closed"] = True
                write_json(self.path, state)
            return state
        same_owner = all(old.get(key) == current[key] for key in ("session", "turn"))
        overlap = any(item in current["items"] for item in old.get("items", []))
        if not same_owner or not overlap or old.get("closed"):
            # A dispatched action cannot become another Escape while awaiting
            # its native receipt, even after a restart or progress-card rollover.
            attempt = state.get("attempt") or {}
            if attempt.get("turn") == current["turn"] and attempt.get("status") != "not_sent":
                return state
            old = dict(current, token=secrets.token_hex(12), since=time.time(),
                       message_id=self.b.flow_message_id, closed=False)
            state["card"] = old
        else:
            old.update(current)
        write_json(self.path, state)
        return state

    def markup(self, *, close=False):
        with self.lock:
            state = self.read() if close else self.refresh()
            card = state.get("card") or {}
            if close and card:
                card["closed"] = True
                write_json(self.path, state)
            if (not card or card.get("closed") or card.get("message_id") != self.b.flow_message_id
                    or time.time() - card["since"] < DELAY):
                return []
            count = len(card["items"])
            english = getattr(getattr(self.b, "language", None), "code", "ko") == "en"
            label = f"🤖 Apply now · {count}" if english else f"🤖 지금 반영 · {count}개"
            return [[{"text": label, "callback_data": PREFIX + card["token"]}]]

    def sent(self, message_id):
        with self.lock:
            state = self.read()
            card = state.get("card") or {}
            if card and not card.get("closed"):
                card["message_id"] = int(message_id)
                write_json(self.path, state)

    def poll(self):
        if time.monotonic() - self.last_poll < 2.0:
            return
        self.last_poll = time.monotonic()
        with self.b.flow_lock:
            if not self.b.flow_message_id:
                return
            kwargs = self.b.stop_button_kwargs()
            markup = kwargs.get("reply_markup")
            key = (self.b.flow_message_id, markup)
            if key == self.failed_markup and time.monotonic() < self.markup_retry_at:
                return
            if markup and key != self.last_markup:
                result = self.b.telegram.call("editMessageReplyMarkup", chat_id=self.b.config.chat_id,
                                              message_id=key[0], reply_markup=markup,
                                              _request_timeout=5, _attempts=1)
                if result and result.get("ok"):
                    self.last_markup = key
                    self.failed_markup = None
                else:
                    self.failed_markup = key
                    self.markup_retry_at = time.monotonic() + 30

    def callback(self, callback):
        data = str(callback.get("data") or "")
        if not data.startswith(PREFIX):
            return False
        b = self.b

        def answer(text, english=None):
            if english and getattr(getattr(b, 'language', None), 'code', 'ko') == 'en':
                text = english
            if callback.get("id"):
                b.telegram.call("answerCallbackQuery", callback_query_id=callback["id"], text=b.tr(text),
                                _request_timeout=5, _attempts=1)

        message = callback.get("message") or {}
        if (str((message.get("chat") or {}).get("id")) != str(b.config.chat_id)
                or str((callback.get("from") or {}).get("id")) != str(b.config.chat_id)):
            answer("This button is not for this chat.")
            return True
        with b.flow_lock, self.lock:
            state = self.read()
            card = state.get("card") or {}
            if (card.get("closed") or data != PREFIX + str(card.get("token"))
                    or not b.flow_message_id or card.get("message_id") != b.flow_message_id
                    or str(message.get("message_id")) != str(b.flow_message_id)):
                answer("이미 처리됐거나 만료된 버튼입니다.", "This request has already ended.")
                return True
            outcome = ("대기 메시지가 바뀌었습니다. 현재 카드를 확인해 주세요.",
                       "Pending messages changed. Check the current card.")
            try:
                with b.repl.composer_lock():
                    current = self.snapshot()
                    if (current and all(current[k] == card.get(k) for k in ("session", "turn"))
                            and any(item in current["items"] for item in card["items"])):
                        # Record intent before the key. Never retry an uncertain delivery.
                        info = Path(b.session_path).stat()
                        state["attempt"] = dict(current, status="requested", path=str(b.session_path),
                                                dev=info.st_dev, ino=info.st_ino, offset=info.st_size)
                        card["closed"] = True
                        write_json(self.path, state)
                        # Re-read the native preview immediately before Escape. If it
                        # vanished, Escape would only stop the model and is forbidden.
                        if self.snapshot() == current:
                            b.repl.tmux("send-keys", "-t", b.config.pane_target, "Escape")
                            outcome = ("지금 반영을 요청했습니다. Codex 수신을 확인 중입니다.",
                                       "Apply now requested. Waiting for Codex confirmation.")
                        else:
                            state["attempt"]["status"] = "not_sent"
                            write_json(self.path, state)
            except Exception:
                outcome = ("전달 결과를 확인하지 못했습니다. 자동 재전송하지 않습니다.",
                           "Delivery is unconfirmed. The bridge will not retry automatically.")
            card["closed"] = True
            # Do not revive the button if the status/markup update fails.
            write_json(self.path, state)
            try:
                b.telegram.call("editMessageReplyMarkup", chat_id=b.config.chat_id,
                                message_id=b.flow_message_id, _request_timeout=5, _attempts=1,
                                **b.stop_button_kwargs())
            except Exception:
                pass
            answer(*outcome)
        return True

    def consume_receipt(self, text, offset=None):
        """Bind Codex's merged native user receipt to the last Telegram input.

The bridge queues are removed only on a fresh, exact receipt in the bound
session. A keypress, disappearance from the pane, or a turn-aborted event is
not proof that the model received the messages.
"""
        with self.lock:
            state = self.read()
            attempt = state.get("attempt") or {}
            if (attempt.get("status") not in ("requested", "confirmed") or offset is None
                    or offset < attempt["offset"] or str(self.b.session_path) != attempt["path"]):
                return None
            info = Path(self.b.session_path).stat()
            if (info.st_dev, info.st_ino) != (attempt["dev"], attempt["ino"]):
                return None
            with self.b.lock:
                items = list(attempt["items"])
                inbox = self.inbox()
                available = {(mid, fingerprint(body)) for body, mid in inbox}
                if any(tuple(item) not in available for item in items):
                    return None
                receipt = fingerprint(self.g["normalize_prompt"](text))
                if receipt != attempt["receipt"]:
                    # A later normal Enter can reach Codex while its interrupt
                    # acknowledgement is in flight. Attribute only an exact
                    # native merged receipt, never assume those later inputs ran.
                    bodies = [next(body for body, entry_mid in inbox if entry_mid == mid
                                   and fingerprint(body) == digest) for mid, digest in items]
                    for body, mid in sorted(inbox, key=lambda entry: entry[1]):
                        if mid <= items[-1][0] or body.lstrip().startswith(("/", "!")):
                            continue
                        bodies.append(body)
                        items.append([mid, fingerprint(body)])
                        if fingerprint(self.g["normalize_prompt"]("\n".join(bodies))) == receipt:
                            break
                    else:
                        return None
                    attempt.update(items=items, receipt=receipt)
                mids = {mid for mid, _ in items}
                attempt["status"] = "confirmed"
                write_json(self.path, state)
                before = {name: list(getattr(self.b, name)) for name in ("pending_telegram", "queued_telegram")}
                try:
                    for name, entries in before.items():
                        setattr(self.b, name, [(body, mid) for body, mid in entries if mid not in mids])
                    self.b.persist_telegram_inbox()
                except Exception:
                    for name, entries in before.items():
                        setattr(self.b, name, entries)
                    raise
            return items[-1][0]
