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
