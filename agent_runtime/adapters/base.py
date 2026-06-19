"""Protocol implemented by terminal agent heads."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Protocol

from agent_runtime.approvals import ApprovalOption
from agent_runtime.capabilities import HeadCapabilities
from agent_runtime.types import AgentEvent, AgentMessage


class HeadAdapter(Protocol):
    name: str

    def spawn(self) -> None:
        raise NotImplementedError

    def send(self, message: AgentMessage) -> None:
        raise NotImplementedError

    def recv(self) -> Iterable[AgentEvent]:
        raise NotImplementedError

    def inject_approval(self, choice: ApprovalOption | str) -> None:
        raise NotImplementedError

    def kill(self) -> None:
        raise NotImplementedError

    def capabilities(self) -> HeadCapabilities:
        raise NotImplementedError
