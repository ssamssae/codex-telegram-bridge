#!/usr/bin/env python3
"""Foreground Windows ConPTY host for one bridge-owned native Codex TUI.

The host owns the pseudoconsole for the full child lifetime. Local keyboard
input and authenticated bridge frames enter one ordered queue, while ConPTY
output is drained on a separate thread and forwarded to stdout. It installs no
service and does not attach to a pre-existing terminal session.
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import queue
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
from contextlib import contextmanager
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable


SCHEMA = 1
MAX_FRAME_BYTES = 256 * 1024
MAX_PROMPT_BYTES = 128 * 1024
DEFAULT_CAPTURE_BYTES = 512 * 1024
BRACKETED_PASTE_START = b"\x1b[200~"
BRACKETED_PASTE_END = b"\x1b[201~"
CLEAR_COMPOSER = b"\x05\x15\x01\x0b"  # C-e, C-u, C-a, C-k
# T-260711-30: submit must trail the paste in its own frame — a same-frame \r
# can be swallowed while the TUI is still processing the bracketed paste.
PASTE_SUBMIT_DELAY_MS = 200
CODEX_UPDATE_CHECK_OVERRIDE = "check_for_update_on_startup=false"
ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
UPDATE_PICKER_MARKERS = (
    "Update available!",
    "Update now",
    "Skip until next version",
)


def safe_log(message: str) -> None:
    """Log lifecycle metadata only; callers must never pass input or secrets."""

    print(f"[repl-host] {message}", file=sys.stderr, flush=True)


def normalize_text(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def encode_key(key: str) -> bytes:
    normalized = key.strip()
    lowered = normalized.lower()
    named = {
        "enter": b"\r",
        "return": b"\r",
        "tab": b"\t",
        "escape": b"\x1b",
        "esc": b"\x1b",
        "up": b"\x1b[A",
        "down": b"\x1b[B",
        "right": b"\x1b[C",
        "left": b"\x1b[D",
        "home": b"\x1b[H",
        "end": b"\x1b[F",
        "delete": b"\x1b[3~",
        "backspace": b"\x7f",
        "c-a": b"\x01",
        "c-e": b"\x05",
        "c-k": b"\x0b",
        "c-u": b"\x15",
        "c-c": b"\x03",
    }
    if lowered in named:
        return named[lowered]
    if len(normalized) == 1:
        return normalized.encode("utf-8")
    raise ValueError("unsupported key")


def encode_paste(
    text: str,
    *,
    clear_before: bool,
    submit_key: str,
    enter_count: int = 1,
) -> tuple[bytes, bytes]:
    """Return (paste_frame, submit_frame) to be queued as separate frames."""

    payload = normalize_text(text).rstrip("\n").encode("utf-8")
    if not payload:
        return b"", b""
    if len(payload) > MAX_PROMPT_BYTES:
        raise ValueError("prompt_too_large")
    count = max(1, min(int(enter_count), 8))
    submit = encode_key(submit_key) * count
    prefix = CLEAR_COMPOSER if clear_before else b""
    return prefix + BRACKETED_PASTE_START + payload + BRACKETED_PASTE_END, submit


@dataclass(frozen=True)
class InputFrame:
    sequence: int
    source: str
    payload: bytes
    delay_before_ms: int = 0


class OrderedInputQueue:
    def __init__(self) -> None:
        self._queue: queue.Queue[InputFrame] = queue.Queue()
        self._lock = threading.Lock()
        self._sequence = 0

    def put(self, source: str, payload: bytes, *, delay_before_ms: int = 0) -> int:
        if not payload:
            return 0
        if len(payload) > MAX_FRAME_BYTES:
            raise ValueError("frame_too_large")
        with self._lock:
            self._sequence += 1
            frame = InputFrame(
                self._sequence, source, bytes(payload), max(0, int(delay_before_ms))
            )
            self._queue.put(frame)
            return frame.sequence

    def get(self, timeout: float | None = None) -> InputFrame:
        return self._queue.get(timeout=timeout)


class RawOutputBuffer:
    def __init__(self, max_bytes: int = DEFAULT_CAPTURE_BYTES) -> None:
        self.max_bytes = max(4096, int(max_bytes))
        self._chunks: deque[bytes] = deque()
        self._size = 0
        self._lock = threading.Lock()

    def append(self, data: bytes) -> None:
        if not data:
            return
        with self._lock:
            self._chunks.append(bytes(data))
            self._size += len(data)
            while self._chunks and self._size > self.max_bytes:
                removed = self._chunks.popleft()
                self._size -= len(removed)

    def screen(self, lines: int) -> str:
        with self._lock:
            data = b"".join(self._chunks)
        text = data.decode("utf-8", errors="replace").replace("\r\n", "\n")
        return "\n".join(text.splitlines()[-max(1, min(int(lines), 500)) :])


class AmbiguousSessionError(RuntimeError):
    pass


class SessionBinder:
    """Bind only a single JSONL path created after this host launched."""

    def __init__(self, roots: Iterable[Path], launched_ns: int | None = None) -> None:
        self.roots = tuple(Path(root).expanduser() for root in roots)
        self.launched_ns = launched_ns if launched_ns is not None else time.time_ns()
        self.baseline = self._paths()

    def _paths(self) -> set[Path]:
        result: set[Path] = set()
        for root in self.roots:
            try:
                result.update(path.resolve() for path in root.rglob("rollout-*.jsonl"))
            except OSError:
                continue
        return result

    def candidates(self, min_mtime_ns: int | None = None) -> list[Path]:
        # T-260711-32: Codex TUIs create their rollout JSONL lazily on the first
        # turn, so "new file since host launch" can catch a foreign Codex (e.g. a
        # deploy smoke test) that turns earlier than our own child. Callers pass
        # min_mtime_ns = when input was first written to our child; only files
        # written at or after that moment can be our child's session.
        threshold = self.launched_ns if min_mtime_ns is None else max(self.launched_ns, min_mtime_ns)
        candidates: list[Path] = []
        for path in self._paths() - self.baseline:
            try:
                if path.stat().st_mtime_ns >= threshold:
                    candidates.append(path)
            except OSError:
                continue
        return sorted(candidates, key=lambda item: item.stat().st_mtime_ns)

    def bind_once(self, min_mtime_ns: int | None = None) -> Path | None:
        candidates = self.candidates(min_mtime_ns)
        if len(candidates) > 1:
            raise AmbiguousSessionError("multiple_new_sessions")
        return candidates[0] if candidates else None


class HostProtocol:
    """Pure request validator; responses never echo prompts or capabilities."""

    def __init__(
        self,
        generation: str,
        capability: str,
        input_queue: OrderedInputQueue,
        output: RawOutputBuffer,
        session_file: Callable[[], Path | None],
        child_alive: Callable[[], bool],
    ) -> None:
        self.generation = generation
        self.capability = capability
        self.input_queue = input_queue
        self.output = output
        self.session_file = session_file
        self.child_alive = child_alive

    def _base(self, request_id: str, *, ok: bool, error: str = "") -> dict[str, Any]:
        response: dict[str, Any] = {
            "schema": SCHEMA,
            "request_id": request_id,
            "generation": self.generation,
            "ok": ok,
        }
        if error:
            response["error"] = error
        return response

    def _bound_session(self) -> Path | None:
        path = self.session_file()
        if path is None or not path.is_file():
            return None
        return path

    def handle(self, request: Any) -> dict[str, Any]:
        if not isinstance(request, dict):
            return self._base("", ok=False, error="invalid_request")
        request_id = request.get("request_id")
        if not isinstance(request_id, str) or not (8 <= len(request_id) <= 128):
            request_id = ""
        if request.get("schema") != SCHEMA:
            return self._base(request_id, ok=False, error="schema_mismatch")
        generation = str(request.get("generation", ""))
        capability = str(request.get("capability", ""))
        if not secrets.compare_digest(generation, self.generation):
            return self._base(request_id, ok=False, error="generation_mismatch")
        if not secrets.compare_digest(capability, self.capability):
            return self._base(request_id, ok=False, error="unauthorized")

        op = request.get("op")
        if not self.child_alive():
            return self._base(request_id, ok=False, error="child_not_running")
        session = self._bound_session()

        try:
            if op == "verify":
                response = self._base(request_id, ok=True)
                response["session_bound"] = session is not None
                if session is not None:
                    response["session_file"] = str(session)
                return response
            if op == "session":
                if session is None:
                    return self._base(request_id, ok=False, error="session_unbound")
                response = self._base(request_id, ok=True)
                response["session_file"] = str(session)
                return response
            if op == "capture":
                response = self._base(request_id, ok=True)
                response["screen"] = self.output.screen(int(request.get("lines", 80)))
                response["screen_model"] = "raw-vt-tail"
                return response
            if op == "clear":
                sequence = self.input_queue.put("ipc", CLEAR_COMPOSER)
                response = self._base(request_id, ok=True)
                response["sequence"] = sequence
                return response
            if op == "key":
                sequence = self.input_queue.put("ipc", encode_key(str(request.get("key", ""))))
                response = self._base(request_id, ok=True)
                response["sequence"] = sequence
                return response
            if op == "choice":
                key = str(request.get("key", ""))
                if key:
                    payload = encode_key(key)
                else:
                    selected = request.get("selected_index")
                    index = int(request.get("index", 0))
                    if isinstance(selected, int):
                        delta = index - selected
                        payload = encode_key("Down" if delta > 0 else "Up") * abs(delta)
                        payload += encode_key("Enter")
                    else:
                        payload = encode_key(str(request.get("value", ""))) + encode_key("Enter")
                sequence = self.input_queue.put("ipc", payload)
                response = self._base(request_id, ok=True)
                response["sequence"] = sequence
                return response
            if op == "paste":
                text = request.get("text")
                if not isinstance(text, str):
                    return self._base(request_id, ok=False, error="invalid_prompt")
                paste_frame, submit_frame = encode_paste(
                    text,
                    clear_before=request.get("clear_before") is True,
                    submit_key=str(request.get("submit_key", "Tab")),
                    enter_count=int(request.get("enter_count", 1)),
                )
                sequence = self.input_queue.put("ipc", paste_frame)
                if sequence and submit_frame:
                    sequence = self.input_queue.put(
                        "ipc", submit_frame, delay_before_ms=PASTE_SUBMIT_DELAY_MS
                    )
                response = self._base(request_id, ok=True)
                response["sequence"] = sequence
                return response
        except (TypeError, ValueError):
            return self._base(request_id, ok=False, error="invalid_frame")
        return self._base(request_id, ok=False, error="unsupported_operation")


class COORD(ctypes.Structure):
    _fields_ = [("X", ctypes.c_short), ("Y", ctypes.c_short)]


class STARTUPINFOW(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("lpReserved", wintypes.LPWSTR),
        ("lpDesktop", wintypes.LPWSTR),
        ("lpTitle", wintypes.LPWSTR),
        ("dwX", wintypes.DWORD),
        ("dwY", wintypes.DWORD),
        ("dwXSize", wintypes.DWORD),
        ("dwYSize", wintypes.DWORD),
        ("dwXCountChars", wintypes.DWORD),
        ("dwYCountChars", wintypes.DWORD),
        ("dwFillAttribute", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("wShowWindow", wintypes.WORD),
        ("cbReserved2", wintypes.WORD),
        ("lpReserved2", ctypes.POINTER(wintypes.BYTE)),
        ("hStdInput", wintypes.HANDLE),
        ("hStdOutput", wintypes.HANDLE),
        ("hStdError", wintypes.HANDLE),
    ]


class STARTUPINFOEXW(ctypes.Structure):
    _fields_ = [("StartupInfo", STARTUPINFOW), ("lpAttributeList", wintypes.LPVOID)]


class PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("hProcess", wintypes.HANDLE),
        ("hThread", wintypes.HANDLE),
        ("dwProcessId", wintypes.DWORD),
        ("dwThreadId", wintypes.DWORD),
    ]


class SECURITY_ATTRIBUTES(ctypes.Structure):
    _fields_ = [
        ("nLength", wintypes.DWORD),
        ("lpSecurityDescriptor", wintypes.LPVOID),
        ("bInheritHandle", wintypes.BOOL),
    ]


class SID_AND_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("Sid", wintypes.LPVOID), ("Attributes", wintypes.DWORD)]


class TOKEN_USER(ctypes.Structure):
    _fields_ = [("User", SID_AND_ATTRIBUTES)]


def _require_windows() -> None:
    if os.name != "nt":
        raise RuntimeError("Windows native host requires os.name == 'nt'")


def _win_error(label: str) -> OSError:
    return ctypes.WinError(ctypes.get_last_error(), label)


def current_user_sid() -> str:
    _require_windows()
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32.GetCurrentProcess.argtypes = []
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
    kernel32.LocalFree.restype = wintypes.HLOCAL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    advapi32.OpenProcessToken.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
    ]
    advapi32.OpenProcessToken.restype = wintypes.BOOL
    advapi32.GetTokenInformation.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    advapi32.GetTokenInformation.restype = wintypes.BOOL
    advapi32.ConvertSidToStringSidW.argtypes = [
        wintypes.LPVOID,
        ctypes.POINTER(wintypes.LPWSTR),
    ]
    advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL
    token = wintypes.HANDLE()
    TOKEN_QUERY = 0x0008
    if not advapi32.OpenProcessToken(kernel32.GetCurrentProcess(), TOKEN_QUERY, ctypes.byref(token)):
        raise _win_error("OpenProcessToken")
    try:
        needed = wintypes.DWORD()
        advapi32.GetTokenInformation(token, 1, None, 0, ctypes.byref(needed))
        buffer = ctypes.create_string_buffer(needed.value)
        if not advapi32.GetTokenInformation(
            token, 1, buffer, needed, ctypes.byref(needed)
        ):
            raise _win_error("GetTokenInformation")
        token_user = ctypes.cast(buffer, ctypes.POINTER(TOKEN_USER)).contents
        sid_string = wintypes.LPWSTR()
        if not advapi32.ConvertSidToStringSidW(token_user.User.Sid, ctypes.byref(sid_string)):
            raise _win_error("ConvertSidToStringSidW")
        try:
            return sid_string.value
        finally:
            kernel32.LocalFree(sid_string)
    finally:
        kernel32.CloseHandle(token)


@contextmanager
def user_only_security_attributes():
    _require_windows()
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.LPVOID),
        ctypes.POINTER(wintypes.DWORD),
    ]
    advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
    kernel32.LocalFree.restype = wintypes.HLOCAL
    descriptor = wintypes.LPVOID()
    sddl = f"D:P(A;;GA;;;{current_user_sid()})"
    if not advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
        sddl, 1, ctypes.byref(descriptor), None
    ):
        raise _win_error("ConvertStringSecurityDescriptorToSecurityDescriptorW")
    attributes = SECURITY_ATTRIBUTES(
        ctypes.sizeof(SECURITY_ATTRIBUTES), descriptor, False
    )
    try:
        yield attributes, descriptor
    finally:
        kernel32.LocalFree(descriptor)


def protect_file_for_current_user(path: Path) -> None:
    _require_windows()
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    advapi32.GetSecurityDescriptorDacl.argtypes = [
        wintypes.LPVOID,
        ctypes.POINTER(wintypes.BOOL),
        ctypes.POINTER(wintypes.LPVOID),
        ctypes.POINTER(wintypes.BOOL),
    ]
    advapi32.GetSecurityDescriptorDacl.restype = wintypes.BOOL
    advapi32.SetNamedSecurityInfoW.argtypes = [
        wintypes.LPWSTR,
        ctypes.c_int,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.LPVOID,
        wintypes.LPVOID,
        wintypes.LPVOID,
    ]
    advapi32.SetNamedSecurityInfoW.restype = wintypes.DWORD
    present = wintypes.BOOL()
    defaulted = wintypes.BOOL()
    dacl = wintypes.LPVOID()
    with user_only_security_attributes() as (_attributes, descriptor):
        if not advapi32.GetSecurityDescriptorDacl(
            descriptor, ctypes.byref(present), ctypes.byref(dacl), ctypes.byref(defaulted)
        ):
            raise _win_error("GetSecurityDescriptorDacl")
        result = advapi32.SetNamedSecurityInfoW(
            str(path),
            1,  # SE_FILE_OBJECT
            0x00000004 | 0x80000000,  # DACL + protected DACL
            None,
            None,
            dacl,
            None,
        )
        if result != 0:
            raise OSError(result, "SetNamedSecurityInfoW")


def write_descriptor_secure(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{secrets.token_hex(6)}.tmp")
    data = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8") + b"\n"
    try:
        with temp.open("xb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        protect_file_for_current_user(temp)
        os.replace(temp, path)
    finally:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass


def windows_command(binary: str, args: list[str]) -> str:
    resolved = shutil.which(binary) or binary
    if Path(resolved).suffix.lower() in {".cmd", ".bat"}:
        comspec = os.environ.get("ComSpec", r"C:\Windows\System32\cmd.exe")
        tail = subprocess.list2cmdline([resolved, *args])
        return f'{subprocess.list2cmdline([comspec])} /d /s /c "{tail}"'
    return subprocess.list2cmdline([resolved, *args])


def unattended_codex_args(args: Iterable[str]) -> list[str]:
    """Disable Codex's interactive startup update check for this child only."""

    return ["-c", CODEX_UPDATE_CHECK_OVERRIDE, *args]


def update_picker_skip_keys(screen: str) -> bytes | None:
    """Return one safe navigation step toward confirming Skip."""

    selected = next(
        (line for line in reversed(screen.splitlines()) if "›" in line),
        "",
    )
    if "Skip until next version" in selected:
        return encode_key("Up")
    if "Skip" in selected:
        return encode_key("Enter")
    if "Update now" in selected:
        return encode_key("Down")
    return None


class WindowsConPty:
    PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE = 0x00020016
    EXTENDED_STARTUPINFO_PRESENT = 0x00080000
    CREATE_UNICODE_ENVIRONMENT = 0x00000400
    INFINITE = 0xFFFFFFFF

    def __init__(self, columns: int, rows: int) -> None:
        _require_windows()
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._configure_api()
        self.columns = columns
        self.rows = rows
        self.hpc = wintypes.HANDLE()
        self.input_write = wintypes.HANDLE()
        self.output_read = wintypes.HANDLE()
        self._pty_input_read = wintypes.HANDLE()
        self._pty_output_write = wintypes.HANDLE()
        self.process_info = PROCESS_INFORMATION()
        self._attribute_buffer: Any = None
        self._create()

    def _configure_api(self) -> None:
        pointer_size = ctypes.c_size_t
        self.kernel32.CreatePipe.argtypes = [
            ctypes.POINTER(wintypes.HANDLE),
            ctypes.POINTER(wintypes.HANDLE),
            ctypes.POINTER(SECURITY_ATTRIBUTES),
            wintypes.DWORD,
        ]
        self.kernel32.CreatePipe.restype = wintypes.BOOL
        self.kernel32.InitializeProcThreadAttributeList.argtypes = [
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.POINTER(pointer_size),
        ]
        self.kernel32.InitializeProcThreadAttributeList.restype = wintypes.BOOL
        self.kernel32.UpdateProcThreadAttribute.argtypes = [
            wintypes.LPVOID,
            pointer_size,
            pointer_size,
            wintypes.LPVOID,
            pointer_size,
            wintypes.LPVOID,
            ctypes.POINTER(pointer_size),
        ]
        self.kernel32.UpdateProcThreadAttribute.restype = wintypes.BOOL
        self.kernel32.DeleteProcThreadAttributeList.argtypes = [wintypes.LPVOID]
        self.kernel32.CreateProcessW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.LPWSTR,
            ctypes.POINTER(SECURITY_ATTRIBUTES),
            ctypes.POINTER(SECURITY_ATTRIBUTES),
            wintypes.BOOL,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.LPCWSTR,
            ctypes.POINTER(STARTUPINFOW),
            ctypes.POINTER(PROCESS_INFORMATION),
        ]
        self.kernel32.CreateProcessW.restype = wintypes.BOOL
        self.kernel32.GetStdHandle.argtypes = [wintypes.DWORD]
        self.kernel32.GetStdHandle.restype = wintypes.HANDLE
        self.kernel32.SetStdHandle.argtypes = [wintypes.DWORD, wintypes.HANDLE]
        self.kernel32.SetStdHandle.restype = wintypes.BOOL
        self.kernel32.ReadFile.argtypes = [
            wintypes.HANDLE,
            wintypes.LPVOID,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
            wintypes.LPVOID,
        ]
        self.kernel32.ReadFile.restype = wintypes.BOOL
        self.kernel32.WriteFile.argtypes = [
            wintypes.HANDLE,
            wintypes.LPCVOID,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
            wintypes.LPVOID,
        ]
        self.kernel32.WriteFile.restype = wintypes.BOOL
        self.kernel32.ResizePseudoConsole.argtypes = [wintypes.HANDLE, COORD]
        self.kernel32.ResizePseudoConsole.restype = ctypes.c_long
        self.kernel32.ClosePseudoConsole.argtypes = [wintypes.HANDLE]
        self.kernel32.ClosePseudoConsole.restype = None
        self.kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        self.kernel32.WaitForSingleObject.restype = wintypes.DWORD
        self.kernel32.GetExitCodeProcess.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.DWORD),
        ]
        self.kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        self.kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        self.kernel32.CloseHandle.restype = wintypes.BOOL

    @contextmanager
    def _without_parent_standard_handles(self):
        """Let ConPTY populate child std handles even when this host is redirected."""

        standard_handle_ids = (0xFFFFFFF6, 0xFFFFFFF5, 0xFFFFFFF4)
        originals = [self.kernel32.GetStdHandle(item) for item in standard_handle_ids]
        cleared: list[int] = []
        try:
            for item in standard_handle_ids:
                if not self.kernel32.SetStdHandle(item, None):
                    raise _win_error("SetStdHandle(clear)")
                cleared.append(item)
            yield
        finally:
            for item, original in zip(standard_handle_ids[: len(cleared)], originals):
                self.kernel32.SetStdHandle(item, original)

    def _create(self) -> None:
        if not self.kernel32.CreatePipe(
            ctypes.byref(self._pty_input_read), ctypes.byref(self.input_write), None, 0
        ):
            raise _win_error("CreatePipe(input)")
        if not self.kernel32.CreatePipe(
            ctypes.byref(self.output_read), ctypes.byref(self._pty_output_write), None, 0
        ):
            raise _win_error("CreatePipe(output)")
        create_pseudo_console = self.kernel32.CreatePseudoConsole
        create_pseudo_console.argtypes = [
            COORD,
            wintypes.HANDLE,
            wintypes.HANDLE,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.HANDLE),
        ]
        result = create_pseudo_console(
            COORD(self.columns, self.rows),
            self._pty_input_read,
            self._pty_output_write,
            0,
            ctypes.byref(self.hpc),
        )
        if result != 0:
            raise OSError(result, "CreatePseudoConsole")

    def launch(self, command_line: str, cwd: Path) -> int:
        size = ctypes.c_size_t()
        initialize = self.kernel32.InitializeProcThreadAttributeList
        initialize(None, 1, 0, ctypes.byref(size))
        self._attribute_buffer = ctypes.create_string_buffer(size.value)
        attribute_list = ctypes.cast(self._attribute_buffer, wintypes.LPVOID)
        if not initialize(attribute_list, 1, 0, ctypes.byref(size)):
            raise _win_error("InitializeProcThreadAttributeList")
        update = self.kernel32.UpdateProcThreadAttribute
        if not update(
            attribute_list,
            0,
            self.PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE,
            ctypes.c_void_p(self.hpc.value),
            ctypes.sizeof(self.hpc),
            None,
            None,
        ):
            raise _win_error("UpdateProcThreadAttribute")
        startup = STARTUPINFOEXW()
        startup.StartupInfo.cb = ctypes.sizeof(startup)
        startup.lpAttributeList = attribute_list
        environment = dict(os.environ)
        environment.setdefault("TERM", "xterm-256color")
        environment.setdefault("COLORTERM", "truecolor")
        environment_block = ctypes.create_unicode_buffer(
            "\0".join(f"{key}={value}" for key, value in sorted(environment.items())) + "\0\0"
        )
        mutable_command = ctypes.create_unicode_buffer(command_line)
        try:
            with self._without_parent_standard_handles():
                ok = self.kernel32.CreateProcessW(
                    None,
                    mutable_command,
                    None,
                    None,
                    False,
                    self.EXTENDED_STARTUPINFO_PRESENT | self.CREATE_UNICODE_ENVIRONMENT,
                    environment_block,
                    str(cwd),
                    ctypes.byref(startup.StartupInfo),
                    ctypes.byref(self.process_info),
                )
        finally:
            self.kernel32.DeleteProcThreadAttributeList(attribute_list)
            self._close_pseudoconsole_pipe_ends()
        if not ok:
            raise _win_error("CreateProcessW")
        self.kernel32.CloseHandle(self.process_info.hThread)
        return int(self.process_info.dwProcessId)

    def write(self, payload: bytes) -> None:
        if not payload:
            return
        written = wintypes.DWORD()
        if not self.kernel32.WriteFile(
            self.input_write, payload, len(payload), ctypes.byref(written), None
        ):
            raise _win_error("WriteFile(ConPTY input)")
        if written.value != len(payload):
            raise OSError("short ConPTY write")

    def read(self, size: int = 8192) -> bytes:
        buffer = ctypes.create_string_buffer(size)
        read = wintypes.DWORD()
        if not self.kernel32.ReadFile(
            self.output_read, buffer, size, ctypes.byref(read), None
        ):
            error = ctypes.get_last_error()
            if error in {6, 109, 232}:  # closed/broken/no-data pipe
                safe_log(f"ConPTY output closed winerror={error}")
                return b""
            raise _win_error("ReadFile(ConPTY output)")
        return buffer.raw[: read.value]

    def resize(self, columns: int, rows: int) -> None:
        result = self.kernel32.ResizePseudoConsole(self.hpc, COORD(columns, rows))
        if result != 0:
            raise OSError(result, "ResizePseudoConsole")
        self.columns, self.rows = columns, rows

    def alive(self) -> bool:
        if not self.process_info.hProcess:
            return False
        return self.kernel32.WaitForSingleObject(self.process_info.hProcess, 0) == 0x102

    def wait(self, timeout_ms: int = INFINITE) -> int:
        result = self.kernel32.WaitForSingleObject(self.process_info.hProcess, timeout_ms)
        if result == 0x102:
            raise TimeoutError("ConPTY child still running")
        exit_code = wintypes.DWORD()
        if not self.kernel32.GetExitCodeProcess(
            self.process_info.hProcess, ctypes.byref(exit_code)
        ):
            raise _win_error("GetExitCodeProcess")
        return int(exit_code.value)

    def close_input(self) -> None:
        if self.input_write:
            self.kernel32.CloseHandle(self.input_write)
            self.input_write = wintypes.HANDLE()

    def _close_pseudoconsole_pipe_ends(self) -> None:
        if self._pty_input_read:
            self.kernel32.CloseHandle(self._pty_input_read)
            self._pty_input_read = wintypes.HANDLE()
        if self._pty_output_write:
            self.kernel32.CloseHandle(self._pty_output_write)
            self._pty_output_write = wintypes.HANDLE()

    def close_pseudoconsole(self) -> None:
        if self.hpc:
            self.kernel32.ClosePseudoConsole(self.hpc)
            self.hpc = wintypes.HANDLE()

    def close(self) -> None:
        self._close_pseudoconsole_pipe_ends()
        self.close_input()
        self.close_pseudoconsole()
        if self.output_read:
            self.kernel32.CloseHandle(self.output_read)
            self.output_read = wintypes.HANDLE()
        if self.process_info.hProcess:
            self.kernel32.CloseHandle(self.process_info.hProcess)
            self.process_info.hProcess = wintypes.HANDLE()


class NamedPipeServer:
    def __init__(self, pipe_name: str, protocol: HostProtocol, stop: threading.Event) -> None:
        self.pipe_name = pipe_name
        self.protocol = protocol
        self.stop = stop

    def _read_request(self, kernel32: Any, handle: wintypes.HANDLE) -> bytes:
        data = bytearray()
        while len(data) <= MAX_FRAME_BYTES:
            buffer = ctypes.create_string_buffer(4096)
            read = wintypes.DWORD()
            if not kernel32.ReadFile(handle, buffer, 4096, ctypes.byref(read), None):
                error = ctypes.get_last_error()
                if error in {109, 232}:
                    break
                raise _win_error("ReadFile(named pipe)")
            if read.value == 0:
                break
            data.extend(buffer.raw[: read.value])
            if b"\n" in data:
                return bytes(data.split(b"\n", 1)[0])
        if len(data) > MAX_FRAME_BYTES:
            raise ValueError("frame_too_large")
        return bytes(data)

    def serve(self) -> None:
        _require_windows()
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateNamedPipeW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.POINTER(SECURITY_ATTRIBUTES),
        ]
        kernel32.CreateNamedPipeW.restype = wintypes.HANDLE
        kernel32.ConnectNamedPipe.argtypes = [wintypes.HANDLE, wintypes.LPVOID]
        kernel32.ConnectNamedPipe.restype = wintypes.BOOL
        kernel32.ReadFile.argtypes = [
            wintypes.HANDLE,
            wintypes.LPVOID,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
            wintypes.LPVOID,
        ]
        kernel32.ReadFile.restype = wintypes.BOOL
        kernel32.WriteFile.argtypes = [
            wintypes.HANDLE,
            wintypes.LPCVOID,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
            wintypes.LPVOID,
        ]
        kernel32.WriteFile.restype = wintypes.BOOL
        kernel32.FlushFileBuffers.argtypes = [wintypes.HANDLE]
        kernel32.FlushFileBuffers.restype = wintypes.BOOL
        kernel32.DisconnectNamedPipe.argtypes = [wintypes.HANDLE]
        kernel32.DisconnectNamedPipe.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value
        while not self.stop.is_set():
            with user_only_security_attributes() as (attributes, _descriptor):
                handle = kernel32.CreateNamedPipeW(
                    self.pipe_name,
                    0x00000003,  # PIPE_ACCESS_DUPLEX
                    0x00000008,  # byte mode + reject remote clients
                    1,
                    65536,
                    65536,
                    0,
                    ctypes.byref(attributes),
                )
            if handle == INVALID_HANDLE_VALUE:
                safe_log("named pipe create failed")
                return
            try:
                connected = kernel32.ConnectNamedPipe(handle, None)
                if not connected and ctypes.get_last_error() != 535:  # ERROR_PIPE_CONNECTED
                    continue
                try:
                    raw = self._read_request(kernel32, handle)
                    request = json.loads(raw.decode("utf-8")) if raw else None
                    response = self.protocol.handle(request)
                except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
                    response = self.protocol._base("", ok=False, error="invalid_request")
                encoded = (
                    json.dumps(response, ensure_ascii=False, separators=(",", ":")) + "\n"
                ).encode("utf-8")
                written = wintypes.DWORD()
                kernel32.WriteFile(handle, encoded, len(encoded), ctypes.byref(written), None)
                kernel32.FlushFileBuffers(handle)
            finally:
                kernel32.DisconnectNamedPipe(handle)
                kernel32.CloseHandle(handle)


def console_size(default_columns: int, default_rows: int) -> tuple[int, int]:
    try:
        size = os.get_terminal_size(sys.stdout.fileno())
        return max(20, size.columns), max(5, size.lines)
    except OSError:
        return default_columns, default_rows


def keyboard_loop(inputs: OrderedInputQueue, stop: threading.Event) -> None:
    import msvcrt

    special = {"H": "Up", "P": "Down", "K": "Left", "M": "Right", "G": "Home", "O": "End", "S": "Delete"}
    while not stop.is_set():
        try:
            char = msvcrt.getwch()
        except (EOFError, KeyboardInterrupt):
            stop.set()
            return
        if char == "\x1d":  # Ctrl-] exits the host without stealing Ctrl-C from Codex.
            stop.set()
            return
        payload = bytearray()
        if char in {"\x00", "\xe0"}:
            key = special.get(msvcrt.getwch())
            if key:
                payload.extend(encode_key(key))
        else:
            payload.extend(char.encode("utf-8"))
        while msvcrt.kbhit() and len(payload) < 65536:
            following = msvcrt.getwch()
            if following in {"\x00", "\xe0"}:
                key = special.get(msvcrt.getwch())
                if key:
                    payload.extend(encode_key(key))
            else:
                payload.extend(following.encode("utf-8"))
        if payload:
            inputs.put("keyboard", bytes(payload))


def writer_loop(
    pty: WindowsConPty,
    inputs: OrderedInputQueue,
    stop: threading.Event,
    input_activity: dict[str, int] | None = None,
    live: Any | None = None,
) -> None:
    while not stop.is_set() and pty.alive():
        try:
            frame = inputs.get(timeout=0.25)
        except queue.Empty:
            continue
        if frame.delay_before_ms > 0 and stop.wait(frame.delay_before_ms / 1000.0):
            return
        try:
            if input_activity is not None and input_activity.get("first_ns", 0) == 0:
                input_activity["first_ns"] = time.time_ns()
            # 텔레그램 주입은 무에코라 콘솔에 흔적이 없다 — 라이브 파일에만 마커를 남겨
            # 뷰어에서 "무엇이 주입됐는지" 가 보이게 한다 (T-260722-066).
            if live is not None and frame.source == "ipc":
                live.note(f"주입 <- {summarize_injection(frame.payload)}")
            pty.write(frame.payload)
        except OSError:
            stop.set()
            return


DEFAULT_LIVE_RAW_MAX_BYTES = 8 * 1024 * 1024


def default_live_raw_path() -> Path:
    """호스트 state_path 와 같은 관례(LOCALAPPDATA/codex-telegram-bridge/)를 쓴다."""
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    return Path(base) / "codex-telegram-bridge" / "conpty-live.raw"


class LiveRawSink:
    """conpty raw 출력을 stdout 과 append-only 파일에 동시에 흘리는 tee (T-260722-066).

    발원: 사용자 실사용 증상 — PowerShell 에서 세션 접속은 되는데 텔레그램 주입
    프롬프트·출력이 터미널에 안 보인다. 근본원인은 버그가 아니라 가시성 설계갭으로,
    raw 출력이 인메모리 RawOutputBuffer 에만 남아 밖에서 볼 방법이 없었다. 파일로
    흘려 뷰어(codex-conpty-live-view.ps1)가 tail 할 수 있게 한다.

    설계 제약:
      - 파일 IO 실패가 호스트를 죽이면 안 된다 → 1회 경고 후 stdout 전용으로 강등.
      - 콘솔 출력이 파일 문제로 유실되면 안 된다 → passthrough 를 먼저 쓴다.
      - 무한증식 금지 → 크기 상한 초과 시 <path>.1 로 1회전(총 2배 상한).
      - 주입 무에코 설계 유지 → note() 마커는 **파일에만** 쓴다. stdout 에 쓰면
        codex TUI 로 에코되어 렌더링을 깨고 무에코 계약을 위반한다.
    """

    def __init__(
        self,
        path: Path,
        passthrough: Any,
        max_bytes: int = DEFAULT_LIVE_RAW_MAX_BYTES,
    ) -> None:
        self._path = path
        self._passthrough = passthrough
        self._max_bytes = max(0, int(max_bytes))
        self._lock = threading.Lock()
        self._handle: Any | None = None
        self._size = 0
        self._degraded = False
        self._open()

    @property
    def path(self) -> Path:
        return self._path

    def _open(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._handle = open(self._path, "ab", buffering=0)
            self._size = self._path.stat().st_size
        except OSError as exc:
            self._degrade(f"open {self._path} failed: {exc}")

    def _degrade(self, reason: str) -> None:
        if not self._degraded:
            self._degraded = True
            safe_log(f"live raw sink disabled ({reason}); stdout only")
        handle, self._handle = self._handle, None
        if handle is not None:
            try:
                handle.close()
            except OSError:
                pass

    def _rotate_locked(self) -> None:
        handle, self._handle = self._handle, None
        if handle is not None:
            try:
                handle.close()
            except OSError:
                pass
        try:
            rotated = self._path.with_name(self._path.name + ".1")
            os.replace(self._path, rotated)
        except OSError as exc:
            self._degrade(f"rotate failed: {exc}")
            return
        self._open()

    def _write_file_locked(self, data: bytes) -> None:
        if self._handle is None:
            return
        if self._max_bytes and self._size + len(data) > self._max_bytes:
            self._rotate_locked()
            if self._handle is None:
                return
        try:
            self._handle.write(data)
            self._size += len(data)
        except OSError as exc:
            self._degrade(f"write failed: {exc}")

    def write(self, data: bytes) -> int:
        # 콘솔이 먼저다 — 파일 쪽 문제로 사람이 보는 출력을 잃지 않는다.
        written = self._passthrough.write(data)
        with self._lock:
            self._write_file_locked(data)
        return written if written is not None else len(data)

    def flush(self) -> None:
        try:
            self._passthrough.flush()
        finally:
            with self._lock:
                if self._handle is not None:
                    try:
                        self._handle.flush()
                    except OSError as exc:
                        self._degrade(f"flush failed: {exc}")

    def note(self, text: str) -> None:
        """사람이 읽는 이벤트 마커 1줄 — 파일 전용(stdout 무에코 계약 유지)."""
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        line = f"\r\n[{stamp}] {text}\r\n".encode("utf-8", "replace")
        with self._lock:
            self._write_file_locked(line)

    def close(self) -> None:
        with self._lock:
            handle, self._handle = self._handle, None
            if handle is not None:
                try:
                    handle.close()
                except OSError:
                    pass


def summarize_injection(payload: bytes, limit: int = 120) -> str:
    """주입 페이로드를 마커용 1줄 요약으로 축약한다 (제어문자 제거·길이 컷)."""
    text = payload.decode("utf-8", "replace")
    text = re.sub(r"[\x00-\x1f\x7f]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return "(제어 키 입력)"
    if len(text) > limit:
        return text[:limit] + f"… (+{len(text) - limit}자)"
    return text


def build_live_raw_sink(passthrough: Any) -> LiveRawSink | None:
    """env 로 경로·상한을 정한다. 경로를 빈 문자열로 두면 비활성."""
    configured = os.environ.get("CODEX_CONPTY_LIVE_RAW")
    if configured is None:
        target = default_live_raw_path()
    else:
        raw_path = configured.strip()
        if not raw_path:
            return None
        target = Path(raw_path).expanduser()
    try:
        max_bytes = int(os.environ.get("CODEX_CONPTY_LIVE_MAX_BYTES", DEFAULT_LIVE_RAW_MAX_BYTES))
    except ValueError:
        max_bytes = DEFAULT_LIVE_RAW_MAX_BYTES
    return LiveRawSink(target, passthrough, max_bytes)


def output_loop(
    pty: WindowsConPty,
    output: RawOutputBuffer,
    stop: threading.Event,
    sink: Any | None = None,
) -> None:
    target = sink if sink is not None else sys.stdout.buffer
    while True:
        try:
            chunk = pty.read()
        except OSError as exc:
            safe_log(f"output read stopped winerror={getattr(exc, 'winerror', None)}")
            chunk = b""
        if not chunk:
            break
        output.append(chunk)
        target.write(chunk)
        target.flush()
    stop.set()


def resize_loop(
    pty: WindowsConPty,
    stop: threading.Event,
    default_columns: int,
    default_rows: int,
) -> None:
    previous = (pty.columns, pty.rows)
    while not stop.wait(0.5):
        current = console_size(default_columns, default_rows)
        if current != previous:
            try:
                pty.resize(*current)
                previous = current
            except OSError:
                return


def wait_for_session(
    binder: SessionBinder,
    timeout_seconds: float,
    keep_waiting: Callable[[], bool] | None = None,
    input_activity: dict[str, int] | None = None,
) -> Path:
    deadline = time.monotonic() + timeout_seconds if timeout_seconds > 0 else None
    while (deadline is None or time.monotonic() < deadline) and (
        keep_waiting is None or keep_waiting()
    ):
        if input_activity is not None:
            # T-260711-32: rollout files appear lazily on the first turn, so a
            # session can only be ours once input has reached our child. Until
            # then any new file is a foreign Codex — do not bind it.
            first_input_ns = input_activity.get("first_ns", 0)
            if first_input_ns:
                path = binder.bind_once(min_mtime_ns=first_input_ns)
                if path is not None:
                    return path
        else:
            path = binder.bind_once()
            if path is not None:
                return path
        time.sleep(0.1)
    raise TimeoutError("session_bind_timeout")


def wait_for_tui_ready(
    output: RawOutputBuffer,
    child_alive: Callable[[], bool],
    timeout_seconds: float = 15.0,
    on_update_picker: Callable[[bytes], None] | None = None,
) -> bool:
    handled_selection = ""
    deadline = time.monotonic() + timeout_seconds
    while child_alive() and time.monotonic() < deadline:
        screen = ANSI_ESCAPE_RE.sub("", output.screen(300))
        picker_present = all(marker in screen for marker in UPDATE_PICKER_MARKERS)
        picker_end = max((screen.rfind(marker) for marker in UPDATE_PICKER_MARKERS), default=-1)
        composer = screen.rfind("›")
        if picker_present and composer <= picker_end:
            selected = next(
                (line for line in reversed(screen.splitlines()) if "›" in line),
                "",
            )
            if on_update_picker is not None and selected != handled_selection:
                skip_keys = update_picker_skip_keys(screen)
                if skip_keys is not None:
                    on_update_picker(skip_keys)
                    handled_selection = selected
            time.sleep(0.05)
            continue
        if composer >= 0:
            return True
        time.sleep(0.05)
    return False


def remove_descriptor_if_generation(path: Path, generation: str) -> None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if secrets.compare_digest(str(payload.get("generation", "")), generation):
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def run_host(args: argparse.Namespace) -> int:
    _require_windows()
    workdir = Path(args.workdir).expanduser().resolve()
    session_roots = [Path(item).expanduser() for item in args.session_root]
    generation = secrets.token_hex(16)
    capability = secrets.token_urlsafe(32)
    sid_hash = hashlib.sha256(current_user_sid().encode("ascii")).hexdigest()[:12]
    pipe_name = rf"\\.\pipe\codex-repl-host-{sid_hash}-{generation}"
    state_path = Path(args.state_path).expanduser()
    columns, rows = console_size(args.columns, args.rows)
    binder = SessionBinder(session_roots)
    pty = WindowsConPty(columns, rows)
    command_line = windows_command(args.codex_bin, unattended_codex_args(args.codex_arg))
    child_pid = pty.launch(command_line, workdir)
    stop = threading.Event()
    inputs = OrderedInputQueue()
    output = RawOutputBuffer(args.capture_bytes)
    session_holder: dict[str, Path | None] = {"path": None}
    input_activity: dict[str, int] = {"first_ns": 0}

    # raw 출력을 append-only 파일로도 흘린다 — 뷰어가 tail 해서 사용자 콘솔에 실시간
    # 표시할 수 있게 하는 배관 (T-260722-066). 실패해도 stdout 전용으로 계속 돈다.
    live_sink = build_live_raw_sink(sys.stdout.buffer)
    if live_sink is not None:
        safe_log(f"live raw sink -> {live_sink.path}")
        live_sink.note(f"conpty host start pid={child_pid} generation={generation[:8]}")

    output_thread = threading.Thread(
        target=output_loop,
        args=(pty, output, stop, live_sink),
        daemon=True,
        name="conpty-output",
    )
    output_thread.start()
    keyboard_thread = threading.Thread(
        target=keyboard_loop, args=(inputs, stop), daemon=True, name="local-keyboard"
    )
    keyboard_thread.start()
    resize_thread = threading.Thread(
        target=resize_loop,
        args=(pty, stop, args.columns, args.rows),
        daemon=True,
        name="conpty-resize",
    )
    resize_thread.start()

    try:
        def skip_update_picker(keys: bytes) -> None:
            pty.write(keys)
            safe_log("Codex startup update picker detected; forcing Skip selection")

        if not wait_for_tui_ready(output, pty.alive, on_update_picker=skip_update_picker):
            safe_log("Codex TUI readiness timeout")
            return 3
        writer_thread = threading.Thread(
            target=writer_loop,
            args=(pty, inputs, stop, input_activity, live_sink),
            daemon=True,
            name="conpty-input",
        )
        writer_thread.start()
        protocol = HostProtocol(
            generation,
            capability,
            inputs,
            output,
            lambda: session_holder["path"],
            pty.alive,
        )
        descriptor = {
            "schema": SCHEMA,
            "generation": generation,
            "capability": capability,
            "pipe_name": pipe_name,
            "host_pid": os.getpid(),
            "child_pid": child_pid,
            "session_bound": False,
            "created_at_ns": time.time_ns(),
        }
        write_descriptor_secure(state_path, descriptor)
        server = NamedPipeServer(pipe_name, protocol, stop)
        threading.Thread(target=server.serve, daemon=True, name="conpty-ipc").start()
        safe_log(f"awaiting session generation={generation[:8]} child_pid={child_pid}")
        session_holder["path"] = wait_for_session(
            binder,
            args.bind_timeout,
            lambda: pty.alive() and not stop.is_set(),
            input_activity=input_activity,
        )
        descriptor["session_bound"] = True
        descriptor["session_file"] = str(session_holder["path"])
        write_descriptor_secure(state_path, descriptor)
        safe_log(f"ready generation={generation[:8]} child_pid={child_pid}")
        while pty.alive() and not stop.wait(0.25):
            pass
        pty.close_input()
        exit_code = pty.wait(5000)
        pty.close_pseudoconsole()
        output_thread.join(timeout=2.0)
        return exit_code
    except (AmbiguousSessionError, TimeoutError) as exc:
        safe_log(str(exc))
        return 3
    finally:
        stop.set()
        remove_descriptor_if_generation(state_path, generation)
        pty.close()


def run_self_test() -> int:
    _require_windows()
    import io

    marker = f"conpty-self-test-{secrets.token_hex(4)}"
    pty = WindowsConPty(80, 24)
    output = RawOutputBuffer()
    stop = threading.Event()
    sink = io.BytesIO()
    try:
        command = windows_command(os.environ.get("ComSpec", "cmd.exe"), ["/d", "/q"])
        pty.launch(command, Path(os.environ.get("TEMP", r"C:\Windows\Temp")))
        thread = threading.Thread(target=output_loop, args=(pty, output, stop, sink), daemon=True)
        thread.start()
        pty.write(f"echo {marker}\rexit\r".encode("ascii"))
        deadline = time.monotonic() + 3.0
        while marker.encode("ascii") not in sink.getvalue() and time.monotonic() < deadline:
            time.sleep(0.05)
        marker_seen = marker.encode("ascii") in sink.getvalue()
        if not marker_seen:
            pty.close_input()
            pty.close_pseudoconsole()
            thread.join(timeout=2.0)
            safe_log(f"self-test input/output timeout captured_bytes={len(sink.getvalue())}")
            return 4
        code = pty.wait(5000)
        pty.close_input()
        pty.close_pseudoconsole()
        thread.join(timeout=2.0)
        captured = sink.getvalue()
        if code != 0 or marker.encode("ascii") not in captured:
            safe_log(
                f"self-test failed exit={code} marker_seen={marker.encode('ascii') in captured} "
                f"captured_bytes={len(captured)}"
            )
            return 4
        print("PASS ConPTY create/launch/input/output/drain")
    finally:
        pty.close()

    generation = secrets.token_hex(16)
    capability = secrets.token_urlsafe(32)
    stop = threading.Event()
    inputs = OrderedInputQueue()
    output = RawOutputBuffer()
    with tempfile.TemporaryDirectory(prefix="codex-repl-host-") as directory:
        root = Path(directory)
        session = root / "rollout-self-test.jsonl"
        session.write_text("{}\n", encoding="utf-8")
        state = root / "repl-host.json"
        sid_hash = hashlib.sha256(current_user_sid().encode("ascii")).hexdigest()[:12]
        pipe_name = rf"\\.\pipe\codex-repl-host-test-{sid_hash}-{secrets.token_hex(8)}"
        protocol = HostProtocol(
            generation,
            capability,
            inputs,
            output,
            lambda: session,
            lambda: True,
        )
        server = NamedPipeServer(pipe_name, protocol, stop)
        thread = threading.Thread(target=server.serve, daemon=True)
        thread.start()
        write_descriptor_secure(
            state,
            {
                "schema": SCHEMA,
                "generation": generation,
                "capability": capability,
                "pipe_name": pipe_name,
            },
        )
        request = {
            "schema": SCHEMA,
            "request_id": secrets.token_hex(8),
            "generation": generation,
            "capability": capability,
            "op": "verify",
        }
        deadline = time.monotonic() + 3.0
        handle = None
        while handle is None and time.monotonic() < deadline:
            try:
                handle = open(pipe_name, "r+b", buffering=0)
            except OSError:
                time.sleep(0.05)
        if handle is None:
            safe_log("self-test IPC connection timeout")
            return 4
        with handle:
            handle.write((json.dumps(request, separators=(",", ":")) + "\n").encode("utf-8"))
            response = json.loads(handle.readline().decode("utf-8"))
            stop.set()
        thread.join(timeout=2.0)
        if response.get("ok") is not True or response.get("generation") != generation:
            safe_log("self-test IPC response invalid")
            return 4
        if json.loads(state.read_text(encoding="utf-8")).get("capability") != capability:
            safe_log("self-test descriptor invalid")
            return 4
    print("PASS local-user-only IPC/capability/descriptor")
    return 0


def prepare_smoke_codex_home(smoke_home: Path, real_home: Path | None = None) -> Path:
    """T-260711-32: isolate the smoke Codex under its own CODEX_HOME.

    The smoke turns immediately while a live host's TUI creates its rollout
    lazily, so a shared session root lets the smoke's JSONL hijack the live
    host's session binding. Copy only login/config into a throwaway home; the
    smoke's sessions then land under <smoke_home>/sessions and never pollute
    the live root.
    """

    real = real_home if real_home is not None else Path.home() / ".codex"
    smoke_home.mkdir(parents=True, exist_ok=True)
    (smoke_home / "sessions").mkdir(parents=True, exist_ok=True)
    for name in ("auth.json", "config.toml"):
        source = real / name
        if source.is_file():
            shutil.copy2(source, smoke_home / name)
    return smoke_home / "sessions"


def remove_smoke_auth_copy(smoke_home: Path) -> None:
    """Delete the copied login first, zeroizing it before a retry if needed."""

    auth_copy = smoke_home / "auth.json"
    try:
        auth_copy.unlink()
        return
    except FileNotFoundError:
        return
    except OSError:
        pass

    try:
        remaining = auth_copy.stat().st_size
        zero_chunk = b"\0" * min(max(remaining, 1), 64 * 1024)
        with auth_copy.open("r+b", buffering=0) as handle:
            while remaining:
                written = handle.write(zero_chunk[:remaining])
                if not written:
                    raise OSError("short write while zeroizing smoke authentication copy")
                remaining -= written
            handle.flush()
            os.fsync(handle.fileno())
        auth_copy.unlink()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise RuntimeError("failed to remove smoke authentication copy") from exc


def run_codex_smoke_test(args: argparse.Namespace) -> int:
    """Launch the native Codex TUI, bind its JSONL, then exit without a model turn."""

    _require_windows()
    import io

    workdir = Path(args.workdir).expanduser().resolve()
    smoke_home = Path(tempfile.mkdtemp(prefix="codex-smoke-home-"))
    pty: WindowsConPty | None = None
    stop = threading.Event()
    output_thread: threading.Thread | None = None

    def final_seen(path: Path) -> bool:
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return False
        for line in reversed(lines):
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            payload = record.get("payload") if isinstance(record.get("payload"), dict) else {}
            if record.get("type") == "event_msg":
                if payload.get("type") == "agent_message" and payload.get("phase") == "final_answer":
                    return True
            if record.get("type") == "response_item":
                metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
                if (
                    payload.get("type") == "message"
                    and payload.get("role") == "assistant"
                    and (payload.get("phase") or metadata.get("phase")) == "final_answer"
                ):
                    return True
        return False

    try:
        smoke_sessions = prepare_smoke_codex_home(smoke_home)
        os.environ["CODEX_HOME"] = str(smoke_home)
        binder = SessionBinder([smoke_sessions])
        pty = WindowsConPty(args.columns, args.rows)
        output = RawOutputBuffer(args.capture_bytes)
        inputs = OrderedInputQueue()
        sink = io.BytesIO()
        pty.launch(
            windows_command(args.codex_bin, unattended_codex_args(args.codex_arg)),
            workdir,
        )
        output_thread = threading.Thread(
            target=output_loop,
            args=(pty, output, stop, sink),
            daemon=True,
            name="codex-smoke-output",
        )
        output_thread.start()
        def skip_update_picker(keys: bytes) -> None:
            pty.write(keys)
            safe_log("Codex smoke startup update picker detected; forcing Skip selection")

        if not wait_for_tui_ready(output, pty.alive, on_update_picker=skip_update_picker):
            safe_log("Codex smoke TUI readiness timeout")
            return 4
        threading.Thread(
            target=writer_loop,
            args=(pty, inputs, stop),
            daemon=True,
            name="codex-smoke-input",
        ).start()
        smoke_paste, smoke_submit = encode_paste(
            "Windows 네이티브 한글 😀 멀티라인 스모크 테스트입니다.\n"
            "파일과 설정은 읽거나 수정하지 말고 READY 한 단어만 답하세요.",
            clear_before=False,
            submit_key="Enter",
        )
        inputs.put("self-test", smoke_paste)
        inputs.put("self-test", smoke_submit, delay_before_ms=PASTE_SUBMIT_DELAY_MS)
        try:
            session = wait_for_session(binder, args.bind_timeout, pty.alive)
        except TimeoutError:
            safe_log(
                "Codex smoke session-bind timeout "
                f"child_alive={pty.alive()} captured_bytes={len(sink.getvalue())}"
            )
            return 4
        final_deadline = time.monotonic() + 60.0
        while not final_seen(session) and pty.alive() and time.monotonic() < final_deadline:
            time.sleep(0.1)
        if not final_seen(session):
            safe_log("Codex smoke final-answer timeout")
            return 4
        exit_paste, exit_submit = encode_paste("/exit", clear_before=False, submit_key="Enter")
        inputs.put("self-test", exit_paste)
        inputs.put("self-test", exit_submit, delay_before_ms=PASTE_SUBMIT_DELAY_MS)
        deadline = time.monotonic() + 10.0
        while pty.alive() and time.monotonic() < deadline:
            time.sleep(0.05)
        if pty.alive():
            inputs.put("self-test", encode_key("C-c") + encode_key("C-c"))
            deadline = time.monotonic() + 3.0
            while pty.alive() and time.monotonic() < deadline:
                time.sleep(0.05)
        if pty.alive():
            safe_log("Codex smoke test exit timeout")
            return 4
        code = pty.wait(5000)
        pty.close_input()
        pty.close_pseudoconsole()
        output_thread.join(timeout=2.0)
        if code != 0 or not session.is_file() or not sink.getvalue():
            safe_log("Codex smoke test validation failed")
            return 4
        print("PASS native Codex TUI launch/session-bind/command/exit")
        return 0
    finally:
        auth_cleanup_error: Exception | None = None
        try:
            remove_smoke_auth_copy(smoke_home)
        except Exception as exc:  # keep closing the child before one bounded retry
            auth_cleanup_error = exc
        try:
            stop.set()
            if pty is not None:
                pty.close()
            if output_thread is not None:
                output_thread.join(timeout=2.0)
        finally:
            auth_copy = smoke_home / "auth.json"
            if auth_cleanup_error is not None and auth_copy.exists():
                try:
                    remove_smoke_auth_copy(smoke_home)
                except Exception as exc:
                    auth_cleanup_error = exc
            try:
                shutil.rmtree(smoke_home)
            except FileNotFoundError:
                pass
            finally:
                if auth_copy.exists():
                    raise RuntimeError("smoke authentication copy remains after cleanup") from auth_cleanup_error


def parser() -> argparse.ArgumentParser:
    default_state = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "codex-telegram-bridge" / "repl-host.json"
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("--codex-bin", default="codex")
    value.add_argument("--codex-arg", action="append", default=[])
    value.add_argument("--workdir", default=str(Path.cwd()))
    value.add_argument("--state-path", default=str(default_state))
    value.add_argument("--session-root", action="append", default=[str(Path.home() / ".codex" / "sessions")])
    value.add_argument(
        "--bind-timeout",
        type=float,
        default=0.0,
        help="seconds to wait for the first bound JSONL session (0 waits for child exit)",
    )
    value.add_argument("--columns", type=int, default=120)
    value.add_argument("--rows", type=int, default=40)
    value.add_argument("--capture-bytes", type=int, default=DEFAULT_CAPTURE_BYTES)
    value.add_argument("--self-test", action="store_true")
    value.add_argument("--codex-smoke-test", action="store_true")
    return value


def main() -> int:
    args = parser().parse_args()
    try:
        if args.self_test:
            return run_self_test()
        if args.codex_smoke_test:
            return run_codex_smoke_test(args)
        return run_host(args)
    except Exception as exc:  # noqa: BLE001
        safe_log(f"fatal: {type(exc).__name__}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
