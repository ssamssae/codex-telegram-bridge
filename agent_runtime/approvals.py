"""Approval request model with expiry and cancellation state."""

from __future__ import annotations

import time
from dataclasses import dataclass, replace


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
            lines.extend(["Command:", self.command[:600], ""])
        if self.reason:
            lines.extend(["Reason:", self.reason[:600], ""])
        lines.append("Choose a button, or reply with the visible number/shortcut.")
        return "\n".join(lines)
