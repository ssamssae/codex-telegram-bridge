"""Workdir lock primitive for multi-head control planes."""

from __future__ import annotations

import json
import os
import time
import hashlib
from dataclasses import dataclass
from pathlib import Path


class WorkdirLockError(RuntimeError):
    pass


def _windows_process_alive(pid: int) -> bool:
    """Check a Windows process without os.kill(pid, 0).

    On Windows, signal 0 is a console control event rather than the POSIX
    existence probe. It can raise WinError 87 or interrupt the current runner.
    OpenProcess + GetExitCodeProcess provides a read-only existence check.
    """

    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    except (AttributeError, OSError):
        return True

    process_query_limited_information = 0x1000
    still_active = 259
    error_access_denied = 5
    error_invalid_parameter = 87
    error_not_found = 1168

    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
    if not handle:
        error = ctypes.get_last_error()
        if error in {error_invalid_parameter, error_not_found}:
            return False
        if error == error_access_denied:
            return True
        return True

    try:
        exit_code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return True
        return exit_code.value == still_active
    finally:
        kernel32.CloseHandle(handle)


def _process_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        return _windows_process_alive(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _lock_name(workdir: Path) -> str:
    resolved = str(workdir.expanduser().resolve())
    digest = hashlib.sha1(resolved.encode("utf-8", errors="replace")).hexdigest()[:16]
    basename = workdir.name or "workdir"
    safe_base = "".join(ch if ch.isalnum() or ch in {"-", "_", "."} else "-" for ch in basename)
    return f"{safe_base[:80]}-{digest}"


@dataclass
class WorkdirLock:
    workdir: Path
    state_dir: Path
    owner: str
    stale_seconds: int = 24 * 60 * 60

    def __post_init__(self) -> None:
        self.workdir = self.workdir.expanduser().resolve()
        self.state_dir = self.state_dir.expanduser().resolve()
        self.lock_file = self.state_dir / "workdir-locks" / f"{_lock_name(self.workdir)}.lock"
        self.acquired = False

    def _payload(self) -> dict[str, object]:
        return {
            "owner": self.owner,
            "pid": os.getpid(),
            "workdir": str(self.workdir),
            "created_at": time.time(),
        }

    def _existing_payload(self) -> dict[str, object]:
        try:
            return json.loads(self.lock_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def _remove_stale_lock_if_possible(self) -> bool:
        payload = self._existing_payload()
        pid = int(payload.get("pid") or 0)
        created_at = float(payload.get("created_at") or 0)
        if pid and _process_alive(pid):
            return False
        if created_at and time.time() - created_at < self.stale_seconds:
            return False
        try:
            self.lock_file.unlink()
            return True
        except OSError:
            return False

    def acquire(self) -> None:
        self.lock_file.parent.mkdir(parents=True, exist_ok=True)
        while True:
            try:
                fd = os.open(str(self.lock_file), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError as exc:
                if self._remove_stale_lock_if_possible():
                    continue
                payload = self._existing_payload()
                raise WorkdirLockError(
                    f"workdir already locked by {payload.get('owner', 'unknown')}: {self.workdir}"
                ) from exc
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(self._payload(), handle)
            self.acquired = True
            return

    def release(self) -> None:
        if not self.acquired:
            return
        payload = self._existing_payload()
        if int(payload.get("pid") or 0) == os.getpid() and payload.get("owner") == self.owner:
            try:
                self.lock_file.unlink()
            except OSError:
                pass
        self.acquired = False

    def __enter__(self) -> "WorkdirLock":
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()
