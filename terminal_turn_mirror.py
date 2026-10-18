"""Terminal / fleet-dispatch prompt mirrors for worker Telegram chats.

T-260910-012: people want the pasted worker instruction and the final answer
on Telegram, in full. APPROVED-FLEET stays classified as dispatch
(T-260910-006) so it is not treated as a human TUI question and is not routed
into another engine room. The prompt is still mirrored as 보낸 지시, secrets
masked, then the existing Telegram chunk sender preserves the whole body.

Canonical send:  [APPROVED-FLEET task=T-… worker=… orchestrator=…]
Same-task helper: [APPROVED-FLEET] T-260910-012 node:grok …
"""
from __future__ import annotations

import re

SENT_DIRECTIVE_HEADER = "보낸 지시"
# T-260914-021: Codex AMBIENT_DIRECTIVE_HEADER 동형 — 주입 즉시 받은 지시 카드.
RECEIVED_DIRECTIVE_HEADER = "📥 받은 지시"
TERMINAL_QUERY_PREFIX = "터미널에서 물어본 것 — "
# Telegram payload budget used by the Telegram chunk sender (not a truncation cap).
TELEGRAM_CHUNK_LIMIT = 3500

_DISPATCH_CARRIER_RE = re.compile(r"\[directive-carrier nonce:\s*carrier-\d+")
_DISPATCH_HEAD_RE = re.compile(
    r"^\[(?:claude-skills HEAD|CLAUDE-REVIEW-ROUTE|NODE-ACK-)",
    re.MULTILINE,
)
_DISPATCH_ROUTE_RE = re.compile(r"^from=\S+\s*\|?\s*task=", re.MULTILINE)
# Canonical `task=` and helper `[APPROVED-FLEET] T-…` / `[APPROVED-FLEET]` follow-up.
_DISPATCH_FLEET_RE = re.compile(r"\[APPROVED-FLEET(?:\s+task=|\])")
_BOT_TOKEN_RE = re.compile(r"\b\d{8,12}:[A-Za-z0-9_-]{20,}\b")
# sk-proj-… keeps hyphens. Do not treat token.json as an assignment.
_SK_RE = re.compile(r"\bsk-[A-Za-z0-9][-A-Za-z0-9]{8,}\b")
_GHP_RE = re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}\b")
_GITHUB_PAT_RE = re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b")
_XOX_RE = re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")
_LABEL = r"(?:api[_-]?key|passwd|password|secret|(?<![A-Za-z0-9_.-])token|otp)"
_LABELED_SECRET_RE = re.compile(
    rf"(?i){_LABEL}\s*[:=]\s*(?:\"[^\"]*\"|'[^']*')"
)
_LABELED_SECRET_BARE_RE = re.compile(
    rf"(?i){_LABEL}\s*[:=]\s*\S+"
)
_KO_SECRET_RE = re.compile(
    r"(비밀번호|비번|인증번호|인증\s*코드)\s*[:=]\s*(?:\"[^\"]*\"|'[^']*'|[^\n]+)"
)
_JSON_SECRET_RE = re.compile(
    r"(?i)(\"(?:api[_-]?key|password|passwd|secret|token|otp|비밀번호)\"\s*:\s*\")([^\"]*)(\")"
)
_TASK_RE = re.compile(r"\b(T-\d{6}-\d+)\b")
_WORKER_EQ_RE = re.compile(r"\bworker=([^\s\]]+)")
_ORCH_EQ_RE = re.compile(r"\borchestrator=([^\s\]]+)")
_WORKER_TOKEN_RE = re.compile(
    r"\bT-\d{6}-\d+\s+([A-Za-z0-9_-]+:(?:codex|cursor|grok))\b",
    re.I,
)


def is_approved_fleet_prompt(question: str) -> bool:
    return bool(_DISPATCH_FLEET_RE.search(question or ""))


def is_dispatch_prompt(question: str) -> bool:
    """Carrier/fleet paste, not a human TUI one-liner."""
    raw = question or ""
    return bool(
        _DISPATCH_CARRIER_RE.search(raw)
        or _DISPATCH_HEAD_RE.search(raw)
        or _DISPATCH_ROUTE_RE.search(raw)
        or _DISPATCH_FLEET_RE.search(raw)
    )


def mask_secrets(text: str) -> str:
    raw = text or ""
    raw = _BOT_TOKEN_RE.sub("<redacted-token>", raw)
    raw = _SK_RE.sub("<redacted-key>", raw)
    raw = _GHP_RE.sub("<redacted-key>", raw)
    raw = _GITHUB_PAT_RE.sub("<redacted-key>", raw)
    raw = _XOX_RE.sub("<redacted-key>", raw)
    raw = _JSON_SECRET_RE.sub(r"\1<redacted>\3", raw)
    raw = _LABELED_SECRET_RE.sub(
        lambda m: re.sub(r"[:=]\s*.*$", "=<redacted>", m.group(0)), raw
    )
    raw = _KO_SECRET_RE.sub(lambda m: f"{m.group(1)}=<redacted>", raw)
    raw = _LABELED_SECRET_BARE_RE.sub(
        lambda m: re.sub(r"[:=]\s*\S+$", "=<redacted>", m.group(0)), raw
    )
    return raw


def fleet_link(text: str) -> tuple[str | None, str | None, str | None]:
    raw = text or ""
    task = None
    # Prefer explicit task= then first T-id in the prompt.
    eq = re.search(r"\[APPROVED-FLEET\s+task=(T-\d{6}-\d+)", raw)
    if eq:
        task = eq.group(1)
    else:
        after = re.search(r"\[APPROVED-FLEET\]\s*(T-\d{6}-\d+)", raw)
        if after:
            task = after.group(1)
        else:
            found = _TASK_RE.search(raw)
            task = found.group(1) if found else None
    worker = None
    wm = _WORKER_EQ_RE.search(raw)
    if wm:
        worker = wm.group(1)
    else:
        tm = _WORKER_TOKEN_RE.search(raw)
        if tm:
            worker = tm.group(1)
    orch = None
    om = _ORCH_EQ_RE.search(raw)
    if om:
        orch = om.group(1)
    return task, worker, orch


def _link_line(text: str) -> str:
    task, worker, orch = fleet_link(text)
    bits = []
    if task:
        bits.append(f"task={task}")
    if worker:
        bits.append(f"worker={worker}")
    if orch:
        bits.append(f"orchestrator={orch}")
    return " ".join(bits)


def format_sent_directive(text: str, limit: int | None = None) -> str:
    """Full masked instruction. `limit` is ignored; Telegram chunks the send."""
    del limit
    masked = mask_secrets(text or "")
    lines = []
    link = _link_line(text or "")
    if link:
        lines.append(link)
    body = masked.strip()
    if not body and not lines:
        return ""
    if body:
        lines.append(body)
    return SENT_DIRECTIVE_HEADER + "\n" + "\n".join(lines)


def format_received_directive(text: str, limit: int | None = None) -> str:
    """Telegram/directive inject echo. Codex `📥 받은 지시` parity (T-260914-021)."""
    del limit
    masked = mask_secrets(text or "").strip()
    if not masked:
        return ""
    link = _link_line(text or "")
    parts = []
    if link:
        parts.append(link)
    parts.append(masked)
    return RECEIVED_DIRECTIVE_HEADER + "\n" + "\n".join(parts)


def format_terminal_query(text: str, limit: int | None = None) -> str:
    del limit
    body = mask_secrets(text or "").strip()
    if not body:
        return ""
    return TERMINAL_QUERY_PREFIX + body


def format_prompt_mirror(text: str, limit: int | None = None) -> str:
    if is_dispatch_prompt(text or ""):
        return format_sent_directive(text, limit=limit)
    return format_terminal_query(text, limit=limit)


def telegram_chunks(text: str, limit: int = TELEGRAM_CHUNK_LIMIT) -> list[str]:
    """Same contract as the Telegram chunk sender: full text, no overlap, last piece keeps the tail."""
    if len(text) <= limit:
        return [text]
    payload_limit = limit - 16
    pieces: list[str] = []
    start = 0
    while start < len(text):
        end = min(len(text), start + payload_limit)
        if end < len(text):
            window = text[start:end]
            cut = max(window.rfind("\n"), window.rfind(". "), window.rfind("。"))
            if cut >= payload_limit // 4:
                end = start + cut + 1
        pieces.append(text[start:end])
        start = end
    total = len(pieces)
    return [f"{piece}\n({idx}/{total})" for idx, piece in enumerate(pieces, start=1)]


# T-261011-071: display metadata is shared by all four bridges. It never grants
# dispatch authority and never selects a transport or another engine's bot.
ENGINE_LABELS = {"claude": "클로드", "codex": "코덱스", "grok": "그록", "cursor": "커서"}


def handoff_node_label(node: str) -> str:
    return mask_secrets(node)[:50]


def handoff_context(text: str, receiver: str, receiver_engine: str = "claude", *, skip_line=None, node_label=None):
    raw = text or ""
    if receiver_engine not in ENGINE_LABELS:
        return None
    route = re.search(r"(?m)^from=([^\s|]+)\s*\|\s*task=(T-\d{6}-\d+)\b([^\n]*)", raw)
    label = node_label or handoff_node_label
    sender_engine = ""
    if route:
        sender, task = route[1], route[2]
        match = re.search(r"\bsender_engine=(claude|codex|grok|cursor)\b", route[3])
        if not match:
            match = re.search(r"(?m)^\[directive-sender-engine: (claude|codex|grok|cursor)\]$", raw)
        sender_engine = match[1] if match else ""
    elif is_approved_fleet_prompt(raw):
        task, worker, issuer = fleet_link(raw)
        if not task or not worker or not issuer or ":" not in worker or ":" not in issuer:
            return None
        target_node, target_engine = worker.rsplit(":", 1)
        sender, sender_engine = issuer.rsplit(":", 1)
        if target_engine != receiver_engine or label(target_node) != label(receiver):
            return None
        if sender_engine not in ENGINE_LABELS:
            return None
    else:
        return None
    lines = [line.strip() for line in raw.splitlines() if line.strip()]
    lines = [line for line in lines if not re.match(
        r"^(?:from=|\[(?:directive-|claude-skills HEAD|APPROVED-FLEET|CLAUDE-REVIEW-ROUTE)|<[^>]+>$)", line)
        and not re.fullmatch(r"T-\d{6}-\d+", line)
        and not (skip_line and skip_line(line))]
    title = lines[0] if lines else "전달받은 작업"
    title = re.sub(r"^#+\s*|^\[(?:명령|지시)\]\s*", "", title)
    title = " ".join(mask_secrets(title.replace(task, "")).split()).strip(" —:|-#")
    title = title[:159] + "…" if len(title) > 160 else title
    source = label(sender) + (" " + ENGINE_LABELS[sender_engine] if sender_engine else "")
    return {"title": title or "전달받은 작업", "task_id": task,
            "route": f"{source} → {label(receiver)} {ENGINE_LABELS[receiver_engine]}"}


def format_handoff_card(context, steps=(), *, done_label="", elapsed_text="", updated=""):
    if isinstance(steps, str):
        steps = steps.splitlines()
    lines = [mask_secrets(str(line)).strip()[:160] for line in steps if str(line).strip()][-6:]
    status = done_label or ("진행 중" if lines else "접수됨")
    head = f"{status}\n{context['title']}\n\n{context['route']}\n{context['task_id']}"
    detail = ("\n".join(lines) or "진행 내용은 작업을 시작하면 표시합니다.") if not done_label else ""
    footer = f"소요 {elapsed_text}" if done_label and elapsed_text else (f"갱신 {updated}" if updated and lines else "")
    return "\n\n".join(part for part in (head, detail, footer) if part)


class HandoffCard:
    """One durable card per native turn; an uncertain send is never reposted.

    Callbacks stay on the receiving bridge's existing transport. Only masked
    display context and native turn hashes are persisted, never prompt bodies.
    """
    def __init__(self, path, send, edit):
        import json
        import threading
        from pathlib import Path
        self.path, self.send, self.edit = Path(path), send, edit
        self.lock = threading.RLock()
        try:
            self.state = json.loads(self.path.read_text())
        except (OSError, ValueError):
            self.state = {}
        self.retry_close()

    @property
    def message_id(self):
        value = self.state.get("message_id")
        return value if type(value) is int and value > 0 else 0

    @property
    def active(self):
        return bool(self.state.get("context")) and not self.state.get("closed")

    def _save(self):
        import json
        import os
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as stream:
            os.chmod(tmp, 0o600)
            json.dump(self.state, stream, ensure_ascii=False)
        os.replace(tmp, self.path)

    def begin(self, context, key):
        import hashlib
        import time
        key = hashlib.sha256(str(key).encode()).hexdigest()
        with self.lock:
            if self.state.get("key") == key:
                self.retry_close()
                return
            if self.active:
                self.finish("새 요청으로 전환")
            pending = list(self.state.get("pending_cards", []))
            if self.message_id and self.state.get("pending_close"):
                pending.append({"message_id": self.message_id, "body": self.state["pending_close"]})
            self.state = dict(key=key, context=context, message_id=0, closed=False,
                              started=time.time(), steps=[], attempted=True, pending_cards=pending)
            self._save()  # Before send: a timeout/crash must not produce a second card.
            try:
                mid = self.send(format_handoff_card(context))
            except Exception:
                mid = 0  # Unknown send: keep the durable intent, never repost.
            try:
                mid = int(mid) if not isinstance(mid, bool) else 0
            except (TypeError, ValueError):
                mid = 0
            self.state["message_id"] = max(0, mid)
            self._save()

    def update(self, step="", *, done_label=""):
        import time
        from datetime import datetime
        with self.lock:
            if not self.active:
                return False
            if self.state.get("pending_close") and not done_label:
                self.retry_close()
                return False
            steps = self.state.get("steps", [])
            step = mask_secrets(step)[:160]
            if step and (not steps or steps[-1] != step):
                steps = (steps + [step])[-6:]
                self.state["steps"] = steps
            elapsed = f"{int(max(0, time.time() - self.state['started']))}초"
            body = format_handoff_card(self.state["context"], steps, done_label=done_label,
                                       elapsed_text=elapsed, updated=datetime.now().strftime("%H:%M"))
            if done_label:
                self.state["pending_close"] = body
                self._save()
            ok = bool(self.message_id and self._edit(self.message_id, body))
            if done_label and (ok or not self.message_id):
                self.state["closed"] = True
                self.state.pop("pending_close", None)
            self._save()
            return ok

    def _edit(self, mid, body):
        try:
            return bool(self.edit(mid, body))
        except Exception:
            return False

    def retry_close(self):
        with self.lock:
            pending = self.state.get("pending_cards", [])
            if pending:
                self.state["pending_cards"] = [item for item in pending
                    if not self._edit(item["message_id"], item["body"])]
                self._save()
            body = self.state.get("pending_close")
            if body and self.message_id and self._edit(self.message_id, body):
                self.state["closed"] = True
                self.state.pop("pending_close", None)
                self._save()

    def finish(self, label="응답 완료"):
        return self.update(done_label=label)

    def forget(self):
        with self.lock:
            if self.active:
                self.finish("새 요청으로 전환")
            pending = list(self.state.get("pending_cards", []))
            if self.message_id and self.state.get("pending_close"):
                pending.append({"message_id": self.message_id, "body": self.state["pending_close"]})
            self.state = {"pending_cards": pending}
            self._save()
