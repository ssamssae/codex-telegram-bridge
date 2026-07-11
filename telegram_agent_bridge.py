#!/usr/bin/env python3
"""Telegram -> Codex bridge.

Polls one Telegram bot, accepts messages from one configured chat id, runs one
Codex turn, and sends only the final answer back to Telegram.
"""

from __future__ import annotations

import json
import os
import queue
import re
import shlex
import shutil
import stat
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

from agent_runtime import WorkdirLock, WorkdirLockError


HOME = Path.home()


class BridgeError(RuntimeError):
    """Expected runtime error that can be reported to Telegram."""


class AgentExecError(BridgeError):
    """Agent command failed or returned an unusable answer."""


def env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    return value if value not in (None, "") else default


def int_env(name: str, default: int, minimum: int = 1) -> int:
    try:
        value = int(env(name, str(default)) or default)
    except (TypeError, ValueError):
        return default
    return value if value >= minimum else default


def bool_env(name: str, default: bool = False) -> bool:
    value = env(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def optional_path_env(name: str, default: str | None = None) -> Path | None:
    raw = env(name, default)
    if raw is None:
        return None
    value = raw.strip()
    if not value or value.lower() in {"0", "false", "no", "off", "none"}:
        return None
    return expand_path(value)


def expand_path(value: str) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(value))).resolve()


WINDOWS_BATCH_EXTENSIONS = {".bat", ".cmd"}


def is_windows_runtime() -> bool:
    return os.name == "nt" or sys.platform == "win32"


def resolve_agent_cmd_for_spawn(agent_cmd: list[str]) -> list[str]:
    if not agent_cmd or not is_windows_runtime():
        return agent_cmd

    first, *rest = agent_cmd
    resolved = shutil.which(first) or first
    if Path(resolved).suffix.lower() in WINDOWS_BATCH_EXTENSIONS:
        cmd_exe = os.environ.get("COMSPEC") or shutil.which("cmd.exe") or shutil.which("cmd") or "cmd.exe"
        return [cmd_exe, "/c", resolved, *rest]
    if resolved != first:
        return [resolved, *rest]
    return agent_cmd


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""


def write_text_atomic(path: Path, value: str | int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(str(value), encoding="utf-8")
    tmp.replace(path)


CODE_FENCE_RE = re.compile(r"```([^\n`]*)\n(.*?)```", re.DOTALL)


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


@dataclass(frozen=True)
class Config:
    bot_token: str
    chat_id: str
    agent_cmd: list[str]
    state_dir: Path
    prefix: str
    prefix_line: bool
    workdir: Path
    workdir_lock: bool
    timeout: int
    telegram_chunk: int
    codex_dangerous_bypass: bool
    codex_extra_args: list[str]
    local_input_path: Path | None
    stdin_input: bool
    typing_interval: int

    @classmethod
    def from_env(cls) -> "Config":
        bot_token = env("TAB_BOT_TOKEN")
        chat_id = env("TAB_CHAT_ID")
        if not bot_token:
            raise BridgeError("TAB_BOT_TOKEN is required")
        if not chat_id:
            raise BridgeError("TAB_CHAT_ID is required")

        agent = (env("TAB_AGENT", "codex") or "codex").strip().lower()
        if agent != "codex":
            raise BridgeError("TAB_AGENT supports only 'codex'")

        raw_cmd = env("TAB_AGENT_CMD", "codex")
        if not raw_cmd:
            raise BridgeError("TAB_AGENT_CMD is required")
        try:
            agent_cmd = shlex.split(raw_cmd)
            codex_extra_args = shlex.split(env("TAB_CODEX_EXTRA_ARGS", "") or "")
        except ValueError as exc:
            raise BridgeError(f"invalid shell-style config: {exc}") from exc
        if not agent_cmd:
            raise BridgeError("TAB_AGENT_CMD must contain at least one command word")
        agent_cmd = resolve_agent_cmd_for_spawn(agent_cmd)

        local_input_default = (
            "~/.local/state/telegram-agent-bridge/input.fifo"
            if hasattr(os, "mkfifo")
            else None
        )

        return cls(
            bot_token=bot_token.strip(),
            chat_id=str(chat_id).strip(),
            agent_cmd=agent_cmd,
            state_dir=expand_path(env("TAB_STATE_DIR", "~/.local/state/telegram-agent-bridge") or ""),
            prefix=env("TAB_PREFIX", "") or "",
            prefix_line=bool_env("TAB_PREFIX_LINE", False),
            workdir=expand_path(env("TAB_WORKDIR", "~") or "~"),
            workdir_lock=bool_env("TAB_WORKDIR_LOCK", True),
            timeout=int_env("TAB_TIMEOUT", 600),
            telegram_chunk=int_env("TAB_TG_CHUNK", 4096),
            codex_dangerous_bypass=bool_env("TAB_CODEX_DANGEROUS_BYPASS", False),
            codex_extra_args=codex_extra_args,
            local_input_path=optional_path_env(
                "TAB_LOCAL_INPUT",
                local_input_default,
            ),
            stdin_input=bool_env("TAB_STDIN_INPUT", sys.stdin.isatty()),
            typing_interval=int_env("TAB_TYPING_INTERVAL", 4),
        )


def parse_json_lines(text: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for line in (text or "").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


class AgentBackend:
    supports_resume = False
    name = "agent"

    def build_exec_cmd(self, prompt: str) -> list[str]:
        raise NotImplementedError

    def build_resume_cmd(self, thread_id: str, prompt: str) -> list[str]:
        raise NotImplementedError

    def parse_thread_id(self, events: list[dict[str, Any]]) -> str:
        return ""

    def parse_answer(
        self,
        events: list[dict[str, Any]],
        stdout: str,
        stderr: str,
    ) -> str:
        raise NotImplementedError

    def cleanup(self) -> None:
        return None


class CodexBackend(AgentBackend):
    supports_resume = True
    name = "codex"

    def __init__(self, config: Config) -> None:
        self.config = config
        self.output_path: Path | None = None

    def _new_output_path(self) -> Path:
        fd, path = tempfile.mkstemp(prefix="codex-telegram-bridge-", suffix=".answer")
        os.close(fd)
        self.output_path = Path(path)
        return self.output_path

    def _base_cmd(self) -> list[str]:
        output_path = self._new_output_path()
        cmd = [
            *self.config.agent_cmd,
            "exec",
            "--json",
            "-o",
            str(output_path),
        ]
        if self.config.codex_dangerous_bypass:
            cmd.append("--dangerously-bypass-approvals-and-sandbox")
        cmd.extend(self.config.codex_extra_args)
        cmd.extend(["-C", str(self.config.workdir)])
        return cmd

    def build_exec_cmd(self, prompt: str) -> list[str]:
        return [*self._base_cmd(), prompt]

    def build_resume_cmd(self, thread_id: str, prompt: str) -> list[str]:
        return [*self._base_cmd(), "resume", thread_id, prompt]

    def parse_thread_id(self, events: list[dict[str, Any]]) -> str:
        for event in events:
            if event.get("type") == "thread.started" and event.get("thread_id"):
                return str(event["thread_id"]).strip()
        return ""

    def parse_answer(
        self,
        events: list[dict[str, Any]],
        stdout: str,
        stderr: str,
    ) -> str:
        if self.output_path:
            answer = read_text(self.output_path)
            if answer:
                return answer

        answer = ""
        for event in events:
            item = event.get("item") if isinstance(event.get("item"), dict) else {}
            if item.get("type") == "agent_message" and item.get("text"):
                answer = str(item["text"])
        return answer.strip()

    def cleanup(self) -> None:
        if self.output_path:
            try:
                self.output_path.unlink()
            except OSError:
                pass
            self.output_path = None


def make_backend(config: Config) -> AgentBackend:
    return CodexBackend(config)


class TelegramClient:
    def __init__(self, token: str) -> None:
        self.api = f"https://api.telegram.org/bot{token}"

    def call(self, method: str, timeout: int = 60, **params: Any) -> dict[str, Any] | None:
        data = urllib.parse.urlencode(params).encode()
        url = f"{self.api}/{method}"
        for attempt in range(3):
            try:
                request = urllib.request.Request(url, data=data)
                with urllib.request.urlopen(request, timeout=timeout) as response:
                    payload = json.load(response)
                return payload if isinstance(payload, dict) else None
            except Exception as exc:  # noqa: BLE001
                if attempt == 2:
                    print(f"telegram {method} failed: {exc}", file=sys.stderr)
                    return None
                time.sleep(2)
        return None


@dataclass(frozen=True)
class BridgeJob:
    source: str
    text: str


class Bridge:
    def __init__(self, config: Config, backend: AgentBackend, telegram: TelegramClient) -> None:
        self.config = config
        self.backend = backend
        self.telegram = telegram
        self.lock = threading.Lock()
        self.jobs: queue.Queue[BridgeJob] = queue.Queue()
        self.offset_file = config.state_dir / "telegram-agent-bridge.offset"
        self.thread_file = config.state_dir / f"telegram-agent-bridge.{backend.name}.thread"
        self.config.state_dir.mkdir(parents=True, exist_ok=True)

    def read_thread_id(self) -> str:
        return read_text(self.thread_file)

    def write_thread_id(self, thread_id: str) -> None:
        write_text_atomic(self.thread_file, (thread_id or "").strip())

    def clear_thread_id(self) -> None:
        self.write_thread_id("")

    def telegram_chunks(self, text: str) -> list[str]:
        text = text or "(empty response)"
        private_chat = is_private_chat_id(self.config.chat_id)
        if private_chat:
            text = strip_leading_emoji_decoration(text)
        if self.config.prefix and not private_chat:
            separator = "\n" if self.config.prefix_line else " "
            prefix = f"{self.config.prefix}{separator}"
        else:
            prefix = ""
        first_limit = max(1, self.config.telegram_chunk - len(prefix))
        chunks = [prefix + text[:first_limit]]
        rest = text[first_limit:]
        for i in range(0, len(rest), self.config.telegram_chunk):
            chunks.append(rest[i : i + self.config.telegram_chunk])
        return chunks

    def raw_chunks(self, text: str) -> list[str]:
        text = text or "(empty response)"
        return [
            text[i : i + self.config.telegram_chunk]
            for i in range(0, len(text), self.config.telegram_chunk)
        ] or ["(empty response)"]

    def send(self, text: str) -> None:
        for segment in split_answer_for_code_blocks(text or "(empty response)"):
            body = str(segment.get("body") or "")
            if segment.get("code"):
                language = str(segment.get("language") or "")
                for chunk in self.raw_chunks(body):
                    self.telegram.call(
                        "sendMessage",
                        chat_id=self.config.chat_id,
                        text=chunk,
                        entities=pre_entities_json(chunk, language),
                    )
                continue
            for chunk in self.telegram_chunks(body):
                self.telegram.call("sendMessage", chat_id=self.config.chat_id, text=chunk)

    def print_local(self, text: str) -> None:
        print(text, flush=True)

    def mirror_prompt(self, job: BridgeJob) -> None:
        if job.source == "local":
            self.send(f"local input:\n{job.text}")
        else:
            self.print_local(f"telegram input:\n{job.text}")

    def mirror_answer(self, job: BridgeJob, text: str) -> None:
        self.print_local(f"codex answer ({job.source}):\n{text}")
        self.send(text)

    def mirror_error(self, job: BridgeJob, text: str) -> None:
        message = f"codex failed: {text}"
        self.print_local(f"{message} ({job.source})")
        self.send(message)

    def typing_until_done(self, stop_event: threading.Event) -> None:
        interval = max(1, self.config.typing_interval)
        while not stop_event.is_set():
            self.telegram.call(
                "sendChatAction",
                timeout=5,
                chat_id=self.config.chat_id,
                action="typing",
            )
            if stop_event.wait(interval):
                break

    def start_typing(self) -> threading.Event:
        stop_event = threading.Event()
        if self.config.typing_interval > 0:
            thread = threading.Thread(
                target=self.typing_until_done,
                args=(stop_event,),
                daemon=True,
                name="tab-typing",
            )
            thread.start()
        return stop_event

    def run_agent_turn(self, prompt: str, thread_id: str = "") -> tuple[str, str]:
        if thread_id and self.backend.supports_resume:
            cmd = self.backend.build_resume_cmd(thread_id, prompt)
        else:
            cmd = self.backend.build_exec_cmd(prompt)

        try:
            lock = (
                WorkdirLock(
                    self.config.workdir,
                    self.config.state_dir,
                    owner=f"{self.backend.name}:exec",
                )
                if self.config.workdir_lock
                else None
            )
            try:
                if lock:
                    lock.acquire()
                try:
                    proc = subprocess.run(
                        cmd,
                        capture_output=True,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        timeout=self.config.timeout,
                        stdin=subprocess.DEVNULL,
                        cwd=str(self.config.workdir),
                    )
                finally:
                    if lock:
                        lock.release()
            except WorkdirLockError as exc:
                raise AgentExecError(str(exc)) from exc
            except subprocess.TimeoutExpired as exc:
                raise AgentExecError(f"codex timed out after {self.config.timeout}s") from exc
            except OSError as exc:
                raise AgentExecError(f"failed to start codex: {exc}") from exc

            stdout = proc.stdout or ""
            stderr = proc.stderr or ""
            if proc.returncode != 0:
                detail_lines = (stderr or stdout).strip().splitlines()
                suffix = f": {detail_lines[-1][:160]}" if detail_lines else ""
                raise AgentExecError(f"codex exited rc={proc.returncode}{suffix}")

            events = parse_json_lines(stdout)
            answer = self.backend.parse_answer(events, stdout, stderr).strip()
            if not answer:
                raise AgentExecError("empty codex response")

            new_thread_id = ""
            if self.backend.supports_resume:
                new_thread_id = self.backend.parse_thread_id(events) or thread_id
                if not new_thread_id:
                    raise AgentExecError("codex did not report a thread id")

            return answer, new_thread_id
        finally:
            cleanup = getattr(self.backend, "cleanup", None)
            if callable(cleanup):
                cleanup()

    def execute_with_session(self, prompt: str) -> str:
        if not self.backend.supports_resume:
            answer, _thread_id = self.run_agent_turn(prompt, "")
            return answer

        thread_id = self.read_thread_id()
        try:
            answer, new_thread_id = self.run_agent_turn(prompt, thread_id)
        except AgentExecError:
            if not thread_id:
                raise
            self.clear_thread_id()
            answer, new_thread_id = self.run_agent_turn(prompt, "")

        self.write_thread_id(new_thread_id)
        return answer

    def process_job(self, job: BridgeJob) -> None:
        self.mirror_prompt(job)
        typing_stop = self.start_typing()
        try:
            with self.lock:
                answer = self.execute_with_session(job.text)
        except BridgeError as exc:
            self.mirror_error(job, str(exc))
            return
        except Exception as exc:  # noqa: BLE001
            print(f"message handling failed: {exc}", file=sys.stderr)
            self.mirror_error(job, "internal error")
            return
        finally:
            typing_stop.set()
        self.mirror_answer(job, answer)

    def job_worker(self) -> None:
        while True:
            job = self.jobs.get()
            try:
                self.process_job(job)
            except Exception as exc:  # noqa: BLE001
                print(f"job worker failed: {exc}", file=sys.stderr)
            finally:
                self.jobs.task_done()

    def handle_message_text(self, text: str, source: str = "telegram") -> None:
        text = (text or "").strip()
        if not text:
            return

        if text.lower() in {"/start", "/ping"}:
            status = f"codex-telegram-bridge running (backend={self.backend.name})"
            self.print_local(status)
            self.send(status)
            return

        self.jobs.put(BridgeJob(source=source, text=text))

    def handle_update(self, update: dict[str, Any]) -> None:
        message = update.get("message") or update.get("edited_message")
        if not isinstance(message, dict):
            return
        chat = message.get("chat") if isinstance(message.get("chat"), dict) else {}
        if str(chat.get("id")) != self.config.chat_id:
            return
        text = message.get("text")
        if not isinstance(text, str):
            return
        print(f">>> codex-telegram-bridge[{self.backend.name}]: {text.strip()}", flush=True)
        self.handle_message_text(text, source="telegram")

    def ensure_local_fifo(self) -> None:
        path = self.config.local_input_path
        if path is None:
            return
        if not hasattr(os, "mkfifo"):
            raise BridgeError("TAB_LOCAL_INPUT requires os.mkfifo support")
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            if not stat.S_ISFIFO(path.stat().st_mode):
                raise BridgeError(f"TAB_LOCAL_INPUT exists but is not a FIFO: {path}")
            return
        os.mkfifo(path, 0o600)

    def local_fifo_loop(self) -> None:
        path = self.config.local_input_path
        if path is None:
            return
        while True:
            try:
                with path.open("r", encoding="utf-8") as fifo:
                    for line in fifo:
                        self.handle_message_text(line, source="local")
            except Exception as exc:  # noqa: BLE001
                print(f"local input failed: {exc}", file=sys.stderr)
                time.sleep(2)

    def stdin_loop(self) -> None:
        for line in sys.stdin:
            self.handle_message_text(line, source="local")

    def start(self) -> None:
        threading.Thread(target=self.job_worker, daemon=True, name="tab-codex-worker").start()
        if self.config.local_input_path is not None:
            self.ensure_local_fifo()
            self.print_local(f"local input fifo: {self.config.local_input_path}")
            threading.Thread(target=self.local_fifo_loop, daemon=True, name="tab-fifo").start()
        if self.config.stdin_input:
            self.print_local("stdin local input enabled")
            threading.Thread(target=self.stdin_loop, daemon=True, name="tab-stdin").start()

    def poll_forever(self) -> None:
        offset_raw = read_text(self.offset_file)
        offset = int(offset_raw) if offset_raw.isdigit() else 0
        while True:
            response = self.telegram.call("getUpdates", offset=offset, timeout=30)
            if not response or not response.get("ok"):
                time.sleep(3)
                continue
            for update in response.get("result", []):
                if not isinstance(update, dict) or "update_id" not in update:
                    continue
                offset = int(update["update_id"]) + 1
                write_text_atomic(self.offset_file, offset)
                try:
                    self.handle_update(update)
                except Exception as exc:  # noqa: BLE001
                    print(f"telegram update handling failed: {exc}", file=sys.stderr)


def main() -> int:
    try:
        config = Config.from_env()
        backend = make_backend(config)
        bridge = Bridge(config, backend, TelegramClient(config.bot_token))
    except BridgeError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2

    print(
        "codex-telegram-bridge start "
        f"backend={backend.name} chat={config.chat_id} state={config.state_dir}",
        flush=True,
    )
    try:
        bridge.start()
    except BridgeError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2
    bridge.poll_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
