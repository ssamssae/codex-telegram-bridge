#!/usr/bin/env python3
"""Telegram bridge for the existing Codex REPL.

This bridge intentionally targets the visible `cx` / `tmux -L codex` REPL:

- Telegram text is pasted into the existing Codex TUI.
- Final answers are read from Codex's JSONL session file, not from screen
  scraping.
- Answers typed directly in the REPL are mirrored to Telegram too.

There is no public non-TTY input API for an already-running Codex TUI, so only
input delivery uses tmux. Reply extraction uses the structured session log.
"""

from __future__ import annotations

import glob
import hashlib
import json
import mimetypes
import os
import re
import secrets
import signal
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agent_runtime.adapters.codex_repl import CodexReplAdapter
from agent_runtime.approvals import ApprovalOption, ApprovalRequest
from agent_runtime.capabilities import CapabilityRegistry
from agent_runtime.types import AgentMessage


HOME = Path.home()
NODE_EMOJI_LINES = {"\U0001f34e", "\U0001f3ed", "\U0001fa9f", "\U0001f5a5", "\U0001f4bb", "\U0001f916"}
FLOW_MIRROR_HEADER = "⚙️ 작업 흐름"
FLOW_MIRROR_LIMIT = 1500
# Per-step line cap: each flow event is collapsed to ONE short line (claude
# parity) instead of the full multi-line narration. Keeps many steps inside one
# edit-in-place card instead of overflowing into many long messages.
FLOW_MIRROR_LINE_LIMIT = 200
FLOW_MIRROR_EDIT_MIN_SECONDS = 1.0
TASK_ID_RE = re.compile(r"\bT-\d{6}-\d+\b")
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
IMAGE_ATTACHMENT_EXTENSIONS = IMAGE_EXTENSIONS | {".bmp", ".tif", ".tiff"}
AUDIO_EXTENSIONS = {".ogg", ".oga", ".opus", ".mp3", ".m4a", ".aac", ".wav", ".flac", ".weba"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".webm", ".avi", ".mkv"}
MEDIA_ATTACHMENT_EXTENSIONS = IMAGE_ATTACHMENT_EXTENSIONS | AUDIO_EXTENSIONS | VIDEO_EXTENSIONS
VOICE_ATTACHMENT_EXTENSIONS = {".ogg", ".oga", ".opus"}
APPROVAL_CALLBACK_PREFIX = "crb_approval"
CHOICE_CALLBACK_PREFIX = "crb_choice"
STATUS_COMMANDS = {"status", "/status"}
CONTEXT_COMMANDS = {"context", "/context"}
LONG_RUNNING_SLASH_COMMANDS = {"/goal"}
ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
REPL_BUSY_RE = re.compile(
    r"esc to interrupt|interrupt to stop|\bWorking\b|tokens used",
    re.IGNORECASE,
)
UNRECOGNIZED_COMMAND_RE = re.compile(r"Unrecognized command ['\"](?P<command>/[^'\"]+)['\"]")
MARKDOWN_LOCAL_PATH_RE = re.compile(r"!?\[[^\]]*]\((?P<path>[^)\n]+)\)")
MEDIA_ATTACHMENT_SUFFIX_RE = "|".join(
    re.escape(ext) for ext in sorted(MEDIA_ATTACHMENT_EXTENSIONS, key=len, reverse=True)
)
RAW_LOCAL_ATTACHMENT_PATH_RE = re.compile(
    rf"(?P<path>(?:file://)?/(?:[^\s\])<>\"']+?)(?:{MEDIA_ATTACHMENT_SUFFIX_RE}))",
    re.IGNORECASE,
)


def env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    return value if value not in (None, "") else default


def int_env(name: str, default: int, minimum: int = 0) -> int:
    try:
        value = int(env(name, str(default)) or default)
    except (TypeError, ValueError):
        return default
    return value if value >= minimum else default


def bool_env(name: str, default: bool = False) -> bool:
    fallback = "1" if default else "0"
    return (env(name, fallback) or fallback).lower() in {"1", "true", "yes", "on"}


def clamp(value: int, minimum: int, maximum: int) -> int:
    return max(minimum, min(maximum, value))


def path_env_list(raw: str | None, fallback: list[Path]) -> tuple[Path, ...]:
    if not raw:
        return tuple(path.expanduser().resolve() for path in fallback)
    paths = []
    for item in raw.split(os.pathsep):
        item = item.strip()
        if item:
            paths.append(Path(item).expanduser().resolve())
    return tuple(paths)


def now_ts() -> str:
    return time.strftime("%H:%M:%S")


def log(label: str, message: str) -> None:
    print(f"[{now_ts()}] {label:<5} {message}", flush=True)


def node_defaults() -> tuple[str, str]:
    hostname = os.uname().nodename
    cleaned = safe_filename_part(hostname).lower()
    return cleaned or "codex", env("TAB_PREFIX", "\U0001f916") or "\U0001f916"


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def read_json(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def write_text_atomic(path: Path, value: str | int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(str(value), encoding="utf-8")
    tmp.replace(path)


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    tmp.replace(path)
    try:
        dir_fd = os.open(str(path.parent), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def safe_filename_part(value: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in {"-", "_", "."} else "-" for ch in value)
    return cleaned.strip("-")[:80] or "file"


def is_codex_slash_command(text: str) -> bool:
    command = slash_command_token(text)
    return bool(command and command not in {"/start", "/ping"})


def slash_command_token(text: str) -> str:
    stripped = (text or "").strip()
    if "\n" in stripped or not stripped.startswith("/"):
        return ""
    return stripped.split(maxsplit=1)[0].lower()


def slash_command_keeps_typing(text: str) -> bool:
    command = slash_command_token(text)
    return command.split("@", 1)[0] in LONG_RUNNING_SLASH_COMMANDS


def should_stop_typing_after_slash_command(text: str, had_error: bool) -> bool:
    return had_error or not slash_command_keeps_typing(text)


def is_status_command(text: str) -> bool:
    stripped = (text or "").strip().lower()
    if stripped in STATUS_COMMANDS:
        return True
    command = stripped.split(maxsplit=1)[0] if stripped else ""
    return command == "/status" or command.startswith("/status@")


def is_context_command(text: str) -> bool:
    stripped = (text or "").strip().lower()
    if stripped in CONTEXT_COMMANDS:
        return True
    command = stripped.split(maxsplit=1)[0] if stripped else ""
    return command == "/context" or command.startswith("/context@")


def extract_codex_status_text(screen: str) -> str:
    lines = (screen or "").splitlines()
    header_index = None
    for index in range(len(lines) - 1, -1, -1):
        if "OpenAI Codex" in lines[index]:
            header_index = index
            break
    if header_index is None:
        return truncate_text((screen or "").strip(), 3500)

    start = header_index
    while start > 0 and "╭" not in lines[start]:
        start -= 1
    if "╭" not in lines[start]:
        start = max(0, header_index - 2)

    end = header_index
    while end < len(lines) - 1 and "╰" not in lines[end]:
        end += 1
    if "╰" not in lines[end]:
        end = min(len(lines) - 1, header_index + 25)

    cleaned: list[str] = []
    for raw_line in lines[start : end + 1]:
        line = raw_line.strip()
        if not line or set(line) <= set("╭╮╰╯─━ "):
            continue
        if "│" in raw_line:
            parts = raw_line.split("│")
            line = "│".join(parts[1:-1]).strip() if len(parts) >= 3 else raw_line.strip(" │")
        if not line:
            continue
        if line.startswith("Visit ") or line.startswith("information on "):
            continue
        cleaned.append(re.sub(r"\s{2,}", "  ", line).rstrip())

    body = "\n".join(cleaned).strip()
    return f"Codex status\n{body}" if body else "Codex status not visible yet."


def extract_codex_context_text(screen: str) -> str:
    status_text = extract_codex_status_text(screen)
    for line in status_text.splitlines():
        if line.startswith("Context window:"):
            return f"Codex context\n{line}"
    return "Codex context not visible yet."


def extract_unrecognized_slash_error(screen: str, command: str) -> str:
    normalized = command.strip().lower()
    if not normalized:
        return ""
    for line in reversed((screen or "").splitlines()):
        cleaned = line.strip()
        match = UNRECOGNIZED_COMMAND_RE.search(cleaned)
        if match and match.group("command").lower() == normalized:
            return cleaned
    return ""


def suffix_from_metadata(file_name: str = "", mime_type: str = "", default: str = ".bin") -> str:
    suffix = Path(file_name or "").suffix.lower()
    if suffix:
        return suffix
    guessed = mimetypes.guess_extension(mime_type or "")
    return guessed.lower() if guessed else default


def command_available(command: str) -> bool:
    if not command:
        return False
    if "/" in command:
        return Path(command).exists()
    return shutil.which(command) is not None


def truncate_text(text: str, limit: int = 12000) -> str:
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + f"\n\n[truncated {len(text) - limit} chars]"


def strip_node_emoji_header(text: str) -> str:
    lines = (text or "").strip().splitlines()
    if not lines:
        return ""
    if lines[0].strip() in NODE_EMOJI_LINES:
        return "\n".join(lines[1:]).strip()
    return "\n".join(lines).strip()


def format_flow_mirror(text: str) -> str:
    body = truncate_text(strip_node_emoji_header(text), FLOW_MIRROR_LIMIT).strip()
    return f"{FLOW_MIRROR_HEADER}\n{body}" if body else ""


def flow_step_summary(text: str, limit: int = FLOW_MIRROR_LINE_LIMIT) -> str:
    """One clean line for a single flow step (claude parity).

    Collapse all whitespace into a single line, drop the node emoji header, and
    hard-truncate with an ellipsis — NO multi-line ``[truncated]`` footer. This
    keeps each accumulated step short so many fit inside one edit-in-place card
    instead of overflowing FLOW_MIRROR_LIMIT into multiple long messages.
    """
    body = " ".join(strip_node_emoji_header(text).split())
    if len(body) > limit:
        body = body[:limit].rstrip() + "…"
    return body.strip()


def flow_mirror_dedup_key(text: str, scope: str = "") -> str:
    body = re.sub(r"\s+", " ", strip_node_emoji_header(text).strip())
    if not body:
        return ""
    digest = hashlib.sha256(body.encode("utf-8", errors="replace")).hexdigest()
    if scope:
        scope_digest = hashlib.sha256(
            normalize_prompt(scope).encode("utf-8", errors="replace")
        ).hexdigest()[:16]
        return f"flow_mirror:{scope_digest}:{digest}"
    return f"flow_mirror:{digest}"


def is_copy_payload_message(text: str) -> bool:
    body = strip_node_emoji_header(text).strip()
    if not body:
        return False
    first_line = body.splitlines()[0].strip()
    return (
        first_line == "/goal"
        or first_line.startswith("/goal ")
        or first_line.startswith("상세스펙:")
        or first_line.startswith("상세 스펙:")
        or first_line.startswith("상세설명:")
        or first_line.startswith("상세 설명:")
        or re.match(r"^제목\s*:", first_line) is not None
        or re.match(r"^(내용|본문)\s*:", first_line) is not None
    )


def copy_payload_kind(text: str) -> str:
    body = strip_node_emoji_header(text).strip()
    if not body:
        return ""
    first_line = body.splitlines()[0].strip()
    if first_line == "/goal" or first_line.startswith("/goal "):
        return "goal"
    if (
        first_line.startswith("상세스펙:")
        or first_line.startswith("상세 스펙:")
        or first_line.startswith("상세설명:")
        or first_line.startswith("상세 설명:")
    ):
        return "spec"
    if re.match(r"^제목\s*:", first_line):
        return "title"
    if re.match(r"^(내용|본문)\s*:", first_line):
        return "content"
    return ""


def split_copy_payload_messages(text: str) -> list[str]:
    body = strip_node_emoji_header(text).strip()
    if not is_copy_payload_message(body):
        return []
    split_re = re.compile(
        r"\n(?=(?:/goal(?:\s|$)|상세\s*스펙:|상세\s*설명:|제목\s*:|(?:내용|본문)\s*:))"
    )
    starts = [0, *[match.start() + 1 for match in split_re.finditer(body)]]
    starts = sorted(set(starts))
    parts: list[str] = []
    for index, start in enumerate(starts):
        end = starts[index + 1] if index + 1 < len(starts) else len(body)
        part = body[start:end].strip()
        if part and is_copy_payload_message(part):
            parts.append(part)
    return parts or [body]


def copy_payload_dedup_key(text: str, scope: str = "") -> str:
    body = strip_node_emoji_header(text).strip()
    if not body:
        return ""
    digest = hashlib.sha256(body.encode("utf-8", errors="replace")).hexdigest()
    if scope:
        scope_digest = hashlib.sha256(
            normalize_prompt(scope).encode("utf-8", errors="replace")
        ).hexdigest()[:16]
        return f"copy_payload:{scope_digest}:{digest}"
    return f"copy_payload:{digest}"


COPY_PAYLOAD_SINGLE_MESSAGE_MARKERS = (
    "한 통",
    "한통",
    "1통",
    "한 메시지",
    "한메시지",
    "한 번에",
    "한번에",
    "같이",
    "합쳐",
    "합쳐서",
    "묶어서",
)
COPY_PAYLOAD_SPLIT_MARKERS = (
    "2통",
    "두 통",
    "두통",
    "2개",
    "두개",
    "두 번",
    "두번",
    "따로",
    "분리",
    "나눠",
    "나누",
    "별도",
    "각각",
    "그 다음",
    "그다음",
    "다음에",
)
GOAL_INTENT_RE = re.compile(r"(^|[\s,+/(])골(\s*명령어)?(?=([\s,+/)]|랑|와|과|하고|및|$))")
SPEC_INTENT_RE = re.compile(r"상세\s*(스[펙팩]|설명|프롬프트)")
TITLE_INTENT_RE = re.compile(r"제목")
CONTENT_INTENT_RE = re.compile(r"내용|본문")


def copy_payload_goal_intent(prompt: str) -> bool:
    body = normalize_prompt(prompt)
    if not body:
        return False
    lowered = body.lower()
    return (
        "/goal" in lowered
        or "goal 명령어" in lowered
        or "goal명령어" in lowered
        or bool(GOAL_INTENT_RE.search(body))
    )


def copy_payload_spec_intent(prompt: str) -> bool:
    return bool(SPEC_INTENT_RE.search(normalize_prompt(prompt)))


def copy_payload_title_intent(prompt: str) -> bool:
    return bool(TITLE_INTENT_RE.search(normalize_prompt(prompt)))


def copy_payload_content_intent(prompt: str) -> bool:
    return bool(CONTENT_INTENT_RE.search(normalize_prompt(prompt)))


def copy_payload_single_message_intent(prompt: str) -> bool:
    body = normalize_prompt(prompt)
    return any(marker in body for marker in COPY_PAYLOAD_SINGLE_MESSAGE_MARKERS)


def copy_payload_split_intent(prompt: str) -> bool:
    body = normalize_prompt(prompt)
    if not body or copy_payload_single_message_intent(body):
        return False
    return (
        any(marker in body for marker in COPY_PAYLOAD_SPLIT_MARKERS)
        or ("먼저" in body and "다음" in body)
        or body.count("보내") >= 2
    )


def prompt_requires_copy_payload_pair(prompt: str) -> bool:
    return (
        copy_payload_split_intent(prompt)
        and (
            (copy_payload_goal_intent(prompt) and copy_payload_spec_intent(prompt))
            or (copy_payload_title_intent(prompt) and copy_payload_content_intent(prompt))
        )
    )


def copy_payload_pair_contract(prompt: str) -> dict[str, Any] | None:
    if not prompt_requires_copy_payload_pair(prompt):
        return None
    required = ["goal", "spec"]
    if copy_payload_title_intent(prompt) and copy_payload_content_intent(prompt):
        required = ["title", "content"]
    digest = hashlib.sha256(normalize_prompt(prompt).encode("utf-8", errors="replace")).hexdigest()
    return {
        "prompt_sha256": digest,
        "required": required,
        "sent": [],
        "notified_missing": False,
    }


def copy_payload_pair_missing(contract: dict[str, Any] | None) -> list[str]:
    if not isinstance(contract, dict):
        return []
    required = contract.get("required")
    if not isinstance(required, list) or not required:
        required = ["goal", "spec"]
    sent = contract.get("sent")
    sent_set = {str(item) for item in sent} if isinstance(sent, list) else set()
    return [str(item) for item in required if str(item) not in sent_set]


def copy_payload_pair_missing_warning(missing: list[str]) -> str:
    labels = {"goal": "/goal", "spec": "상세스펙/상세설명", "title": "제목", "content": "내용"}
    missing_labels = ", ".join(labels.get(item, item) for item in missing)
    if any(item in {"title", "content"} for item in missing):
        return (
            f"복붙용 2통 요청에서 {missing_labels} 메시지가 누락됐어요. "
            "제목 한 통과 내용 한 통을 각각 따로 다시 보내 달라고 요청해 주세요."
        )
    return (
        f"복붙용 2통 요청에서 {missing_labels} 메시지가 누락됐어요. "
        "/goal 한 통과 상세스펙 한 통을 각각 따로 다시 보내 달라고 요청해 주세요."
    )


def copy_payload_pair_repair_prompt(missing: list[str]) -> str:
    labels = {
        "goal": "/goal 명령어",
        "spec": "상세스펙 또는 상세설명",
        "title": "제목",
        "content": "내용 또는 본문",
    }
    missing_labels = ", ".join(labels.get(item, item) for item in missing)
    if any(item in {"title", "content"} for item in missing):
        return (
            "방금 텔레그램 사용자는 복붙용 메시지 2통을 따로 요청했습니다. "
            f"아직 {missing_labels} 메시지가 누락됐습니다.\n"
            "지금 누락된 복붙용 메시지만 텔레그램에 그대로 보낼 수 있게 출력하세요. "
            "설명, 사과, 헤더, 구분선 없이 순수 복붙용 콘텐츠만 출력하세요.\n"
            "누락이 제목이면 첫 줄은 제목: 으로 시작하세요. "
            "누락이 내용이면 첫 줄은 내용: 으로 시작하세요."
        )
    return (
        "방금 텔레그램 사용자는 복붙용 메시지 2통을 따로 요청했습니다. "
        f"아직 {missing_labels} 메시지가 누락됐습니다.\n"
        "지금 누락된 복붙용 메시지만 텔레그램에 그대로 보낼 수 있게 출력하세요. "
        "설명, 사과, 헤더, 구분선 없이 순수 복붙용 콘텐츠만 출력하세요.\n"
        "누락이 /goal이면 첫 줄은 /goal 로 시작하세요. "
        "누락이 상세 메시지이면 첫 줄은 상세스펙: 또는 상세설명: 으로 시작하세요."
    )


def format_progress_summary(text: str, limit: int = 180) -> str:
    body = " ".join(strip_node_emoji_header(text).split())
    return truncate_text(body, limit).strip()


def extract_task_id(text: str) -> str:
    match = TASK_ID_RE.search(text or "")
    return match.group(0) if match else ""


def format_long_running_progress_message(
    prompt: str,
    elapsed_seconds: float,
    task_id: str = "",
    recent_progress: str = "",
) -> str:
    label = " ".join((prompt or "").strip().split())
    label = truncate_text(label or "(empty request)", 160)
    elapsed_minutes = max(1, int(elapsed_seconds // 60))
    lines = [
        "Still working on your Telegram request.",
        f"Task: {label}",
    ]
    if task_id:
        lines.append(f"Task ID: {task_id}")
    lines.append(f"Elapsed: about {elapsed_minutes} min")
    if recent_progress:
        lines.append(f"Recent: {format_progress_summary(recent_progress, 220)}")
    else:
        lines.append("Recent: no public progress note yet")
    lines.extend(
        [
            "Current: waiting for the final answer",
            "Next: I will send the final answer when it is ready.",
            "Blocked: no blocker reported",
        ]
    )
    return "\n".join(lines)


def strip_inline_node_emoji_header(text: str) -> str:
    lines = (text or "").splitlines()
    if not lines:
        return text
    first = lines[0].lstrip()
    for emoji in NODE_EMOJI_LINES:
        if first.startswith(emoji):
            rest = first[len(emoji) :]
            if rest and rest[0].isspace():
                lines[0] = rest.lstrip()
                return "\n".join(lines).strip()
    return text


def process_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def load_token(token_file: Path | None) -> str:
    token = (env("CRB_BOT_TOKEN") or env("TAB_BOT_TOKEN") or "").strip()
    if token:
        return token
    if token_file is None:
        raise RuntimeError("TAB_BOT_TOKEN is required")
    try:
        payload = json.loads(token_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"failed to read token file {token_file}: {exc}") from exc
    token = str(payload.get("api_key") or "").strip()
    if not token:
        raise RuntimeError(f"empty api_key in token file {token_file}")
    return token


@dataclass(frozen=True)
class SessionIdentity:
    path: str
    dev: int
    ino: int
    size: int


@dataclass(frozen=True)
class JsonlEvent:
    kind: str
    text: str
    timestamp: float
    start: int
    end: int
    key: str


def session_identity(path: Path) -> SessionIdentity:
    stat = path.stat()
    return SessionIdentity(
        path=str(path),
        dev=int(stat.st_dev),
        ino=int(stat.st_ino),
        size=int(stat.st_size),
    )


def parse_event_timestamp(record: dict[str, Any]) -> float:
    raw = str(record.get("timestamp") or "")
    if not raw:
        return 0.0
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def event_dedup_key(record: dict[str, Any], kind: str, text: str) -> str:
    raw = "\0".join(
        [
            str(record.get("timestamp") or ""),
            kind,
            (text or "")[:512],
        ]
    )
    return hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()


def bridge_state_default(identity: SessionIdentity, offset: int = 0) -> dict[str, Any]:
    return {
        "session_path": identity.path,
        "dev": identity.dev,
        "ino": identity.ino,
        "offset": offset,
        "coord_ring": [],
        "last_sent_ts": 0,
    }


def ring_values(state: dict[str, Any]) -> list[str]:
    values = state.get("coord_ring")
    if not isinstance(values, list):
        return []
    return [str(item) for item in values if isinstance(item, str)]


def ring_contains(state: dict[str, Any], key: str) -> bool:
    return key in set(ring_values(state))


def ring_push(state: dict[str, Any], key: str, cap: int) -> None:
    if not key:
        return
    ring = [item for item in ring_values(state) if item != key]
    ring.append(key)
    state["coord_ring"] = ring[-max(1, cap) :]
    state["last_sent_ts"] = int(time.time())


def cursor_offset_for_state(state: dict[str, Any] | None, identity: SessionIdentity) -> int | None:
    if not state:
        return None
    try:
        dev = int(state.get("dev"))
        ino = int(state.get("ino"))
        offset = int(state.get("offset"))
    except (TypeError, ValueError):
        return None
    if dev != identity.dev or ino != identity.ino:
        return None
    if offset < 0 or offset > identity.size:
        return None
    return offset


def parse_jsonl_event_line(line: str, start: int, end: int) -> JsonlEvent | None:
    try:
        record = json.loads(line)
    except json.JSONDecodeError:
        return None
    event = extract_event(record)
    if not event:
        return None
    kind, text = event
    return JsonlEvent(
        kind=kind,
        text=text,
        timestamp=parse_event_timestamp(record),
        start=start,
        end=end,
        key=event_dedup_key(record, kind, text),
    )


def read_tail_jsonl_events(path: Path, max_bytes: int) -> list[JsonlEvent]:
    size = path.stat().st_size
    start = max(0, size - max(1, max_bytes))
    events: list[JsonlEvent] = []
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        handle.seek(start)
        if start > 0:
            handle.readline()
        while True:
            line_start = handle.tell()
            line = handle.readline()
            if not line:
                break
            line_end = handle.tell()
            event = parse_jsonl_event_line(line, line_start, line_end)
            if event:
                events.append(event)
    return events


def eligible_backfill_events(
    events: list[JsonlEvent],
    state: dict[str, Any],
    now_epoch: float,
    window_seconds: int,
    limit: int,
) -> list[JsonlEvent]:
    latest_user_index = None
    for index, event in enumerate(events):
        if event.kind == "user":
            latest_user_index = index
    if latest_user_index is None:
        return []
    after_user = [
        event
        for event in events[latest_user_index + 1 :]
        if event.kind == "assistant" and event.text.strip()
    ]
    if not after_user:
        return []
    fresh = []
    for event in after_user:
        if ring_contains(state, event.key):
            continue
        if event.timestamp <= 0:
            continue
        age = max(0.0, now_epoch - event.timestamp)
        if age <= window_seconds:
            fresh.append(event)
    return fresh[-max(1, limit) :]


@dataclass(frozen=True)
class Config:
    node: str
    emoji: str
    token_file: Path | None
    chat_id: str
    state_dir: Path
    tmux_bin: str
    tmux_socket: str
    tmux_session: str
    submit_key: str
    enter_count: int
    codex_bin: str
    codex_timeout: int
    image_mode: str
    ffmpeg_bin: str
    ffprobe_bin: str
    audio_transcribe_cmd: str | None
    video_frame_count: int
    typing_max_seconds: int
    typing_liveness_seconds: int
    long_running_progress_seconds: int
    telegram_fallback_seconds: int
    approval_ttl_seconds: int
    workdir: Path
    attachment_roots: tuple[Path, ...]
    max_attachment_bytes: int
    telegram_chunk: int
    poll_timeout: int
    start_at_end: bool
    state_path: Path
    backfill_enabled: bool
    backfill_max: int
    backfill_window_sec: int
    tail_scan_bytes: int
    state_ring_cap: int
    bridge_kill: bool
    flow_mirror: bool

    @classmethod
    def from_env(cls) -> "Config":
        default_node, default_emoji = node_defaults()
        node = env("CRB_NODE", default_node) or default_node
        token_file_raw = env("CRB_TOKEN_FILE")
        chat_id = env("CRB_CHAT_ID") or env("TAB_CHAT_ID")
        if not chat_id:
            raise RuntimeError("TAB_CHAT_ID is required")
        state_dir = Path(
            env("CRB_STATE_DIR", env("TAB_STATE_DIR", "~/.local/state/telegram-agent-bridge") or "")
            or ""
        ).expanduser()
        workdir = Path(env("TAB_WORKDIR", str(HOME)) or str(HOME)).expanduser()
        default_state_path = state_dir / f"codex-repl-bridge-{node}.state.json"
        return cls(
            node=node,
            emoji=env("CRB_EMOJI", env("TAB_PREFIX", default_emoji) or default_emoji) or "",
            token_file=Path(token_file_raw).expanduser() if token_file_raw else None,
            chat_id=chat_id,
            state_dir=state_dir,
            tmux_bin=env("CRB_TMUX_BIN", "tmux") or "tmux",
            tmux_socket=env("CRB_TMUX_SOCKET", env("TAB_AGENT_TMUX_SOCKET", "codex") or "codex")
            or "codex",
            tmux_session=env("CRB_TMUX_SESSION", env("TAB_AGENT_TMUX_SESSION", "codex") or "codex")
            or "codex",
            submit_key=env("CRB_TMUX_SUBMIT_KEY", env("TAB_AGENT_TMUX_SUBMIT_KEY", "Tab") or "Tab")
            or "Tab",
            enter_count=int_env("CRB_TMUX_ENTER_COUNT", int_env("TAB_AGENT_TMUX_ENTER_COUNT", 5), minimum=1),
            codex_bin=env("CRB_CODEX_BIN", env("TAB_AGENT_CMD", "codex") or "codex") or "codex",
            codex_timeout=int_env("CRB_CODEX_TIMEOUT", 600, minimum=30),
            image_mode=env("CRB_IMAGE_MODE", "repl") or "repl",
            ffmpeg_bin=env("CRB_FFMPEG_BIN", "ffmpeg") or "ffmpeg",
            ffprobe_bin=env("CRB_FFPROBE_BIN", "ffprobe") or "ffprobe",
            audio_transcribe_cmd=env("CRB_AUDIO_TRANSCRIBE_CMD"),
            video_frame_count=int_env("CRB_VIDEO_FRAME_COUNT", 3, minimum=1),
            typing_max_seconds=int_env("CRB_TYPING_MAX_SECONDS", 7200, minimum=30),
            typing_liveness_seconds=int_env("CRB_TYPING_LIVENESS_SECONDS", 10, minimum=0),
            long_running_progress_seconds=int_env(
                "CRB_LONG_RUNNING_PROGRESS_SECONDS",
                0,
                minimum=0,
            ),
            telegram_fallback_seconds=int_env(
                "CRB_TELEGRAM_FALLBACK_SECONDS",
                90,
                minimum=0,
            ),
            approval_ttl_seconds=int_env("CRB_APPROVAL_TTL_SECONDS", 300, minimum=30),
            workdir=workdir,
            attachment_roots=path_env_list(
                env("CRB_ATTACHMENT_ROOTS"),
                [state_dir, workdir, Path("/tmp")],
            ),
            max_attachment_bytes=int_env(
                "CRB_MAX_ATTACHMENT_BYTES",
                50 * 1024 * 1024,
                minimum=1024,
            ),
            telegram_chunk=int_env("CRB_TG_CHUNK", 4096, minimum=512),
            poll_timeout=int_env("CRB_TG_POLL_TIMEOUT", 2, minimum=1),
            start_at_end=bool_env("CRB_START_AT_END", True),
            state_path=Path(env("CRB_STATE_PATH", str(default_state_path)) or "").expanduser(),
            backfill_enabled=bool_env("CRB_BACKFILL", True),
            backfill_max=clamp(int_env("CRB_BACKFILL_MAX", 1, minimum=1), 1, 3),
            backfill_window_sec=int_env("CRB_BACKFILL_WINDOW_SEC", 600, minimum=1),
            tail_scan_bytes=int_env("CRB_TAIL_SCAN_BYTES", 65536, minimum=1024),
            state_ring_cap=int_env("CRB_STATE_RING_CAP", 64, minimum=1),
            bridge_kill=bool_env("CRB_KILL", False),
            flow_mirror=bool_env("CRB_FLOW_MIRROR", True),
        )

    @property
    def session_target(self) -> str:
        target = self.tmux_session
        if target.startswith("%") or ":" in target or "." in target:
            return target
        return f"={target}"

    @property
    def pane_target(self) -> str:
        target = self.tmux_session
        if target.startswith("%") or ":" in target or "." in target:
            return target
        return f"={target}:"

    @property
    def offset_file(self) -> Path:
        return self.state_dir / f"codex-repl-bridge-{self.node}.offset"

    @property
    def pid_file(self) -> Path:
        return self.state_dir / f"codex-repl-bridge-{self.node}.pid"


class TelegramClient:
    def __init__(self, token: str, chat_id: str, emoji: str, chunk_size: int) -> None:
        self.token = token
        self.api = f"https://api.telegram.org/bot{token}"
        self.chat_id = chat_id
        self.emoji = emoji
        self.chunk_size = chunk_size

    def call(self, method: str, **params: Any) -> dict[str, Any] | None:
        data = urllib.parse.urlencode(params).encode()
        url = f"{self.api}/{method}"
        for attempt in range(3):
            try:
                request = urllib.request.Request(url, data=data)
                with urllib.request.urlopen(request, timeout=60) as response:
                    payload = json.load(response)
                return payload if isinstance(payload, dict) else None
            except Exception as exc:  # noqa: BLE001
                if attempt == 2:
                    log("TGERR", f"{method} failed: {exc}")
                    return None
                time.sleep(2)
        return None

    def call_multipart(
        self,
        method: str,
        fields: dict[str, str],
        file_field: str,
        file_path: Path,
    ) -> dict[str, Any] | None:
        boundary = "----crb" + secrets.token_hex(16)
        parts: list[bytes] = []
        for name, value in fields.items():
            parts.append(f"--{boundary}\r\n".encode())
            parts.append(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
            parts.append(str(value).encode("utf-8"))
            parts.append(b"\r\n")

        filename = file_path.name
        content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        parts.append(f"--{boundary}\r\n".encode())
        parts.append(
            (
                f'Content-Disposition: form-data; name="{file_field}"; '
                f'filename="{filename}"\r\n'
            ).encode()
        )
        parts.append(f"Content-Type: {content_type}\r\n\r\n".encode())
        parts.append(file_path.read_bytes())
        parts.append(b"\r\n")
        parts.append(f"--{boundary}--\r\n".encode())

        request = urllib.request.Request(
            f"{self.api}/{method}",
            data=b"".join(parts),
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )
        for attempt in range(3):
            try:
                with urllib.request.urlopen(request, timeout=60) as response:
                    payload = json.load(response)
                return payload if isinstance(payload, dict) else None
            except Exception as exc:  # noqa: BLE001
                if attempt == 2:
                    log("TGERR", f"{method} upload failed: {exc}")
                    return None
                time.sleep(2)
        return None

    def send_typing(self) -> None:
        self.call("sendChatAction", chat_id=self.chat_id, action="typing")

    def download_file(
        self,
        file_id: str,
        output_dir: Path,
        name_hint: str,
        default_suffix: str = ".bin",
        allowed_extensions: set[str] | None = None,
    ) -> Path:
        payload = self.call("getFile", file_id=file_id)
        if not payload or not payload.get("ok") or not isinstance(payload.get("result"), dict):
            raise RuntimeError("Telegram getFile failed")
        file_path = str(payload["result"].get("file_path") or "")
        if not file_path:
            raise RuntimeError("Telegram getFile returned empty file_path")

        suffix = Path(file_path).suffix.lower()
        if not suffix:
            suffix = default_suffix
        if allowed_extensions is not None and suffix not in allowed_extensions:
            suffix = default_suffix
        output_dir.mkdir(parents=True, exist_ok=True)
        output_path = output_dir / f"{safe_filename_part(name_hint)}{suffix}"

        quoted_path = urllib.parse.quote(file_path, safe="/")
        url = f"https://api.telegram.org/file/bot{self.token}/{quoted_path}"
        request = urllib.request.Request(url)
        with urllib.request.urlopen(request, timeout=60) as response:
            data = response.read()
        output_path.write_bytes(data)
        return output_path

    def with_emoji_prefix(self, text: str) -> str:
        if not self.emoji:
            return text
        first_line = text.splitlines()[0].strip() if text.splitlines() else ""
        if first_line in NODE_EMOJI_LINES:
            return text
        text = strip_inline_node_emoji_header(text)
        return f"{self.emoji}\n{text}"

    def chunks(self, text: str) -> list[str]:
        text = text or "(empty response)"
        text = self.with_emoji_prefix(text)
        out = [text[: self.chunk_size]]
        rest = text[self.chunk_size :]
        while rest:
            out.append(rest[: self.chunk_size])
            rest = rest[self.chunk_size :]
        return out

    def send(self, text: str) -> bool:
        ok = True
        for chunk in self.chunks(text):
            payload = self.call("sendMessage", chat_id=self.chat_id, text=chunk)
            ok = ok and bool(payload and payload.get("ok"))
        return ok

    def send_message_id(self, text: str) -> int | None:
        payload = self.call(
            "sendMessage",
            chat_id=self.chat_id,
            text=self.with_emoji_prefix(text),
        )
        result = payload.get("result") if isinstance(payload, dict) else None
        if isinstance(result, dict) and isinstance(result.get("message_id"), int):
            return int(result["message_id"])
        return None

    def edit(self, message_id: int, text: str) -> bool:
        payload = self.call(
            "editMessageText",
            chat_id=self.chat_id,
            message_id=message_id,
            text=self.with_emoji_prefix(text),
        )
        return bool(payload and payload.get("ok"))

    def send_local_attachment(self, path: Path, max_bytes: int) -> bool:
        try:
            size = path.stat().st_size
        except OSError:
            return False
        if size > max_bytes:
            log("ATTACH", f"skip {path}: file too large ({size} bytes)")
            return False

        suffix = path.suffix.lower()
        if suffix in {".jpg", ".jpeg", ".png", ".webp"} and size <= 10 * 1024 * 1024:
            payload = self.call_multipart("sendPhoto", {"chat_id": self.chat_id}, "photo", path)
        elif suffix in VIDEO_EXTENSIONS:
            payload = self.call_multipart("sendVideo", {"chat_id": self.chat_id}, "video", path)
            if not payload or not payload.get("ok"):
                payload = self.call_multipart(
                    "sendDocument",
                    {"chat_id": self.chat_id},
                    "document",
                    path,
                )
        elif suffix in VOICE_ATTACHMENT_EXTENSIONS:
            payload = self.call_multipart("sendVoice", {"chat_id": self.chat_id}, "voice", path)
            if not payload or not payload.get("ok"):
                payload = self.call_multipart(
                    "sendDocument",
                    {"chat_id": self.chat_id},
                    "document",
                    path,
                )
        elif suffix in AUDIO_EXTENSIONS:
            payload = self.call_multipart("sendAudio", {"chat_id": self.chat_id}, "audio", path)
            if not payload or not payload.get("ok"):
                payload = self.call_multipart(
                    "sendDocument",
                    {"chat_id": self.chat_id},
                    "document",
                    path,
                )
        else:
            payload = self.call_multipart("sendDocument", {"chat_id": self.chat_id}, "document", path)
        return bool(payload and payload.get("ok"))

    def send_approval_prompt(self, prompt: ApprovalRequest) -> int | None:
        buttons = [
            [
                {
                    "text": f"{option.number}. {option.short_label}",
                    "callback_data": (
                        f"{APPROVAL_CALLBACK_PREFIX}:{prompt.short_signature}:{option.number}"
                    ),
                }
            ]
            for option in prompt.options
        ]
        reply_markup = json.dumps({"inline_keyboard": buttons}, ensure_ascii=False)
        payload = self.call(
            "sendMessage",
            chat_id=self.chat_id,
            text=self.with_emoji_prefix(prompt.telegram_text()),
            reply_markup=reply_markup,
        )
        result = payload.get("result") if isinstance(payload, dict) else None
        if isinstance(result, dict) and isinstance(result.get("message_id"), int):
            return int(result["message_id"])
        return None

    def update_approval_prompt(
        self,
        message_id: int | None,
        prompt: ApprovalRequest,
        status_text: str,
        selected: ApprovalOption | None = None,
    ) -> None:
        if message_id is None:
            return
        if selected:
            buttons = [
                [
                    {
                        "text": f"✅ {selected.number}. {selected.short_label}",
                        "callback_data": f"{APPROVAL_CALLBACK_PREFIX}:done:{selected.number}",
                    }
                ]
            ]
        else:
            buttons = []
        self.call(
            "editMessageText",
            chat_id=self.chat_id,
            message_id=message_id,
            text=self.with_emoji_prefix(f"{prompt.telegram_text()}\n\n{status_text}"),
            reply_markup=json.dumps({"inline_keyboard": buttons}, ensure_ascii=False),
        )

    def send_choice_prompt(self, prompt: ChoicePrompt) -> int | None:
        buttons = [
            [
                {
                    "text": f"{option.value}. {option.short_label}",
                    "callback_data": (
                        f"{CHOICE_CALLBACK_PREFIX}:{prompt.short_signature}:{option.value}"
                    ),
                }
            ]
            for option in prompt.options
        ]
        reply_markup = json.dumps({"inline_keyboard": buttons}, ensure_ascii=False)
        payload = self.call(
            "sendMessage",
            chat_id=self.chat_id,
            text=self.with_emoji_prefix(prompt.telegram_text()),
            reply_markup=reply_markup,
        )
        result = payload.get("result") if isinstance(payload, dict) else None
        if isinstance(result, dict) and isinstance(result.get("message_id"), int):
            return int(result["message_id"])
        return None

    def update_choice_prompt(
        self,
        message_id: int | None,
        prompt: ChoicePrompt,
        status_text: str,
        selected: ChoiceOption | None = None,
    ) -> None:
        if message_id is None:
            return
        if selected:
            buttons = [
                [
                    {
                        "text": f"✅ {selected.value}. {selected.short_label}",
                        "callback_data": f"{CHOICE_CALLBACK_PREFIX}:done:{selected.value}",
                    }
                ]
            ]
        else:
            buttons = []
        self.call(
            "editMessageText",
            chat_id=self.chat_id,
            message_id=message_id,
            text=self.with_emoji_prefix(f"{prompt.telegram_text()}\n\n{status_text}"),
            reply_markup=json.dumps({"inline_keyboard": buttons}, ensure_ascii=False),
        )


class CodexRepl:
    def __init__(self, config: Config) -> None:
        self.config = config

    def tmux(self, *args: str, input_text: str | None = None) -> subprocess.CompletedProcess[str]:
        cmd = [self.config.tmux_bin, "-L", self.config.tmux_socket, *args]
        kwargs: dict[str, Any] = {
            "capture_output": True,
            "text": True,
            "timeout": 15,
        }
        if input_text is None:
            kwargs["stdin"] = subprocess.DEVNULL
        else:
            kwargs["input"] = input_text
        proc = subprocess.run(cmd, **kwargs)
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip()
            raise RuntimeError(f"tmux {' '.join(args)} failed: {detail}")
        return proc

    def verify(self) -> None:
        self.tmux("has-session", "-t", self.config.session_target)

    def pane_pid(self) -> int:
        out = self.tmux("display-message", "-p", "-t", self.config.pane_target, "#{pane_pid}")
        raw = out.stdout.strip()
        if not raw.isdigit():
            raise RuntimeError(f"could not resolve pane pid: {raw!r}")
        return int(raw)

    def paste_prompt(self, prompt: str) -> None:
        payload = prompt.rstrip("\n")
        if not payload:
            return
        self.verify()
        self.tmux("load-buffer", "-", input_text=payload)
        self.tmux("paste-buffer", "-p", "-t", self.config.pane_target)
        # Codex TUI uses Tab as the submit/queue key while a turn is running.
        # Repeated Enter can leave Telegram-origin text sitting in the composer.
        slash_command = is_codex_slash_command(payload)
        key = "Enter" if slash_command else self.config.submit_key
        count = 1 if slash_command else self.config.enter_count if key == "Enter" else 1
        for _ in range(count):
            self.tmux("send-keys", "-t", self.config.pane_target, key)
            time.sleep(0.3)

    def clear_composer(self) -> None:
        self.verify()
        # C-u only clears text before the cursor. After a rejected slash command,
        # Codex can leave the cursor before stale text, so clear both sides.
        for key in ("C-e", "C-u", "C-a", "C-k"):
            self.tmux("send-keys", "-t", self.config.pane_target, key)
            time.sleep(0.05)
        time.sleep(0.1)

    def capture_pane(self, lines: int = 80) -> str:
        out = self.tmux(
            "capture-pane",
            "-p",
            "-J",
            "-S",
            f"-{max(1, lines)}",
            "-t",
            self.config.pane_target,
        )
        return out.stdout

    def send_approval_key(self, key: str) -> None:
        normalized = key.strip().lower()
        if normalized in {"esc", "escape"}:
            self.tmux("send-keys", "-t", self.config.pane_target, "Escape")
            return
        if normalized in {"enter", "return"}:
            self.tmux("send-keys", "-t", self.config.pane_target, "Enter")
            return
        if not normalized:
            raise RuntimeError("empty approval key")
        self.tmux("send-keys", "-t", self.config.pane_target, normalized)
        time.sleep(0.1)
        self.tmux("send-keys", "-t", self.config.pane_target, "Enter")

    def send_choice_option(self, prompt: ChoicePrompt, option: ChoiceOption) -> None:
        if option.key:
            self.send_approval_key(option.key)
            return

        selected = prompt.selected_option()
        if selected is not None:
            delta = option.index - selected.index
            key = "Down" if delta > 0 else "Up"
            for _ in range(abs(delta)):
                self.tmux("send-keys", "-t", self.config.pane_target, key)
                time.sleep(0.05)
            self.tmux("send-keys", "-t", self.config.pane_target, "Enter")
            return

        self.send_approval_key(option.value)

    def session_file(self) -> Path:
        pid = self.pane_pid()
        path = session_file_from_descendants(pid)
        if path:
            return path
        path = newest_codex_tui_session()
        if path:
            return path
        raise RuntimeError("could not find active Codex TUI session JSONL")


def proc_ppid(pid: int) -> int | None:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    try:
        return int(stat.rsplit(") ", 1)[1].split()[1])
    except (IndexError, ValueError):
        return None


def descendants(root_pid: int) -> set[int]:
    ppids: dict[int, int] = {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        ppid = proc_ppid(pid)
        if ppid is not None:
            ppids[pid] = ppid

    result = {root_pid}
    changed = True
    while changed:
        changed = False
        for pid, ppid in ppids.items():
            if pid not in result and ppid in result:
                result.add(pid)
                changed = True
    return result


def session_file_from_descendants(root_pid: int) -> Path | None:
    if not Path("/proc").exists():
        return None
    candidates: list[Path] = []
    for pid in descendants(root_pid):
        fd_dir = Path(f"/proc/{pid}/fd")
        try:
            fds = list(fd_dir.iterdir())
        except OSError:
            continue
        for fd in fds:
            try:
                target = os.readlink(fd)
            except OSError:
                continue
            if "/.codex/sessions/" in target and target.endswith(".jsonl"):
                candidates.append(Path(target))
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime if p.exists() else 0)


def newest_codex_tui_session() -> Path | None:
    pattern = str(HOME / ".codex" / "sessions" / "*" / "*" / "*" / "rollout-*.jsonl")
    candidates = sorted((Path(p) for p in glob.glob(pattern)), key=lambda p: p.stat().st_mtime, reverse=True)
    for path in candidates[:20]:
        try:
            first = path.open(encoding="utf-8").readline()
            record = json.loads(first)
        except (OSError, json.JSONDecodeError):
            continue
        payload = record.get("payload") if isinstance(record.get("payload"), dict) else {}
        if payload.get("originator") == "codex-tui":
            return path
    return None


def normalize_prompt(text: str) -> str:
    return (text or "").replace("\r\n", "\n").strip()


def is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def normalize_local_path_candidate(raw: str) -> Path | None:
    value = raw.strip().strip("<>\"'")
    if not value:
        return None
    if value.startswith("file://"):
        value = urllib.parse.unquote(urllib.parse.urlparse(value).path)
    else:
        value = urllib.parse.unquote(value)
    if " " in value and not Path(value).exists():
        value = value.split(" ", 1)[0].strip()
    path = Path(value).expanduser()
    return path if path.is_absolute() else None


def extract_local_attachment_paths(
    text: str,
    roots: tuple[Path, ...],
    max_bytes: int,
) -> list[Path]:
    candidates: list[str] = []
    for match in MARKDOWN_LOCAL_PATH_RE.finditer(text or ""):
        candidates.append(match.group("path"))
    for match in RAW_LOCAL_ATTACHMENT_PATH_RE.finditer(text or ""):
        candidates.append(match.group("path"))

    out: list[Path] = []
    seen: set[Path] = set()
    resolved_roots = tuple(root.expanduser().resolve() for root in roots)
    for raw in candidates:
        path = normalize_local_path_candidate(raw)
        if path is None or path.suffix.lower() not in MEDIA_ATTACHMENT_EXTENSIONS:
            continue
        try:
            resolved = path.resolve()
            stat = resolved.stat()
        except OSError:
            continue
        if not resolved.is_file() or stat.st_size <= 0 or stat.st_size > max_bytes:
            continue
        if not any(is_relative_to(resolved, root) for root in resolved_roots):
            log("ATTACH", f"skip outside allowed roots: {resolved}")
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        out.append(resolved)
    return out


def extract_local_image_paths(
    text: str,
    roots: tuple[Path, ...],
    max_bytes: int,
) -> list[Path]:
    return [
        path
        for path in extract_local_attachment_paths(text, roots, max_bytes)
        if path.suffix.lower() in IMAGE_ATTACHMENT_EXTENSIONS
    ]


def strip_sent_attachment_references(text: str, attachments: list[Path]) -> str:
    if not attachments:
        return (text or "").strip()
    attachment_set = {path.resolve() for path in attachments}

    def is_sent_attachment(raw: str) -> bool:
        path = normalize_local_path_candidate(raw)
        if path is None:
            return False
        try:
            return path.resolve() in attachment_set
        except OSError:
            return False

    def replace_markdown(match: re.Match[str]) -> str:
        return "" if is_sent_attachment(match.group("path")) else match.group(0)

    def replace_raw_path(match: re.Match[str]) -> str:
        return "" if is_sent_attachment(match.group("path")) else match.group(0)

    cleaned = MARKDOWN_LOCAL_PATH_RE.sub(replace_markdown, text or "")
    cleaned = RAW_LOCAL_ATTACHMENT_PATH_RE.sub(replace_raw_path, cleaned)
    lines = []
    for line in cleaned.splitlines():
        line = re.sub(r"[ \t]{2,}", " ", line).strip()
        if line:
            lines.append(line)
    return "\n".join(lines).strip() or "파일을 첨부했어요."


def clean_pane_lines(screen: str) -> list[str]:
    return [ANSI_RE.sub("", line).rstrip() for line in screen.splitlines()]


def screen_has_repl_busy_marker(screen: str) -> bool:
    for line in clean_pane_lines(screen)[-20:]:
        cleaned = line.strip()
        if not cleaned:
            continue
        if "clear to save" in cleaned.lower():
            continue
        if REPL_BUSY_RE.search(cleaned):
            return True
    return False


@dataclass(frozen=True)
class ParsedScreenOption:
    value: str
    label: str
    key: str
    index: int
    line_index: int
    selected: bool = False


OPTION_LINE_RE = re.compile(
    r"^(?:(?:\[(?P<bracket>[A-Za-z0-9]{1,3})\])|(?P<value>[A-Za-z0-9]{1,3})[\.\)])\s+"
    r"(?P<label>.+?)\s*$"
)
CHOICE_HINT_RE = re.compile(
    r"\b("
    r"press\s+enter|enter\s+to\s+confirm|esc\s+to\s+cancel|escape\s+to\s+cancel|"
    r"use\s+(?:the\s+)?(?:arrow|up|down|↑|↓)|select(?:\s+one)?|choose(?:\s+one)?|"
    r"confirm\s+or\s+cancel"
    r")\b",
    re.IGNORECASE,
)
YES_NO_RE = re.compile(
    r"(?:\[(?P<bracket>[yYnN]/[yYnN])\]|\((?P<paren>y/n|n/y|yes/no|no/yes)\)|\b(?P<plain>y/n|n/y|yes/no|no/yes)\b)",
    re.IGNORECASE,
)
SHORTCUT_WORDS = {
    "y",
    "n",
    "yes",
    "no",
    "p",
    "a",
    "all",
    "always",
    "esc",
    "escape",
    "enter",
    "return",
    "tab",
    "space",
}


def is_choice_hint_line(line: str) -> bool:
    return bool(CHOICE_HINT_RE.search(line.strip()))


def split_trailing_shortcut(label: str, value: str) -> tuple[str, str]:
    match = re.search(r"\(([^()]+)\)\s*$", label)
    if not match:
        return label.strip(), ""
    raw = match.group(1).strip()
    lowered = raw.lower()
    if (
        lowered in SHORTCUT_WORDS
        or lowered == value.lower()
        or re.fullmatch(r"[A-Za-z0-9]", raw)
    ):
        return label[: match.start()].rstrip(), lowered
    return label.strip(), ""


def is_option_boundary(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return True
    if is_choice_hint_line(stripped):
        return True
    return stripped.startswith(("Reason:", "$ ", "Command:", "Would you like to run"))


def parse_screen_options(lines: list[str]) -> list[ParsedScreenOption]:
    options: list[ParsedScreenOption] = []
    current: dict[str, Any] | None = None

    def flush() -> None:
        nonlocal current
        if current is None:
            return
        raw_label = " ".join(part.strip() for part in current["label_parts"] if part.strip())
        label, key = split_trailing_shortcut(raw_label, str(current["value"]))
        options.append(
            ParsedScreenOption(
                value=str(current["value"]),
                label=label,
                key=key,
                index=len(options),
                line_index=int(current["line_index"]),
                selected=bool(current["selected"]),
            )
        )
        current = None

    for line_index, raw_line in enumerate(lines):
        stripped = raw_line.strip()
        selected = stripped.startswith(("›", ">"))
        normalized = stripped[1:].strip() if selected else stripped
        match = OPTION_LINE_RE.match(normalized)
        if match:
            flush()
            value = match.group("bracket") or match.group("value") or ""
            current = {
                "value": value,
                "label_parts": [match.group("label")],
                "line_index": line_index,
                "selected": selected,
            }
            continue
        if current is not None and not is_option_boundary(stripped):
            current["label_parts"].append(stripped)
            continue
        flush()

    flush()
    return options


def approval_fallback_key(option: ParsedScreenOption) -> str:
    label = option.label.lower()
    if "don't ask" in label or "do not ask" in label:
        return "p"
    if label.startswith("yes") or "proceed" in label:
        return "y"
    if label.startswith("no"):
        return "esc"
    return option.value.lower()


def parse_approval_prompt(screen: str, ttl_seconds: int = 300) -> ApprovalRequest | None:
    lines = clean_pane_lines(screen)
    trigger_index = -1
    for index, line in enumerate(lines):
        if "Would you like to run the following command?" in line:
            trigger_index = index
            break
    if trigger_index < 0:
        return None

    window = lines[trigger_index : trigger_index + 24]
    reason = ""
    command = ""
    parsed_options = parse_screen_options(window)

    for line in window:
        stripped = line.strip()
        if stripped.startswith("Reason:"):
            reason = stripped.removeprefix("Reason:").strip()
            continue
        if stripped.startswith("$ "):
            command = stripped
            continue

    options = [
        ApprovalOption(
            number=option.value,
            label=option.label,
            key=option.key or approval_fallback_key(option),
        )
        for option in parsed_options
        if option.value.isdigit()
    ]
    if len(options) < 2:
        return None

    signature_source = "\n".join(
        [
            command,
            reason,
            *[f"{option.number}:{option.label}:{option.key}" for option in options],
        ]
    ).strip()
    signature = hashlib.sha256(signature_source.encode("utf-8", errors="replace")).hexdigest()
    return ApprovalRequest.create(
        approval_id=signature,
        source_head="codex_repl",
        command=command,
        reason=reason,
        options=tuple(options),
        ttl_seconds=ttl_seconds,
    )


def parse_yes_no_choice_prompt(lines: list[str], ttl_seconds: int = 300) -> ChoicePrompt | None:
    for line in reversed(lines[-24:]):
        stripped = line.strip()
        if not stripped or not ("?" in stripped or is_choice_hint_line(stripped)):
            continue
        match = YES_NO_RE.search(stripped)
        if not match:
            continue
        raw = match.group("bracket") or match.group("paren") or match.group("plain") or "y/n"
        first, second = [part.strip() for part in raw.split("/", 1)]
        options: list[ChoiceOption] = []
        for value in (first, second):
            lowered = value.lower()
            label = "Yes" if lowered.startswith("y") else "No"
            options.append(
                ChoiceOption(
                    value=lowered[0],
                    label=label,
                    key=lowered[0],
                    index=len(options),
                    selected=value.isupper(),
                )
            )
        signature_source = "\n".join(
            [
                stripped,
                *[f"{option.value}:{option.label}:{option.key}" for option in options],
            ]
        )
        signature = hashlib.sha256(signature_source.encode("utf-8", errors="replace")).hexdigest()
        return ChoicePrompt.create(
            choice_id=signature,
            source_head="codex_repl",
            title=stripped,
            options=tuple(options),
            ttl_seconds=ttl_seconds,
        )
    return None


def parse_choice_prompt(screen: str, ttl_seconds: int = 300) -> ChoicePrompt | None:
    if parse_approval_prompt(screen, ttl_seconds=ttl_seconds):
        return None

    lines = clean_pane_lines(screen)
    tail_start = max(0, len(lines) - 40)
    tail = lines[tail_start:]
    yes_no = parse_yes_no_choice_prompt(tail, ttl_seconds=ttl_seconds)
    if yes_no:
        return yes_no

    parsed_options = parse_screen_options(tail)
    if len(parsed_options) < 2:
        return None
    has_selected = any(option.selected for option in parsed_options)
    has_hint = any(is_choice_hint_line(line) for line in tail)
    if not has_selected and not has_hint:
        return None

    first_line_index = min(option.line_index for option in parsed_options)
    title_candidates = [line.strip() for line in tail[:first_line_index] if line.strip()]
    title = title_candidates[-1] if title_candidates else "Select one option"
    options = tuple(
        ChoiceOption(
            value=option.value,
            label=option.label,
            key=option.key,
            index=option.index,
            selected=option.selected,
        )
        for option in parsed_options[:12]
    )
    signature_source = "\n".join(
        [
            title,
            *[
                f"{option.value}:{option.label}:{option.key}:{int(option.selected)}"
                for option in options
            ],
        ]
    )
    signature = hashlib.sha256(signature_source.encode("utf-8", errors="replace")).hexdigest()
    return ChoicePrompt.create(
        choice_id=signature,
        source_head="codex_repl",
        title=title,
        options=options,
        ttl_seconds=ttl_seconds,
    )


def extract_event(record: dict[str, Any]) -> tuple[str, str] | None:
    kind = record.get("type")
    payload = record.get("payload") if isinstance(record.get("payload"), dict) else {}

    if kind == "event_msg":
        payload_type = payload.get("type")
        if payload_type == "user_message":
            return "user", str(payload.get("message") or "")
        if payload_type == "agent_message":
            message = str(payload.get("message") or "")
            if payload.get("phase") == "final_answer":
                return "assistant", message
            if is_copy_payload_message(message):
                return "copy_payload", message
            return "progress", message

    if kind == "response_item":
        if payload.get("type") == "message" and payload.get("role") == "assistant":
            phase = payload.get("phase") or payload.get("metadata", {}).get("phase")
            content = payload.get("content")
            parts = []
            if isinstance(content, list):
                for item in content:
                    if isinstance(item, dict) and item.get("type") == "output_text":
                        parts.append(str(item.get("text") or ""))
            text = "\n".join(parts)
            if phase != "final_answer":
                if text:
                    if is_copy_payload_message(text):
                        return "copy_payload", text
                    return "progress", text
                return None
            return "assistant", text
    return None


def parse_exec_answer(stdout: str) -> str:
    answer = ""
    for line in (stdout or "").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue

        item = event.get("item") if isinstance(event.get("item"), dict) else {}
        if item.get("type") == "agent_message" and item.get("text"):
            answer = str(item["text"])
            continue

        event_from_record = extract_event(event)
        if event_from_record and event_from_record[0] == "assistant":
            answer = event_from_record[1]
    return answer.strip()


class CodexExecError(RuntimeError):
    pass


@dataclass(frozen=True)
class TelegramPrompt:
    text: str
    image_path: Path | None = None
    kind: str = "text"


@dataclass(frozen=True)
class ChoiceOption:
    value: str
    label: str
    key: str = ""
    index: int = 0
    selected: bool = False

    @property
    def short_label(self) -> str:
        label = self.label.strip()
        return label[:40] or self.value


@dataclass(frozen=True)
class ChoicePrompt:
    choice_id: str
    source_head: str
    title: str
    options: tuple[ChoiceOption, ...]
    expires_at: float
    cancelled: bool = False

    @classmethod
    def create(
        cls,
        choice_id: str,
        source_head: str,
        title: str,
        options: tuple[ChoiceOption, ...],
        ttl_seconds: int,
        now: float | None = None,
    ) -> "ChoicePrompt":
        base = time.time() if now is None else now
        return cls(
            choice_id=choice_id,
            source_head=source_head,
            title=title,
            options=options,
            expires_at=base + max(1, ttl_seconds),
        )

    @property
    def short_signature(self) -> str:
        return self.choice_id[:16]

    def is_expired(self, now: float | None = None) -> bool:
        current = time.time() if now is None else now
        return current >= self.expires_at

    def is_active(self, now: float | None = None) -> bool:
        return not self.cancelled and not self.is_expired(now)

    def cancel(self) -> "ChoicePrompt":
        return replace(self, cancelled=True)

    def option(self, value: str) -> ChoiceOption | None:
        lowered = value.strip().lower()
        for item in self.options:
            if item.value.lower() == lowered or item.key.lower() == lowered:
                return item
        return None

    def selected_option(self) -> ChoiceOption | None:
        return next((item for item in self.options if item.selected), None)

    def telegram_text(self) -> str:
        lines = [f"{self.source_head} is waiting for a selection.", ""]
        if self.title:
            lines.extend(["Prompt:", self.title[:600], ""])
        lines.append("Choose a button, or reply with the visible number/letter/shortcut.")
        return "\n".join(lines)


class Bridge:
    def __init__(self, config: Config, telegram: TelegramClient, repl: CodexRepl) -> None:
        self.config = config
        self.telegram = telegram
        self.repl = repl
        self.head = CodexReplAdapter(repl, workdir=config.workdir)
        self.capabilities = CapabilityRegistry()
        self.capabilities.register(self.head.capabilities())
        self.lock = threading.Lock()
        self.exec_lock = threading.Lock()
        self.typing_lock = threading.Lock()
        self.long_running_progress_lock = threading.Lock()
        self.telegram_fallback_lock = threading.Lock()
        self.approval_lock = threading.Lock()
        self.choice_lock = threading.Lock()
        self.repl_typing_stop: threading.Event | None = None
        self.long_running_progress_stop: threading.Event | None = None
        self.telegram_fallback_stop: threading.Event | None = None
        self.pending_telegram: list[str] = []
        self.pending_approval: ApprovalRequest | None = None
        self.pending_approval_message_id: int | None = None
        self.resolved_approval_ids: set[str] = set()
        self.pending_choice: ChoicePrompt | None = None
        self.pending_choice_message_id: int | None = None
        self.resolved_choice_ids: set[str] = set()
        self.stop_event = threading.Event()
        self.current_origin: str | None = None
        self.current_flow_scope = ""
        self.suppress_until_user = False
        self.needs_composer_clear = False
        self.active_telegram_prompt = ""
        self.last_public_progress: str = ""
        # ⚙️ flow mirror edit-in-place state: accumulate a turn's steps into ONE
        # growing card (edit the same message) instead of one message per step.
        self.flow_message_id = 0
        self.flow_body = ""
        self.flow_scope = ""
        self.flow_last_edit_at = 0.0
        self.telegram_fallback_sent = False
        self.session_path: Path | None = None
        self.session_identity: SessionIdentity | None = None
        self.bridge_state: dict[str, Any] | None = None
        self.session_pos = 0

    def acquire_lock(self) -> None:
        self.config.state_dir.mkdir(parents=True, exist_ok=True)
        existing = read_text(self.config.pid_file)
        if existing.isdigit() and int(existing) != os.getpid() and process_alive(int(existing)):
            raise RuntimeError(f"bridge already running pid={existing}")
        write_text_atomic(self.config.pid_file, os.getpid())

    def release_lock(self) -> None:
        if read_text(self.config.pid_file) == str(os.getpid()):
            try:
                self.config.pid_file.unlink()
            except OSError:
                pass

    def add_pending_telegram(self, prompt: str) -> None:
        with self.lock:
            self.pending_telegram.append(normalize_prompt(prompt))
            self.pending_telegram = self.pending_telegram[-20:]

    def consume_pending_match(self, prompt: str) -> bool:
        normalized = normalize_prompt(prompt)
        with self.lock:
            for index, item in enumerate(self.pending_telegram):
                if item == normalized:
                    del self.pending_telegram[index]
                    return True
        return False

    def handle_user_event(self, text: str) -> None:
        self.suppress_until_user = False
        self.last_public_progress = ""
        self.current_flow_scope = normalize_prompt(text)
        if self.consume_pending_match(text):
            self.current_origin = "telegram"
            log("JSONL", "matched Telegram-origin prompt")
            self.begin_repl_typing()
        else:
            self.current_origin = "terminal"
            self.stop_telegram_fallback()
            log("JSONL", "terminal-origin prompt")
            self.begin_repl_typing()

    def handle_progress_event(self, text: str, key: str = "") -> bool:
        summary = format_progress_summary(text, 220)
        if not summary:
            return True
        with self.lock:
            self.last_public_progress = summary
        if not self.config.flow_mirror:
            return True
        flow_key = flow_mirror_dedup_key(
            text,
            self.active_telegram_prompt or self.current_flow_scope,
        )
        dedup_key = flow_key or key
        if self.bridge_state and ring_contains(self.bridge_state, dedup_key):
            log("SEND", "skip duplicate flow mirror")
            return True
        # Collapse each flow event to a single short line (claude parity). Full
        # narration was overflowing FLOW_MIRROR_LIMIT after 1-2 steps and
        # spilling into multiple long messages instead of one growing card.
        summary = flow_step_summary(text)
        if not summary:
            return True
        if self.config.bridge_kill:
            log("SEND", "flow mirror blocked by CRB_KILL=1")
            return True
        # Edit-in-place: accumulate this turn's steps into ONE growing card
        # (edit the same message) until it overflows FLOW_MIRROR_LIMIT.
        scope = self.active_telegram_prompt or self.current_flow_scope
        if self.flow_scope != scope:
            self.reset_flow_card()
            self.flow_scope = scope
        candidate = f"{self.flow_body}\n{summary}".strip() if self.flow_body else summary
        if not self.flow_message_id or len(candidate) > FLOW_MIRROR_LIMIT:
            message_id = self.telegram.send_message_id(format_flow_mirror(summary))
            if not message_id:
                log("SEND", "flow mirror failed")
                return False
            self.flow_body = summary
            self.flow_message_id = message_id
            log("SEND", f"sent flow mirror mid={message_id}")
        else:
            self.wait_for_flow_edit_budget()
            if not self.telegram.edit(self.flow_message_id, format_flow_mirror(candidate)):
                log("SEND", "flow mirror edit failed")
                return False
            self.flow_body = candidate
            self.flow_last_edit_at = time.monotonic()
            log("SEND", f"edited flow mirror mid={self.flow_message_id}")
        self.persist_state(event_key=dedup_key)
        return True

    def reset_flow_card(self) -> None:
        self.flow_message_id = 0
        self.flow_body = ""
        self.flow_scope = ""
        self.flow_last_edit_at = 0.0

    def wait_for_flow_edit_budget(self) -> None:
        if self.flow_last_edit_at <= 0:
            return
        elapsed = time.monotonic() - self.flow_last_edit_at
        remaining = FLOW_MIRROR_EDIT_MIN_SECONDS - elapsed
        if remaining > 0:
            time.sleep(remaining)

    def handle_copy_payload_event(self, text: str) -> bool:
        messages = split_copy_payload_messages(text)
        if not messages:
            return True
        for message in messages:
            key = self.copy_payload_dedup_key_for_turn(message)
            if key and self.bridge_state and ring_contains(self.bridge_state, key):
                log("SEND", "skip duplicate copy payload part")
                self.record_copy_payload_pair_part(message)
                continue
            log("SEND", "Telegram copy payload from commentary")
            ok = self.send_answer(message)
            if not ok:
                return False
            self.mark_telegram_fallback_sent()
            self.record_copy_payload_pair_part(message)
            if key:
                self.persist_state(event_key=key)
        return True

    def handle_assistant_event(self, text: str) -> bool:
        self.stop_repl_typing()
        self.stop_long_running_progress()
        self.stop_telegram_fallback()
        if self.suppress_until_user:
            return True
        answer = (text or "").strip()
        if not answer:
            return True
        origin = self.current_origin or "terminal"
        log("SEND", f"Telegram mirror from {origin}")
        if is_copy_payload_message(answer):
            if not self.handle_copy_payload_event(answer):
                return False
        else:
            if not self.send_answer(answer):
                return False
        if self.request_incomplete_copy_payload_pair_repair_if_needed():
            return True
        self.warn_incomplete_copy_payload_pair_if_needed()
        self.clear_active_telegram_prompt()
        self.current_origin = None
        self.current_flow_scope = ""
        self.suppress_until_user = True
        return True

    def send_answer(self, answer: str) -> bool:
        if self.config.bridge_kill:
            log("SEND", "blocked by CRB_KILL=1")
            return False
        attachments = extract_local_attachment_paths(
            answer,
            self.config.attachment_roots,
            self.config.max_attachment_bytes,
        )
        ok = self.telegram.send(strip_sent_attachment_references(answer, attachments))
        if not ok:
            return False
        for path in attachments:
            log("ATTACH", f"send {path}")
            if not self.telegram.send_local_attachment(path, self.config.max_attachment_bytes):
                log("ATTACH", f"send failed: {path}")
                ok = False
        return ok

    def persist_state(self, offset: int | None = None, event_key: str | None = None) -> None:
        identity = self.session_identity
        if identity is None:
            return
        state = self.bridge_state or bridge_state_default(identity, self.session_pos)
        state["session_path"] = identity.path
        state["dev"] = identity.dev
        state["ino"] = identity.ino
        if offset is not None:
            state["offset"] = offset
        if event_key:
            ring_push(state, event_key, self.config.state_ring_cap)
        write_json_atomic(self.config.state_path, state)
        self.bridge_state = state

    def set_copy_payload_pair_contract(self, prompt: str) -> None:
        contract = copy_payload_pair_contract(prompt)
        with self.lock:
            if self.bridge_state is None:
                return
            if contract:
                self.bridge_state["copy_payload_pair_contract"] = contract
                self.bridge_state.pop("last_copy_payload_pair_missing", None)
            else:
                self.bridge_state.pop("copy_payload_pair_contract", None)
        self.persist_state()

    def copy_payload_pair_contract_state(self) -> dict[str, Any] | None:
        with self.lock:
            if self.bridge_state is None:
                return None
            contract = self.bridge_state.get("copy_payload_pair_contract")
            return contract if isinstance(contract, dict) else None

    def record_copy_payload_pair_part(self, message: str) -> None:
        part = copy_payload_kind(message)
        if not part:
            return
        with self.lock:
            if self.bridge_state is None:
                return
            contract = self.bridge_state.get("copy_payload_pair_contract")
            if not isinstance(contract, dict):
                return
            sent = contract.get("sent")
            sent_list = [str(item) for item in sent] if isinstance(sent, list) else []
            if part not in sent_list:
                sent_list.append(part)
            contract["sent"] = sent_list
            self.bridge_state["copy_payload_pair_contract"] = contract
        self.persist_state()

    def request_incomplete_copy_payload_pair_repair_if_needed(self) -> bool:
        contract = self.copy_payload_pair_contract_state()
        if not contract:
            return False
        missing = copy_payload_pair_missing(contract)
        if not missing:
            with self.lock:
                if self.bridge_state is not None:
                    self.bridge_state.pop("copy_payload_pair_contract", None)
                    self.bridge_state.pop("last_copy_payload_pair_missing", None)
            self.persist_state()
            return False
        if contract.get("repair_requested"):
            return False
        if self.config.bridge_kill:
            log("SEND", "copy payload pair repair blocked by CRB_KILL=1")
            return False
        repair_prompt = copy_payload_pair_repair_prompt(missing)
        try:
            self.repl.paste_prompt(repair_prompt)
        except Exception as exc:  # noqa: BLE001
            log("SEND", f"copy payload pair repair inject failed: {exc}")
            return False
        with self.lock:
            if self.bridge_state is None:
                return True
            contract = self.bridge_state.get("copy_payload_pair_contract")
            if isinstance(contract, dict):
                contract["repair_requested"] = True
                contract["repair_missing"] = missing
                self.bridge_state["copy_payload_pair_contract"] = contract
                self.bridge_state["last_copy_payload_pair_missing"] = {
                    "missing": missing,
                    "prompt_sha256": str(contract.get("prompt_sha256") or ""),
                    "ts": int(time.time()),
                    "auto_repair_requested": True,
                }
        self.persist_state()
        return True

    def warn_incomplete_copy_payload_pair_if_needed(self) -> None:
        contract = self.copy_payload_pair_contract_state()
        if not contract:
            return
        missing = copy_payload_pair_missing(contract)
        if not missing:
            with self.lock:
                if self.bridge_state is not None:
                    self.bridge_state.pop("copy_payload_pair_contract", None)
                    self.bridge_state.pop("last_copy_payload_pair_missing", None)
            self.persist_state()
            return
        if contract.get("notified_missing"):
            return
        warning = copy_payload_pair_missing_warning(missing)
        if self.config.bridge_kill:
            log("SEND", "copy payload pair warning blocked by CRB_KILL=1")
            return
        if not self.telegram.send(warning):
            log("SEND", "copy payload pair warning failed")
            return
        with self.lock:
            if self.bridge_state is None:
                return
            contract = self.bridge_state.get("copy_payload_pair_contract")
            if isinstance(contract, dict):
                contract["notified_missing"] = True
                self.bridge_state["copy_payload_pair_contract"] = contract
                self.bridge_state["last_copy_payload_pair_missing"] = {
                    "missing": missing,
                    "prompt_sha256": str(contract.get("prompt_sha256") or ""),
                    "ts": int(time.time()),
                }
        self.persist_state()

    def process_line(
        self,
        line: str,
        line_start: int | None = None,
        line_end: int | None = None,
    ) -> bool:
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            return True
        event = extract_event(record)
        if not event:
            if line_end is not None:
                self.persist_state(line_end)
            return True
        kind, text = event
        if kind == "user":
            self.handle_user_event(text)
            if line_end is not None:
                self.persist_state(line_end)
            return True
        elif kind == "progress":
            key = event_dedup_key(record, kind, text)
            if not self.handle_progress_event(text, key):
                return False
            if line_end is not None:
                self.persist_state(line_end)
            return True
        elif kind == "copy_payload":
            key = self.copy_payload_dedup_key_for_turn(text) or event_dedup_key(record, kind, text)
            if self.bridge_state and ring_contains(self.bridge_state, key):
                log("SEND", "skip duplicate copy payload")
                if line_end is not None:
                    self.persist_state(line_end)
                return True
            if not self.handle_copy_payload_event(text):
                return False
            if line_end is not None:
                self.persist_state(line_end, key)
            return True
        elif kind == "assistant":
            key = event_dedup_key(record, kind, text)
            copy_key = self.copy_payload_dedup_key_for_turn(text) if is_copy_payload_message(text) else ""
            if copy_key and self.bridge_state and ring_contains(self.bridge_state, copy_key):
                log("SEND", "skip duplicate copy payload final_answer")
                self.stop_repl_typing()
                self.stop_long_running_progress()
                self.stop_telegram_fallback()
                for message in split_copy_payload_messages(text):
                    self.record_copy_payload_pair_part(message)
                if self.request_incomplete_copy_payload_pair_repair_if_needed():
                    if line_end is not None:
                        self.persist_state(line_end)
                    return True
                self.warn_incomplete_copy_payload_pair_if_needed()
                self.clear_active_telegram_prompt()
                if line_end is not None:
                    self.persist_state(line_end)
                return True
            if self.bridge_state and ring_contains(self.bridge_state, key):
                log("SEND", "skip duplicate final_answer")
                self.stop_repl_typing()
                self.stop_long_running_progress()
                self.stop_telegram_fallback()
                self.clear_active_telegram_prompt()
                if line_end is not None:
                    self.persist_state(line_end)
                return True
            if not self.handle_assistant_event(text):
                return False
            if copy_key:
                self.persist_state(event_key=copy_key)
            if line_end is not None:
                self.persist_state(line_end, key)
            return True
        if line_end is not None:
            self.persist_state(line_end)
        return True

    def ensure_session_file(self) -> Path:
        path = self.head.session_file()
        if self.session_path != path:
            identity = session_identity(path)
            state = read_json(self.config.state_path)
            cursor = cursor_offset_for_state(state, identity)
            self.session_path = path
            self.session_identity = identity
            if state:
                state["coord_ring"] = ring_values(state)
            self.bridge_state = state or bridge_state_default(identity)
            with self.lock:
                self.active_telegram_prompt = str(
                    self.bridge_state.get("active_telegram_prompt") or ""
                )
            if cursor is not None:
                self.session_pos = cursor
                log("REPL", f"watching {path} from cursor {cursor}")
            else:
                self.session_pos = identity.size if self.config.start_at_end else 0
                log("REPL", f"watching {path}")
                if self.config.start_at_end:
                    self.backfill_cursorless_session(path, identity)
                self.persist_state(self.session_pos)
        return path

    def backfill_cursorless_session(self, path: Path, identity: SessionIdentity) -> None:
        if not self.config.backfill_enabled:
            log("BACKF", "disabled")
            return
        try:
            events = read_tail_jsonl_events(path, self.config.tail_scan_bytes)
        except OSError as exc:
            log("BACKF", f"scan failed: {exc}")
            return
        state = self.bridge_state or bridge_state_default(identity)
        candidates = eligible_backfill_events(
            events,
            state,
            time.time(),
            self.config.backfill_window_sec,
            self.config.backfill_max,
        )
        if not candidates:
            log("BACKF", "no eligible final_answer")
            return
        for event in candidates:
            copy_key = copy_payload_dedup_key(event.text) if is_copy_payload_message(event.text) else ""
            if copy_key and ring_contains(state, copy_key):
                log("BACKF", "skip duplicate copy payload final_answer")
                continue
            if ring_contains(state, event.key):
                continue
            log("BACKF", f"send final_answer at offset {event.start}")
            if not self.send_answer(event.text):
                self.session_pos = event.start
                log("BACKF", "send failed; retry from candidate offset")
                return
            if copy_key:
                ring_push(state, copy_key, self.config.state_ring_cap)
            ring_push(state, event.key, self.config.state_ring_cap)
        self.bridge_state = state
        self.session_pos = identity.size

    def jsonl_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                path = self.ensure_session_file()
                with path.open("r", encoding="utf-8", errors="replace") as f:
                    f.seek(self.session_pos)
                    while True:
                        line_start = f.tell()
                        line = f.readline()
                        if not line:
                            break
                        line_end = f.tell()
                        if not self.process_line(line, line_start, line_end):
                            self.session_pos = line_start
                            break
                        self.session_pos = line_end
            except Exception as exc:  # noqa: BLE001
                log("JSONL", f"watch error: {exc}")
            time.sleep(0.5)

    def start_typing_loop(self, max_seconds: int | None = None) -> threading.Event:
        stop_event = threading.Event()

        def loop() -> None:
            deadline = time.monotonic() + max_seconds if max_seconds else None
            while not stop_event.is_set():
                if deadline is not None and time.monotonic() >= deadline:
                    break
                self.telegram.send_typing()
                wait_seconds = 4.0
                if deadline is not None:
                    wait_seconds = min(wait_seconds, max(0.0, deadline - time.monotonic()))
                if wait_seconds <= 0:
                    break
                stop_event.wait(wait_seconds)

        threading.Thread(target=loop, daemon=True, name="crb-typing").start()
        return stop_event

    def has_repl_typing(self) -> bool:
        with self.typing_lock:
            return self.repl_typing_stop is not None

    def has_long_running_progress(self) -> bool:
        with self.long_running_progress_lock:
            return self.long_running_progress_stop is not None

    def begin_repl_typing(self) -> None:
        with self.typing_lock:
            if self.repl_typing_stop:
                self.repl_typing_stop.set()
                log("TYPE", "restart")
            else:
                log("TYPE", "start")
            self.repl_typing_stop = self.start_typing_loop(self.config.typing_max_seconds)

    def stop_repl_typing(self) -> None:
        with self.typing_lock:
            if self.repl_typing_stop:
                self.repl_typing_stop.set()
                self.repl_typing_stop = None
                log("TYPE", "stop")

    def set_active_telegram_prompt(self, prompt: str) -> None:
        prompt = normalize_prompt(prompt)
        with self.lock:
            self.active_telegram_prompt = prompt
            if self.bridge_state is not None:
                if prompt:
                    self.bridge_state["active_telegram_prompt"] = prompt
                else:
                    self.bridge_state.pop("active_telegram_prompt", None)
        self.persist_state()

    def clear_active_telegram_prompt(self) -> None:
        self.set_active_telegram_prompt("")

    def active_prompt_for_recovery(self) -> str:
        with self.lock:
            prompt = self.active_telegram_prompt
            if not prompt and self.bridge_state is not None:
                prompt = str(self.bridge_state.get("active_telegram_prompt") or "")
        return normalize_prompt(prompt)

    def copy_payload_dedup_key_for_turn(self, text: str) -> str:
        return copy_payload_dedup_key(text, self.active_prompt_for_recovery())

    def begin_telegram_prompt_tracking(self, prompt: str) -> None:
        self.add_pending_telegram(prompt)
        with self.lock:
            self.current_origin = "telegram"
            self.suppress_until_user = False
            self.last_public_progress = ""
            self.telegram_fallback_sent = False
        self.set_active_telegram_prompt(prompt)
        self.set_copy_payload_pair_contract(prompt)
        self.start_telegram_fallback(prompt)
        self.start_long_running_progress(prompt)

    def start_long_running_progress(self, prompt: str) -> None:
        self.stop_long_running_progress()
        interval = int(getattr(self.config, "long_running_progress_seconds", 0) or 0)
        if interval <= 0:
            return
        stop_event = threading.Event()
        started = time.monotonic()

        def loop() -> None:
            while not stop_event.wait(interval):
                elapsed = time.monotonic() - started
                log("PROG", f"send long-running update after {int(elapsed)}s")
                if not self.telegram.send(self.long_running_progress_message(prompt, elapsed)):
                    log("PROG", "send failed")

        with self.long_running_progress_lock:
            self.long_running_progress_stop = stop_event
        threading.Thread(target=loop, daemon=True, name="crb-long-progress").start()

    def start_telegram_fallback(self, prompt: str) -> None:
        self.stop_telegram_fallback()
        interval = int(getattr(self.config, "telegram_fallback_seconds", 0) or 0)
        if interval <= 0:
            return
        stop_event = threading.Event()

        def loop() -> None:
            if stop_event.wait(interval):
                return
            self.send_telegram_fallback(prompt, float(interval))

        with self.telegram_fallback_lock:
            self.telegram_fallback_stop = stop_event
        threading.Thread(target=loop, daemon=True, name="crb-telegram-fallback").start()

    def mark_telegram_fallback_sent(self) -> None:
        with self.lock:
            self.telegram_fallback_sent = True
        self.stop_telegram_fallback()

    def send_telegram_fallback(self, prompt: str, elapsed_seconds: float) -> None:
        prompt = normalize_prompt(prompt)
        with self.lock:
            active = normalize_prompt(self.active_telegram_prompt)
            if (
                self.telegram_fallback_sent
                or self.suppress_until_user
                or self.current_origin != "telegram"
                or not active
                or active != prompt
            ):
                return
            self.telegram_fallback_sent = True
        if self.config.bridge_kill:
            log("PROG", "telegram fallback blocked by CRB_KILL=1")
            return
        log("PROG", f"send telegram fallback after {int(elapsed_seconds)}s")
        if not self.telegram.send(self.long_running_progress_message(prompt, elapsed_seconds)):
            log("PROG", "telegram fallback send failed")

    def current_task_id(self, prompt: str) -> str:
        task_id = extract_task_id(prompt)
        if task_id:
            return task_id
        state_task = read_text(self.config.state_dir / "current-task").splitlines()
        if not state_task:
            return ""
        candidate = state_task[0].strip()
        return candidate if TASK_ID_RE.fullmatch(candidate) else ""

    def long_running_progress_message(self, prompt: str, elapsed_seconds: float) -> str:
        with self.lock:
            recent_progress = self.last_public_progress
        return format_long_running_progress_message(
            prompt,
            elapsed_seconds,
            task_id=self.current_task_id(prompt),
            recent_progress=recent_progress,
        )

    def stop_long_running_progress(self) -> None:
        with self.long_running_progress_lock:
            stop_event = self.long_running_progress_stop
            self.long_running_progress_stop = None
        if stop_event:
            stop_event.set()
            log("PROG", "stop")

    def stop_telegram_fallback(self) -> None:
        with self.telegram_fallback_lock:
            stop_event = self.telegram_fallback_stop
            self.telegram_fallback_stop = None
        if stop_event:
            stop_event.set()

    def repl_is_working(self) -> bool:
        try:
            screen = self.head.capture_pane(60)
        except Exception as exc:  # noqa: BLE001
            log("LIVE", f"pane capture failed: {exc}")
            return False
        return screen_has_repl_busy_marker(screen)

    def recover_repl_liveness(self, reason: str = "poll") -> bool:
        if not self.repl_is_working():
            return False
        recovered = False
        if not self.has_repl_typing():
            log("LIVE", f"recover typing ({reason})")
            self.begin_repl_typing()
            recovered = True
        prompt = self.active_prompt_for_recovery()
        if prompt and not self.has_long_running_progress():
            log("LIVE", f"recover progress ({reason})")
            self.start_long_running_progress(prompt)
            recovered = True
        return recovered

    def liveness_loop(self) -> None:
        interval = int(getattr(self.config, "typing_liveness_seconds", 0) or 0)
        if interval <= 0:
            return
        while not self.stop_event.wait(interval):
            self.recover_repl_liveness()

    def handle_status_command(self, text: str) -> bool:
        context_only = is_context_command(text)
        if not (context_only or is_status_command(text)):
            return False
        label = "context" if context_only else "status"
        log("TG", f"{label} -> Codex REPL")
        self.begin_repl_typing()
        try:
            self.clear_composer_before_telegram_input(label)
            self.head.send(AgentMessage("/status"))
            time.sleep(1.0)
            screen = self.head.capture_pane(120)
            answer = extract_codex_context_text(screen) if context_only else extract_codex_status_text(screen)
            self.telegram.send(answer)
        except Exception as exc:  # noqa: BLE001
            log("REPL", f"{label} failed: {exc}")
            self.telegram.send(f"codex {label} failed: {exc}")
        finally:
            self.stop_repl_typing()
        return True

    def clear_composer_before_telegram_input(self, label: str = "telegram input") -> None:
        try:
            self.head.clear_composer()
            log("REPL", f"cleared composer before {label}")
        except Exception as exc:  # noqa: BLE001
            log("REPL", f"composer clear failed before {label}: {exc}")
        finally:
            self.needs_composer_clear = False

    def clear_stale_composer_if_needed(self) -> None:
        if not self.needs_composer_clear:
            return
        self.clear_composer_before_telegram_input("stale slash command")

    def handle_slash_command_result(self, text: str) -> bool:
        command = slash_command_token(text)
        if not command:
            return False
        screen = self.head.capture_pane(120)
        error = extract_unrecognized_slash_error(screen, command)
        if error:
            self.needs_composer_clear = True
            log("REPL", f"slash command error: {error}")
            self.telegram.send(
                f"Codex slash command error\n{error}\n\n"
                "터미널 입력줄도 바로 비웠습니다."
            )
            self.clear_composer_before_telegram_input("slash command error")
            return True
        return False

    def approval_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                screen = self.head.capture_pane()
                prompt = parse_approval_prompt(
                    screen,
                    ttl_seconds=self.config.approval_ttl_seconds,
                )
                choice_prompt = None if prompt else parse_choice_prompt(
                    screen,
                    ttl_seconds=self.config.approval_ttl_seconds,
                )
                should_send = False
                if prompt:
                    with self.choice_lock:
                        choice_to_clear = self.pending_choice
                        choice_message_id = self.pending_choice_message_id
                        if choice_to_clear is not None:
                            log("CHOICE", "choice prompt cleared by approval")
                            if choice_to_clear.choice_id not in self.resolved_choice_ids:
                                self.telegram.update_choice_prompt(
                                    choice_message_id,
                                    choice_to_clear,
                                    "Selection prompt is no longer active.",
                                )
                        self.pending_choice = None
                        self.pending_choice_message_id = None
                    with self.approval_lock:
                        previous = self.pending_approval
                        if previous is None or previous.approval_id != prompt.approval_id:
                            self.pending_approval = prompt
                            self.pending_approval_message_id = None
                            should_send = True
                    if should_send:
                        log("APPROV", f"approval prompt detected {prompt.short_signature}")
                        message_id = self.telegram.send_approval_prompt(prompt)
                        with self.approval_lock:
                            if (
                                self.pending_approval
                                and self.pending_approval.approval_id == prompt.approval_id
                            ):
                                self.pending_approval_message_id = message_id
                elif choice_prompt:
                    with self.approval_lock:
                        prompt_to_clear = self.pending_approval
                        message_id = self.pending_approval_message_id
                        if prompt_to_clear is not None:
                            log("APPROV", "approval prompt cleared by choice")
                            if prompt_to_clear.approval_id not in self.resolved_approval_ids:
                                self.telegram.update_approval_prompt(
                                    message_id,
                                    prompt_to_clear,
                                    "Approval prompt is no longer active.",
                                )
                        self.pending_approval = None
                        self.pending_approval_message_id = None
                    with self.choice_lock:
                        previous_choice = self.pending_choice
                        if (
                            previous_choice is None
                            or previous_choice.choice_id != choice_prompt.choice_id
                        ):
                            self.pending_choice = choice_prompt
                            self.pending_choice_message_id = None
                            should_send = True
                    if should_send:
                        log("CHOICE", f"choice prompt detected {choice_prompt.short_signature}")
                        message_id = self.telegram.send_choice_prompt(choice_prompt)
                        with self.choice_lock:
                            if (
                                self.pending_choice
                                and self.pending_choice.choice_id == choice_prompt.choice_id
                            ):
                                self.pending_choice_message_id = message_id
                else:
                    with self.approval_lock:
                        prompt_to_clear = self.pending_approval
                        message_id = self.pending_approval_message_id
                        if prompt_to_clear is not None:
                            log("APPROV", "approval prompt cleared")
                            if prompt_to_clear.approval_id not in self.resolved_approval_ids:
                                self.telegram.update_approval_prompt(
                                    message_id,
                                    prompt_to_clear,
                                    "Approval prompt is no longer active.",
                                )
                        self.pending_approval = None
                        self.pending_approval_message_id = None
                    with self.choice_lock:
                        choice_to_clear = self.pending_choice
                        message_id = self.pending_choice_message_id
                        if choice_to_clear is not None:
                            log("CHOICE", "choice prompt cleared")
                            if choice_to_clear.choice_id not in self.resolved_choice_ids:
                                self.telegram.update_choice_prompt(
                                    message_id,
                                    choice_to_clear,
                                    "Selection prompt is no longer active.",
                                )
                        self.pending_choice = None
                        self.pending_choice_message_id = None
            except Exception as exc:  # noqa: BLE001
                log("APPROV", f"watch error: {exc}")
            self.stop_event.wait(1.0)

    def approval_choice_from_text(self, text: str) -> str | None:
        value = text.strip().lower()
        if value.startswith("/approve"):
            parts = value.split(maxsplit=1)
            value = parts[1].strip() if len(parts) > 1 else ""
        with self.approval_lock:
            prompt = self.pending_approval
        if prompt is not None:
            aliases = {
                "yes": "y",
                "yep": "y",
                "yeah": "y",
                "no": "n",
                "nope": "n",
                "cancel": "esc",
                "escape": "esc",
            }
            candidates = {value}
            if value in aliases:
                candidates.add(aliases[value])
            if value in {"n", "no", "nope", "cancel", "esc", "escape"}:
                candidates.add("esc")
            for option in prompt.options:
                values = {option.number.lower(), option.key.lower()}
                if option.label:
                    values.add(option.label.lower())
                    if option.label.lower().startswith("no"):
                        values.add("no")
                if candidates & values:
                    return option.number
        aliases = {
            "1": "1",
            "y": "1",
            "yes": "1",
            "2": "2",
            "p": "2",
            "permanent": "2",
            "always": "2",
            "3": "3",
            "n": "3",
            "no": "3",
            "esc": "3",
            "escape": "3",
            "cancel": "3",
        }
        return aliases.get(value)

    def handle_approval_choice(
        self,
        choice: str,
        signature: str | None = None,
        callback_query_id: str | None = None,
    ) -> bool:
        with self.approval_lock:
            prompt = self.pending_approval
            if prompt and not prompt.is_active():
                self.pending_approval = prompt.cancel()
            message_id = self.pending_approval_message_id
        if not prompt:
            if callback_query_id:
                self.telegram.call(
                    "answerCallbackQuery",
                    callback_query_id=callback_query_id,
                    text="No active Codex approval prompt.",
                )
            return False
        if not prompt.is_active():
            self.telegram.update_approval_prompt(
                message_id,
                prompt,
                "⏳ Approval expired. Please trigger the command again if you still want it.",
            )
            if callback_query_id:
                self.telegram.call(
                    "answerCallbackQuery",
                    callback_query_id=callback_query_id,
                    text="That approval prompt expired.",
                )
            return True
        if signature and signature != prompt.short_signature:
            if callback_query_id:
                self.telegram.call(
                    "answerCallbackQuery",
                    callback_query_id=callback_query_id,
                    text="That approval prompt is no longer active.",
                )
            return True
        if prompt.approval_id in self.resolved_approval_ids:
            if callback_query_id:
                self.telegram.call(
                    "answerCallbackQuery",
                    callback_query_id=callback_query_id,
                    text="This approval choice was already sent.",
                )
            return True

        option = prompt.option(choice)
        if option is None:
            return False
        try:
            self.head.inject_approval(option)
        except Exception as exc:  # noqa: BLE001
            log("APPROV", f"choice failed: {exc}")
            self.telegram.send(f"Codex approval choice failed: {exc}")
            if callback_query_id:
                self.telegram.call(
                    "answerCallbackQuery",
                    callback_query_id=callback_query_id,
                    text="Approval choice failed.",
                )
            return True

        log("APPROV", f"choice {choice} -> {option.key}")
        self.telegram.update_approval_prompt(
            message_id,
            prompt,
            f"✅ Selected {option.number}. {option.short_label}",
            selected=option,
        )
        with self.approval_lock:
            if self.pending_approval and self.pending_approval.approval_id == prompt.approval_id:
                self.resolved_approval_ids.add(prompt.approval_id)
                self.pending_approval = prompt.cancel()
                self.pending_approval_message_id = None
        if callback_query_id:
            self.telegram.call(
                "answerCallbackQuery",
                callback_query_id=callback_query_id,
                text=f"Sent choice {choice} to Codex.",
            )
        else:
            self.telegram.send(f"Sent Codex approval choice {choice}: {option.short_label}")
        return True

    def handle_callback_query(self, callback: dict[str, Any]) -> bool:
        data = str(callback.get("data") or "")
        if data.startswith(f"{CHOICE_CALLBACK_PREFIX}:"):
            return self.handle_choice_callback_query(callback)
        if not data.startswith(f"{APPROVAL_CALLBACK_PREFIX}:"):
            return False
        message = callback.get("message") if isinstance(callback.get("message"), dict) else {}
        chat = message.get("chat") if isinstance(message.get("chat"), dict) else {}
        if str(chat.get("id")) != self.config.chat_id:
            return True
        parts = data.split(":")
        if len(parts) != 3:
            return True
        _prefix, signature, choice = parts
        if signature == "done":
            if callback.get("id"):
                self.telegram.call(
                    "answerCallbackQuery",
                    callback_query_id=str(callback.get("id") or ""),
                    text="This approval choice was already sent.",
                )
            return True
        return self.handle_approval_choice(
            choice,
            signature=signature,
            callback_query_id=str(callback.get("id") or ""),
        )

    def handle_approval_text(self, text: str) -> bool:
        with self.approval_lock:
            has_pending = self.pending_approval is not None
        if not has_pending:
            return False
        choice = self.approval_choice_from_text(text)
        if not choice:
            return False
        return self.handle_approval_choice(choice)

    def choice_from_text(self, text: str, prompt: ChoicePrompt) -> str | None:
        value = text.strip().lower()
        if value.startswith("/choose"):
            parts = value.split(maxsplit=1)
            value = parts[1].strip() if len(parts) > 1 else ""
        aliases = {
            "yes": "y",
            "yep": "y",
            "yeah": "y",
            "no": "n",
            "nope": "n",
            "cancel": "esc",
            "escape": "esc",
        }
        candidates = {value}
        if value in aliases:
            candidates.add(aliases[value])
        for option in prompt.options:
            values = {option.value.lower(), option.key.lower()}
            if option.label:
                values.add(option.label.lower())
            if candidates & values:
                return option.value
        return None

    def handle_choice_choice(
        self,
        choice: str,
        signature: str | None = None,
        callback_query_id: str | None = None,
    ) -> bool:
        with self.choice_lock:
            prompt = self.pending_choice
            if prompt and not prompt.is_active():
                self.pending_choice = prompt.cancel()
            message_id = self.pending_choice_message_id
        if not prompt:
            if callback_query_id:
                self.telegram.call(
                    "answerCallbackQuery",
                    callback_query_id=callback_query_id,
                    text="No active Codex selection prompt.",
                )
            return False
        if not prompt.is_active():
            self.telegram.update_choice_prompt(
                message_id,
                prompt,
                "⏳ Selection expired. Please trigger it again if you still need it.",
            )
            if callback_query_id:
                self.telegram.call(
                    "answerCallbackQuery",
                    callback_query_id=callback_query_id,
                    text="That selection prompt expired.",
                )
            return True
        if signature and signature != prompt.short_signature:
            if callback_query_id:
                self.telegram.call(
                    "answerCallbackQuery",
                    callback_query_id=callback_query_id,
                    text="That selection prompt is no longer active.",
                )
            return True
        if prompt.choice_id in self.resolved_choice_ids:
            if callback_query_id:
                self.telegram.call(
                    "answerCallbackQuery",
                    callback_query_id=callback_query_id,
                    text="This selection was already sent.",
                )
            return True

        option = prompt.option(choice)
        if option is None:
            return False
        try:
            self.repl.send_choice_option(prompt, option)
        except Exception as exc:  # noqa: BLE001
            log("CHOICE", f"choice failed: {exc}")
            self.telegram.send(f"Codex selection failed: {exc}")
            if callback_query_id:
                self.telegram.call(
                    "answerCallbackQuery",
                    callback_query_id=callback_query_id,
                    text="Selection failed.",
                )
            return True

        log("CHOICE", f"choice {choice} -> {option.key or option.value}")
        self.telegram.update_choice_prompt(
            message_id,
            prompt,
            f"✅ Selected {option.value}. {option.short_label}",
            selected=option,
        )
        with self.choice_lock:
            if self.pending_choice and self.pending_choice.choice_id == prompt.choice_id:
                self.resolved_choice_ids.add(prompt.choice_id)
                self.pending_choice = prompt.cancel()
                self.pending_choice_message_id = None
        if callback_query_id:
            self.telegram.call(
                "answerCallbackQuery",
                callback_query_id=callback_query_id,
                text=f"Sent choice {choice} to Codex.",
            )
        else:
            self.telegram.send(f"Sent Codex selection {choice}: {option.short_label}")
        return True

    def handle_choice_callback_query(self, callback: dict[str, Any]) -> bool:
        data = str(callback.get("data") or "")
        message = callback.get("message") if isinstance(callback.get("message"), dict) else {}
        chat = message.get("chat") if isinstance(message.get("chat"), dict) else {}
        if str(chat.get("id")) != self.config.chat_id:
            return True
        parts = data.split(":")
        if len(parts) != 3:
            return True
        _prefix, signature, choice = parts
        if signature == "done":
            if callback.get("id"):
                self.telegram.call(
                    "answerCallbackQuery",
                    callback_query_id=str(callback.get("id") or ""),
                    text="This selection was already sent.",
                )
            return True
        return self.handle_choice_choice(
            choice,
            signature=signature,
            callback_query_id=str(callback.get("id") or ""),
        )

    def handle_choice_text(self, text: str) -> bool:
        with self.choice_lock:
            prompt = self.pending_choice
        if prompt is None:
            return False
        choice = self.choice_from_text(text, prompt)
        if not choice:
            return False
        return self.handle_choice_choice(choice)

    def run_codex_image(self, prompt: str, image_path: Path) -> str:
        self.config.state_dir.mkdir(parents=True, exist_ok=True)
        fd, output_raw = tempfile.mkstemp(
            prefix=f"codex-repl-image-{self.config.node}-",
            suffix=".answer",
            dir=str(self.config.state_dir),
        )
        os.close(fd)
        output_path = Path(output_raw)
        cmd = [
            self.config.codex_bin,
            "exec",
            "--json",
            "-o",
            str(output_path),
            "-i",
            str(image_path),
            "--dangerously-bypass-approvals-and-sandbox",
            "--skip-git-repo-check",
            "-C",
            str(HOME),
            prompt,
        ]
        try:
            try:
                proc = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=self.config.codex_timeout,
                    stdin=subprocess.DEVNULL,
                )
            except subprocess.TimeoutExpired as exc:
                raise CodexExecError(f"image analysis timed out after {self.config.codex_timeout}s") from exc

            if proc.returncode != 0:
                detail = (proc.stderr or proc.stdout or "").strip().splitlines()
                suffix = f": {detail[-1][:200]}" if detail else ""
                raise CodexExecError(f"codex exec -i failed rc={proc.returncode}{suffix}")

            answer = read_text(output_path) or parse_exec_answer(proc.stdout)
            if not answer.strip():
                raise CodexExecError("codex exec -i returned an empty answer")
            return answer.strip()
        finally:
            try:
                output_path.unlink()
            except OSError:
                pass

    def handle_image_prompt(self, prompt: TelegramPrompt) -> None:
        if not prompt.image_path:
            return
        log("IMG", f"codex exec -i {prompt.image_path}")
        stop_typing = self.start_typing_loop()
        try:
            with self.exec_lock:
                answer = self.run_codex_image(prompt.text, prompt.image_path)
        except CodexExecError as exc:
            log("IMG", f"analysis failed: {exc}")
            self.telegram.send(f"codex image analysis failed: {exc}")
            return
        except Exception as exc:  # noqa: BLE001
            log("IMG", f"analysis error: {exc}")
            self.telegram.send("codex image analysis failed: internal error")
            return
        finally:
            stop_typing.set()
        self.send_answer(answer)

    def image_prompt_text(self, caption_text: str, image_path: Path) -> str:
        header = (
            "[Telegram image received]\n"
            f"local_path: {image_path}\n"
        )
        if caption_text:
            return (
                header +
                f"caption: {caption_text}\n\n"
                "Open the local image path with the local image tool, inspect it, and answer "
                "the Telegram user's caption in Korean. Keep the answer concise and useful."
            )
        return (
            header +
            "Open the local image path with the local image tool, inspect it, and briefly "
            "describe what is visible in Korean."
        )

    def format_metadata(self, metadata: dict[str, Any]) -> str:
        parts = []
        for key, value in metadata.items():
            if value in (None, "", [], {}):
                continue
            parts.append(f"{key}={value}")
        return "; ".join(parts)

    def ffprobe_summary(self, media_path: Path) -> str:
        if not command_available(self.config.ffprobe_bin):
            return ""
        cmd = [
            self.config.ffprobe_bin,
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            str(media_path),
        ]
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=20,
                stdin=subprocess.DEVNULL,
            )
        except subprocess.TimeoutExpired:
            return "ffprobe: timed out"
        except OSError as exc:
            return f"ffprobe: {exc}"
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip().splitlines()
            suffix = f": {detail[-1][:200]}" if detail else ""
            return f"ffprobe: failed rc={proc.returncode}{suffix}"
        try:
            payload = json.loads(proc.stdout or "{}")
        except json.JSONDecodeError:
            return "ffprobe: invalid json"

        lines: list[str] = []
        fmt = payload.get("format") if isinstance(payload.get("format"), dict) else {}
        format_parts = []
        for key in ("format_name", "duration", "size", "bit_rate"):
            value = fmt.get(key)
            if value not in (None, ""):
                format_parts.append(f"{key}={value}")
        if format_parts:
            lines.append("format: " + "; ".join(format_parts))

        streams = payload.get("streams") if isinstance(payload.get("streams"), list) else []
        for stream in streams[:6]:
            if not isinstance(stream, dict):
                continue
            stream_parts = []
            for key in (
                "codec_type",
                "codec_name",
                "width",
                "height",
                "sample_rate",
                "channels",
                "duration",
            ):
                value = stream.get(key)
                if value not in (None, ""):
                    stream_parts.append(f"{key}={value}")
            if stream_parts:
                lines.append("stream: " + "; ".join(stream_parts))
        return "\n".join(lines)

    def transcribe_audio(self, media_path: Path) -> tuple[str, str]:
        template = self.config.audio_transcribe_cmd
        if not template:
            return "", "not_available: set CRB_AUDIO_TRANSCRIBE_CMD to enable audio transcription"

        quoted_path = shlex.quote(str(media_path))
        if "{path}" in template:
            cmd = template.replace("{path}", quoted_path)
        else:
            cmd = f"{template} {quoted_path}"
        try:
            proc = subprocess.run(
                cmd,
                shell=True,
                capture_output=True,
                text=True,
                timeout=self.config.codex_timeout,
                stdin=subprocess.DEVNULL,
            )
        except subprocess.TimeoutExpired:
            return "", f"failed: transcription timed out after {self.config.codex_timeout}s"
        except OSError as exc:
            return "", f"failed: {exc}"

        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip().splitlines()
            suffix = f": {detail[-1][:200]}" if detail else ""
            return "", f"failed: transcription command rc={proc.returncode}{suffix}"
        transcript = (proc.stdout or "").strip()
        if not transcript:
            return "", "failed: transcription command returned empty stdout"
        return truncate_text(transcript), "ok"

    def download_thumbnail(
        self,
        media: dict[str, Any],
        media_dir: Path,
        update_id: int,
        prefix: str,
    ) -> Path | None:
        thumbnail = media.get("thumbnail") or media.get("thumb")
        if not isinstance(thumbnail, dict) or not thumbnail.get("file_id"):
            return None
        name_hint = f"{prefix}-{update_id}-{thumbnail.get('file_unique_id') or thumbnail.get('file_id')}"
        try:
            return self.telegram.download_file(
                str(thumbnail["file_id"]),
                media_dir,
                name_hint,
                default_suffix=".jpg",
                allowed_extensions=IMAGE_EXTENSIONS,
            )
        except Exception as exc:  # noqa: BLE001
            log("TG", f"thumbnail download failed: {exc}")
            return None

    def extract_video_frames(
        self,
        video_path: Path,
        media_dir: Path,
        duration: int | float | None,
    ) -> tuple[Path, ...]:
        if not command_available(self.config.ffmpeg_bin):
            return ()
        frame_dir = media_dir / f"{safe_filename_part(video_path.stem)}-frames"
        frame_dir.mkdir(parents=True, exist_ok=True)
        frame_pattern = frame_dir / "frame-%02d.jpg"

        count = max(1, self.config.video_frame_count)
        vf = "fps=1"
        if isinstance(duration, (int, float)) and duration > 0:
            vf = f"fps={count}/{max(int(duration) + 1, 1)}"
        cmd = [
            self.config.ffmpeg_bin,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(video_path),
            "-vf",
            vf,
            "-frames:v",
            str(count),
            str(frame_pattern),
        ]
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=60,
                stdin=subprocess.DEVNULL,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            log("VID", f"frame extraction failed: {exc}")
            return ()
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip().splitlines()
            suffix = f": {detail[-1][:200]}" if detail else ""
            log("VID", f"frame extraction failed rc={proc.returncode}{suffix}")
            return ()
        return tuple(sorted(frame_dir.glob("frame-*.jpg"))[:count])

    def audio_prompt_text(
        self,
        media_kind: str,
        caption_text: str,
        media_path: Path,
        metadata: dict[str, Any],
        transcript: str,
        transcript_status: str,
        probe_summary: str,
    ) -> str:
        lines = [
            "[Telegram audio received]",
            f"local_path: {media_path}",
            f"media_kind: {media_kind}",
        ]
        if caption_text:
            lines.append(f"caption: {caption_text}")
        metadata_line = self.format_metadata(metadata)
        if metadata_line:
            lines.append(f"metadata: {metadata_line}")
        if probe_summary:
            lines.extend(["", "ffprobe:", probe_summary])
        lines.append("")
        if transcript:
            lines.extend(["transcript:", transcript])
        else:
            lines.append(f"transcript_status: {transcript_status}")
        lines.extend(
            [
                "",
                "Answer the Telegram user in Korean. If transcript is unavailable, say the audio file "
                "was received but this node has no transcription backend configured, and ask for text "
                "or CRB_AUDIO_TRANSCRIBE_CMD setup.",
            ]
        )
        return "\n".join(lines)

    def video_prompt_text(
        self,
        media_kind: str,
        caption_text: str,
        media_path: Path,
        metadata: dict[str, Any],
        thumbnail_path: Path | None,
        frame_paths: tuple[Path, ...],
        transcript: str,
        transcript_status: str,
        probe_summary: str,
    ) -> str:
        lines = [
            "[Telegram video received]",
            f"local_path: {media_path}",
            f"media_kind: {media_kind}",
        ]
        if thumbnail_path:
            lines.append(f"thumbnail_path: {thumbnail_path}")
        if frame_paths:
            lines.append("frame_paths:")
            lines.extend(f"- {path}" for path in frame_paths)
        if caption_text:
            lines.append(f"caption: {caption_text}")
        metadata_line = self.format_metadata(metadata)
        if metadata_line:
            lines.append(f"metadata: {metadata_line}")
        if probe_summary:
            lines.extend(["", "ffprobe:", probe_summary])
        lines.append("")
        if transcript:
            lines.extend(["audio_transcript:", transcript])
        else:
            lines.append(f"audio_transcript_status: {transcript_status}")
        lines.extend(
            [
                "",
                "Open thumbnail_path or frame_paths with the local image tool if present. Answer the "
                "Telegram user in Korean based on visible frames, caption, metadata, and transcript. "
                "If frames or transcript are unavailable, state that limitation briefly.",
            ]
        )
        return "\n".join(lines)

    def document_prompt_text(
        self,
        caption_text: str,
        media_path: Path,
        metadata: dict[str, Any],
    ) -> str:
        lines = [
            "[Telegram file received]",
            f"local_path: {media_path}",
            "media_kind: document",
        ]
        if caption_text:
            lines.append(f"caption: {caption_text}")
        metadata_line = self.format_metadata(metadata)
        if metadata_line:
            lines.append(f"metadata: {metadata_line}")
        lines.extend(
            [
                "",
                "Answer the Telegram user in Korean. Use the local_path and metadata above. "
                "If the file cannot be inspected directly, say that the file was received and "
                "ask for the specific action needed.",
            ]
        )
        return "\n".join(lines)

    def prompt_from_telegram_message(self, message: dict[str, Any], update_id: int) -> TelegramPrompt:
        text = message.get("text")
        if isinstance(text, str) and text.strip():
            return TelegramPrompt(text=text)

        caption = message.get("caption")
        caption_text = caption.strip() if isinstance(caption, str) else ""
        media_dir = self.config.state_dir / "codex-repl-bridge-media" / self.config.node

        photos = message.get("photo")
        if isinstance(photos, list) and photos:
            candidates = [item for item in photos if isinstance(item, dict) and item.get("file_id")]
            if candidates:
                photo = max(
                    candidates,
                    key=lambda item: (
                        int(item.get("file_size") or 0),
                        int(item.get("width") or 0) * int(item.get("height") or 0),
                    ),
                )
                name_hint = f"telegram-{update_id}-{photo.get('file_unique_id') or photo.get('file_id')}"
                image_path = self.telegram.download_file(
                    str(photo["file_id"]),
                    media_dir,
                    name_hint,
                    default_suffix=".jpg",
                    allowed_extensions=IMAGE_EXTENSIONS,
                )
                return TelegramPrompt(
                    text=self.image_prompt_text(caption_text, image_path),
                    image_path=image_path,
                    kind="photo",
                )

        document = message.get("document")
        if isinstance(document, dict) and str(document.get("mime_type") or "").startswith("image/"):
            file_id = str(document.get("file_id") or "")
            if file_id:
                name_hint = f"telegram-{update_id}-{document.get('file_unique_id') or file_id}"
                default_suffix = suffix_from_metadata(
                    str(document.get("file_name") or ""),
                    str(document.get("mime_type") or ""),
                    ".jpg",
                )
                image_path = self.telegram.download_file(
                    file_id,
                    media_dir,
                    name_hint,
                    default_suffix=default_suffix,
                    allowed_extensions=IMAGE_EXTENSIONS,
                )
                return TelegramPrompt(
                    text=self.image_prompt_text(caption_text, image_path),
                    image_path=image_path,
                    kind="image_document",
                )

        audio: dict[str, Any] | None = None
        audio_kind = ""
        for key, kind in (("voice", "voice"), ("audio", "audio")):
            candidate = message.get(key)
            if isinstance(candidate, dict) and candidate.get("file_id"):
                audio = candidate
                audio_kind = kind
                break
        if audio is None and isinstance(document, dict) and document.get("file_id"):
            mime_type = str(document.get("mime_type") or "")
            file_name = str(document.get("file_name") or "")
            if mime_type.startswith("audio/") or Path(file_name).suffix.lower() in AUDIO_EXTENSIONS:
                audio = document
                audio_kind = "audio_document"

        if audio is not None:
            file_id = str(audio.get("file_id") or "")
            name_hint = f"telegram-{update_id}-{audio.get('file_unique_id') or file_id}"
            default_suffix = suffix_from_metadata(
                str(audio.get("file_name") or ""),
                str(audio.get("mime_type") or ""),
                ".ogg" if audio_kind == "voice" else ".mp3",
            )
            media_path = self.telegram.download_file(
                file_id,
                media_dir,
                name_hint,
                default_suffix=default_suffix,
                allowed_extensions=AUDIO_EXTENSIONS,
            )
            metadata = {
                "duration": audio.get("duration"),
                "mime_type": audio.get("mime_type"),
                "file_name": audio.get("file_name"),
                "title": audio.get("title"),
                "performer": audio.get("performer"),
                "file_size": audio.get("file_size"),
            }
            probe_summary = self.ffprobe_summary(media_path)
            transcript, transcript_status = self.transcribe_audio(media_path)
            return TelegramPrompt(
                text=self.audio_prompt_text(
                    audio_kind,
                    caption_text,
                    media_path,
                    metadata,
                    transcript,
                    transcript_status,
                    probe_summary,
                ),
                kind=audio_kind,
            )

        video: dict[str, Any] | None = None
        video_kind = ""
        for key, kind in (
            ("video", "video"),
            ("video_note", "video_note"),
            ("animation", "animation"),
        ):
            candidate = message.get(key)
            if isinstance(candidate, dict) and candidate.get("file_id"):
                video = candidate
                video_kind = kind
                break
        if video is None and isinstance(document, dict) and document.get("file_id"):
            mime_type = str(document.get("mime_type") or "")
            file_name = str(document.get("file_name") or "")
            if mime_type.startswith("video/") or Path(file_name).suffix.lower() in VIDEO_EXTENSIONS:
                video = document
                video_kind = "video_document"

        if video is not None:
            file_id = str(video.get("file_id") or "")
            name_hint = f"telegram-{update_id}-{video.get('file_unique_id') or file_id}"
            default_suffix = suffix_from_metadata(
                str(video.get("file_name") or ""),
                str(video.get("mime_type") or ""),
                ".mp4",
            )
            media_path = self.telegram.download_file(
                file_id,
                media_dir,
                name_hint,
                default_suffix=default_suffix,
                allowed_extensions=VIDEO_EXTENSIONS,
            )
            metadata = {
                "duration": video.get("duration"),
                "mime_type": video.get("mime_type"),
                "file_name": video.get("file_name"),
                "width": video.get("width") or video.get("length"),
                "height": video.get("height") or video.get("length"),
                "file_size": video.get("file_size"),
            }
            thumbnail_path = self.download_thumbnail(video, media_dir, update_id, "telegram-video-thumb")
            duration = video.get("duration")
            frame_paths = self.extract_video_frames(media_path, media_dir, duration)
            probe_summary = self.ffprobe_summary(media_path)
            transcript, transcript_status = self.transcribe_audio(media_path)
            return TelegramPrompt(
                text=self.video_prompt_text(
                    video_kind,
                    caption_text,
                    media_path,
                    metadata,
                    thumbnail_path,
                    frame_paths,
                    transcript,
                    transcript_status,
                    probe_summary,
                ),
                kind=video_kind,
            )

        if isinstance(document, dict) and document.get("file_id"):
            file_id = str(document.get("file_id") or "")
            name_hint = f"telegram-{update_id}-{document.get('file_unique_id') or file_id}"
            default_suffix = suffix_from_metadata(
                str(document.get("file_name") or ""),
                str(document.get("mime_type") or ""),
                ".bin",
            )
            media_path = self.telegram.download_file(
                file_id,
                media_dir,
                name_hint,
                default_suffix=default_suffix,
            )
            metadata = {
                "mime_type": document.get("mime_type"),
                "file_name": document.get("file_name"),
                "file_size": document.get("file_size"),
            }
            return TelegramPrompt(
                text=self.document_prompt_text(caption_text, media_path, metadata),
                kind="document",
            )

        return TelegramPrompt(text="")

    def telegram_loop(self) -> None:
        offset_raw = read_text(self.config.offset_file)
        offset = int(offset_raw) if offset_raw.isdigit() else 0
        while not self.stop_event.is_set():
            response = self.telegram.call("getUpdates", offset=offset, timeout=self.config.poll_timeout)
            if not response or not response.get("ok"):
                time.sleep(2)
                continue
            for update in response.get("result", []):
                if not isinstance(update, dict) or "update_id" not in update:
                    continue
                offset = int(update["update_id"]) + 1
                write_text_atomic(self.config.offset_file, offset)

                callback = update.get("callback_query")
                if isinstance(callback, dict):
                    if self.handle_callback_query(callback):
                        continue

                message = update.get("message") or update.get("edited_message")
                if not isinstance(message, dict):
                    continue
                chat = message.get("chat") if isinstance(message.get("chat"), dict) else {}
                if str(chat.get("id")) != self.config.chat_id:
                    continue
                try:
                    prompt = self.prompt_from_telegram_message(message, int(update["update_id"]))
                except Exception as exc:  # noqa: BLE001
                    log("TG", f"media download failed: {exc}")
                    self.telegram.send(f"codex REPL media delivery failed: {exc}")
                    continue
                if not prompt.text.strip():
                    continue
                if self.handle_approval_text(prompt.text):
                    continue
                if self.handle_choice_text(prompt.text):
                    continue
                if prompt.image_path and self.config.image_mode == "exec":
                    self.handle_image_prompt(prompt)
                    continue
                if prompt.text.strip().lower() in {"/start", "/ping"}:
                    self.telegram.send("codex REPL bridge running")
                    continue
                if self.handle_status_command(prompt.text):
                    continue
                log("TG", "prompt -> Codex REPL")
                self.begin_repl_typing()
                slash_command = is_codex_slash_command(prompt.text)
                self.begin_telegram_prompt_tracking(prompt.text)
                try:
                    self.clear_composer_before_telegram_input("telegram prompt")
                    self.head.send(AgentMessage(prompt.text))
                    if slash_command:
                        time.sleep(0.8)
                        had_slash_error = self.handle_slash_command_result(prompt.text)
                        if should_stop_typing_after_slash_command(prompt.text, had_slash_error):
                            self.stop_repl_typing()
                            self.stop_long_running_progress()
                            self.clear_active_telegram_prompt()
                except Exception as exc:  # noqa: BLE001
                    self.stop_repl_typing()
                    self.stop_long_running_progress()
                    self.clear_active_telegram_prompt()
                    log("REPL", f"paste failed: {exc}")
                    self.telegram.send(f"codex REPL delivery failed: {exc}")

    def run(self) -> None:
        self.head.spawn()
        self.ensure_session_file()
        self.acquire_lock()
        jsonl_thread = threading.Thread(target=self.jsonl_loop, daemon=True)
        jsonl_thread.start()
        approval_thread = threading.Thread(target=self.approval_loop, daemon=True)
        approval_thread.start()
        self.recover_repl_liveness("startup")
        liveness_thread = threading.Thread(target=self.liveness_loop, daemon=True)
        liveness_thread.start()
        try:
            self.telegram_loop()
        finally:
            self.stop_event.set()
            self.release_lock()


def main() -> int:
    try:
        config = Config.from_env()
        token = load_token(config.token_file)
        bridge = Bridge(
            config,
            TelegramClient(token, config.chat_id, config.emoji, config.telegram_chunk),
            CodexRepl(config),
        )
    except Exception as exc:  # noqa: BLE001
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2

    def stop(signum: int, _frame: Any) -> None:
        bridge.stop_event.set()
        bridge.release_lock()
        raise SystemExit(128 + signum)

    for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(signum, stop)

    log(
        "START",
        f"node={config.node} chat={config.chat_id} tmux={config.tmux_socket}/{config.tmux_session}",
    )
    try:
        bridge.run()
    except Exception as exc:  # noqa: BLE001
        bridge.release_lock()
        print(f"runtime error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
