"""Adapter for the visible Codex REPL controlled through tmux."""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from agent_runtime.approvals import ApprovalOption
from agent_runtime.capabilities import HeadCapabilities
from agent_runtime.types import AgentEvent, AgentMessage


class CodexReplAdapter:
    name = "codex_repl"

    def __init__(self, repl: Any, workdir: Path | None = None) -> None:
        self.repl = repl
        self.workdir = workdir
        self._session_path: Path | None = None
        self._session_pos = 0

    def spawn(self) -> None:
        self.repl.verify()

    def send(self, message: AgentMessage) -> None:
        self.repl.paste_prompt(message.text)

    def recv(self) -> Iterable[AgentEvent]:
        path = self.session_file()
        if path != self._session_path:
            self._session_path = path
            self._session_pos = 0

        events: list[AgentEvent] = []
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            handle.seek(self._session_pos)
            while True:
                line = handle.readline()
                if not line:
                    break
                self._session_pos = handle.tell()
                event = self._event_from_json_line(line)
                if event:
                    events.append(event)
        return events

    def inject_approval(self, choice: ApprovalOption | str) -> None:
        key = choice.key if isinstance(choice, ApprovalOption) else str(choice)
        self.repl.send_approval_key(key)

    def kill(self) -> None:
        return None

    def capabilities(self) -> HeadCapabilities:
        roots = (self.workdir,) if self.workdir else ()
        return HeadCapabilities(
            head=self.name,
            vision=True,
            audio=True,
            video=True,
            repl=True,
            approval=True,
            streaming=True,
            workdir_access=roots,
            notes=("visible Codex TUI via tmux", "final answers read from Codex JSONL"),
        )

    def capture_pane(self, lines: int = 80) -> str:
        return self.repl.capture_pane(lines)

    def session_file(self) -> Path:
        return self.repl.session_file()

    def _event_from_json_line(self, line: str) -> AgentEvent | None:
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            return None
        if not isinstance(record, dict):
            return None

        kind = record.get("type")
        payload = record.get("payload") if isinstance(record.get("payload"), dict) else {}
        if kind == "event_msg":
            payload_type = payload.get("type")
            if payload_type == "user_message":
                return AgentEvent("user", str(payload.get("message") or ""), source_head=self.name)
            if payload_type == "agent_message" and payload.get("phase") == "final_answer":
                return AgentEvent("assistant", str(payload.get("message") or ""), source_head=self.name)

        if kind == "response_item":
            if payload.get("type") == "message" and payload.get("role") == "assistant":
                phase = payload.get("phase") or payload.get("metadata", {}).get("phase")
                if phase != "final_answer":
                    return None
                content = payload.get("content")
                if isinstance(content, list):
                    parts = [
                        str(item.get("text") or "")
                        for item in content
                        if isinstance(item, dict) and item.get("type") == "output_text"
                    ]
                    return AgentEvent("assistant", "\n".join(parts), source_head=self.name)
        return None
