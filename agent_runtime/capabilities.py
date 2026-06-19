"""Capability declarations for terminal agent heads."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class HeadCapabilities:
    head: str
    vision: bool = False
    audio: bool = False
    video: bool = False
    repl: bool = False
    approval: bool = False
    streaming: bool = False
    workdir_access: tuple[Path, ...] = ()
    notes: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "head": self.head,
            "vision": self.vision,
            "audio": self.audio,
            "video": self.video,
            "repl": self.repl,
            "approval": self.approval,
            "streaming": self.streaming,
            "workdir_access": [str(path) for path in self.workdir_access],
            "notes": list(self.notes),
        }


@dataclass
class CapabilityRegistry:
    _heads: dict[str, HeadCapabilities] = field(default_factory=dict)

    def register(self, capabilities: HeadCapabilities) -> None:
        if not capabilities.head:
            raise ValueError("head capability name is required")
        self._heads[capabilities.head] = capabilities

    def get(self, head: str) -> HeadCapabilities | None:
        return self._heads.get(head)

    def require(self, head: str) -> HeadCapabilities:
        capabilities = self.get(head)
        if capabilities is None:
            raise KeyError(f"unknown head: {head}")
        return capabilities

    def supports(self, head: str, capability: str) -> bool:
        capabilities = self.require(head)
        value = getattr(capabilities, capability, None)
        return bool(value)

    def as_dict(self) -> dict[str, dict[str, object]]:
        return {head: caps.as_dict() for head, caps in sorted(self._heads.items())}
