#!/usr/bin/env python3
"""Telegram -> terminal AI agent bridge.

Polls one Telegram bot, accepts messages from one configured chat id, runs one
terminal agent turn, and sends only the final answer back to Telegram.
"""

from __future__ import annotations

import json
import os
import shlex
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


def expand_path(value: str) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(value))).resolve()


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


@dataclass(frozen=True)
class Config:
    bot_token: str
    chat_id: str
    agent: str
    agent_cmd: list[str]
    state_dir: Path
    prefix: str
    workdir: Path
    timeout: int
    telegram_chunk: int
    codex_dangerous_bypass: bool
    codex_extra_args: list[str]

    @classmethod
    def from_env(cls) -> "Config":
        bot_token = env("TAB_BOT_TOKEN")
        chat_id = env("TAB_CHAT_ID")
        if not bot_token:
            raise BridgeError("TAB_BOT_TOKEN is required")
        if not chat_id:
            raise BridgeError("TAB_CHAT_ID is required")

        agent = (env("TAB_AGENT", "codex") or "codex").strip().lower()
        if agent not in {"codex", "generic"}:
            raise BridgeError("TAB_AGENT must be either 'codex' or 'generic'")

        raw_cmd = env("TAB_AGENT_CMD", "codex" if agent == "codex" else None)
        if not raw_cmd:
            raise BridgeError("TAB_AGENT_CMD is required for TAB_AGENT=generic")
        try:
            agent_cmd = shlex.split(raw_cmd)
            codex_extra_args = shlex.split(env("TAB_CODEX_EXTRA_ARGS", "") or "")
        except ValueError as exc:
            raise BridgeError(f"invalid shell-style config: {exc}") from exc
        if not agent_cmd:
            raise BridgeError("TAB_AGENT_CMD must contain at least one command word")

        return cls(
            bot_token=bot_token.strip(),
            chat_id=str(chat_id).strip(),
            agent=agent,
            agent_cmd=agent_cmd,
            state_dir=expand_path(env("TAB_STATE_DIR", "~/.local/state/telegram-agent-bridge") or ""),
            prefix=env("TAB_PREFIX", "") or "",
            workdir=expand_path(env("TAB_WORKDIR", "~") or "~"),
            timeout=int_env("TAB_TIMEOUT", 600),
            telegram_chunk=int_env("TAB_TG_CHUNK", 4096),
            codex_dangerous_bypass=bool_env("TAB_CODEX_DANGEROUS_BYPASS", False),
            codex_extra_args=codex_extra_args,
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
        fd, path = tempfile.mkstemp(prefix="telegram-agent-bridge-", suffix=".answer")
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


class GenericBackend(AgentBackend):
    supports_resume = False
    name = "generic"

    def __init__(self, config: Config) -> None:
        self.config = config

    def _with_prompt(self, prompt: str) -> list[str]:
        replaced = False
        cmd: list[str] = []
        for arg in self.config.agent_cmd:
            if "{prompt}" in arg:
                cmd.append(arg.replace("{prompt}", prompt))
                replaced = True
            else:
                cmd.append(arg)
        if not replaced:
            cmd.append(prompt)
        return cmd

    def build_exec_cmd(self, prompt: str) -> list[str]:
        return self._with_prompt(prompt)

    def build_resume_cmd(self, thread_id: str, prompt: str) -> list[str]:
        return self.build_exec_cmd(prompt)

    def parse_answer(
        self,
        events: list[dict[str, Any]],
        stdout: str,
        stderr: str,
    ) -> str:
        return (stdout or "").strip()


def make_backend(config: Config) -> AgentBackend:
    if config.agent == "codex":
        return CodexBackend(config)
    if config.agent == "generic":
        return GenericBackend(config)
    raise BridgeError(f"unsupported TAB_AGENT: {config.agent}")


class TelegramClient:
    def __init__(self, token: str) -> None:
        self.api = f"https://api.telegram.org/bot{token}"

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
                    print(f"telegram {method} failed: {exc}", file=sys.stderr)
                    return None
                time.sleep(2)
        return None


class Bridge:
    def __init__(self, config: Config, backend: AgentBackend, telegram: TelegramClient) -> None:
        self.config = config
        self.backend = backend
        self.telegram = telegram
        self.lock = threading.Lock()
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
        prefix = f"{self.config.prefix} " if self.config.prefix else ""
        first_limit = max(1, self.config.telegram_chunk - len(prefix))
        chunks = [prefix + text[:first_limit]]
        rest = text[first_limit:]
        for i in range(0, len(rest), self.config.telegram_chunk):
            chunks.append(rest[i : i + self.config.telegram_chunk])
        return chunks

    def send(self, text: str) -> None:
        for chunk in self.telegram_chunks(text):
            self.telegram.call("sendMessage", chat_id=self.config.chat_id, text=chunk)

    def run_agent_turn(self, prompt: str, thread_id: str = "") -> tuple[str, str]:
        if thread_id and self.backend.supports_resume:
            cmd = self.backend.build_resume_cmd(thread_id, prompt)
        else:
            cmd = self.backend.build_exec_cmd(prompt)

        try:
            try:
                proc = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=self.config.timeout,
                    stdin=subprocess.DEVNULL,
                    cwd=str(self.config.workdir),
                )
            except subprocess.TimeoutExpired as exc:
                raise AgentExecError(f"agent timed out after {self.config.timeout}s") from exc
            except OSError as exc:
                raise AgentExecError(f"failed to start agent: {exc}") from exc

            stdout = proc.stdout or ""
            stderr = proc.stderr or ""
            if proc.returncode != 0:
                detail_lines = (stderr or stdout).strip().splitlines()
                suffix = f": {detail_lines[-1][:160]}" if detail_lines else ""
                raise AgentExecError(f"agent exited rc={proc.returncode}{suffix}")

            events = parse_json_lines(stdout)
            answer = self.backend.parse_answer(events, stdout, stderr).strip()
            if not answer:
                raise AgentExecError("empty agent response")

            new_thread_id = ""
            if self.backend.supports_resume:
                new_thread_id = self.backend.parse_thread_id(events) or thread_id
                if not new_thread_id:
                    raise AgentExecError("agent did not report a thread id")

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

    def handle_message_text(self, text: str) -> None:
        text = (text or "").strip()
        if not text:
            return

        if text.lower() in {"/start", "/ping"}:
            self.send(f"telegram-agent-bridge running (agent={self.backend.name})")
            return

        self.telegram.call("sendChatAction", chat_id=self.config.chat_id, action="typing")
        with self.lock:
            try:
                answer = self.execute_with_session(text)
            except BridgeError as exc:
                self.send(f"agent failed: {exc}")
                return
            except Exception as exc:  # noqa: BLE001
                print(f"message handling failed: {exc}", file=sys.stderr)
                self.send("agent failed: internal error")
                return
        self.send(answer)

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
        print(f">>> telegram-agent-bridge[{self.backend.name}]: {text.strip()}", flush=True)
        self.handle_message_text(text)

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
        "telegram-agent-bridge start "
        f"agent={backend.name} chat={config.chat_id} state={config.state_dir}",
        flush=True,
    )
    bridge.poll_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
