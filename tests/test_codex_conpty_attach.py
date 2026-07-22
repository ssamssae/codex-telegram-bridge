"""T-260722-076 attach 클라이언트 단위 검증.

Windows 전용 런타임(콘솔 모드·named pipe·msvcrt)은 lazy import 라 이 테스트는
어느 노드에서나 돈다. 검증 대상은 화면을 망가뜨리는 두 축이다:
  1) 주입 마커 제거 — 잘못 걷어내면 TUI 제어열까지 먹어 화면이 깨진다.
  2) 키 매핑 — 호스트 encode_key 계약(공백은 strip 되어 거부)과 어긋나면 입력이 죽는다.
"""

from __future__ import annotations

import importlib.util
import unittest
from datetime import datetime
from pathlib import Path

# 내부 레이아웃(scripts/tests/ 옆의 scripts/windows/)과 공개 패키지 레이아웃(tests/ 옆의
# 저장소 루트) 양쪽에서 같은 파일이 그대로 돌아야 한다 — export 가 이 테스트를 복사해
# 공개 tests/ 로 싣는다(PUBLIC_TESTS.manifest 의 copy: 항목). 정본은 하나다.
_CANDIDATES = (
    Path(__file__).resolve().parents[1] / "windows" / "codex_conpty_attach.py",
    Path(__file__).resolve().parents[1] / "codex_conpty_attach.py",
)
MODULE_PATH = next((path for path in _CANDIDATES if path.is_file()), _CANDIDATES[0])


def load_module():
    spec = importlib.util.spec_from_file_location("codex_conpty_attach", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


attach = load_module()


def host_marker(text: str) -> bytes:
    """LiveRawSink.note() 와 같은 방식으로 마커를 만든다(형식 표류 감지용)."""

    stamp = datetime(2026, 7, 22, 18, 4, 38).strftime("%Y-%m-%d %H:%M:%S")
    return f"\r\n[{stamp}] {text}\r\n".encode("utf-8")


class StripMarkersTest(unittest.TestCase):
    def test_removes_injection_marker(self) -> None:
        data = b"before" + host_marker("주입 <- [200~ㅎㅇ [201~") + b"after"
        visible, carry = attach.strip_markers(data)
        self.assertEqual(visible, b"beforeafter")
        self.assertEqual(carry, b"")

    def test_keeps_tui_escape_sequences(self) -> None:
        data = b"\x1b[2J\x1b[H\x1b[38;5;12m> \xed\x95\x9c\xea\xb8\x80\x1b[0m\r\n"
        visible, carry = attach.strip_markers(data)
        self.assertEqual(visible, data)
        self.assertEqual(carry, b"")

    def test_marker_split_across_chunks(self) -> None:
        marker = host_marker("주입 <- (제어 키 입력)")
        first, second = marker[:12], marker[12:]
        visible_a, carry = attach.strip_markers(b"head" + first)
        self.assertEqual(visible_a, b"head")
        self.assertTrue(carry)
        visible_b, carry = attach.strip_markers(second + b"tail", carry)
        self.assertEqual(visible_b, b"tail")
        self.assertEqual(carry, b"")

    def test_does_not_eat_ordinary_bracket_line(self) -> None:
        data = b"\r\n[not a marker] still text\r\n"
        visible, carry = attach.strip_markers(data)
        self.assertEqual(visible, data)
        self.assertEqual(carry, b"")

    def test_bracketed_paste_marker_survives(self) -> None:
        # TUI 스트림에 실제로 등장하는 bracketed paste 시작열은 마커가 아니다.
        data = b"\r\n\x1b[200~pasted\x1b[201~"
        visible, carry = attach.strip_markers(data)
        self.assertEqual(visible + carry, data)

    def test_carry_overflow_flushes_instead_of_stalling(self) -> None:
        data = b"\r\n[2026-07-22 18:04:38] " + b"x" * 8192
        visible, carry = attach.strip_markers(data, b"", max_carry=64)
        self.assertTrue(visible)
        self.assertEqual(len(visible) + len(carry), len(data))


class MapKeyTest(unittest.TestCase):
    def test_named_control_chars(self) -> None:
        self.assertEqual(attach.map_key("\r"), ("key", "Enter"))
        self.assertEqual(attach.map_key("\t"), ("key", "Tab"))
        self.assertEqual(attach.map_key("\x1b"), ("key", "Escape"))
        self.assertEqual(attach.map_key("\x08"), ("key", "Backspace"))

    def test_arrow_scan_codes(self) -> None:
        self.assertEqual(attach.map_key("", "H"), ("key", "Up"))
        self.assertEqual(attach.map_key("", "P"), ("key", "Down"))
        self.assertEqual(attach.map_key("", "M"), ("key", "Right"))
        self.assertEqual(attach.map_key("", "?"), ("ignore", ""))

    def test_plain_and_unicode_characters_pass_through(self) -> None:
        self.assertEqual(attach.map_key("a"), ("key", "a"))
        self.assertEqual(attach.map_key("가"), ("key", "가"))
        self.assertEqual(attach.map_key("\x03"), ("key", "\x03"))  # Ctrl+C 는 codex 로

    def test_space_goes_through_its_own_path_by_name(self) -> None:
        # 호스트 encode_key 는 key 를 strip() 하므로 " " 를 그대로 보내면 거부된다.
        # 이름("space")으로 보내야 하고, 그 이름 지원은 호스트 세대를 탄다.
        self.assertEqual(attach.map_key(" "), ("space", "space"))

    def test_detach_key(self) -> None:
        self.assertEqual(attach.map_key("\x1d"), ("detach", ""))


class SpaceCompatibilityTest(unittest.TestCase):
    """공백 1칸의 호스트 세대 호환 계약.

    0.9.4+ 호스트는 encode_key 이름 맵에 "space" 가 있어 키 하나로 끝나고,
    0.9.3 이하는 그 이름을 모른다 — 그 경우에만 paste 우회로 내려가야 한다.
    """

    descriptor = {"generation": "gen123", "capability": "cap456", "pipe_name": r"\\.\pipe\x"}

    def test_native_request_is_a_named_key(self) -> None:
        request = attach.space_request(self.descriptor, True)
        self.assertEqual(request["op"], "key")
        self.assertEqual(request["key"], "space")

    def test_fallback_request_pastes_with_discarded_submit(self) -> None:
        request = attach.space_request(self.descriptor, False)
        self.assertEqual(request["op"], "paste")
        self.assertEqual(request["text"], " ")
        # submit 프레임은 뗄 수 없으므로 터미널이 버리는 NUL 을 쓴다 — 화면 무영향.
        self.assertEqual(request["submit_key"], "\x00")
        self.assertIs(request["clear_before"], False)

    def test_falls_back_only_on_frame_rejection(self) -> None:
        self.assertTrue(attach.space_fallback_needed({"ok": False, "error": "invalid_frame"}))
        self.assertTrue(
            attach.space_fallback_needed({"ok": False, "error": "unsupported_operation"})
        )

    def test_does_not_mask_real_failures_as_old_host(self) -> None:
        # 호스트가 죽었거나 인증이 틀린 건 구호스트가 아니다 — 우회로 덮으면 진짜 고장이 숨는다.
        for error in ("child_not_running", "unauthorized", "generation_mismatch"):
            with self.subTest(error=error):
                self.assertFalse(attach.space_fallback_needed({"ok": False, "error": error}))

    def test_success_never_falls_back(self) -> None:
        self.assertFalse(attach.space_fallback_needed({"ok": True, "sequence": 3}))


class RequestTest(unittest.TestCase):
    descriptor = {"generation": "gen123", "capability": "cap456", "pipe_name": r"\\.\pipe\x"}

    def test_request_shape_matches_host_validator(self) -> None:
        request = attach.build_request(self.descriptor, "key", key="Enter")
        self.assertEqual(request["schema"], attach.SCHEMA)
        self.assertEqual(request["generation"], "gen123")
        self.assertEqual(request["capability"], "cap456")
        self.assertEqual(request["op"], "key")
        self.assertEqual(request["key"], "Enter")
        # 호스트: request_id 는 8..128 자여야 무시되지 않는다.
        self.assertTrue(8 <= len(request["request_id"]) <= 128)

    def test_request_ids_are_unique(self) -> None:
        ids = {attach.build_request(self.descriptor, "verify")["request_id"] for _ in range(50)}
        self.assertEqual(len(ids), 50)


if __name__ == "__main__":
    unittest.main()
