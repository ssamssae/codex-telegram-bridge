"""codex ConPTY attach 클라이언트 — 보이고 입력도 되는 tmux 패리티 창 (T-260722-076).

왜 (요건 = tmux 패리티: 보이고 입력도 되는 터미널, 로그 표시가 아니다):
읽기전용 tail 뷰어는 화면을 반복 재그리기하는 스트림에서 제어열을 걷어내고 줄
단위로 흘린다 — 그래서 같은 자리를 고쳐 그린 조각이 아래로 쌓여 글자가 뭉갠다.
게다가 tail 은 입력을 못 보낸다. 표시 방식 자체가 요건 미달이었다.

이 클라이언트는 PTY 를 소유하지 않는다. 헤드리스 호스트가 계속 소유하고 여기서는
  표시 = 호스트가 흘리는 append-only raw 파일(LiveRawSink, crb 0.9.3)을 구독해
         제어열을 살린 채 콘솔로 그대로 흘린다(VT 패스스루).
  입력 = 호스트 named-pipe 의 key op — 브릿지가 이미 쓰는 인증 경로 그대로.
둘 다 기성 경로라 호스트 모듈·site-packages 무변경이다. 두 번째 콘솔이 PTY 를
빼앗는 선점 문제도 성립하지 않는다.

화면 크기: 헤드리스 호스트는 자기 stdout 에 콘솔이 없어 --columns/--rows 기본값으로
ConPTY 를 만든다(codex_repl_host_windows.console_size 의 폴백 = 120x40). 창 크기가
그와 다르면 TUI 가 그린 줄이 어긋나므로 시작할 때 맞춘다.

주입 마커: 호스트는 텔레그램 주입을 raw 파일에만 한 줄 남긴다(무에코 설계 보완).
그 줄은 TUI 가 그린 화면이 아니므로 그대로 뿌리면 화면을 헤집는다 — 여기서 걷어낸다.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import sys
import time
from pathlib import Path

SCHEMA = 1

# 호스트 argparse 기본값. 헤드리스라 console_size() 가 폴백해 이 값이 실제 ConPTY 크기가 된다.
DEFAULT_COLUMNS = 120
DEFAULT_ROWS = 40

# LiveRawSink.note() 가 쓰는 형식: "\r\n[YYYY-MM-DD HH:MM:SS] <text>\r\n"
MARKER_RE = re.compile(rb"\r\n\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\][^\r\n]*\r\n")
MARKER_HEAD_TEMPLATE = "\r\n[dddd-dd-dd dd:dd:dd] "
MAX_CARRY = 4096

DETACH_KEY = "\x1d"  # Ctrl+]  — Ctrl+C 는 codex 로 넘겨야 하므로 탈출키를 따로 둔다.

# 구호스트가 프레임을 못 알아먹었을 때만 공백 폴백으로 내려간다.
SPACE_FALLBACK_ERRORS = {"invalid_frame", "unsupported_operation"}

# 스캔코드(getwch 가 \x00/\xe0 뒤에 주는 두 번째 문자) → 호스트 key 이름.
SCAN_KEYS = {
    "H": "Up",
    "P": "Down",
    "K": "Left",
    "M": "Right",
    "G": "Home",
    "O": "End",
    "S": "Delete",
}

# 호스트 encode_key 가 이름으로만 받는 문자들. key 값을 strip() 하므로 공백류는 이름 필수.
NAMED_CHARS = {
    "\r": "Enter",
    "\n": "Enter",
    "\t": "Tab",
    "\x1b": "Escape",
    "\x08": "Backspace",
    "\x7f": "Backspace",
}


def _head_prefix_ok(rest: bytes) -> bool:
    """rest 가 마커 머리("\\r\\n[YYYY-MM-DD HH:MM:SS] ")의 접두사와 모순되지 않는가."""

    for index, byte in enumerate(rest[: len(MARKER_HEAD_TEMPLATE)]):
        expected = MARKER_HEAD_TEMPLATE[index]
        char = chr(byte)
        if expected == "d":
            if not char.isdigit():
                return False
        elif char != expected:
            return False
    return True


def strip_markers(data: bytes, carry: bytes = b"", max_carry: int = MAX_CARRY) -> tuple[bytes, bytes]:
    """주입 마커 줄을 걷어낸 (표시할 바이트, 다음 청크로 넘길 carry) 를 돌려준다.

    마커가 청크 경계에서 잘릴 수 있으므로, 마커가 될 수 있는 꼬리는 carry 로 보류한다.
    보류가 max_carry 를 넘으면 마커가 아니라고 보고 흘려보낸다(화면이 멎지 않게).
    """

    buffer = carry + data
    out = bytearray()
    index = 0
    while True:
        start = buffer.find(b"\r\n[", index)
        if start == -1:
            out += buffer[index:]
            return bytes(out), b""

        match = MARKER_RE.match(buffer, start)
        if match:
            out += buffer[index:start]
            index = match.end()
            continue

        rest = buffer[start:]
        unterminated = b"\r\n" not in rest[2:]
        if unterminated and _head_prefix_ok(rest) and len(rest) < max_carry:
            out += buffer[index:start]
            return bytes(out), bytes(rest)

        # 마커가 아니다 — 여는 "\r\n[" 를 그대로 내보내고 그 뒤에서 다시 찾는다.
        out += buffer[index : start + 3]
        index = start + 3


def map_key(char: str, scan: str = "") -> tuple[str, str]:
    """콘솔에서 읽은 키 → (동작, 호스트 key 이름).

    동작: key(그대로 전송) / space(호스트 세대에 따라 경로가 갈림) / detach(창 닫기) / ignore.
    """

    if scan:
        name = SCAN_KEYS.get(scan, "")
        return ("key", name) if name else ("ignore", "")
    if char == DETACH_KEY:
        return ("detach", "")
    if char == " ":
        # 공백은 이름으로만 보낼 수 있다 — 호스트 encode_key 가 key 값을 strip() 해서
        # " " 는 빈 문자열이 된다. 이름 지원은 0.9.4 부터라 구호스트 폴백이 따로 있다.
        return ("space", "space")
    if char in NAMED_CHARS:
        return ("key", NAMED_CHARS[char])
    if len(char) == 1:
        return ("key", char)
    return ("ignore", "")


def build_request(descriptor: dict, op: str, **fields) -> dict:
    """호스트가 검증하는 형식 그대로의 요청 프레임."""

    request = {
        "schema": SCHEMA,
        "request_id": secrets.token_hex(8),
        "generation": str(descriptor.get("generation", "")),
        "capability": str(descriptor.get("capability", "")),
        "op": op,
    }
    request.update(fields)
    return request


def load_descriptor(path: Path) -> dict:
    descriptor = json.loads(path.read_text(encoding="utf-8"))
    if descriptor.get("schema") != SCHEMA:
        raise SystemExit(f"host descriptor schema mismatch: {descriptor.get('schema')!r}")
    for field in ("pipe_name", "capability", "generation"):
        if not descriptor.get(field):
            raise SystemExit(f"host descriptor is missing {field}")
    return descriptor


def pipe_request(pipe_name: str, request: dict, timeout_ms: int = 1500) -> dict:
    """호스트 named pipe 로 한 프레임 왕복. 브릿지 _pipe_request 와 같은 계약이다.

    호스트는 요청 1개마다 파이프 인스턴스를 새로 만들고 인스턴스는 1개뿐이라,
    연결이 안 잡히는 구간만 재시도한다 — 쓰기 뒤에는 절대 재시도하지 않는다.
    """

    import ctypes

    wait_named_pipe = ctypes.windll.kernel32.WaitNamedPipeW
    wait_named_pipe.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32]
    wait_named_pipe.restype = ctypes.c_int

    encoded = (json.dumps(request, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
    deadline = time.monotonic() + timeout_ms / 1000.0

    handle = None
    while handle is None:
        remaining = int((deadline - time.monotonic()) * 1000)
        if remaining <= 0:
            raise TimeoutError("ConPTY host IPC is unavailable")
        if not wait_named_pipe(pipe_name, remaining):
            time.sleep(0.01)
            continue
        try:
            handle = open(pipe_name, "r+b", buffering=0)
        except OSError:
            time.sleep(0.01)

    with handle:
        handle.write(encoded)
        handle.flush()
        raw = handle.readline()

    response = json.loads(raw.decode("utf-8"))
    if response.get("request_id") != request["request_id"]:
        raise RuntimeError("ConPTY host IPC response id mismatch")
    return response


class ConsoleMode:
    """VT 패스스루 + 원시 입력으로 콘솔을 바꾸고, 나갈 때 원상복구한다."""

    ENABLE_PROCESSED_OUTPUT = 0x0001
    ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
    ENABLE_PROCESSED_INPUT = 0x0001
    ENABLE_LINE_INPUT = 0x0002
    ENABLE_ECHO_INPUT = 0x0004

    def __init__(self) -> None:
        import ctypes

        self.kernel32 = ctypes.windll.kernel32
        self.stdout_handle = self.kernel32.GetStdHandle(-11)
        self.stdin_handle = self.kernel32.GetStdHandle(-10)
        self.saved_out = None
        self.saved_in = None
        self.saved_cp = None

    def _mode(self, handle):
        import ctypes

        mode = ctypes.c_uint32()
        if not self.kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return None
        return mode.value

    def __enter__(self) -> "ConsoleMode":
        self.saved_out = self._mode(self.stdout_handle)
        self.saved_in = self._mode(self.stdin_handle)
        self.saved_cp = self.kernel32.GetConsoleOutputCP()

        if self.saved_out is not None:
            self.kernel32.SetConsoleMode(
                self.stdout_handle,
                self.saved_out | self.ENABLE_PROCESSED_OUTPUT | self.ENABLE_VIRTUAL_TERMINAL_PROCESSING,
            )
        if self.saved_in is not None:
            # Ctrl+C 를 codex 로 넘겨야 하므로 콘솔이 가로채지 않게 한다.
            self.kernel32.SetConsoleMode(
                self.stdin_handle,
                self.saved_in
                & ~(self.ENABLE_PROCESSED_INPUT | self.ENABLE_LINE_INPUT | self.ENABLE_ECHO_INPUT),
            )
        self.kernel32.SetConsoleOutputCP(65001)  # raw 는 UTF-8 이라 한글이 깨지지 않게
        return self

    def __exit__(self, *exc) -> None:
        if self.saved_out is not None:
            self.kernel32.SetConsoleMode(self.stdout_handle, self.saved_out)
        if self.saved_in is not None:
            self.kernel32.SetConsoleMode(self.stdin_handle, self.saved_in)
        if self.saved_cp:
            self.kernel32.SetConsoleOutputCP(self.saved_cp)


def resize_console(columns: int, rows: int) -> str:
    """콘솔을 호스트 ConPTY 크기에 맞춘다. 실패해도 진행하고 사유만 돌려준다."""

    try:
        current = os.get_terminal_size()
    except OSError:
        current = None
    if current and current.columns == columns and current.lines == rows:
        return ""
    os.system(f"mode con: cols={columns} lines={rows}")
    try:
        after = os.get_terminal_size()
    except OSError:
        return ""
    if after.columns != columns or after.lines != rows:
        return f"창 크기 {after.columns}x{after.lines} != 호스트 {columns}x{rows} — 창을 키우거나 글꼴을 줄이면 줄 어긋남이 사라집니다."
    return ""


def default_raw_path() -> Path:
    base = os.environ.get("CODEX_CONPTY_LIVE_RAW")
    if base:
        return Path(base)
    root = os.environ.get("LOCALAPPDATA") or os.environ.get("USERPROFILE") or "."
    return Path(root) / "codex-telegram-bridge" / "conpty-live.raw"


def default_state_path() -> Path:
    base = os.environ.get("CRB_CONPTY_STATE_PATH")
    if base:
        return Path(base)
    root = os.environ.get("LOCALAPPDATA") or os.environ.get("USERPROFILE") or "."
    return Path(root) / "codex-telegram-bridge" / "repl-host.json"


def send_key(descriptor: dict, name: str) -> dict:
    return pipe_request(str(descriptor["pipe_name"]), build_request(descriptor, "key", key=name))


def space_request(descriptor: dict, native: bool) -> dict:
    """공백 1칸 요청 프레임.

    native  = 0.9.4+ 호스트. encode_key 이름 맵에 "space" 가 있어 키 하나로 끝난다.
    폴백    = 0.9.3 이하 호스트. 이름이 없어 key op 로는 공백을 못 보낸다. paste op 는
              submit 프레임을 반드시 덧붙이므로, 터미널이 버리는 NUL 을 submit 으로 써서
              화면에 영향을 남기지 않고 공백만 넣는다. 대신 프레임 간 지연을 문다.
    """

    if native:
        return build_request(descriptor, "key", key="space")
    return build_request(
        descriptor, "paste", text=" ", clear_before=False, submit_key="\x00", enter_count=1
    )


def space_fallback_needed(response: dict) -> bool:
    """구호스트라 native 공백이 거부된 것인가.

    호스트가 살아 있는데 프레임을 못 알아먹은 경우에만 폴백한다. child_not_running 같은
    다른 실패까지 폴백하면 진짜 고장을 공백 우회로 덮어버린다.
    """

    if response.get("ok"):
        return False
    return str(response.get("error", "")) in SPACE_FALLBACK_ERRORS


def send_space(descriptor: dict, native: bool) -> tuple[dict, bool]:
    """(응답, 앞으로도 native 를 쓸지). 한 번 거부되면 그 세션 내내 폴백을 쓴다."""

    pipe_name = str(descriptor["pipe_name"])
    response = pipe_request(pipe_name, space_request(descriptor, native))
    if native and space_fallback_needed(response):
        return pipe_request(pipe_name, space_request(descriptor, False)), False
    return response, native


def run(args: argparse.Namespace) -> int:
    if os.name != "nt":
        print("이 클라이언트는 Windows 에서만 동작합니다 (ConPTY 호스트가 Windows 전용).", file=sys.stderr)
        return 2

    import msvcrt

    state_path = Path(args.state_path) if args.state_path else default_state_path()
    raw_path = Path(args.raw_path) if args.raw_path else default_raw_path()

    if not state_path.is_file():
        print(f"호스트 상태 파일이 없습니다: {state_path}", file=sys.stderr)
        return 3
    descriptor = load_descriptor(state_path)

    if not raw_path.is_file():
        print(f"라이브 raw 파일이 없습니다: {raw_path}", file=sys.stderr)
        print("crb 0.9.3 이상 호스트가 떠 있어야 합니다.", file=sys.stderr)
        return 4

    if not args.read_only:
        probe = pipe_request(str(descriptor["pipe_name"]), build_request(descriptor, "verify"))
        if not probe.get("ok"):
            print(f"호스트 IPC 거부: {probe.get('error')}", file=sys.stderr)
            return 5
        if not probe.get("session_bound"):
            print("세션이 아직 안 붙었습니다 — 봇에 프롬프트를 한 번 보낸 뒤 다시 실행하세요.", file=sys.stderr)
            return 6

    warning = "" if args.no_resize else resize_console(args.columns, args.rows)

    handle = open(raw_path, "rb")
    size = raw_path.stat().st_size
    handle.seek(max(0, size - max(0, args.replay)))

    carry = b""
    errors: list[str] = []
    space_native = True  # 0.9.4+ 가정, 거부되면 그 세션 내내 폴백
    out = sys.stdout.buffer

    with ConsoleMode():
        out.write(b"\x1b[2J\x1b[H")  # 이전 콘솔 내용 지우고 시작
        if warning:
            out.write(("[attach] " + warning + "\r\n").encode("utf-8"))
        out.write(
            f"[attach] {raw_path.name} 구독 · 입력은 호스트로 전달 · 나가기 Ctrl+] \r\n".encode("utf-8")
        )
        out.flush()

        try:
            while True:
                chunk = handle.read(65536)
                if chunk:
                    visible, carry = strip_markers(chunk, carry)
                    if visible:
                        out.write(visible)
                        out.flush()
                else:
                    current = raw_path.stat().st_size
                    if current < handle.tell():  # 호스트가 파일을 회전시켰다
                        handle.seek(0)

                typed = False
                while msvcrt.kbhit():
                    char = msvcrt.getwch()
                    if char in ("\x00", "\xe0"):
                        action, name = map_key("", msvcrt.getwch())
                    else:
                        action, name = map_key(char)

                    if action == "detach":
                        return 0
                    try:
                        if action == "key":
                            response = send_key(descriptor, name)
                        elif action == "space":
                            response, space_native = send_space(descriptor, space_native)
                        else:
                            continue
                        if not response.get("ok"):
                            errors.append(f"{action} {name!r}: {response.get('error')}")
                        typed = True
                    except (OSError, TimeoutError, RuntimeError, ValueError) as error:
                        errors.append(f"{action} {name!r}: {error}")

                # 입력 직후엔 화면 갱신이 곧 오므로 잠깐 촘촘히 돈다.
                time.sleep((args.poll_ms if not typed else 5) / 1000.0)
        except KeyboardInterrupt:
            return 0
        finally:
            handle.close()
            out.write(b"\x1b[?1049l")  # TUI 가 켰을 수 있는 대체 화면에서 빠져나온다
            out.flush()
            if errors:
                print("\n[attach] 전달 실패 " + str(len(errors)) + "건:", file=sys.stderr)
                for line in errors[-10:]:
                    print("  " + line, file=sys.stderr)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="codex ConPTY attach client (표시+입력)")
    parser.add_argument("--state-path", default="")
    parser.add_argument("--raw-path", default="")
    parser.add_argument("--columns", type=int, default=DEFAULT_COLUMNS)
    parser.add_argument("--rows", type=int, default=DEFAULT_ROWS)
    parser.add_argument("--replay", type=int, default=65536, help="붙을 때 되감아 다시 그릴 바이트")
    parser.add_argument("--poll-ms", type=int, default=30)
    parser.add_argument("--no-resize", action="store_true")
    parser.add_argument("--read-only", action="store_true", help="입력 없이 화면만")
    return parser


def main(argv: list[str] | None = None) -> int:
    return run(build_parser().parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
