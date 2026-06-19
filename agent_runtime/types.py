"""Common message and event types shared by head adapters."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal


AgentEventKind = Literal[
    "started",
    "user",
    "assistant",
    "approval_requested",
    "approval_resolved",
    "error",
    "stopped",
]


@dataclass(frozen=True)
class AgentMessage:
    text: str
    media_paths: tuple[Path, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AgentEvent:
    kind: AgentEventKind
    text: str = ""
    source_head: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
