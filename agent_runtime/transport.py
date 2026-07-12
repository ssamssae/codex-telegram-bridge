"""Transport abstractions for local control-plane input."""

from __future__ import annotations

import os
import queue
import select
import time
from pathlib import Path
from typing import Protocol


class Transport(Protocol):
    def send(self, message: str) -> None:
        raise NotImplementedError

    def recv(self, timeout: float | None = None) -> str:
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError


class QueueTransport:
    def __init__(self) -> None:
        self._queue: queue.Queue[str] = queue.Queue()

    def send(self, message: str) -> None:
        self._queue.put(message)

    def recv(self, timeout: float | None = None) -> str:
        return self._queue.get(timeout=timeout)

    def close(self) -> None:
        return None


class FifoTransport:
    def __init__(self, path: Path) -> None:
        if os.name == "nt" or not hasattr(os, "mkfifo"):
            raise NotImplementedError(
                "FifoTransport requires POSIX named pipes; use the native Windows transport"
            )
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            os.mkfifo(self.path, 0o600)

    def send(self, message: str) -> None:
        with self.path.open("w", encoding="utf-8") as fifo:
            fifo.write(message.rstrip("\n") + "\n")

    def recv(self, timeout: float | None = None) -> str:
        if timeout is None:
            with self.path.open("r", encoding="utf-8") as fifo:
                return fifo.readline().rstrip("\n")

        deadline = time.monotonic() + max(0.0, timeout)
        fd = os.open(self.path, os.O_RDONLY | os.O_NONBLOCK)
        try:
            chunks: list[bytes] = []
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(f"timed out waiting for FIFO input: {self.path}")
                readable, _, _ = select.select([fd], [], [], remaining)
                if not readable:
                    raise TimeoutError(f"timed out waiting for FIFO input: {self.path}")
                data = os.read(fd, 4096)
                if not data:
                    time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
                    continue
                chunks.append(data)
                if b"\n" in data:
                    break
            return b"".join(chunks).split(b"\n", 1)[0].decode("utf-8", errors="replace")
        finally:
            os.close(fd)

    def close(self) -> None:
        return None
