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
import json
import mimetypes
import os
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
from dataclasses import dataclass
from pathlib import Path
from typing import Any


HOME = Path.home()
NODE_EMOJI_LINES = {"\U0001f34e", "\U0001f3ed", "\U0001fa9f", "\U0001f5a5", "\U0001f4bb", "\U0001f916"}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
AUDIO_EXTENSIONS = {".ogg", ".oga", ".opus", ".mp3", ".m4a", ".aac", ".wav", ".flac", ".weba"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".webm", ".avi", ".mkv"}


def env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    return value if value not in (None, "") else default


def int_env(name: str, default: int, minimum: int = 0) -> int:
    try:
        value = int(env(name, str(default)) or default)
    except (TypeError, ValueError):
        return default
    return value if value >= minimum else default


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


def write_text_atomic(path: Path, value: str | int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(str(value), encoding="utf-8")
    tmp.replace(path)


def safe_filename_part(value: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in {"-", "_", "."} else "-" for ch in value)
    return cleaned.strip("-")[:80] or "file"


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
    telegram_chunk: int
    poll_timeout: int
    start_at_end: bool

    @classmethod
    def from_env(cls) -> "Config":
        default_node, default_emoji = node_defaults()
        node = env("CRB_NODE", default_node) or default_node
        token_file_raw = env("CRB_TOKEN_FILE")
        chat_id = env("CRB_CHAT_ID") or env("TAB_CHAT_ID")
        if not chat_id:
            raise RuntimeError("TAB_CHAT_ID is required")
        return cls(
            node=node,
            emoji=env("CRB_EMOJI", env("TAB_PREFIX", default_emoji) or default_emoji) or "",
            token_file=Path(token_file_raw).expanduser() if token_file_raw else None,
            chat_id=chat_id,
            state_dir=Path(
                env("CRB_STATE_DIR", env("TAB_STATE_DIR", "~/.local/state/telegram-agent-bridge") or "")
                or ""
            ).expanduser(),
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
            typing_max_seconds=int_env("CRB_TYPING_MAX_SECONDS", 1800, minimum=30),
            telegram_chunk=int_env("CRB_TG_CHUNK", 4096, minimum=512),
            poll_timeout=int_env("CRB_TG_POLL_TIMEOUT", 2, minimum=1),
            start_at_end=(env("CRB_START_AT_END", "1") or "1").lower()
            in {"1", "true", "yes", "on"},
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

    def send(self, text: str) -> None:
        for chunk in self.chunks(text):
            self.call("sendMessage", chat_id=self.chat_id, text=chunk)


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
        key = self.config.submit_key
        count = self.config.enter_count if key == "Enter" else 1
        for _ in range(count):
            self.tmux("send-keys", "-t", self.config.pane_target, key)
            time.sleep(0.3)

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


def extract_event(record: dict[str, Any]) -> tuple[str, str] | None:
    kind = record.get("type")
    payload = record.get("payload") if isinstance(record.get("payload"), dict) else {}

    if kind == "event_msg":
        payload_type = payload.get("type")
        if payload_type == "user_message":
            return "user", str(payload.get("message") or "")
        if payload_type == "agent_message" and payload.get("phase") == "final_answer":
            return "assistant", str(payload.get("message") or "")

    if kind == "response_item":
        if payload.get("type") == "message" and payload.get("role") == "assistant":
            phase = payload.get("phase") or payload.get("metadata", {}).get("phase")
            if phase != "final_answer":
                return None
            content = payload.get("content")
            if isinstance(content, list):
                parts = []
                for item in content:
                    if isinstance(item, dict) and item.get("type") == "output_text":
                        parts.append(str(item.get("text") or ""))
                return "assistant", "\n".join(parts)
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


class Bridge:
    def __init__(self, config: Config, telegram: TelegramClient, repl: CodexRepl) -> None:
        self.config = config
        self.telegram = telegram
        self.repl = repl
        self.lock = threading.Lock()
        self.exec_lock = threading.Lock()
        self.typing_lock = threading.Lock()
        self.repl_typing_stop: threading.Event | None = None
        self.pending_telegram: list[str] = []
        self.stop_event = threading.Event()
        self.current_origin: str | None = None
        self.suppress_until_user = False
        self.session_path: Path | None = None
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
        if self.consume_pending_match(text):
            self.current_origin = "telegram"
            log("JSONL", "matched Telegram-origin prompt")
            self.begin_repl_typing()
        else:
            self.current_origin = "terminal"
            log("JSONL", "terminal-origin prompt")
            self.begin_repl_typing()

    def handle_assistant_event(self, text: str) -> None:
        self.stop_repl_typing()
        if self.suppress_until_user:
            return
        answer = (text or "").strip()
        if not answer:
            return
        origin = self.current_origin or "terminal"
        log("SEND", f"Telegram mirror from {origin}")
        self.telegram.send(answer)
        self.current_origin = None
        self.suppress_until_user = True

    def process_line(self, line: str) -> None:
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            return
        event = extract_event(record)
        if not event:
            return
        kind, text = event
        if kind == "user":
            self.handle_user_event(text)
        elif kind == "assistant":
            self.handle_assistant_event(text)

    def ensure_session_file(self) -> Path:
        path = self.repl.session_file()
        if self.session_path != path:
            self.session_path = path
            self.session_pos = path.stat().st_size if self.config.start_at_end else 0
            log("REPL", f"watching {path}")
        return path

    def jsonl_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                path = self.ensure_session_file()
                with path.open("r", encoding="utf-8", errors="replace") as f:
                    f.seek(self.session_pos)
                    while True:
                        line = f.readline()
                        if not line:
                            break
                        self.session_pos = f.tell()
                        self.process_line(line)
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
        self.telegram.send(answer)

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
                if prompt.image_path and self.config.image_mode == "exec":
                    self.handle_image_prompt(prompt)
                    continue
                if prompt.text.strip().lower() in {"/start", "/ping"}:
                    self.telegram.send("codex REPL bridge running")
                    continue
                log("TG", "prompt -> Codex REPL")
                self.begin_repl_typing()
                self.add_pending_telegram(prompt.text)
                try:
                    self.repl.paste_prompt(prompt.text)
                except Exception as exc:  # noqa: BLE001
                    self.stop_repl_typing()
                    log("REPL", f"paste failed: {exc}")
                    self.telegram.send(f"codex REPL delivery failed: {exc}")

    def run(self) -> None:
        self.repl.verify()
        self.ensure_session_file()
        self.acquire_lock()
        jsonl_thread = threading.Thread(target=self.jsonl_loop, daemon=True)
        jsonl_thread.start()
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
