"""Shared runtime contracts for Telegram-controlled terminal agents."""

from .approvals import ApprovalOption, ApprovalRequest
from .capabilities import CapabilityRegistry, HeadCapabilities
from .locks import WorkdirLock, WorkdirLockError
from .types import AgentEvent, AgentMessage

__all__ = [
    "AgentEvent",
    "AgentMessage",
    "ApprovalOption",
    "ApprovalRequest",
    "CapabilityRegistry",
    "HeadCapabilities",
    "WorkdirLock",
    "WorkdirLockError",
]
