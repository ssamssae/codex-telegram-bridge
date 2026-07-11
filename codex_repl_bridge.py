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

import fcntl
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
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


HOME = Path.home()
KST = timezone(timedelta(hours=9), "KST")
NODE_EMOJI_LINES = {"\U0001f34e", "\U0001f3ed", "\U0001fa9f", "\U0001f5a5", "\U0001f4bb", "\U0001f916"}


def is_private_chat_id(chat_id: object) -> bool:
    try:
        return int(str(chat_id).strip()) > 0
    except (TypeError, ValueError):
        return False


def strip_leading_emoji_decoration(text: str) -> str:
    value = (text or "").lstrip()
    index = 0
    seen_decoration = False
    while index < len(value):
        codepoint = ord(value[index])
        if (
            0x1F1E6 <= codepoint <= 0x1F1FF
            or 0x1F300 <= codepoint <= 0x1FAFF
            or 0x2190 <= codepoint <= 0x2BFF
            or 0x1F3FB <= codepoint <= 0x1F3FF
            or codepoint in {0x200D, 0xFE0E, 0xFE0F}
        ):
            seen_decoration = True
            index += 1
            continue
        if seen_decoration and value[index].isspace():
            index += 1
            continue
        break
    return value[index:].lstrip() if seen_decoration else value


REASONING_HEADER = "\U0001f9e0 코덱스 사고"
REASONING_MIRROR_LIMIT = 3500
FLOW_MIRROR_HEADER = "⚙️ 작업 흐름"
FLOW_MIRROR_LIMIT = 1500
# Per-step line cap: each flow event is collapsed to ONE short line (claude
# parity) instead of the full multi-line narration. Keeps many steps inside one
# edit-in-place card instead of overflowing into many long messages.
FLOW_MIRROR_LINE_LIMIT = 100
# Sliding-window size: the flow card keeps only the last N step lines in ONE
# edit-in-place message (no length-based rollover to a new message). Older steps
# scroll off instead of spawning multiple long cards. (T-260628-36)
FLOW_MIRROR_WINDOW = 6
FLOW_MIRROR_EDIT_MIN_SECONDS = 1.0
# ⚙️ A2 받은-지시 카드 (T-260630-22): 노드/오케가 codex REPL 에 주입한 디렉티브를 결과(코덱스
# 답)보다 먼저 1~2줄로 보여줘 맥락이 끊기던 문제 보완. codex 는 TUI 라 JSONL terminal-origin
# 에 수동입력이 섞여 origin 만으론 노드주입을 못 가리므로(claude 헤드리스와 다름), 신뢰성 있는
# directive-received-ack.sh 가 주입 시 남긴 신호파일을 브릿지가 tail 해 카드화한다. flow_mirror
# 토글 게이트. claude-telegram-bridge.py 의 '📥 받은 지시'(PR#248, node-origin 직접감지)와 한 쌍.
AMBIENT_DIRECTIVE_HEADER = "📥 받은 지시"
SENT_DIRECTIVE_HEADER = "📤 보낸 지시"
# T-260709-56: 터미널 에코(자기→자기)는 라우팅 줄이 "라이덴 → 라이덴" 으로 읽혀 혼선 —
# 자기 노드 입력은 ⌨️ 헤더 + 본문만. 노드간(from≠to)은 📤 라우팅 카드 유지.
TERMINAL_INPUT_HEADER = "⌨️ 터미널 입력"
AMBIENT_DIRECTIVE_LIMIT = 400
DIRECTIVE_SIGNAL_ENV = "CRB_DIRECTIVE_SIGNAL_PATH"
# Codex REPL "dead/interrupted mid-turn" markers. When these appear at the
# BOTTOM of the pane, codex aborted the turn but the bridge can keep pulsing
# typing for up to CRB_TYPING_MAX_SECONDS (default 7200s = 2h). The typing loop
# watches for these and self-terminates. (T-260628-15)
CODEX_INTERRUPT_MARKERS = ("Conversation interrupted", "Something went wrong")
TASK_ID_RE = re.compile(r"\bT-\d{6}-\d+\b")
CODE_FENCE_RE = re.compile(r"```([^\n`]*)\n(.*?)```", re.DOTALL)
IMAGE_DELIVERY_CLAIM_IMAGE_RE = re.compile(
    r"(이미지|사진|그림|첨부|파일|image|photo|picture|attachment|png|jpe?g|webp)",
    re.IGNORECASE,
)
IMAGE_DELIVERY_CLAIM_SENT_RE = re.compile(
    r"(보냈|전송했|첨부했|올렸|sent|uploaded|attached|delivered)",
    re.IGNORECASE,
)
IMAGE_DELIVERY_CLAIM_NEGATIVE_RE = re.compile(
    r"(안\s*보냈|못\s*보냈|전송\s*실패|첨부\s*실패|업로드\s*실패|"
    r"not\s+sent|did(?:\s+not|n't)\s+send|could(?:\s+not|n't)\s+send|"
    r"failed\s+to\s+(?:send|upload|attach))",
    re.IGNORECASE,
)


def utf16_code_units(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def split_answer_for_code_blocks(text: str) -> list[dict[str, Any]]:
    segments: list[dict[str, Any]] = []
    cursor = 0
    for match in CODE_FENCE_RE.finditer(text):
        before = text[cursor : match.start()].strip()
        if before:
            segments.append({"body": before, "code": False})
        language = match.group(1).strip()
        code = match.group(2)
        if code.endswith("\n"):
            code = code[:-1]
        if code:
            segment: dict[str, Any] = {"body": code, "code": True}
            if language:
                segment["language"] = language
            segments.append(segment)
        cursor = match.end()
    tail = text[cursor:].strip()
    if tail:
        segments.append({"body": tail, "code": False})
    return segments or [{"body": text, "code": False}]


def pre_entities_json(text: str, language: str = "") -> str:
    entity: dict[str, Any] = {
        "type": "pre",
        "offset": 0,
        "length": utf16_code_units(text),
    }
    if language:
        entity["language"] = language
    return json.dumps([entity], ensure_ascii=False)


IMAGE_DELIVERY_PROMPT_OBJECT_RE = re.compile(
    r"(이미지|사진|그림|표지|로고|배너|썸네일|포스터|카드뉴스|짤|"
    r"image|photo|picture|cover|logo|banner|thumbnail|poster|png|jpe?g|webp)",
    re.IGNORECASE,
)
IMAGE_DELIVERY_PROMPT_ACTION_RE = re.compile(
    r"(만들어\s*(?:줘|주세요|달)|생성\s*해\s*(?:줘|주세요|달)|생성해\s*(?:줘|주세요|달)|"
    r"그려\s*(?:줘|주세요|달)|보내\s*(?:줘|주세요|달)|전송\s*해\s*(?:줘|주세요|달)|"
    r"전송해\s*(?:줘|주세요|달)|첨부\s*해\s*(?:줘|주세요|달)|첨부해\s*(?:줘|주세요|달)|"
    r"올려\s*(?:줘|주세요|달)|generate|create|draw|send|upload|attach)",
    re.IGNORECASE,
)
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
IMAGE_ATTACHMENT_EXTENSIONS = IMAGE_EXTENSIONS | {".bmp", ".tif", ".tiff"}
AUDIO_EXTENSIONS = {".ogg", ".oga", ".opus", ".mp3", ".m4a", ".aac", ".wav", ".flac", ".weba"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".webm", ".avi", ".mkv"}
MEDIA_ATTACHMENT_EXTENSIONS = IMAGE_ATTACHMENT_EXTENSIONS | AUDIO_EXTENSIONS | VIDEO_EXTENSIONS
VOICE_ATTACHMENT_EXTENSIONS = {".ogg", ".oga", ".opus"}
APPROVAL_CALLBACK_PREFIX = "crb_approval"
CHOICE_CALLBACK_PREFIX = "crb_choice"
BRIDGE_RESTART_CALLBACK_PREFIX = "crb_restart"
STATUS_COMMANDS = {"status", "/status"}
CONTEXT_COMMANDS = {"context", "/context"}
LONG_RUNNING_SLASH_COMMANDS = {"/goal"}
STATUS_WIDE_CAPTURE_COLUMNS = 132
ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
REPL_BUSY_RE = re.compile(
    r"esc to interrupt|interrupt to stop|\bWorking\b|Churning|Saut[eé]ed|✻|✽",
    re.IGNORECASE,
)
REPL_ACTIVE_BUSY_RE = re.compile(
    r"esc to interru|interrupt to stop|^[•*]\s*Working\b|Churning|Saut[eé]ed|✻|✽",
    re.IGNORECASE,
)
REPL_QUEUED_RE = re.compile(r"\bQueued follow-up inputs\b", re.IGNORECASE)
REPL_IDLE_DONE_RE = re.compile(r"\bGoal achieved\b", re.IGNORECASE)
REPL_PROMPT_READY_RE = re.compile(r"^[›>]\s*(?:$|.+)")
FOOTER_CONTEXT_RE = re.compile(r"\bContext\s+(?P<used>\d{1,3})%\s+used\b", re.IGNORECASE)
FOOTER_FIVE_HOUR_RE = re.compile(r"\b5h\s+(?P<left>\d{1,3})%\s+left\b", re.IGNORECASE)
FOOTER_WEEKLY_RE = re.compile(
    r"\b(?:weekly|week|w)\w*\s+(?P<left>\d{1,3})%\s+left\b",
    re.IGNORECASE,
)
STATUS_BOX_LIMIT_RE = re.compile(r"\blimit\s*:", re.IGNORECASE)
STATUS_BOX_FRAGMENT_RE = re.compile(r"^(?:\(?r|on|\d{1,2}|[A-Z][a-z]?)$")
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


def release_hold_response(text: str) -> str | None:
    # Personal release-hold automation is stripped from the public export.
    return None


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


def optional_path_env(name: str, default: str | None = None) -> Path | None:
    raw = env(name, default)
    if raw is None:
        return None
    value = raw.strip()
    if not value or value.lower() in {"0", "off", "false", "none", "null"}:
        return None
    return Path(value).expanduser()


def parse_signal_prompt(raw: str) -> str:
    raw = (raw or "").strip()
    if not raw:
        return ""
    if not raw.startswith("{"):
        return raw
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return raw
    if not isinstance(payload, dict):
        return raw
    for key in ("prompt", "text", "message"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def now_ts() -> str:
    return time.strftime("%H:%M:%S")


def run_midreport_obligation(args: list[str]) -> None:
    if not bool_env("CRB_PROGRESS_OBLIGATION", True):
        return
    raw_cmd = env("CRB_PROGRESS_OBLIGATION_CMD")
    if raw_cmd:
        cmd = shlex.split(raw_cmd)
        if len(cmd) == 1 and cmd[0].endswith(".py"):
            cmd = [sys.executable, cmd[0]]
    else:
        helper = Path(__file__).resolve().parent / "midreport-obligation.py"
        if not helper.exists():
            return
        cmd = [sys.executable, str(helper)]
    try:
        subprocess.run(
            [*cmd, *args],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log("PROG", f"midreport obligation failed: {exc}")


def log(label: str, message: str) -> None:
    print(f"[{now_ts()}] {label:<5} {message}", flush=True)


# ─ codex-CLI-style startup version check + button/auto self-update ─────────────
# When a newer release exists on PyPI, offer a one-tap Telegram "update" button
# (or fully auto-update with CRB_AUTO_UPDATE=1). Only active for pip-installed
# copies — source/editable checkouts, offline state, errors, or opt-out all leave
# the running version untouched. Requested 2026-06-29 (Seonyeob Rim feedback).
SELF_UPDATE_PACKAGE = "codex-telegram-bridge"
SELF_UPDATE_MODULE = "codex_repl_bridge"
SELF_UPDATE_PREFIX = "CRB"
SELF_UPDATE_CALLBACK = "crb_update"
SELF_UPDATE_PYPI_TIMEOUT = 4


def _self_update_installed_version() -> str | None:
    try:
        from importlib.metadata import version, PackageNotFoundError  # noqa: F401
    except Exception:  # noqa: BLE001
        return None
    try:
        return version(SELF_UPDATE_PACKAGE)
    except Exception:  # noqa: BLE001
        return None


def _self_update_is_pip_managed() -> bool:
    path = str(Path(__file__).resolve())
    return "site-packages" in path or "dist-packages" in path


def _self_update_version_tuple(text: str) -> tuple[int, ...]:
    parts: list[int] = []
    for chunk in str(text).split("."):
        digits = ""
        for ch in chunk:
            if ch.isdigit():
                digits += ch
            else:
                break
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


def _self_update_latest() -> str | None:
    try:
        req = urllib.request.Request(
            f"https://pypi.org/pypi/{SELF_UPDATE_PACKAGE}/json",
            headers={"User-Agent": f"{SELF_UPDATE_PACKAGE}-bridge"},
        )
        with urllib.request.urlopen(req, timeout=SELF_UPDATE_PYPI_TIMEOUT) as resp:
            info = json.loads(resp.read().decode("utf-8"))
        latest = str((info.get("info") or {}).get("version") or "").strip()
        return latest or None
    except Exception as exc:  # noqa: BLE001 — offline / PyPI down is non-fatal
        log("UPDATE", f"version check skipped: {exc}")
        return None


def self_update_available() -> str | None:
    """Return the newer PyPI version string if an update should be offered, else None.
    Returns None for opt-out, source checkouts, already-updated lineage, offline,
    or when already current. Never raises."""
    try:
        if bool_env(f"{SELF_UPDATE_PREFIX}_NO_UPDATE_CHECK", False):
            return None
        if env(f"{SELF_UPDATE_PREFIX}_SELF_UPDATED"):
            return None
        if not _self_update_is_pip_managed():
            return None
        current = _self_update_installed_version()
        if not current:
            return None
        latest = _self_update_latest()
        if not latest:
            return None
        if _self_update_version_tuple(latest) <= _self_update_version_tuple(current):
            return None
        return latest
    except Exception as exc:  # noqa: BLE001
        log("UPDATE", f"version check error: {exc}")
        return None


def perform_self_update(latest: str) -> None:
    """pip-upgrade to `latest` then re-exec so the new code runs. Fail-safe: any
    error leaves the running version untouched."""
    try:
        log("UPDATE", f"upgrading {SELF_UPDATE_PACKAGE} -> {latest}")
        proc = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--upgrade", f"{SELF_UPDATE_PACKAGE}=={latest}"],
            capture_output=True,
            text=True,
            timeout=300,
        )
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip()[:200]
            log("UPDATE", f"pip upgrade failed (staying put): {detail}")
            return
        os.environ[f"{SELF_UPDATE_PREFIX}_SELF_UPDATED"] = latest
        log("UPDATE", f"upgraded to {latest}; restarting")
        os.execv(sys.executable, [sys.executable, "-m", SELF_UPDATE_MODULE] + sys.argv[1:])
    except Exception as exc:  # noqa: BLE001
        log("UPDATE", f"self-update error (new version active next restart): {exc}")


def node_defaults() -> tuple[str, str]:
    return "codex", "🤖"


# Public sender labels — 받은-지시 신호의 from=<alias> (또는 hostname) 를 (한글 라벨, 이모지) 로
# 매핑. claude-telegram-bridge.py node_label_emoji 동형. 받은-지시 카드 "발신 → 수신" 줄용.
_NODE_ALIAS_LABELS: dict[str, tuple[str, str]] = {}
_NODE_HOST_LABELS: list[tuple[str, tuple[str, str]]] = []


def node_label_emoji(token: str) -> tuple[str, str]:
    raw = (token or "").strip()
    return (raw, "🤖") if raw else ("", "🤖")


# T-260701-68: the internal mesh bus/ledger layer is stripped from the public
# export, but call sites survive newer internal commits. Documented no-op stubs
# keep the public bridge on the direct Telegram API path (None => legacy send).
def mesh_ledger_record(*args, **kwargs):
    return None


def mesh_cutover_call(method, params):
    return None


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


def _atomic_tmp(path: Path) -> tuple[int, Path]:
    # T-260704-39 F7: tmp 는 호출마다 유니크(mkstemp) — 고정 '<name>.tmp' 는 동시
    # 쓰기가 같은 경로를 공유하다 첫 replace 후 두번째 replace 가 FileNotFoundError
    # 로 죽는 race. claude 브릿지 F5(T-260704-37, 라이덴 크래시 실측)와 동일 클래스 선제수리.
    fd, name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    return fd, Path(name)


def write_text_atomic(path: Path, value: str | int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = _atomic_tmp(path)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(str(value))
        tmp.replace(path)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = _atomic_tmp(path)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        tmp.replace(path)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
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


def is_fast_slash_command(text: str) -> bool:
    command = slash_command_token(text)
    return command.split("@", 1)[0] == "/fast"


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

    body = "\n".join(normalize_codex_status_box_lines(cleaned)).strip()
    return f"Codex status\n{body}" if body else "Codex status not visible yet."


def normalize_codex_status_box_lines(lines: list[str]) -> list[str]:
    if not any(STATUS_BOX_LIMIT_RE.search(line) for line in lines):
        return lines
    normalized: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.lower() == "limit:" and normalized:
            normalized[-1] = f"{normalized[-1].rstrip()} limit:"
            continue
        if STATUS_BOX_LIMIT_RE.search(stripped):
            normalized.append(stripped)
            continue
        if stripped == "OpenAI Codex" or stripped.startswith(("Model:", "Path:", "Context window:")):
            normalized.append(stripped)
            continue
        if STATUS_BOX_FRAGMENT_RE.fullmatch(stripped):
            continue
        normalized.append(stripped)
    return normalized


def extract_codex_context_text(screen: str) -> str:
    status_text = extract_codex_status_text(screen)
    for line in status_text.splitlines():
        if line.startswith("Context window:"):
            return f"Codex context\n{line}"
    return "Codex context not visible yet."


def composer_lock_path() -> Path:
    return Path(os.environ.get("CODEX_COMPOSER_LOCK", "~/.local/state/codex-telegram-bridge/codex-composer.lock")).expanduser()


def extract_codex_footer_status_text(screen: str) -> str:
    footer = parse_codex_footer_status(screen)
    if not footer:
        return ""
    lines = ["Codex status"]
    if footer.get("model"):
        lines.append(f"Model: {footer['model']}")
    if footer.get("path"):
        lines.append(f"Path: {footer['path']}")
    lines.append(f"Context: {footer['context_used']}% used")
    if footer.get("five_hour_left"):
        lines.append(f"5h: {footer['five_hour_left']}% left")
    if footer.get("weekly_left"):
        lines.append(f"Weekly: {footer['weekly_left']}% left")
    return "\n".join(lines)


def extract_codex_footer_context_text(screen: str) -> str:
    footer = parse_codex_footer_status(screen)
    if not footer:
        return ""
    return f"Codex context\nContext: {footer['context_used']}% used"


def format_rate_limit_reset(value: Any) -> str:
    try:
        timestamp = int(value)
    except (TypeError, ValueError):
        return ""
    if timestamp <= 0:
        return ""
    return datetime.fromtimestamp(timestamp, KST).strftime("%Y-%m-%d %H:%M KST")


def extract_latest_rate_limit_resets(path: Path, max_bytes: int = 65536) -> dict[str, str]:
    try:
        size = path.stat().st_size
    except OSError:
        return {}
    start = max(0, size - max(1, max_bytes))
    resets: dict[str, str] = {}
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            handle.seek(start)
            if start > 0:
                handle.readline()
            for line in handle:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                payload = record.get("payload")
                if not isinstance(payload, dict) or payload.get("type") != "token_count":
                    continue
                rate_limits = payload.get("rate_limits")
                if not isinstance(rate_limits, dict):
                    continue
                primary = rate_limits.get("primary")
                if isinstance(primary, dict):
                    five_hour = format_rate_limit_reset(primary.get("resets_at"))
                    if five_hour:
                        resets["five_hour_reset"] = five_hour
                secondary = rate_limits.get("secondary")
                if isinstance(secondary, dict):
                    weekly = format_rate_limit_reset(secondary.get("resets_at"))
                    if weekly:
                        resets["weekly_reset"] = weekly
    except OSError:
        return {}
    return resets


def append_rate_limit_resets(status_text: str, path: Path | None) -> str:
    if not status_text or path is None:
        return status_text
    resets = extract_latest_rate_limit_resets(path)
    lines: list[str] = []
    if resets.get("five_hour_reset") and "5h reset:" not in status_text:
        lines.append(f"5h reset: {resets['five_hour_reset']}")
    if resets.get("weekly_reset") and "Weekly reset:" not in status_text:
        lines.append(f"Weekly reset: {resets['weekly_reset']}")
    return "\n".join([status_text, *lines]) if lines else status_text


def codex_bridge_launchd_label(node: str) -> str:
    return f"com.codex-telegram-bridge.{node}"


def codex_bridge_restart_command(node: str) -> list[str]:
    """codex 텔레그램 브릿지 재시작 명령(플랫폼 분기).

    macOS 본진/맥미니는 launchd 잡(노드별 라벨)으로, Linux 노드(라이덴/데스크탑/노트북)는
    systemd user unit(codex-bridge.service)으로 브릿지를 관리한다. 기존엔 launchctl 만 호출해
    launchctl 부재인 Linux 노드에서 재시작 버튼이 무동작이었다(T-260630-18, PR #246 리뷰노트).
    """
    if sys.platform == "darwin":
        return [
            "launchctl",
            "kickstart",
            "-k",
            f"gui/{os.getuid()}/{codex_bridge_launchd_label(node)}",
        ]
    unit = os.environ.get("CODEX_BRIDGE_SYSTEMD_UNIT", "codex-bridge.service")
    return ["systemctl", "--user", "restart", unit]


def restart_codex_bridge(node: str) -> None:
    subprocess.Popen(
        codex_bridge_restart_command(node),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def parse_codex_footer_status(screen: str) -> dict[str, str]:
    lines = [" ".join(line.strip().split()) for line in clean_pane_lines(screen)[-20:]]

    def footer_continuation(line: str) -> bool:
        return bool(
            line
            and (
                "·" in line
                or FOOTER_CONTEXT_RE.search(line)
                or FOOTER_FIVE_HOUR_RE.search(line)
                or FOOTER_WEEKLY_RE.search(line)
            )
        )

    for index in range(len(lines) - 1, -1, -1):
        if not FOOTER_CONTEXT_RE.search(lines[index]):
            continue
        start = index
        while start > 0 and index - start < 2 and footer_continuation(lines[start - 1]):
            start -= 1
        end = index
        while end < len(lines) - 1 and end - index < 2 and footer_continuation(lines[end + 1]):
            end += 1
        line = " ".join(lines[start : end + 1])
        parts = [part.strip() for part in line.split("·") if part.strip()]
        context_index = next(
            (index for index, part in enumerate(parts) if FOOTER_CONTEXT_RE.search(part)),
            -1,
        )
        if context_index < 0:
            continue
        context_match = FOOTER_CONTEXT_RE.search(parts[context_index])
        if not context_match:
            continue
        five_hour_left = ""
        weekly_left = ""
        for part in parts[context_index + 1 :]:
            if not five_hour_left and (match := FOOTER_FIVE_HOUR_RE.search(part)):
                five_hour_left = match.group("left")
                continue
            if not weekly_left and (match := FOOTER_WEEKLY_RE.search(part)):
                weekly_left = match.group("left")
                continue
        prefix = parts[:context_index]
        return {
            "model": prefix[0] if prefix else "",
            "path": prefix[1] if len(prefix) >= 2 else "",
            "context_used": context_match.group("used"),
            "five_hour_left": five_hour_left,
            # 78-col panes can truncate the weekly segment to "w…"; omit it rather
            # than touching the composer just to recover an optional value.
            "weekly_left": weekly_left,
        }
    return {}


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


def _clean_terminal_line(raw_line: str) -> str:
    line = ANSI_RE.sub("", raw_line).strip()
    if "│" in raw_line:
        parts = raw_line.split("│")
        line = "│".join(parts[1:-1]).strip() if len(parts) >= 3 else line.strip(" │")
    return re.sub(r"\s{2,}", " ", line).strip()


def extract_fast_mode_notice(screen: str) -> str:
    lines = [_clean_terminal_line(line) for line in (screen or "").splitlines()]
    lines = [line for line in lines if line]

    for line in reversed(lines[-40:]):
        normalized = line.lower()
        if normalized == "fast":
            return "패스트모드 켜졌습니다"
        if re.search(r"\bfast(?:\s+mode)?\s*[:=-]?\s*(?:off|disabled)\b", normalized):
            return "패스트모드 꺼졌습니다"
        if re.search(r"\bfast(?:\s+mode)?\s*[:=-]?\s*(?:on|enabled)\b", normalized):
            return "패스트모드 켜졌습니다"

    for line in reversed(lines[-20:]):
        normalized = line.lower()
        if "context" not in normalized or "·" not in normalized:
            continue
        model_segment = normalized.split("·", 1)[0]
        return "패스트모드 켜졌습니다" if re.search(r"\bfast\b", model_segment) else "패스트모드 꺼졌습니다"

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


def format_reasoning_mirror(text: str) -> str:
    body = truncate_text(strip_node_emoji_header(text), REASONING_MIRROR_LIMIT).strip()
    return f"{REASONING_HEADER}\n{body}" if body else ""


def format_flow_mirror(text: str) -> str:
    body = flow_mirror_body(text)
    return f"{FLOW_MIRROR_HEADER}\n{body}" if body else ""


def flow_mirror_body(text: str) -> str:
    return truncate_text(strip_node_emoji_header(text), FLOW_MIRROR_LIMIT).strip()


def format_ambient_directive(
    gist: str,
    from_node: str | None = None,
    task: str | None = None,
    self_emoji: str | None = None,
    self_alias: str | None = None,
) -> str:
    # ⚙️ A2 받은-지시 카드 본문 — directive-received-ack.sh 가 신호로 남긴 디렉티브 gist
    # (이미 보일러플레이트 헤더 제거된 1~2줄)에 "발신 → 수신 · task" 라우트 줄을 얹는다.
    # from/task 는 신호 메타. claude-telegram-bridge.py format_ambient_directive 동형
    # (claude 는 full text 파싱, codex 는 신호 메타 사용). (T-260630-33)
    body = (gist or "").strip()
    # alt3 이야기체 (spec v0.2 매트릭스 directive_sent aniki_dm 동형, claude PR#358 이관 —
    # T-260703-14): 라우트 줄을 사람 문장으로. 발신·수신 라벨이 둘 다 해석될 때만 —
    # 못 읽으면 아래 v0.1 카드 그대로 (fallback, 유실 0).
    if from_node and alt3_narrative_enabled():
        s_label, s_emoji = node_label_emoji(from_node)
        recv_token = self_alias or node_defaults()[0]
        r_label, r_emoji = node_label_emoji(recv_token)
        if s_label and r_label:
            sentence = (
                f"{s_emoji} {s_label}{subject_particle(s_label)} "
                f"{r_emoji} {r_label}에게 맡겼어요"
            )
            if task:
                sentence = f"{sentence} ({task})"
            parts = [sentence]
            if body:
                parts.append(body)
            return "\n".join(parts)[:AMBIENT_DIRECTIVE_LIMIT].strip()
    route_line = ""
    if from_node:
        label, emoji = node_label_emoji(from_node)
        sender = f"{emoji} {label}".strip()
        recv = self_emoji if self_emoji is not None else node_defaults()[1]
        route_line = f"{sender} → {recv}"
        if task:
            route_line = f"{route_line} · {task}"
    parts: list[str] = []
    if route_line:
        parts.append(route_line)
    if body:
        parts.append(body)
    elif task and not route_line:
        parts.append(task)
    out = "\n".join(parts)[:AMBIENT_DIRECTIVE_LIMIT].strip()
    return f"{AMBIENT_DIRECTIVE_HEADER}\n{out}" if out else ""


def format_sent_directive(text: str, from_alias: str, to_alias: str) -> str:
    body = strip_inline_node_emoji_header(strip_node_emoji_header(text or "")).strip()
    if not body:
        return ""
    if (from_alias or "").strip() == (to_alias or "").strip():
        gist = body[:AMBIENT_DIRECTIVE_LIMIT].strip()
        return f"{TERMINAL_INPUT_HEADER}\n{gist}" if gist else ""
    sender_label, sender_emoji = node_label_emoji(from_alias)
    receiver_label, receiver_emoji = node_label_emoji(to_alias)
    sender = f"{sender_emoji} {sender_label}".strip()
    receiver = f"{receiver_emoji} {receiver_label}".strip()
    route_line = f"{sender} → {receiver}" if sender or receiver else ""
    parts = [part for part in (route_line, body) if part]
    gist = "\n".join(parts)[:AMBIENT_DIRECTIVE_LIMIT].strip()
    return f"{SENT_DIRECTIVE_HEADER}\n{gist}" if gist else ""


def flow_step_summary(text: str, limit: int = FLOW_MIRROR_LINE_LIMIT) -> str:
    """One clean line for a single flow step (claude parity).

    Collapse all whitespace into a single line, drop node/reasoning headers, and
    hard-truncate with an ellipsis — NO multi-line ``[truncated]`` footer. This
    keeps each accumulated step short so many fit inside one edit-in-place card
    instead of overflowing FLOW_MIRROR_LIMIT into multiple long messages.
    """
    body = " ".join(strip_node_emoji_header(text).split())
    if body.startswith(REASONING_HEADER):
        body = body[len(REASONING_HEADER) :].strip()
    # function_call one-liners ("• <label> · <detail>") are already collapsed and
    # length-capped (no ellipsis) by function_call_flow_summary — keep them whole
    # so the claude-parity flow card shows the full tool detail. (T-260628-43)
    if body.startswith("• "):
        return body.strip()
    if len(body) > limit:
        body = body[:limit].rstrip() + "…"
    return body.strip()


# Tool-call → Korean label map for the flow card (claude parity). Each codex
# function_call becomes ONE short line "• <label> · <detail>" instead of long
# commentary prose that overflowed and got ellipsis-truncated. (T-260628-43)
TOOL_LABEL_KO = {
    "exec_command": "실행",
    "write_stdin": "입력",
    "apply_patch": "편집",
    "view_image": "이미지확인",
    "imagegen": "이미지생성",
    "update_plan": "계획",
    "parallel": "병렬",
    "web": "웹열기",
    "web_open": "웹열기",
    "open_page": "웹열기",
    "search": "웹검색",
    "web_search": "웹검색",
    "finance": "금융조회",
    "weather": "날씨조회",
    "sports": "스포츠조회",
    "time": "시간조회",
}

# Substring families: when an exact name is not in TOOL_LABEL_KO, match these
# family keywords so vendor-prefixed tool names (e.g. "browser.web_search")
# still get the right Korean label.
TOOL_LABEL_FAMILIES = (
    ("web_search", "웹검색"),
    ("search", "웹검색"),
    ("web", "웹열기"),
    ("finance", "금융조회"),
    ("weather", "날씨조회"),
    ("sports", "스포츠조회"),
    ("time", "시간조회"),
)

PATCH_TARGET_RE = re.compile(
    r"^\*\*\*\s+(?:Update|Add|Delete)\s+File:\s*(?P<target>.+?)\s*$",
    re.MULTILINE,
)

FLOW_TOOL_DETAIL_LIMIT = 140

# T-260706-01 (2026-07-06 '코덱스 흐름카드 규칙요약' 요청): 셸 명령 원문을
# 말줄임으로 자르는 대신 코드 규칙으로 '동작+대상'만 추출 — claude 카드(모델이 붙인 짧은
# 설명)와 정보밀도 패리티. LLM 호출 0 (토큰 비용 0).
FLOW_SUMMARY_MAX_SEGMENTS = 3
FLOW_SUMMARY_TOKEN_CAP = 40
FLOW_SUMMARY_QUOTED_CAP = 20
_SHELL_WRAPPER_NAMES = {"bash", "sh", "zsh", "dash"}
_SHELL_NOISE_COMMANDS = {"cd", "set", "export", "trap", "true", ":", "sleep"}
_PYTHON_NAMES_RE = re.compile(r"^python[0-9.]*$")
_REDIRECT_DROP_RE = re.compile(r"^\d*>>?(&\d+|/dev/null)$|^</dev/null$")
_ENV_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def _flow_cap_text(text: str, cap: int) -> str:
    """cap 초과 시 단어 경계에서 자르고 '…' — 흐름카드 요약용 미니 컷."""
    if len(text) <= cap:
        return text
    cut = text[:cap]
    sp = cut.rfind(" ")
    if sp > 0:
        cut = cut[:sp]
    return cut.rstrip() + "…"


def _flow_shorten_token(token: str) -> str:
    """경로는 basename 으로, 인용 문자열/장토큰은 컷 — 폰 카드 가독성용."""
    if " " in token:  # shlex 언쿼트된 인용 문자열
        return _flow_cap_text(token, FLOW_SUMMARY_QUOTED_CAP)
    if "/" in token and not token.startswith("-") and not token.startswith("http"):
        stripped = token.rstrip("/")
        base = stripped.rsplit("/", 1)[-1]
        if base:
            token = base + ("/" if token.endswith("/") else "")
    if len(token) > FLOW_SUMMARY_TOKEN_CAP:
        token = token[: FLOW_SUMMARY_TOKEN_CAP - 1].rstrip() + "…"
    return token


def _flow_split_segments(cmd: str) -> list[str]:
    """&&·||·|·;·& 를 인용부호 밖에서만 분할 (2>&1 의 & 는 리다이렉트라 제외)."""
    segments: list[str] = []
    buf: list[str] = []
    quote = ""
    i = 0
    while i < len(cmd):
        ch = cmd[i]
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = ""
            i += 1
            continue
        if ch in "'\"":
            quote = ch
            buf.append(ch)
            i += 1
            continue
        two = cmd[i : i + 2]
        if two in ("&&", "||"):
            segments.append("".join(buf))
            buf = []
            i += 2
            continue
        if ch in "|;" or (ch == "&" and (i == 0 or cmd[i - 1] != ">")):
            segments.append("".join(buf))
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    segments.append("".join(buf))
    return [seg.strip() for seg in segments if seg.strip()]


def _flow_summarize_segment(segment: str) -> str | None:
    """세그먼트 1개 → '명령 [대상 최대 2]' 요약. 노이즈면 None, 파싱 불능이면 ValueError."""
    heredoc = segment.find("<<")
    if heredoc >= 0:
        segment = segment[:heredoc].strip()
    if not segment:
        return None
    tokens = shlex.split(segment)  # 인용 불균형 시 ValueError → 호출측 폴백
    while tokens and _ENV_ASSIGN_RE.match(tokens[0]):
        tokens.pop(0)
    if not tokens:
        return None

    head = tokens[0].rstrip("/").rsplit("/", 1)[-1] or tokens[0]
    rest = tokens[1:]

    # 셸 래퍼(bash -lc "...") 언랩 → 내부 명령을 재귀 요약
    if head in _SHELL_WRAPPER_NAMES:
        flags = [t for t in rest if t.startswith("-")]
        inner = next((t for t in rest if not t.startswith("-")), "")
        if any("c" in f for f in flags) and inner:
            return summarize_shell_command(inner) or None

    if head in _SHELL_NOISE_COMMANDS:
        return None

    # python -m mod → mod 가 명령 / python script.py → 스크립트 basename 노출
    if _PYTHON_NAMES_RE.match(head) and rest:
        if rest[0] == "-m" and len(rest) >= 2:
            head = rest[1]
            rest = rest[2:]
        elif rest[0].endswith(".py"):
            head = f"{head} {_flow_shorten_token(rest[0])}"
            rest = rest[1:]
    elif head in {"git", "gh", "npm", "cargo", "docker", "flutter", "dart", "systemctl", "tmux"} and rest and not rest[0].startswith("-"):
        head = f"{head} {rest[0]}"
        rest = rest[1:]

    salient: list[str] = []
    idx = 0
    while idx < len(rest):
        token = rest[idx]
        idx += 1
        if _REDIRECT_DROP_RE.match(token):
            continue
        if token in {">", ">>"}:
            if idx < len(rest):
                salient.append(">" + _flow_shorten_token(rest[idx]))
                idx += 1
            continue
        if token.startswith("-") and len(token) > 1:
            continue
        if not any(ch.isalnum() for ch in token):
            continue
        if len(salient) < 2:
            salient.append(_flow_shorten_token(token))

    if head == "echo" and not salient:
        return None  # 구분선 echo(===) 노이즈
    return " ".join([head, *salient]).strip()


def summarize_shell_command(cmd: str) -> str:
    """셸 명령 원문 → 규칙 요약 (T-260706-01). 파싱 불능/전부 노이즈면 원문 반환."""
    original = (cmd or "").strip()
    if not original:
        return original
    try:
        summaries: list[str] = []
        for segment in _flow_split_segments(original):
            summary = _flow_summarize_segment(segment)
            if summary:
                summaries.append(summary)
    except ValueError:
        return original
    if not summaries:
        return original
    shown = summaries[:FLOW_SUMMARY_MAX_SEGMENTS]
    out = " → ".join(shown)
    remaining = len(summaries) - len(shown)
    if remaining > 0:
        out += f" +{remaining}"
    return out


def compact_flow_detail(detail: str, limit: int = FLOW_TOOL_DETAIL_LIMIT) -> str:
    """flow 카드 detail 컷 — T-260705-12 (사용자 제보 '쓰다가 끊긴 느낌').

    limit 초과 시 토큰(공백) 경계에서 자르고 '…' 를 붙인다. 경계가 컷 지점에서
    너무 멀면(마지막 30자 밖) 하드컷+…. limit 이하는 원문 그대로. 기존
    'no ellipsis (claude parity)' 규칙은 결과 비대칭이라 폐기 — claude 카드는
    짧은 설명문(description)이라 컷에 거의 안 걸리지만 codex 카드는 셸 명령
    원문이라 항상 걸려 mid-token 끊김('port=922' 류)으로 보였다."""
    if len(detail) <= limit:
        return detail
    cut = detail[:limit]
    sp = cut.rfind(" ")
    if sp >= limit - 30:
        cut = cut[:sp]
    return cut.rstrip(" ·|&;,(") + "…"


GENERIC_DETAIL_KEYS = (
    "path",
    "file_path",
    "query",
    "q",
    "url",
    "pattern",
    "prompt",
    "skill",
    "location",
    "ticker",
    "ref_id",
    "uri",
    "workdir",
)


def tool_label_ko(name: str) -> str:
    key = (name or "").strip()
    if not key:
        return "도구"
    if key in TOOL_LABEL_KO:
        return TOOL_LABEL_KO[key]
    lowered = key.lower()
    for family, label in TOOL_LABEL_FAMILIES:
        if family in lowered:
            return label
    return key


def parse_tool_arguments(payload: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """Return (parsed_dict, raw_string) for a function_call payload.

    Reads ``arguments`` (or ``input``). A JSON-object string is parsed into a
    dict; a freeform string is kept as the raw value. (T-260628-43)
    """
    raw_value = payload.get("arguments")
    if raw_value is None:
        raw_value = payload.get("input")
    if isinstance(raw_value, dict):
        return raw_value, ""
    raw = str(raw_value or "")
    stripped = raw.strip()
    if stripped.startswith("{"):
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict):
            return parsed, ""
    return {}, raw


def patch_target_from_text(text: str) -> str:
    match = PATCH_TARGET_RE.search(text or "")
    if not match:
        return ""
    return match.group("target").strip()


def tool_detail(name: str, args: dict[str, Any], raw: str) -> str:
    key = (name or "").strip()
    if key == "apply_patch":
        target = patch_target_from_text(raw) or patch_target_from_text(
            str(args.get("patch") or args.get("input") or "")
        )
        if target:
            return target
    if key == "exec_command":
        cmd = args.get("cmd")
        if isinstance(cmd, list):
            cmd = " ".join(str(part) for part in cmd)
        if cmd:
            # T-260706-01: 원문 대신 규칙 요약 — 말줄임 컷은 compact_flow_detail 최종 가드로만
            return summarize_shell_command(str(cmd))
    for detail_key in GENERIC_DETAIL_KEYS:
        value = args.get(detail_key)
        if value:
            if isinstance(value, list):
                value = " ".join(str(part) for part in value)
            return str(value)
    if raw.strip():
        return raw.strip()
    return ""


def function_call_flow_summary(payload: dict[str, Any]) -> str:
    name = str(payload.get("name") or "")
    args, raw = parse_tool_arguments(payload)
    label = tool_label_ko(name)
    detail = " ".join(tool_detail(name, args, raw).split())
    detail = compact_flow_detail(detail)
    return f"• {label} · {detail}".rstrip(" ·").rstrip() if not detail else f"• {label} · {detail}"


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
    if body.startswith(REASONING_HEADER):
        body = body[len(REASONING_HEADER) :].strip()
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
    label = truncate_text(label or "(empty prompt)", 160)
    elapsed_minutes = max(1, int(elapsed_seconds // 60))
    recent = format_progress_summary(recent_progress, 220) if recent_progress else ""
    task_clause = f"{task_id} " if task_id else ""
    if recent:
        headline = f"✓ Progress so far — {recent}"
        body = (
            f"{task_clause}{label} has been running for about {elapsed_minutes} min. "
            f"Recent progress: {recent}. I will send the final answer as soon as it is ready."
        )
    else:
        headline = "✓ Still working — no progress note yet"
        body = (
            f"{task_clause}{label} has been running for about {elapsed_minutes} min. "
            "I am waiting for the final answer."
        )
    return f"Progress update\n\n{headline}\n\n{body}"


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
        raise RuntimeError("CRB_BOT_TOKEN or TAB_BOT_TOKEN is required")
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


@dataclass(frozen=True, kw_only=True)
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
    generated_image_roots: tuple[Path, ...] = ()
    generated_image_autosend: bool = True
    generated_image_window_sec: int = 1800
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
    reasoning_mirror: bool
    flow_mirror: bool
    signal_path: Path | None = None
    directive_signal_path: Path | None = None

    @classmethod
    def from_env(cls) -> "Config":
        default_node, default_emoji = node_defaults()
        node = env("CRB_NODE", default_node) or default_node
        state_dir = Path(
            env("CRB_STATE_DIR", env("TAB_STATE_DIR", "~/.local/state/codex-telegram-bridge") or "") or ""
        ).expanduser()
        workdir = Path(env("TAB_WORKDIR", str(HOME)) or str(HOME)).expanduser()
        token_file_raw = env("CRB_TOKEN_FILE")
        token_file = (
            Path(token_file_raw).expanduser()
            if token_file_raw
            else None
            if env("TAB_BOT_TOKEN")
            else Path("~/.config/codex-telegram-bridge/token.json").expanduser()
        )
        default_state_path = state_dir / f"codex-repl-bridge-{node}.state.json"
        return cls(
            node=node,
            emoji=env("CRB_EMOJI", env("TAB_PREFIX", default_emoji) or default_emoji)
            or default_emoji,
            token_file=token_file,
            chat_id=env("CRB_CHAT_ID", env("TAB_CHAT_ID", "") or "")
            or "",
            state_dir=state_dir,
            tmux_bin=env("CRB_TMUX_BIN", "tmux") or "tmux",
            tmux_socket=env("CRB_TMUX_SOCKET", env("TAB_AGENT_TMUX_SOCKET", "codex") or "codex")
            or "codex",
            tmux_session=env("CRB_TMUX_SESSION", env("TAB_AGENT_TMUX_SESSION", "codex") or "codex")
            or "codex",
            submit_key=env("CRB_TMUX_SUBMIT_KEY", env("TAB_AGENT_TMUX_SUBMIT_KEY", "Tab") or "Tab")
            or "Tab",
            enter_count=int_env(
                "CRB_TMUX_ENTER_COUNT",
                int_env("TAB_AGENT_TMUX_ENTER_COUNT", 5),
                minimum=1,
            ),
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
                600,
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
                [state_dir, workdir, HOME / ".codex/generated_images", Path("/tmp")],
            ),
            generated_image_roots=path_env_list(
                env("CRB_GENERATED_IMAGE_ROOTS"),
                [HOME / ".codex/generated_images"],
            ),
            generated_image_autosend=bool_env("CRB_GENERATED_IMAGE_AUTOSEND", True),
            generated_image_window_sec=int_env(
                "CRB_GENERATED_IMAGE_WINDOW_SEC",
                1800,
                minimum=0,
            ),
            max_attachment_bytes=int_env(
                "CRB_MAX_ATTACHMENT_BYTES",
                50 * 1024 * 1024,
                minimum=1024,
            ),
            telegram_chunk=int_env("CRB_TG_CHUNK", 4096, minimum=512),
            poll_timeout=int_env("CRB_TG_POLL_TIMEOUT", 2, minimum=1),
            start_at_end=(env("CRB_START_AT_END", "1") or "1").lower()
            in {"1", "true", "yes", "on"},
            state_path=Path(env("CRB_STATE_PATH", str(default_state_path)) or "").expanduser(),
            backfill_enabled=bool_env("CRB_BACKFILL", True),
            backfill_max=clamp(int_env("CRB_BACKFILL_MAX", 1, minimum=1), 1, 3),
            backfill_window_sec=int_env("CRB_BACKFILL_WINDOW_SEC", 600, minimum=1),
            tail_scan_bytes=int_env("CRB_TAIL_SCAN_BYTES", 65536, minimum=1024),
            state_ring_cap=int_env("CRB_STATE_RING_CAP", 64, minimum=1),
            bridge_kill=bool_env("CRB_KILL", False),
            reasoning_mirror=bool_env("CRB_REASONING_MIRROR", True),
            flow_mirror=bool_env("CRB_FLOW_MIRROR", True),
            signal_path=optional_path_env("CRB_SIGNAL_PATH", env("TAB_LOCAL_INPUT")),
            directive_signal_path=optional_path_env(DIRECTIVE_SIGNAL_ENV, str(state_dir / "received-directive.jsonl")),
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


# F3≡C3 (T-260705-72): 429 flood control 대응 상수 — claude-telegram-bridge.py 동형.
# 고정 2s×N 재시도 예산이 통상 30s+ flood 대기를 못 넘겨 발신이 영구 유실되던 갭.
TELEGRAM_FLOOD_MAX_WAITS = 3
TELEGRAM_FLOOD_WAIT_CAP_SECONDS = 61.0


def telegram_retry_after_seconds(body: str, headers: Any = None, default: float = 3.0) -> float:
    """429 응답에서 대기 초를 해석: body JSON parameters.retry_after → Retry-After 헤더 → default."""
    try:
        payload = json.loads(body)
        value = (payload.get("parameters") or {}).get("retry_after")
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
            return float(value)
    except Exception:  # noqa: BLE001
        pass
    try:
        raw = headers.get("Retry-After") if headers is not None else None
        if raw is not None:
            return max(0.0, float(raw))
    except Exception:  # noqa: BLE001
        pass
    return default


class CodexEgressRouteError(RuntimeError):
    """Raised before a Codex bot token can send to a non-Codex chat route."""


def codex_secret_token_files() -> list[Path]:
    return []


def read_codex_secret_token(path: Path) -> str:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    return str(payload.get("api_key") or "").strip()


def codex_registry_aliases_for_token(token: str) -> list[str]:
    if not token:
        return []
    aliases: list[str] = []
    for path in codex_secret_token_files():
        if read_codex_secret_token(path) == token:
            alias = path.stem.removeprefix("codex_bridge_")
            aliases.append(alias or path.name)
    return aliases


def expected_codex_chat_id() -> str:
    return (env("CRB_CHAT_ID") or env("TAB_CHAT_ID") or "").strip()


def assert_codex_egress_chat(token: str, chat_id: str | None) -> tuple[bool, str]:
    aliases = codex_registry_aliases_for_token(token)
    if not aliases:
        return False, ""
    expected = expected_codex_chat_id()
    if not expected:
        raise CodexEgressRouteError(
            "Codex Telegram egress blocked: CRB_CHAT_ID/TAB_CHAT_ID is required for "
            "codex_bridge_<node>.json tokens; refusing legacy TELEGRAM_CHAT_ID fallback."
        )
    if str(chat_id or "") != expected:
        alias_hint = ",".join(sorted(aliases))
        raise CodexEgressRouteError(
            "Codex Telegram egress blocked: chat_id mismatch for "
            f"codex_bridge registry alias={alias_hint}; target must equal CRB_CHAT_ID/TAB_CHAT_ID."
        )
    return True, expected


class TelegramClient:
    def __init__(self, token: str, chat_id: str, emoji: str, chunk_size: int) -> None:
        self.token = token
        self.api = f"https://api.telegram.org/bot{token}"
        self.chat_id = chat_id
        self.emoji = emoji
        self.chunk_size = chunk_size
        self.codex_route_guard_active, self.codex_expected_chat_id = assert_codex_egress_chat(token, chat_id)

    def assert_outbound_chat(self, chat_id: Any) -> None:
        if self.codex_route_guard_active and chat_id is not None and str(chat_id) != self.codex_expected_chat_id:
            raise CodexEgressRouteError(
                "Codex Telegram egress blocked: chat_id mismatch for codex_bridge registry token."
            )

    def call(
        self,
        method: str,
        *,
        _request_timeout: float = 60.0,
        _attempts: int = 3,
        _retry_delay: float = 2.0,
        **params: Any,
    ) -> dict[str, Any] | None:
        self.assert_outbound_chat(params.get("chat_id"))
        cutover_payload = mesh_cutover_call(method, params)
        if cutover_payload is not None:
            return cutover_payload
        data = urllib.parse.urlencode(params).encode()
        url = f"{self.api}/{method}"
        attempts = max(1, _attempts)
        # C3 (T-260705-72): 429 는 retry_after 를 지켜 기다렸다 재시도 — flood 대기는
        # 일반 재시도 예산과 별도로 센다. 단발(_attempts=1) 호출(typing 등)은 fast-fail 유지.
        flood_budget = TELEGRAM_FLOOD_MAX_WAITS if attempts > 1 else 0
        attempt = 0
        flood_waits = 0
        while True:
            try:
                request = urllib.request.Request(url, data=data)
                with urllib.request.urlopen(request, timeout=_request_timeout) as response:
                    payload = json.load(response)
                mesh_ledger_record(method, params.get("chat_id"), params.get("text"), payload, message_id=params.get("message_id"))
                return payload if isinstance(payload, dict) else None
            except urllib.error.HTTPError as exc:
                body = exc.read().decode("utf-8", errors="replace")
                if exc.code == 429 and flood_waits < flood_budget:
                    flood_waits += 1
                    wait = min(telegram_retry_after_seconds(body, exc.headers) + 1.0, TELEGRAM_FLOOD_WAIT_CAP_SECONDS)
                    log("TGERR", f"{method} 429 flood; waiting {wait:.0f}s ({flood_waits}/{flood_budget})")
                    time.sleep(wait)
                    continue
                if 400 <= exc.code < 500:
                    # 4xx(flood 예산 소진 포함)는 재시도 무의미 — 즉시 단락.
                    log("TGERR", f"{method} failed: HTTP {exc.code} {body[:200]}")
                    mesh_ledger_record(method, params.get("chat_id"), params.get("text"), None, message_id=params.get("message_id"))
                    return None
                attempt += 1
                if attempt >= attempts:
                    log("TGERR", f"{method} failed: HTTP {exc.code} {body[:200]}")
                    mesh_ledger_record(method, params.get("chat_id"), params.get("text"), None, message_id=params.get("message_id"))
                    return None
                if _retry_delay > 0:
                    time.sleep(_retry_delay)
            except Exception as exc:  # noqa: BLE001
                attempt += 1
                if attempt >= attempts:
                    log("TGERR", f"{method} failed: {exc}")
                    mesh_ledger_record(method, params.get("chat_id"), params.get("text"), None, message_id=params.get("message_id"))
                    return None
                if _retry_delay > 0:
                    time.sleep(_retry_delay)

    def call_multipart(
        self,
        method: str,
        fields: dict[str, str],
        file_field: str,
        file_path: Path,
    ) -> dict[str, Any] | None:
        self.assert_outbound_chat(fields.get("chat_id"))
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
        # C2·C3 (T-260705-72): call() 과 동형 — 429 는 retry_after 대기 후 재시도(별도 예산),
        # 그 외 4xx(치수거부 PNG 등 영구 오류)는 즉시 단락해 폴백/공지 경로로 넘긴다.
        attempt = 0
        flood_waits = 0
        while True:
            try:
                with urllib.request.urlopen(request, timeout=60) as response:
                    payload = json.load(response)
                return payload if isinstance(payload, dict) else None
            except urllib.error.HTTPError as exc:
                body = exc.read().decode("utf-8", errors="replace")
                if exc.code == 429 and flood_waits < TELEGRAM_FLOOD_MAX_WAITS:
                    flood_waits += 1
                    wait = min(telegram_retry_after_seconds(body, exc.headers) + 1.0, TELEGRAM_FLOOD_WAIT_CAP_SECONDS)
                    log("TGERR", f"{method} upload 429 flood; waiting {wait:.0f}s ({flood_waits}/{TELEGRAM_FLOOD_MAX_WAITS})")
                    time.sleep(wait)
                    continue
                if 400 <= exc.code < 500:
                    log("TGERR", f"{method} upload failed: HTTP {exc.code} {body[:200]}")
                    return None
                attempt += 1
                if attempt >= 3:
                    log("TGERR", f"{method} upload failed: HTTP {exc.code} {body[:200]}")
                    return None
                time.sleep(2)
            except Exception as exc:  # noqa: BLE001
                attempt += 1
                if attempt >= 3:
                    log("TGERR", f"{method} upload failed: {exc}")
                    return None
                time.sleep(2)

    def send_typing(self) -> bool:
        payload = self.call(
            "sendChatAction",
            _request_timeout=10,
            _attempts=1,
            chat_id=self.chat_id,
            action="typing",
        )
        ok = bool(payload and payload.get("ok") and payload.get("result"))
        if payload and not ok:
            detail = payload.get("description") or payload.get("error_code") or payload
            log("TGERR", f"sendChatAction rejected: {detail}")
        return ok

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
        # T-260705-43: 단발 통짜 read()는 텔레그램 파일서버 지연 국면에
        # 'read operation timed out' 으로 그대로 실패(2026-07-05 본진+라이덴 크로스노드 재현).
        # chunk read 는 소켓 타임아웃이 청크마다 갱신되고, transient 실패는 백오프 재시도.
        last_err: Exception | None = None
        for attempt in range(3):
            try:
                buf = bytearray()
                with urllib.request.urlopen(request, timeout=60) as response:
                    while True:
                        chunk = response.read(1 << 16)
                        if not chunk:
                            break
                        buf.extend(chunk)
                output_path.write_bytes(bytes(buf))
                return output_path
            except OSError as err:
                last_err = err
                if attempt < 2:
                    time.sleep(2 * (attempt + 1))
        raise RuntimeError(f"file download failed after 3 attempts: {last_err}")

    def with_emoji_prefix(self, text: str) -> str:
        original_text = text
        first_line = text.splitlines()[0].strip() if text.splitlines() else ""
        private_chat = is_private_chat_id(self.chat_id)
        if first_line == self.emoji and not private_chat:
            return text
        text = strip_inline_node_emoji_header(strip_node_emoji_header(text))
        if private_chat:
            text = strip_leading_emoji_decoration(text)
            return text if text.strip() else original_text
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

    def raw_chunks(self, text: str) -> list[str]:
        text = text or "(empty response)"
        out = [text[: self.chunk_size]]
        rest = text[self.chunk_size :]
        while rest:
            out.append(rest[: self.chunk_size])
            rest = rest[self.chunk_size :]
        return out

    def send(self, text: str, reply_to_message_id: int | None = None) -> bool:
        ok = True
        sent_count = 0
        for segment in split_answer_for_code_blocks(text or "(empty response)"):
            body = str(segment.get("body") or "")
            if segment.get("code"):
                language = str(segment.get("language") or "")
                chunks = self.raw_chunks(body)
            else:
                language = ""
                chunks = self.chunks(body)
            for chunk in chunks:
                params: dict[str, Any] = {"chat_id": self.chat_id, "text": chunk}
                if segment.get("code"):
                    params["entities"] = pre_entities_json(chunk, language)
                if reply_to_message_id and sent_count == 0:
                    # alt3 타래 (spec v0.2 §5, T-260703-14): 분할 시 첫 chunk 만 루트에 단다.
                    # §5-4 — 루트 삭제 등 거부 케이스 포함 유실 0.
                    params["reply_to_message_id"] = reply_to_message_id
                    params["allow_sending_without_reply"] = "true"
                payload = self.call("sendMessage", **params)
                ok = ok and bool(payload and payload.get("ok"))
                sent_count += 1
        return ok

    def send_update_button(self, text: str, callback_data: str) -> None:
        reply_markup = json.dumps(
            {"inline_keyboard": [[{"text": "\U0001f504 지금 업데이트", "callback_data": callback_data}]]},
            ensure_ascii=False,
        )
        self.call("sendMessage", chat_id=self.chat_id, text=self.with_emoji_prefix(text), reply_markup=reply_markup)

    def send_restart_button(self, text: str, callback_data: str) -> bool:
        reply_markup = json.dumps(
            {"inline_keyboard": [[{"text": "\U0001f504 브릿지 재시작", "callback_data": callback_data}]]},
            ensure_ascii=False,
        )
        payload = self.call(
            "sendMessage",
            chat_id=self.chat_id,
            text=self.with_emoji_prefix(text),
            reply_markup=reply_markup,
        )
        return bool(payload and payload.get("ok"))

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
            if not payload or not payload.get("ok"):
                # C2 (T-260705-72): 치수/포맷 거부 등 sendPhoto 영구 거부는 문서로 폴백
                # (video/voice/audio 와 동형). 폴백 부재가 첨부 무한 재전송 폭풍의 트리거였다.
                payload = self.call_multipart(
                    "sendDocument",
                    {"chat_id": self.chat_id},
                    "document",
                    path,
                )
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

    def send_approval_prompt(self, prompt: "ApprovalPrompt") -> int | None:
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
        prompt: "ApprovalPrompt",
        status_text: str,
        selected: "ApprovalOption | None" = None,
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

    def send_choice_prompt(self, prompt: "ChoicePrompt") -> int | None:
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
        prompt: "ChoicePrompt",
        status_text: str,
        selected: "ChoiceOption | None" = None,
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

    @contextmanager
    def composer_lock(self):
        path = composer_lock_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as lock_file:
            # ⚠️ 제거 금지 (DO NOT REMOVE) — codex composer single-writer guard (T-260628-35)
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

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

    def _paste_prompt_unlocked(self, prompt: str) -> None:
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

    def paste_prompt(self, prompt: str) -> None:
        with self.composer_lock():
            self._paste_prompt_unlocked(prompt)

    def _clear_composer_unlocked(self) -> None:
        self.verify()
        # C-u only clears text before the cursor. After a rejected slash command,
        # Codex can leave the cursor before stale text, so clear both sides.
        for key in ("C-e", "C-u", "C-a", "C-k"):
            self.tmux("send-keys", "-t", self.config.pane_target, key)
            time.sleep(0.05)
        time.sleep(0.1)

    def clear_composer(self) -> None:
        with self.composer_lock():
            self._clear_composer_unlocked()

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

    @contextmanager
    def temporary_window_width(self, columns: int = STATUS_WIDE_CAPTURE_COLUMNS):
        try:
            out = self.tmux(
                "display-message",
                "-p",
                "-t",
                self.config.pane_target,
                "#{window_id} #{window_width} #{window_height} #{pane_width} #{pane_height}",
            )
            window_id, window_width, window_height, pane_width, pane_height = out.stdout.strip().split()
            original_window_width = int(window_width)
            original_window_height = int(window_height)
            original_pane_width = int(pane_width)
            original_pane_height = int(pane_height)
        except Exception as exc:  # noqa: BLE001
            log("REPL", f"wide status capture unavailable: {exc}")
            yield
            return

        target_width = max(columns, original_window_width)
        resized = False
        if target_width > original_window_width:
            try:
                self.tmux("resize-window", "-t", window_id, "-x", str(target_width), "-y", str(original_window_height))
                self.tmux("resize-pane", "-t", self.config.pane_target, "-x", str(target_width))
                resized = True
                time.sleep(0.15)
            except Exception as exc:  # noqa: BLE001
                log("REPL", f"wide status capture resize failed: {exc}")
        try:
            yield
        finally:
            if resized:
                try:
                    self.tmux(
                        "resize-pane",
                        "-t",
                        self.config.pane_target,
                        "-x",
                        str(original_pane_width),
                        "-y",
                        str(original_pane_height),
                    )
                    self.tmux(
                        "resize-window",
                        "-t",
                        window_id,
                        "-x",
                        str(original_window_width),
                        "-y",
                        str(original_window_height),
                    )
                except Exception as exc:  # noqa: BLE001
                    log("REPL", f"wide status capture restore failed: {exc}")

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


def claims_image_delivery_success(text: str) -> bool:
    body = normalize_prompt(text)
    if not body or IMAGE_DELIVERY_CLAIM_NEGATIVE_RE.search(body):
        return False
    return bool(
        IMAGE_DELIVERY_CLAIM_IMAGE_RE.search(body)
        and IMAGE_DELIVERY_CLAIM_SENT_RE.search(body)
    )


def requests_image_delivery(text: str) -> bool:
    body = normalize_prompt(text)
    if not body:
        return False
    return bool(
        IMAGE_DELIVERY_PROMPT_OBJECT_RE.search(body)
        and IMAGE_DELIVERY_PROMPT_ACTION_RE.search(body)
    )


def image_delivery_unverified_text() -> str:
    return (
        "이미지 생성 또는 첨부 요청을 처리했지만, 텔레그램 사진 업로드 성공 증거가 없어 "
        "전송 완료로 처리하지 않았어요. 생성 파일 경로를 답변에 포함하거나 최근 생성 이미지 "
        "후보가 감지되도록 다시 전송해야 합니다."
    )


def recent_generated_image_paths(
    roots: tuple[Path, ...],
    since_ts: float,
    window_sec: int,
    max_bytes: int,
    exclude: list[Path] | None = None,
) -> list[Path]:
    excluded: set[Path] = set()
    for path in exclude or []:
        try:
            excluded.add(path.resolve())
        except OSError:
            continue

    now = time.time()
    cutoff = since_ts if since_ts > 0 else now - max(0, window_sec)
    if window_sec > 0:
        cutoff = max(cutoff, now - window_sec)
    cutoff = max(0.0, cutoff - 2.0)

    candidates: list[tuple[float, Path]] = []
    for root in roots:
        try:
            resolved_root = root.expanduser().resolve()
        except OSError:
            continue
        if not resolved_root.exists():
            continue
        paths = [resolved_root] if resolved_root.is_file() else resolved_root.rglob("*")
        for path in paths:
            try:
                resolved = path.resolve()
                stat = resolved.stat()
            except OSError:
                continue
            if resolved in excluded:
                continue
            if not resolved.is_file() or resolved.suffix.lower() not in IMAGE_ATTACHMENT_EXTENSIONS:
                continue
            if stat.st_size <= 0 or stat.st_size > max_bytes:
                continue
            if stat.st_mtime < cutoff or stat.st_mtime > now + 60:
                continue
            candidates.append((stat.st_mtime, resolved))

    candidates.sort(key=lambda item: item[0], reverse=True)
    return [path for _mtime, path in candidates[:1]]


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


@dataclass(frozen=True)
class ApprovalOption:
    number: str
    label: str
    key: str

    @property
    def short_label(self) -> str:
        label = self.label.strip()
        lowered = label.lower()
        if "don't ask" in lowered or "do not ask" in lowered:
            return "Yes, don't ask again"
        if lowered.startswith("no"):
            return "No"
        if lowered.startswith("yes"):
            return "Yes"
        return label[:40] or self.number


@dataclass(frozen=True)
class ApprovalRequest:
    approval_id: str
    source_head: str
    command: str
    reason: str
    options: tuple[ApprovalOption, ...]
    expires_at: float
    cancelled: bool = False

    @classmethod
    def create(
        cls,
        approval_id: str,
        source_head: str,
        command: str,
        reason: str,
        options: tuple[ApprovalOption, ...],
        ttl_seconds: int,
        now: float | None = None,
    ) -> "ApprovalRequest":
        base = time.time() if now is None else now
        return cls(
            approval_id=approval_id,
            source_head=source_head,
            command=command,
            reason=reason,
            options=options,
            expires_at=base + max(1, ttl_seconds),
        )

    @property
    def signature(self) -> str:
        return self.approval_id

    @property
    def short_signature(self) -> str:
        return self.approval_id[:16]

    def is_expired(self, now: float | None = None) -> bool:
        current = time.time() if now is None else now
        return current >= self.expires_at

    def is_active(self, now: float | None = None) -> bool:
        return not self.cancelled and not self.is_expired(now)

    def cancel(self) -> "ApprovalRequest":
        return replace(self, cancelled=True)

    def option(self, choice: str) -> ApprovalOption | None:
        return next((item for item in self.options if item.number == choice), None)

    def telegram_text(self) -> str:
        lines = [f"{self.source_head} is waiting for command approval.", ""]
        if self.command:
            lines.extend(["Command:", truncate_text(self.command, 600), ""])
        if self.reason:
            lines.extend(["Reason:", truncate_text(self.reason, 600), ""])
        lines.append("Choose a button, or reply with the visible number/shortcut.")
        return "\n".join(lines)


@dataclass(frozen=True)
class ApprovalPrompt:
    signature: str
    command: str
    reason: str
    options: tuple[ApprovalOption, ...]

    @property
    def approval_id(self) -> str:
        return self.signature

    @property
    def short_signature(self) -> str:
        return self.signature[:16]

    def telegram_text(self) -> str:
        lines = ["Codex is waiting for command approval.", ""]
        if self.command:
            lines.extend(["Command:", truncate_text(self.command, 600), ""])
        if self.reason:
            lines.extend(["Reason:", truncate_text(self.reason, 600), ""])
        lines.append("Choose a button, or reply with the visible number/shortcut.")
        return "\n".join(lines)


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
    signature: str
    title: str
    options: tuple[ChoiceOption, ...]
    source_head: str = "codex_repl"
    expires_at: float = 0.0
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
            signature=choice_id,
            title=title,
            options=options,
            source_head=source_head,
            expires_at=base + max(1, ttl_seconds),
        )

    @property
    def approval_id(self) -> str:
        return self.signature

    @property
    def choice_id(self) -> str:
        return self.signature

    @property
    def short_signature(self) -> str:
        return self.signature[:16]

    def is_expired(self, now: float | None = None) -> bool:
        if self.expires_at <= 0:
            return False
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
        lines = ["Codex is waiting for a selection.", ""]
        if self.title:
            lines.extend(["Prompt:", truncate_text(self.title, 600), ""])
        lines.append("Choose a button, or reply with the visible number/letter/shortcut.")
        return "\n".join(lines)


def clean_pane_lines(screen: str) -> list[str]:
    return [ANSI_RE.sub("", line).rstrip() for line in screen.splitlines()]


def repl_pane_activity_from_screen(screen: str, max_lines: int) -> str:
    state = ""
    active_turn = False
    lines = [
        line.strip()
        for line in clean_pane_lines(screen or "")[-max_lines:]
        if line.strip()
    ]
    for cleaned in lines:
        if any(marker in cleaned for marker in CODEX_INTERRUPT_MARKERS):
            state = "interrupt"
            active_turn = False
        elif REPL_QUEUED_RE.search(cleaned):
            state = "alive"
            active_turn = True
        elif REPL_IDLE_DONE_RE.search(cleaned):
            state = "idle"
            active_turn = False
        elif REPL_ACTIVE_BUSY_RE.search(cleaned):
            state = "alive"
            active_turn = True
        elif REPL_PROMPT_READY_RE.search(cleaned):
            if not active_turn:
                state = "idle"
        elif "clear to save" not in cleaned.lower() and REPL_BUSY_RE.search(cleaned):
            state = "alive"
    return state


def screen_has_repl_busy_marker(screen: str) -> bool:
    return repl_pane_activity_from_screen(screen, 20) == "alive"


def repl_typing_stop_signal_from_screen(screen: str) -> str:
    return repl_pane_activity_from_screen(screen, 6)


def update_typing_watch_hits(
    signal: str,
    marker_hits: int,
    capture_error_hits: int,
) -> tuple[bool, int, int]:
    if signal == "idle":
        return True, 0, 0
    if signal == "interrupt":
        marker_hits += 1
        return marker_hits >= 2, marker_hits, 0
    if signal == "capture_error":
        capture_error_hits += 1
        return capture_error_hits >= 3, 0, capture_error_hits
    return False, 0, 0


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


def parse_approval_prompt(screen: str) -> ApprovalPrompt | None:
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
    return ApprovalPrompt(
        signature=signature,
        command=command,
        reason=reason,
        options=tuple(options),
    )


def parse_yes_no_choice_prompt(lines: list[str]) -> ChoicePrompt | None:
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
        return ChoicePrompt(signature=signature, title=stripped, options=tuple(options))
    return None


def parse_choice_prompt(screen: str) -> ChoicePrompt | None:
    if parse_approval_prompt(screen):
        return None

    lines = clean_pane_lines(screen)
    tail = lines[max(0, len(lines) - 40) :]
    yes_no = parse_yes_no_choice_prompt(tail)
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
    return ChoicePrompt(signature=signature, title=title, options=options)


def extract_message_content(payload: dict[str, Any]) -> str:
    content = payload.get("content")
    if not isinstance(content, list):
        return ""
    parts = []
    for item in content:
        if isinstance(item, dict) and item.get("type") == "output_text":
            parts.append(str(item.get("text") or ""))
    return "\n".join(parts)


def collect_reasoning_summary(value: Any, parts: list[str]) -> None:
    if isinstance(value, str):
        text = value.strip()
        if text:
            parts.append(text)
        return
    if isinstance(value, list):
        for item in value:
            collect_reasoning_summary(item, parts)
        return
    if not isinstance(value, dict):
        return
    for key in ("text", "summary", "content"):
        if key in value:
            collect_reasoning_summary(value.get(key), parts)


def extract_reasoning_summary(payload: dict[str, Any]) -> str:
    parts: list[str] = []
    collect_reasoning_summary(payload.get("summary"), parts)
    return "\n".join(parts).strip()


def extract_event(record: dict[str, Any]) -> tuple[str, str] | None:
    kind = record.get("type")
    payload = record.get("payload") if isinstance(record.get("payload"), dict) else {}

    if kind == "event_msg":
        payload_type = payload.get("type")
        if payload_type == "user_message":
            return "user", str(payload.get("message") or "")
        if payload_type == "agent_message":
            phase = payload.get("phase")
            message = str(payload.get("message") or "")
            if phase == "final_answer":
                return "assistant", message
            if not phase and message:
                if is_copy_payload_message(message):
                    return "copy_payload", message
                return "flow", message
            if phase == "commentary":
                # Generic commentary no longer drives the flow card — only
                # copy-paste payloads (e.g. /goal, 제목/내용) stay routed.
                # Tool activity is sourced from function_call records below so
                # the flow card shows ONE LINE PER TOOL CALL. (T-260628-43)
                if is_copy_payload_message(message):
                    return "copy_payload", message
                return None

    if kind == "response_item":
        payload_type = payload.get("type")
        if payload_type == "function_call":
            summary = function_call_flow_summary(payload)
            if summary:
                return "flow", summary
            return None
        if payload_type == "reasoning":
            summary = extract_reasoning_summary(payload)
            if summary:
                if is_copy_payload_message(summary):
                    return "copy_payload", summary
                return "reasoning", summary
        if payload_type == "message" and payload.get("role") == "assistant":
            phase = payload.get("phase") or payload.get("metadata", {}).get("phase")
            content = extract_message_content(payload)
            if phase == "final_answer":
                return "assistant", content
            if phase == "commentary":
                if is_copy_payload_message(content):
                    return "copy_payload", content
                return None
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
    message_id: int = 0


# C3 (T-260705-72): 발송 실패한 승인/선택 카드 재발송 간격. approval_loop 은 1s tick 이라
# 매 tick 재발송하면 flood 를 악화시킨다 — 실패 카드만 이 간격으로 재시도.
PROMPT_CARD_RESEND_SECONDS = 15.0


def should_resend_prompt_card(
    message_id: int | None,
    signature: str,
    resolved_ids: set[str],
    last_attempt_at: float,
    now: float,
    interval: float = PROMPT_CARD_RESEND_SECONDS,
) -> bool:
    """C3 (T-260705-72): 화면에 남아 있는 같은 signature 프롬프트인데 카드 발송이 실패해
    message_id 가 없으면(미해결 한정) 재발송한다 — signature 동일성만으로 재발송을 영구
    억제하면 flood/네트워크 단발 실패 시 codex 가 사람 게이트에 무기한 블록되는데
    폰에는 카드가 없다."""
    return (
        message_id is None
        and signature not in resolved_ids
        and now - last_attempt_at >= interval
    )


class Bridge:
    def __init__(self, config: Config, telegram: TelegramClient, repl: CodexRepl) -> None:
        self.config = config
        self.telegram = telegram
        self.repl = repl
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
        self.pending_approval: ApprovalPrompt | None = None
        self.pending_approval_message_id: int | None = None
        self.pending_approval_send_attempt_at = 0.0
        self.resolved_approval_ids: set[str] = set()
        self.pending_choice: ChoicePrompt | None = None
        self.pending_choice_message_id: int | None = None
        self.pending_choice_send_attempt_at = 0.0
        self.resolved_choice_ids: set[str] = set()
        self.stop_event = threading.Event()
        self.current_origin: str | None = None
        self.current_flow_scope = ""
        self.last_repl_activity_at = 0.0
        self.suppress_until_user = False
        self.needs_composer_clear = False
        self.pending_reasoning_mirror: tuple[str, str] | None = None
        self.active_telegram_prompt = ""
        self.active_telegram_prompt_started_at = 0.0
        self.active_telegram_message_id = 0
        self.flow_message_id = 0
        self.flow_body = ""
        self.flow_scope = ""
        # ⚙️ 받은지시 카드를 코덱스 답의 앵커로 재사용 (T-260630-48 phase2, claude 브릿지 동형):
        # 노드-origin 답 도착 시 새 카드 대신 받은지시 카드를 in-place edit 해 받은지시→코덱스답을
        # 1장으로 통합(받은지시/코덱스답 2장 중복 제거). 0 = 열린 앵커 없음. 휘발성(미persist).
        self.ambient_directive_message_id = 0
        self.ambient_directive_body = ""
        self.flow_last_edit_at = 0.0
        self.last_public_progress: str = ""
        self.telegram_fallback_sent = False
        self.session_path: Path | None = None
        self.session_identity: SessionIdentity | None = None
        self.bridge_state: dict[str, Any] | None = None
        self.session_pos = 0
        self.pid_handle = None
        self.pid_lock_acquired = False
        # ⚙️ A2 받은-지시 신호 tail 오프셋 (T-260630-22). None=미초기화 → 첫 poll 에서
        # 파일 END 로 seek(기존 엔트리 skip, 새 append 만 카드화).
        self.directive_signal_pos: int | None = None

    def acquire_lock(self) -> None:
        # flock 기반 락 — 프로세스 사망 시 커널이 자동 해제하고 PID 재사용에 면역.
        # ⚠️ 제거 금지 (DO NOT REMOVE) — 옛 pid-file + process_alive 방식은 tight
        # restart 루프 중 죽은 pid 가 재사용되면 "already running" 오판으로 자기영속
        # 크래시 루프(NRestarts 1320회)에 빠졌음. claude-telegram-bridge.py 와 동일한
        # flock 으로 통일. 근거: issues/2026-06-26-codex-bridge-stale-pid-loop.md
        self.config.state_dir.mkdir(parents=True, exist_ok=True)
        self.pid_handle = self.config.pid_file.open("a+")
        try:
            fcntl.flock(self.pid_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            try:
                self.pid_handle.close()
            except OSError:
                pass
            self.pid_handle = None
            raise RuntimeError(f"bridge already running for pid file {self.config.pid_file}") from exc
        self.pid_lock_acquired = True
        self.pid_handle.seek(0)
        self.pid_handle.truncate()
        self.pid_handle.write(f"{os.getpid()}\n")
        self.pid_handle.flush()
        os.fsync(self.pid_handle.fileno())

    def release_lock(self) -> None:
        handle = getattr(self, "pid_handle", None)
        owns_lock = bool(getattr(self, "pid_lock_acquired", False))
        if handle is not None:
            if owns_lock:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass
            try:
                handle.close()
            except OSError:
                pass
            self.pid_handle = None
        self.pid_lock_acquired = False

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

    def active_telegram_prompt_matches(self, prompt: str) -> bool:
        normalized = normalize_prompt(prompt)
        if not normalized:
            return False
        active = self.active_prompt_for_recovery()
        if active != normalized:
            return False
        return self.startup_recovery_has_recent_active_prompt(active)

    def mark_repl_activity(self) -> None:
        with self.lock:
            self.last_repl_activity_at = time.monotonic()

    def has_recent_repl_activity(self) -> bool:
        interval = int(getattr(self.config, "typing_liveness_seconds", 0) or 0)
        grace_seconds = max(60.0, float(interval) * 6.0)
        with self.lock:
            last_activity = self.last_repl_activity_at
        return last_activity > 0 and time.monotonic() - last_activity <= grace_seconds

    def handle_user_event(self, text: str) -> None:
        self.mark_repl_activity()
        self.suppress_until_user = False
        self.pending_reasoning_mirror = None
        self.reset_flow_card()
        self.current_flow_scope = normalize_prompt(text)
        if self.consume_pending_match(text):
            self.current_origin = "telegram"
            log("JSONL", "matched Telegram-origin prompt")
            self.begin_repl_typing()
        elif self.active_telegram_prompt_matches(text):
            self.current_origin = "telegram"
            log("JSONL", "matched active Telegram-origin prompt")
            self.emit_received_telegram_directive_card(text)
            self.begin_repl_typing()
        else:
            self.current_origin = "terminal"
            self.stop_telegram_fallback()
            self.emit_sent_directive_card(text)
            log("JSONL", "terminal-origin prompt")
            self.begin_repl_typing()

    def emit_sent_directive_card(self, text: str) -> None:
        if self.config.bridge_kill:
            log("SEND", "sent-directive echo blocked by CRB_KILL=1")
            return
        node = str(getattr(self.config, "node", "") or node_defaults()[0])
        message = format_sent_directive(text, from_alias=node, to_alias=node)
        if not message:
            return
        if not self.telegram.send(message):
            log("SEND", "sent-directive echo failed")
            return
        log("SEND", "sent terminal-origin directive card")

    def emit_received_telegram_directive_card(self, text: str) -> None:
        if self.config.bridge_kill:
            return
        message = format_ambient_directive(text, from_node=None, task=None)
        if not message:
            return
        if not self.telegram.send(message):
            log("SEND", "received-directive telegram echo failed")
            return
        log("SEND", "sent received-directive telegram echo")

    def handle_reasoning_event(self, text: str, key: str) -> None:
        if is_copy_payload_message(text):
            self.handle_copy_payload_event(text)
            return
        self.mark_repl_activity()
        if not self.config.reasoning_mirror:
            return
        message = format_reasoning_mirror(text)
        if not message:
            return
        if self.bridge_state and ring_contains(self.bridge_state, key):
            log("SEND", "skip duplicate reasoning mirror")
            mesh_ledger_record("sendMessage", self.config.chat_id, message, result="suppressed")
            return
        with self.lock:
            self.last_public_progress = format_progress_summary(text, 220)
        self.pending_reasoning_mirror = (message, key)

    def handle_flow_event(self, text: str, key: str) -> None:
        if is_copy_payload_message(text):
            self.handle_copy_payload_event(text)
            return
        self.mark_repl_activity()
        with self.lock:
            self.last_public_progress = format_progress_summary(text, 220)
        if not self.has_repl_typing():
            log("TYPE", "recover from flow")
            self.begin_repl_typing()
        if not self.config.flow_mirror:
            return
        scope = self.active_telegram_prompt or self.current_flow_scope
        flow_key = flow_mirror_dedup_key(text, scope)
        dedup_key = flow_key or key
        if self.bridge_state and ring_contains(self.bridge_state, dedup_key):
            log("SEND", "skip duplicate flow mirror")
            mesh_ledger_record("sendMessage", self.config.chat_id, text, result="suppressed")
            return
        # Collapse each flow event to a single short line (claude parity).
        # Full narration was overflowing FLOW_MIRROR_LIMIT after 1-2 steps and
        # spilling into multiple long messages instead of one growing card.
        summary = flow_step_summary(text)
        if not summary:
            return
        if self.config.bridge_kill:
            log("SEND", "flow mirror blocked by CRB_KILL=1")
            return

        if self.flow_scope != scope:
            self.reset_flow_card()
            self.flow_scope = scope

        # Sliding window: keep only the last FLOW_MIRROR_WINDOW step lines in ONE
        # edit-in-place card (claude parity). No length-based rollover to a new
        # message — old steps scroll off instead of spawning multiple long cards.
        prior_lines = self.flow_body.split("\n") if self.flow_body else []
        candidate = "\n".join((prior_lines + [summary])[-FLOW_MIRROR_WINDOW:])
        if not self.flow_message_id:
            message_id = self.telegram.send_message_id(format_flow_mirror(candidate))
            if not message_id:
                log("SEND", "flow mirror failed")
                return
            self.flow_body = candidate
            self.flow_message_id = message_id
            log("SEND", f"sent flow mirror mid={message_id}")
        else:
            self.wait_for_flow_edit_budget()
            if not self.telegram.edit(self.flow_message_id, format_flow_mirror(candidate)):
                log("SEND", "flow mirror edit failed")
                return
            self.flow_body = candidate
            self.flow_last_edit_at = time.monotonic()
            log("SEND", f"edited flow mirror mid={self.flow_message_id}")
        self.persist_state(event_key=dedup_key)

    def handle_progress_event(self, text: str, key: str = "") -> bool:
        dedup_key = key or flow_mirror_dedup_key(text, self.active_telegram_prompt)
        self.handle_flow_event(text, dedup_key)
        return True

    def reset_flow_card(self) -> None:
        self.flow_message_id = 0
        self.flow_body = ""
        self.flow_scope = ""
        self.flow_last_edit_at = 0.0
        # persisted across restart (anti-fragmentation): clear the saved card id too, so a
        # restart right after reset starts a fresh card instead of resuming a stale one.
        self.persist_state()

    def wait_for_flow_edit_budget(self) -> None:
        if self.flow_last_edit_at <= 0:
            return
        elapsed = time.monotonic() - self.flow_last_edit_at
        remaining = FLOW_MIRROR_EDIT_MIN_SECONDS - elapsed
        if remaining > 0:
            time.sleep(remaining)

    def send_pending_reasoning_mirror(self) -> None:
        pending = self.pending_reasoning_mirror
        self.pending_reasoning_mirror = None
        if not pending:
            return
        if not self.config.reasoning_mirror:
            return
        message, key = pending
        if self.bridge_state and ring_contains(self.bridge_state, key):
            mesh_ledger_record("sendMessage", self.config.chat_id, message, result="suppressed")
            return
        if self.config.bridge_kill:
            log("SEND", "reasoning mirror blocked by CRB_KILL=1")
            return
        if not self.telegram.send(message):
            log("SEND", "reasoning mirror failed")
            return
        self.persist_state(event_key=key)

    def handle_copy_payload_event(self, text: str) -> bool:
        messages = split_copy_payload_messages(text)
        if not messages:
            return True
        self.pending_reasoning_mirror = None
        for message in messages:
            key = self.copy_payload_dedup_key_for_turn(message)
            if key and self.bridge_state and ring_contains(self.bridge_state, key):
                log("SEND", "skip duplicate copy payload part")
                mesh_ledger_record("sendMessage", self.config.chat_id, message, result="suppressed")
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
            mesh_ledger_record("sendMessage", self.config.chat_id, text, result="suppressed")
            return True
        answer = (text or "").strip()
        if not answer:
            return True
        origin = self.current_origin or "terminal"
        log("SEND", f"Telegram mirror from {origin}")
        if is_copy_payload_message(answer):
            self.pending_reasoning_mirror = None
            if not self.handle_copy_payload_event(answer):
                return False
        else:
            # 🧠 코덱스 사고 미러는 최종 답변이 성공적으로 발송된 *뒤*에 보낸다
            # (Claude 브릿지의 🧠 클로드 사고와 동일 순서). 내부 추론 원문이 아니라
            # 런타임이 준 공개 reasoning summary 만 전송 — send_pending_reasoning_mirror
            # 이 reasoning_mirror 토글·dedup·non-fatal 을 그대로 적용. (T-260628-38)
            if not self.send_answer(answer):
                return False
            self.send_pending_reasoning_mirror()
        self.resolve_midreport_obligation("complete", "final answer sent")
        if self.request_incomplete_copy_payload_pair_repair_if_needed():
            return True
        self.warn_incomplete_copy_payload_pair_if_needed()
        self.clear_active_telegram_prompt()
        self.reset_flow_card()
        self.mark_repl_turn_finished()
        return True

    def mark_repl_turn_finished(self) -> None:
        with self.lock:
            self.current_origin = None
            self.current_flow_scope = ""
            self.last_repl_activity_at = 0.0
            self.suppress_until_user = True

    def completed_turn_blocks_liveness_recovery(self, prompt: str) -> bool:
        with self.lock:
            return (
                self.suppress_until_user
                and not prompt
                and not self.current_origin
                and not self.current_flow_scope
                and self.last_repl_activity_at <= 0
            )

    def finish_duplicate_final_turn(self) -> None:
        self.clear_active_telegram_prompt()
        self.reset_flow_card()
        self.mark_repl_turn_finished()

    def send_answer(self, answer: str) -> bool:
        if self.config.bridge_kill:
            log("SEND", "blocked by CRB_KILL=1")
            mesh_ledger_record("sendMessage", self.config.chat_id, answer, result="suppressed")
            return False
        attachments = extract_local_attachment_paths(
            answer,
            self.config.attachment_roots,
            self.config.max_attachment_bytes,
        )
        if (
            self.current_origin == "telegram"
            and self.config.generated_image_autosend
            and requests_image_delivery(self.active_prompt_for_recovery())
            and claims_image_delivery_success(answer)
            and not extract_local_image_paths(
                answer,
                self.config.attachment_roots,
                self.config.max_attachment_bytes,
            )
        ):
            started_at = self.active_telegram_prompt_started_at
            if started_at <= 0 and self.bridge_state is not None:
                try:
                    started_at = float(self.bridge_state.get("active_telegram_prompt_started_at") or 0)
                except (TypeError, ValueError):
                    started_at = 0.0
            generated = recent_generated_image_paths(
                self.config.generated_image_roots,
                started_at,
                self.config.generated_image_window_sec,
                self.config.max_attachment_bytes,
                exclude=attachments,
            )
            if generated:
                log("ATTACH", f"auto generated image {generated[0]}")
                attachments.extend(generated)
            else:
                log("ATTACH", "blocked unverified image delivery claim")
                answer = image_delivery_unverified_text()
        outgoing = strip_sent_attachment_references(answer, attachments)
        # ⚙️ T-260630-48 phase2 — 노드-origin(받은지시 카드 존재) 코덱스 답은 새 카드 대신 받은지시
        # 앵커를 edit 해 받은지시→코덱스답을 1장 통합. 첨부 있으면(편집 불가)/길이 4000 초과/edit 실패
        # 시 폴백 send → 답 1장 보장(0장도 2장폭발도 아님). 텔레그램-origin 답은 영향 없음.
        anchor = self.ambient_directive_message_id
        telegram_reply_anchor = (
            self.active_telegram_message_id
            if self.current_origin == "telegram" and self.active_telegram_message_id > 0
            else None
        )
        sent_via_reply = False
        if self.current_origin != "telegram" and anchor and alt3_narrative_enabled():
            # alt3 (spec v0.2 §6, T-260703-14 — claude PR#358 동형): 받은지시 카드 edit-통합
            # 폐기 → 코덱스 답은 받은지시 루트에 native reply (같은 chat·같은 봇, §5-3 충족).
            # reply 실패 시 아래 공통 send 폴백 (답 1장 보장). 첨부는 reply 여부 무관 아래 루프.
            self.ambient_directive_message_id = 0
            self.ambient_directive_body = ""
            sent_via_reply = bool(self.telegram.send(outgoing, reply_to_message_id=anchor))
            if sent_via_reply:
                log("SEND", f"sent codex answer as reply to directive root mid={anchor}")
            else:
                log("SEND", "codex answer reply to directive root failed → fallback plain send")
        elif self.current_origin != "telegram" and anchor and not attachments:
            unified = f"{self.ambient_directive_body}\n\n{outgoing}"
            self.ambient_directive_message_id = 0
            self.ambient_directive_body = ""
            if len(unified) <= 4000 and self.telegram.edit(anchor, unified):
                log("SEND", f"edited codex answer into directive anchor mid={anchor}")
                return True
            # 폴백: 길이초과/edit실패 → 아래에서 새 카드 send
        ok = sent_via_reply or self.telegram.send(outgoing, reply_to_message_id=telegram_reply_anchor)
        if not ok:
            # 본문 자체가 실패 — 이벤트 재시도 대상 (커서 미전진, 중복 발신 없음).
            return False
        for path in attachments:
            log("ATTACH", f"send {path}")
            if not self.telegram.send_local_attachment(path, self.config.max_attachment_bytes):
                # C2 (T-260705-72): 본문이 이미 발송된 뒤의 첨부 실패로 턴을 실패시키면
                # jsonl 커서가 안 전진해 같은 본문을 폴링마다 영구 재발신(+후속 답 전면 웨지).
                # 첨부 실패는 1줄 공지로 가시화하고 턴은 완료 처리 — 파일은 노드에 보존.
                log("ATTACH", f"send failed: {path}")
                self.telegram.send(f"⚠️ 첨부 전송 실패 — 파일은 노드에 보존됨: {path}")
        return True

    def persist_state(self, offset: int | None = None, event_key: str | None = None) -> None:
        identity = self.session_identity
        if identity is None:
            return
        state = self.bridge_state or bridge_state_default(identity, self.session_pos)
        state["session_path"] = identity.path
        state["dev"] = identity.dev
        state["ino"] = identity.ino
        # persisted across restart (anti-fragmentation): keep the ⚙️ flow card identity
        # so a daemon restart resumes edit-in-place instead of spawning a new card per step.
        state["flow_message_id"] = self.flow_message_id
        state["flow_body"] = self.flow_body
        state["flow_scope"] = self.flow_scope
        if self.active_telegram_message_id > 0:
            state["active_telegram_message_id"] = self.active_telegram_message_id
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
            self.clear_and_paste_prompt(repair_prompt, "copy payload repair")
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
        elif kind == "reasoning":
            key = event_dedup_key(record, kind, text)
            self.handle_reasoning_event(text, key)
            if line_end is not None:
                self.persist_state(line_end)
            return True
        elif kind == "flow":
            key = event_dedup_key(record, kind, text)
            self.handle_flow_event(text, key)
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
                self.pending_reasoning_mirror = None
                for message in split_copy_payload_messages(text):
                    self.record_copy_payload_pair_part(message)
                if self.request_incomplete_copy_payload_pair_repair_if_needed():
                    if line_end is not None:
                        self.persist_state(line_end)
                    return True
                self.warn_incomplete_copy_payload_pair_if_needed()
                self.resolve_midreport_obligation("complete", "duplicate final already handled")
                self.finish_duplicate_final_turn()
                if line_end is not None:
                    self.persist_state(line_end)
                return True
            if self.bridge_state and ring_contains(self.bridge_state, key):
                log("SEND", "skip duplicate final_answer")
                self.stop_repl_typing()
                self.stop_long_running_progress()
                self.stop_telegram_fallback()
                self.resolve_midreport_obligation("complete", "duplicate final already handled")
                self.finish_duplicate_final_turn()
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
        path = self.repl.session_file()
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
                self.active_telegram_message_id = int(self.bridge_state.get("active_telegram_message_id") or 0)
            # persisted across restart (anti-fragmentation): resume the ⚙️ flow card so the
            # next tool step edits the existing card instead of sending a fresh one.
            self.flow_message_id = int(self.bridge_state.get("flow_message_id") or 0)
            self.flow_body = str(self.bridge_state.get("flow_body") or "")
            self.flow_scope = str(self.bridge_state.get("flow_scope") or "")
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
            self.poll_directive_signals()
            time.sleep(0.5)

    def poll_directive_signals(self) -> None:
        # ⚙️ A2 (T-260630-22) — 수신노드 신호파일을 tail 해 '📥 받은 지시' 카드 렌더.
        # directive-received-ack.sh 가 노드주입 디렉티브 1건당 1줄(JSON) append 한다. 브릿지가
        # JSONL 만으론 노드주입 vs 사람 수동입력을 구분 못 하므로, 신뢰성 있는 ack 스크립트가
        # 남긴 신호만 카드화(수동입력 오미러 X). flow_mirror ON 한정. 시작 시 기존 엔트리는
        # 건너뛰고(END seek) 이후 새 append 만 emit. Non-fatal — 메시지 전달엔 영향 X.
        if not self.config.flow_mirror:
            return
        path = self.config.directive_signal_path
        if path is None:
            return
        try:
            if not path.exists():
                return
            size = path.stat().st_size
            if self.directive_signal_pos is None:
                self.directive_signal_pos = size  # 시작점 = END (기존 엔트리 skip)
                return
            if size < self.directive_signal_pos:
                self.directive_signal_pos = 0  # truncate/rotate 감지 → 처음부터
            with path.open("r", encoding="utf-8", errors="replace") as f:
                f.seek(self.directive_signal_pos)
                while True:
                    line = f.readline()
                    if not line:
                        break
                    self.directive_signal_pos = f.tell()
                    stripped = line.strip()
                    if stripped:
                        self.emit_directive_signal_card(stripped)
        except Exception as exc:  # noqa: BLE001
            log("JSONL", f"directive signal poll error (non-fatal): {exc}")

    def emit_directive_signal_card(self, line: str) -> None:
        # ⚙️ A2 받은-지시 카드 1장 emit. 신호 1줄(JSON {ts,from,gist}) → '📥 받은 지시'.
        # 새 bout 경계 → reset_flow_card() 로 flow 카드 묶음(받은지시→작업흐름→코덱스답) 리셋.
        if self.config.bridge_kill:
            return
        try:
            data = json.loads(line)
        except Exception:  # noqa: BLE001
            return
        body = format_ambient_directive(
            str(data.get("gist", "")),
            from_node=str(data.get("from", "")) or None,
            task=str(data.get("task", "")) or None,
        )
        if not body:
            return
        self.reset_flow_card()
        try:
            message_id = self.telegram.send_message_id(body)
            if message_id:
                # ⚙️ T-260630-48 phase2 — 받은지시 카드를 코덱스 답의 앵커로 보관.
                self.ambient_directive_message_id = message_id
                self.ambient_directive_body = body
                log("SEND", f"sent received-directive card mid={message_id}")
            else:
                self.ambient_directive_message_id = 0
                self.ambient_directive_body = ""
                log("SEND", "received-directive card failed")
        except Exception as exc:  # noqa: BLE001
            log("SEND", f"received-directive card send failed (non-fatal): {exc}")

    def repl_interrupted(self) -> bool:
        """True if codex aborted the turn (interrupt/error marker at pane bottom).

        Only the last few non-empty lines are inspected so a stale marker still
        sitting in scrollback from an OLD error does not falsely trip detection.
        """
        try:
            screen = self.repl.capture_pane(15)
        except Exception:  # noqa: BLE001
            return False
        return repl_typing_stop_signal_from_screen(screen) == "interrupt"

    def repl_typing_stop_signal(self) -> str:
        try:
            screen = self.repl.capture_pane(15)
        except Exception:  # noqa: BLE001
            return "capture_error"
        return repl_typing_stop_signal_from_screen(screen)

    def abort_typing_on_interrupt(self, owner: threading.Event) -> None:
        """Stop the REPL typing loop + clear zombie turn state on codex interrupt.

        Guarded by a turn token (the loop's own stop_event): if a newer turn has
        already taken over typing, do nothing — never wipe a live turn's state.
        """
        with self.typing_lock:
            if self.repl_typing_stop is not owner:
                return
            self.repl_typing_stop = None
        self.clear_active_telegram_prompt()
        log("TYPE", "codex interrupted/dead -> typing stopped + turn state cleared")

    def start_typing_loop(
        self, max_seconds: int | None = None, watch_interrupt: bool = False
    ) -> threading.Event:
        stop_event = threading.Event()

        def loop() -> None:
            deadline = time.monotonic() + max_seconds if max_seconds else None
            pulse_count = 0
            last_ok: bool | None = None
            interrupt_hits = 0
            capture_error_hits = 0
            while not stop_event.is_set():
                if deadline is not None and time.monotonic() >= deadline:
                    break
                # Every other pulse (~8s), check whether codex aborted the turn.
                # Require consecutive hits for interrupt/capture failures to ignore transients.
                if watch_interrupt and pulse_count >= 1 and pulse_count % 2 == 0:
                    abort, interrupt_hits, capture_error_hits = update_typing_watch_hits(
                        self.repl_typing_stop_signal(),
                        interrupt_hits,
                        capture_error_hits,
                    )
                    if abort:
                        self.abort_typing_on_interrupt(stop_event)
                        break
                pulse_count += 1
                try:
                    ok = self.telegram.send_typing()
                except Exception as exc:  # noqa: BLE001
                    ok = False
                    log("TGERR", f"sendChatAction raised: {exc}")
                if pulse_count == 1:
                    log("TYPE", "pulse ok" if ok else "pulse failed")
                elif not ok and last_ok is not False:
                    log("TYPE", "pulse failed")
                elif ok and last_ok is False:
                    log("TYPE", "pulse recovered")
                last_ok = ok
                wait_seconds = 1.0 if pulse_count == 1 else 4.0
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
            self.repl_typing_stop = self.start_typing_loop(
                self.config.typing_max_seconds, watch_interrupt=True
            )

    def stop_repl_typing(self) -> None:
        with self.typing_lock:
            if self.repl_typing_stop:
                self.repl_typing_stop.set()
                self.repl_typing_stop = None
                log("TYPE", "stop")

    def set_active_telegram_prompt(
        self,
        prompt: str,
        started_at: float | None = None,
        message_id: int | None = None,
    ) -> None:
        prompt = normalize_prompt(prompt)
        with self.lock:
            self.active_telegram_prompt = prompt
            if started_at is not None:
                self.active_telegram_prompt_started_at = started_at if prompt else 0.0
            if message_id is not None:
                self.active_telegram_message_id = int(message_id or 0) if prompt else 0
            if self.bridge_state is not None:
                if prompt:
                    self.bridge_state["active_telegram_prompt"] = prompt
                    if started_at is not None:
                        self.bridge_state["active_telegram_prompt_started_at"] = started_at
                    if self.active_telegram_message_id > 0:
                        self.bridge_state["active_telegram_message_id"] = self.active_telegram_message_id
                    else:
                        self.bridge_state.pop("active_telegram_message_id", None)
                else:
                    self.bridge_state.pop("active_telegram_prompt", None)
                    self.bridge_state.pop("active_telegram_prompt_started_at", None)
                    self.bridge_state.pop("active_telegram_message_id", None)
        self.persist_state()

    def clear_active_telegram_prompt(self) -> None:
        self.set_active_telegram_prompt("", 0.0, 0)

    def active_prompt_for_recovery(self) -> str:
        with self.lock:
            prompt = self.active_telegram_prompt
            if not prompt and self.bridge_state is not None:
                prompt = str(self.bridge_state.get("active_telegram_prompt") or "")
        return normalize_prompt(prompt)

    def active_prompt_started_at_for_recovery(self) -> float:
        with self.lock:
            started_at = self.active_telegram_prompt_started_at
            if started_at <= 0 and self.bridge_state is not None:
                try:
                    started_at = float(self.bridge_state.get("active_telegram_prompt_started_at") or 0)
                except (TypeError, ValueError):
                    started_at = 0.0
        return started_at

    def startup_recovery_has_recent_active_prompt(self, prompt: str) -> bool:
        if not prompt:
            return False
        started_at = self.active_prompt_started_at_for_recovery()
        if started_at <= 0:
            return False
        max_age = max(30.0, float(getattr(self.config, "typing_max_seconds", 0) or 0))
        return time.time() - started_at <= max_age

    def copy_payload_dedup_key_for_turn(self, text: str) -> str:
        return copy_payload_dedup_key(text, self.active_prompt_for_recovery())

    def begin_telegram_prompt_tracking(self, prompt: str, message_id: int | None = None) -> None:
        self.add_pending_telegram(prompt)
        started_at = time.time()
        with self.lock:
            self.current_origin = "telegram"
            self.suppress_until_user = False
            self.last_public_progress = ""
            self.telegram_fallback_sent = False
            self.reset_flow_card()
        self.set_active_telegram_prompt(prompt, started_at, message_id or 0)
        self.set_copy_payload_pair_contract(prompt)
        self.start_telegram_fallback(prompt)
        self.start_long_running_progress(prompt)

    def begin_long_running_telegram_prompt(self, prompt: str, message_id: int | None = None) -> None:
        self.begin_telegram_prompt_tracking(prompt, message_id=message_id)

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
                message = self.long_running_progress_message(prompt, elapsed)
                if self.telegram.send(message):
                    self.record_midreport_obligation(prompt, elapsed, message)
                else:
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
        message = self.long_running_progress_message(prompt, elapsed_seconds)
        if self.telegram.send(message):
            self.record_midreport_obligation(prompt, elapsed_seconds, message)
        else:
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

    def record_midreport_obligation(
        self,
        prompt: str,
        elapsed_seconds: float,
        message: str,
    ) -> None:
        prompt = normalize_prompt(prompt)
        if not prompt:
            return
        run_midreport_obligation(
            [
                "record",
                "--source",
                "codex-progress",
                "--node",
                self.config.node,
                "--task",
                self.current_task_id(prompt),
                "--title",
                "중간보고",
                "--detail",
                f"elapsed={int(elapsed_seconds)}s prompt={format_progress_summary(prompt, 120)}\n{message}",
            ]
        )

    def resolve_midreport_obligation(self, status: str, title: str) -> None:
        with self.lock:
            if self.current_origin == "terminal":
                return
        prompt = self.active_prompt_for_recovery()
        if not prompt:
            return
        run_midreport_obligation(
            [
                "resolve",
                "--source",
                "codex-progress",
                "--node",
                self.config.node,
                "--task",
                self.current_task_id(prompt),
                "--status",
                status,
                "--title",
                title,
                "--detail",
                f"prompt={format_progress_summary(prompt, 120)}",
            ]
        )

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
            screen = self.repl.capture_pane(60)
        except Exception as exc:  # noqa: BLE001
            log("LIVE", f"pane capture failed: {exc}")
            return False
        return screen_has_repl_busy_marker(screen)

    def recover_repl_liveness(self, reason: str = "poll") -> bool:
        repl_working = self.repl_is_working()
        prompt = self.active_prompt_for_recovery()
        if reason == "startup" and (
            not repl_working or not self.startup_recovery_has_recent_active_prompt(prompt)
        ):
            log("LIVE", "skip startup stale typing recovery")
            self.clear_active_telegram_prompt()
            return False
        if not repl_working:
            if (
                self.has_repl_typing()
                and not prompt
                and not self.has_recent_repl_activity()
            ):
                log("LIVE", f"idle -> stop recovered typing ({reason})")
                self.stop_repl_typing()
                return True
            return False
        if self.completed_turn_blocks_liveness_recovery(prompt):
            log("LIVE", f"skip stale typing recovery ({reason})")
            return False
        recovered = False
        if not self.has_repl_typing():
            log("LIVE", f"recover typing ({reason})")
            self.begin_repl_typing()
            recovered = True
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
        log("TG", f"{label} -> Codex footer")
        self.begin_repl_typing()
        try:
            screen = self.repl.capture_pane(30)
            answer = (
                extract_codex_footer_context_text(screen)
                if context_only
                else extract_codex_footer_status_text(screen)
            )
            footer_answer = bool(answer)
            if not answer:
                log("TG", f"{label} footer unavailable -> Codex REPL fallback")
                wide_context = getattr(self.repl, "temporary_window_width", None)
                if callable(wide_context):
                    with wide_context(STATUS_WIDE_CAPTURE_COLUMNS):
                        self.clear_and_paste_prompt("/status", label)
                        time.sleep(1.0)
                        screen = self.repl.capture_pane(120)
                else:
                    self.clear_and_paste_prompt("/status", label)
                    time.sleep(1.0)
                    screen = self.repl.capture_pane(120)
                answer = (
                    extract_codex_context_text(screen)
                    if context_only
                    else extract_codex_status_text(screen)
            )
            if not context_only and footer_answer:
                answer = append_rate_limit_resets(answer, self.repl.session_file())
            if context_only:
                self.telegram.send(answer)
            else:
                self.telegram.send_restart_button(
                    answer,
                    f"{BRIDGE_RESTART_CALLBACK_PREFIX}:{self.config.node}",
                )
        except Exception as exc:  # noqa: BLE001
            log("REPL", f"{label} failed: {exc}")
            self.telegram.send(f"codex {label} failed: {exc}")
        finally:
            self.stop_repl_typing()
        return True

    def clear_and_paste_prompt(self, prompt: str, label: str = "telegram input") -> None:
        if not is_codex_slash_command(prompt) and self.repl_is_working():
            self.paste_prompt_without_clearing_composer(prompt, label)
            return
        composer_lock = getattr(self.repl, "composer_lock", None)
        clear_unlocked = getattr(self.repl, "_clear_composer_unlocked", None)
        paste_unlocked = getattr(self.repl, "_paste_prompt_unlocked", None)
        if callable(composer_lock) and callable(clear_unlocked) and callable(paste_unlocked):
            with composer_lock():
                clear_unlocked()
                log("REPL", f"cleared composer before {label}")
                paste_unlocked(prompt)
            self.needs_composer_clear = False
            return
        self.clear_composer_before_telegram_input(label)
        self.repl.paste_prompt(prompt)

    def clear_composer_before_telegram_input(self, label: str = "telegram input") -> None:
        try:
            self.repl.clear_composer()
            log("REPL", f"cleared composer before {label}")
        except Exception as exc:  # noqa: BLE001
            log("REPL", f"composer clear failed before {label}: {exc}")
        finally:
            self.needs_composer_clear = False

    def clear_stale_composer_if_needed(self) -> None:
        if not self.needs_composer_clear:
            return
        self.clear_composer_before_telegram_input("stale slash command")

    def paste_prompt_without_clearing_composer(self, prompt: str, label: str = "telegram input") -> None:
        composer_lock = getattr(self.repl, "composer_lock", None)
        paste_unlocked = getattr(self.repl, "_paste_prompt_unlocked", None)
        if callable(composer_lock) and callable(paste_unlocked):
            with composer_lock():
                paste_unlocked(prompt)
        else:
            self.repl.paste_prompt(prompt)
        self.needs_composer_clear = False
        log("REPL", f"queued {label} while busy without clearing composer")

    def handle_slash_command_result(self, text: str) -> bool:
        command = slash_command_token(text)
        if not command:
            return False
        screen = self.repl.capture_pane(120)
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
        if is_fast_slash_command(text):
            notice = extract_fast_mode_notice(screen)
            if notice:
                self.telegram.send(notice)
        return False

    def approval_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                screen = self.repl.capture_pane()
                prompt = parse_approval_prompt(screen)
                choice_prompt = None if prompt else parse_choice_prompt(screen)
                should_send = False
                if prompt:
                    with self.choice_lock:
                        choice_to_clear = self.pending_choice
                        choice_message_id = self.pending_choice_message_id
                        if choice_to_clear is not None:
                            log("CHOICE", "choice prompt cleared by approval")
                            if choice_to_clear.signature not in self.resolved_choice_ids:
                                self.telegram.update_choice_prompt(
                                    choice_message_id,
                                    choice_to_clear,
                                    "Selection prompt is no longer active.",
                                )
                        self.pending_choice = None
                        self.pending_choice_message_id = None
                    with self.approval_lock:
                        previous = self.pending_approval
                        if previous is None or previous.signature != prompt.signature:
                            self.pending_approval = prompt
                            self.pending_approval_message_id = None
                            self.pending_approval_send_attempt_at = 0.0
                            should_send = True
                        elif should_resend_prompt_card(
                            self.pending_approval_message_id,
                            prompt.signature,
                            self.resolved_approval_ids,
                            self.pending_approval_send_attempt_at,
                            time.monotonic(),
                        ):
                            # C3 (T-260705-72): 발송 실패 승인카드 재발송
                            should_send = True
                        if should_send:
                            self.pending_approval_send_attempt_at = time.monotonic()
                    if should_send:
                        log("APPROV", f"approval prompt detected {prompt.short_signature}")
                        message_id = self.telegram.send_approval_prompt(prompt)
                        with self.approval_lock:
                            if self.pending_approval and self.pending_approval.signature == prompt.signature:
                                self.pending_approval_message_id = message_id
                elif choice_prompt:
                    with self.approval_lock:
                        prompt_to_clear = self.pending_approval
                        message_id = self.pending_approval_message_id
                        if prompt_to_clear is not None:
                            log("APPROV", "approval prompt cleared by choice")
                            if prompt_to_clear.signature not in self.resolved_approval_ids:
                                self.telegram.update_approval_prompt(
                                    message_id,
                                    prompt_to_clear,
                                    "Approval prompt is no longer active.",
                                )
                        self.pending_approval = None
                        self.pending_approval_message_id = None
                    with self.choice_lock:
                        previous_choice = self.pending_choice
                        if previous_choice is None or previous_choice.signature != choice_prompt.signature:
                            self.pending_choice = choice_prompt
                            self.pending_choice_message_id = None
                            self.pending_choice_send_attempt_at = 0.0
                            should_send = True
                        elif should_resend_prompt_card(
                            self.pending_choice_message_id,
                            choice_prompt.signature,
                            self.resolved_choice_ids,
                            self.pending_choice_send_attempt_at,
                            time.monotonic(),
                        ):
                            # C3 (T-260705-72): 발송 실패 선택카드 재발송 — 승인카드와 동형
                            should_send = True
                        if should_send:
                            self.pending_choice_send_attempt_at = time.monotonic()
                    if should_send:
                        log("CHOICE", f"choice prompt detected {choice_prompt.short_signature}")
                        message_id = self.telegram.send_choice_prompt(choice_prompt)
                        with self.choice_lock:
                            if self.pending_choice and self.pending_choice.signature == choice_prompt.signature:
                                self.pending_choice_message_id = message_id
                else:
                    with self.approval_lock:
                        prompt_to_clear = self.pending_approval
                        message_id = self.pending_approval_message_id
                        if prompt_to_clear is not None:
                            log("APPROV", "approval prompt cleared")
                            if prompt_to_clear.signature not in self.resolved_approval_ids:
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
                            if choice_to_clear.signature not in self.resolved_choice_ids:
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
            message_id = self.pending_approval_message_id
        if not prompt:
            if callback_query_id:
                self.telegram.call(
                    "answerCallbackQuery",
                    callback_query_id=callback_query_id,
                    text="No active Codex approval prompt.",
                )
            return False
        if signature and signature != prompt.short_signature:
            if callback_query_id:
                self.telegram.call(
                    "answerCallbackQuery",
                    callback_query_id=callback_query_id,
                    text="That approval prompt is no longer active.",
                )
            return True
        if getattr(prompt, "is_expired", lambda: False)():
            with self.approval_lock:
                if self.pending_approval and self.pending_approval.signature == prompt.signature:
                    self.pending_approval = None
                    self.pending_approval_message_id = None
            if callback_query_id:
                self.telegram.call(
                    "answerCallbackQuery",
                    callback_query_id=callback_query_id,
                    text="That approval prompt expired.",
                )
            return True
        if prompt.signature in self.resolved_approval_ids:
            if callback_query_id:
                self.telegram.call(
                    "answerCallbackQuery",
                    callback_query_id=callback_query_id,
                    text="This approval choice was already sent.",
                )
            return True

        option = next((item for item in prompt.options if item.number == choice), None)
        if option is None:
            return False
        try:
            self.repl.send_approval_key(option.key)
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
            if self.pending_approval and self.pending_approval.signature == prompt.signature:
                self.resolved_approval_ids.add(prompt.signature)
                self.pending_approval = prompt
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
        if data.startswith(f"{SELF_UPDATE_CALLBACK}::"):
            message = callback.get("message") if isinstance(callback.get("message"), dict) else {}
            chat = message.get("chat") if isinstance(message.get("chat"), dict) else {}
            if str(chat.get("id")) != self.config.chat_id:
                return True
            if callback.get("id"):
                self.telegram.call(
                    "answerCallbackQuery",
                    callback_query_id=str(callback.get("id") or ""),
                    text="업데이트를 시작합니다…",
                )
            perform_self_update(data.split("::", 1)[1])
            return True
        if data.startswith(f"{BRIDGE_RESTART_CALLBACK_PREFIX}:"):
            message = callback.get("message") if isinstance(callback.get("message"), dict) else {}
            chat = message.get("chat") if isinstance(message.get("chat"), dict) else {}
            if str(chat.get("id")) != self.config.chat_id:
                return True
            _prefix, _, target_node = data.partition(":")
            callback_query_id = str(callback.get("id") or "")
            if target_node != self.config.node:
                if callback_query_id:
                    self.telegram.call(
                        "answerCallbackQuery",
                        callback_query_id=callback_query_id,
                        text="다른 노드의 재시작 버튼입니다.",
                    )
                return True
            if callback_query_id:
                self.telegram.call(
                    "answerCallbackQuery",
                    callback_query_id=callback_query_id,
                    text="코덱스 브릿지를 재시작합니다…",
                )
            self.telegram.send("코덱스 브릿지 재시작합니다. 잠깐 끊겼다가 다시 붙습니다.")
            try:
                restart_codex_bridge(self.config.node)
            except Exception as exc:  # noqa: BLE001
                log("TG", f"bridge restart failed: {exc}")
                self.telegram.send(f"코덱스 브릿지 재시작 실패: {exc}")
            return True
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
            message_id = self.pending_choice_message_id
        if not prompt:
            if callback_query_id:
                self.telegram.call(
                    "answerCallbackQuery",
                    callback_query_id=callback_query_id,
                    text="No active Codex selection prompt.",
                )
            return False
        if signature and signature != prompt.short_signature:
            if callback_query_id:
                self.telegram.call(
                    "answerCallbackQuery",
                    callback_query_id=callback_query_id,
                    text="That selection prompt is no longer active.",
                )
            return True
        if getattr(prompt, "is_expired", lambda: False)():
            with self.choice_lock:
                if self.pending_choice and self.pending_choice.signature == prompt.signature:
                    self.pending_choice = None
                    self.pending_choice_message_id = None
            if callback_query_id:
                self.telegram.call(
                    "answerCallbackQuery",
                    callback_query_id=callback_query_id,
                    text="That selection prompt expired.",
                )
            return True
        if prompt.signature in self.resolved_choice_ids:
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
            if self.pending_choice and self.pending_choice.signature == prompt.signature:
                self.resolved_choice_ids.add(prompt.signature)
                self.pending_choice = prompt
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
        try:
            message_id = int(message.get("message_id") or 0)
        except (TypeError, ValueError):
            message_id = 0
        text = message.get("text")
        if isinstance(text, str) and text.strip():
            return TelegramPrompt(text=text, message_id=message_id)

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
                    message_id=message_id,
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
                    message_id=message_id,
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
                message_id=message_id,
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
                message_id=message_id,
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
                message_id=message_id,
            )

        return TelegramPrompt(text="", message_id=message_id)

    def offer_update_if_available(self) -> None:
        latest = self_update_available()
        if not latest:
            return
        if bool_env(f"{SELF_UPDATE_PREFIX}_AUTO_UPDATE", False):
            perform_self_update(latest)
            return
        current = _self_update_installed_version() or "?"
        try:
            self.telegram.send_update_button(
                f"\U0001f195 새 버전 v{latest} 가 출시됐어요! (현재 v{current})\n업데이트하려면 아래 버튼을 누르세요.",
                f"{SELF_UPDATE_CALLBACK}::{latest}",
            )
        except Exception as exc:  # noqa: BLE001
            log("UPDATE", f"update offer failed: {exc}")

    def telegram_loop(self) -> None:
        offset_raw = read_text(self.config.offset_file)
        offset = int(offset_raw) if offset_raw.isdigit() else 0
        self.offer_update_if_available()
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
                raw_text = message.get("text")
                if isinstance(raw_text, str):
                    hold_response = release_hold_response(raw_text.strip())
                    if hold_response:
                        self.telegram.send(hold_response)
                        continue
                try:
                    prompt = self.prompt_from_telegram_message(message, int(update["update_id"]))
                except Exception as exc:  # noqa: BLE001
                    log("TG", f"media download failed: {exc}")
                    self.telegram.send(f"codex REPL media delivery failed: {exc}. 다시 보내주시면 재시도합니다.")
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
                self.begin_telegram_prompt_tracking(prompt.text, message_id=prompt.message_id)
                try:
                    self.clear_and_paste_prompt(prompt.text, "telegram prompt")
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
                    log("REPL", f"paste failed: {exc}")
                    self.resolve_midreport_obligation("blocked", "codex REPL delivery failed")
                    self.clear_active_telegram_prompt()
                    self.telegram.send(f"codex REPL delivery failed: {exc}")

    def ensure_signal_fifo(self) -> None:
        path = self.config.signal_path
        if path is None:
            return
        if not hasattr(os, "mkfifo"):
            raise RuntimeError("CRB_SIGNAL_PATH requires os.mkfifo support")
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            if not path.is_fifo():
                raise RuntimeError(f"CRB_SIGNAL_PATH exists but is not a FIFO: {path}")
            try:
                path.chmod(0o600)
            except OSError:
                pass
            return
        os.mkfifo(path, 0o600)
        try:
            path.chmod(0o600)
        except OSError:
            pass

    def signal_loop(self) -> None:
        path = self.config.signal_path
        if path is None:
            return
        while not self.stop_event.is_set():
            try:
                with path.open("r", encoding="utf-8") as fifo:
                    for line in fifo:
                        if self.stop_event.is_set():
                            break
                        self.process_signal_text(line)
            except Exception as exc:  # noqa: BLE001
                if not self.stop_event.is_set():
                    log("SIGNAL", f"read failed: {exc}")
                    time.sleep(2)

    def process_signal_text(self, raw: str) -> bool:
        prompt_text = parse_signal_prompt(raw)
        if not prompt_text:
            return False
        log("SIGNAL", "prompt -> Codex REPL")
        self.begin_repl_typing()
        slash_command = is_codex_slash_command(prompt_text)
        self.begin_telegram_prompt_tracking(prompt_text)
        try:
            self.clear_and_paste_prompt(prompt_text, "signal prompt")
            if slash_command:
                time.sleep(0.8)
                had_slash_error = self.handle_slash_command_result(prompt_text)
                if should_stop_typing_after_slash_command(prompt_text, had_slash_error):
                    self.stop_repl_typing()
                    self.stop_long_running_progress()
                    self.clear_active_telegram_prompt()
        except Exception as exc:  # noqa: BLE001
            self.stop_repl_typing()
            self.stop_long_running_progress()
            log("SIGNAL", f"paste failed: {exc}")
            self.resolve_midreport_obligation("blocked", "codex signal delivery failed")
            self.clear_active_telegram_prompt()
            self.telegram.send(f"codex signal delivery failed: {exc}")
            return False
        return True

    def run(self) -> None:
        self.repl.verify()
        self.ensure_session_file()
        self.acquire_lock()
        jsonl_thread = threading.Thread(target=self.jsonl_loop, daemon=True)
        jsonl_thread.start()
        approval_thread = threading.Thread(target=self.approval_loop, daemon=True)
        approval_thread.start()
        self.recover_repl_liveness("startup")
        liveness_thread = threading.Thread(target=self.liveness_loop, daemon=True)
        liveness_thread.start()
        if self.config.signal_path is not None:
            self.ensure_signal_fifo()
            log("SIGNAL", f"fifo ready: {self.config.signal_path}")
            signal_thread = threading.Thread(target=self.signal_loop, daemon=True, name="crb-signal")
            signal_thread.start()
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
