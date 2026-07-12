from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
BRIDGE_PATH = ROOT / "codex_repl_bridge.py"
HOST_PATH = ROOT / "codex_repl_host_windows.py"


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class TmuxConfig:
    tmux_bin = "tmux"
    tmux_socket = "codex"
    tmux_session = "codex"
    submit_key = "Tab"
    enter_count = 5

    @property
    def session_target(self) -> str:
        return "=codex"

    @property
    def pane_target(self) -> str:
        return "=codex:"


class ReplTransportTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.bridge = load_module("codex_repl_transport_bridge_test", BRIDGE_PATH)
        cls.host = load_module("codex_repl_transport_host_test", HOST_PATH)

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_tmux_transport_preserves_prompt_command_sequence(self) -> None:
        calls: list[tuple[list[str], dict]] = []

        def run(cmd, **kwargs):
            calls.append((list(cmd), kwargs))
            return subprocess.CompletedProcess(cmd, 0, "", "")

        transport = self.bridge.TmuxTransport(TmuxConfig())
        with mock.patch.object(self.bridge, "composer_lock_path", return_value=self.root / "lock"), \
            mock.patch.object(self.bridge.subprocess, "run", side_effect=run), \
            mock.patch.object(self.bridge.time, "sleep"):
            transport.paste_prompt("한글 😀\n둘째 줄")

        self.assertIsInstance(transport, self.bridge.ReplTransport)
        self.assertEqual(calls[0][0], ["tmux", "-L", "codex", "has-session", "-t", "=codex"])
        self.assertEqual(calls[1][0], ["tmux", "-L", "codex", "load-buffer", "-"])
        self.assertEqual(calls[1][1]["input"], "한글 😀\n둘째 줄")
        self.assertEqual(
            calls[2][0],
            ["tmux", "-L", "codex", "paste-buffer", "-p", "-t", "=codex:"],
        )
        self.assertEqual(
            calls[3][0],
            ["tmux", "-L", "codex", "send-keys", "-t", "=codex:", "Tab"],
        )

    def test_tmux_transport_replace_prompt_keeps_single_writer_lock(self) -> None:
        calls: list[list[str]] = []

        def run(cmd, **kwargs):
            calls.append(list(cmd))
            return subprocess.CompletedProcess(cmd, 0, "screen", "")

        transport = self.bridge.TmuxTransport(TmuxConfig())
        with mock.patch.object(self.bridge, "composer_lock_path", return_value=self.root / "lock"), \
            mock.patch.object(self.bridge.subprocess, "run", side_effect=run), \
            mock.patch.object(self.bridge.time, "sleep"):
            transport.replace_prompt("새 프롬프트")

        clear_keys = [call[-1] for call in calls if "send-keys" in call][:4]
        self.assertEqual(clear_keys, ["C-e", "C-u", "C-a", "C-k"])
        self.assertTrue(any("load-buffer" in call for call in calls))
        self.assertTrue(any("paste-buffer" in call for call in calls))

    def descriptor(self) -> Path:
        path = self.root / "host.json"
        path.write_text(
            json.dumps(
                {
                    "schema": 1,
                    "generation": "g" * 32,
                    "capability": "c" * 48,
                    "pipe_name": r"\\.\pipe\codex-repl-host-test-generation",
                }
            ),
            encoding="utf-8",
        )
        return path

    def conpty_config(self, path: Path):
        return SimpleNamespace(
            conpty_state_path=path,
            state_dir=self.root,
            conpty_timeout_ms=1000,
            submit_key="Tab",
            enter_count=5,
        )

    def test_conpty_transport_sends_unicode_multiline_as_one_authenticated_frame(self) -> None:
        requests: list[dict] = []

        def requester(request):
            requests.append(request)
            return {
                "request_id": request["request_id"],
                "generation": request["generation"],
                "ok": True,
            }

        transport = self.bridge.ConPtyTransport(
            self.conpty_config(self.descriptor()), requester=requester
        )
        transport.replace_prompt("첫 줄 한글 😀\n둘째 줄")

        self.assertFalse(transport.supports_pane_features)
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0]["op"], "paste")
        self.assertEqual(requests[0]["text"], "첫 줄 한글 😀\n둘째 줄")
        self.assertTrue(requests[0]["clear_before"])
        self.assertEqual(requests[0]["capability"], "c" * 48)

    def test_conpty_transport_fails_closed_on_generation_change(self) -> None:
        def requester(request):
            return {
                "request_id": request["request_id"],
                "generation": "new-generation-that-must-not-match",
                "ok": True,
            }

        transport = self.bridge.ConPtyTransport(
            self.conpty_config(self.descriptor()), requester=requester
        )
        with self.assertRaisesRegex(RuntimeError, "generation changed"):
            transport.verify()

    def test_conpty_session_function_call_sends_enabled_flow_card(self) -> None:
        session = self.root / "rollout.jsonl"
        session.touch()
        requests: list[dict] = []

        def requester(request):
            requests.append(request)
            response = {
                "request_id": request["request_id"],
                "generation": request["generation"],
                "ok": True,
            }
            if request["op"] == "session":
                response["session_file"] = str(session)
            return response

        with mock.patch.dict(
            os.environ,
            {
                "TAB_CHAT_ID": "1234",
                "TAB_STATE_DIR": str(self.root / "state"),
                "CRB_STATE_PATH": str(self.root / "bridge-state.json"),
                "CRB_WORKDIR": str(self.root),
                "CRB_REPL_TRANSPORT": "conpty",
                "CRB_CONPTY_STATE_PATH": str(self.descriptor()),
                "CRB_FLOW_MIRROR": "1",
            },
            clear=False,
        ):
            config = self.bridge.Config.from_env()

        transport = self.bridge.ConPtyTransport(config, requester=requester)
        self.assertEqual(transport.session_file(), session)
        self.assertTrue(config.flow_mirror)

        class RecordingTelegram:
            def __init__(self):
                self.sent: list[str] = []

            def send_message_id(self, text):
                self.sent.append(text)
                return 77

            def edit(self, _message_id, _text):
                return True

        telegram = RecordingTelegram()
        bridge = self.bridge.Bridge(config, telegram, transport)
        identity = self.bridge.session_identity(session)
        bridge.session_identity = identity
        bridge.bridge_state = self.bridge.bridge_state_default(identity)
        bridge.current_flow_scope = "PowerShell synchronized turn"
        bridge.repl_typing_stop = threading.Event()
        event = self.bridge.extract_event(
            {
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "name": "exec_command",
                    "arguments": '{"cmd":"git status"}',
                },
            }
        )
        self.assertIsNotNone(event)
        kind, text = event
        self.assertEqual(kind, "flow")
        bridge.handle_flow_event(text, "conpty-flow-key")

        self.assertEqual(requests[0]["op"], "session")
        self.assertEqual(
            telegram.sent,
            [f"{self.bridge.FLOW_MIRROR_HEADER}\n• 실행 · git status"],
        )

    def test_host_bracketed_paste_preserves_unicode_and_multiline(self) -> None:
        paste_frame, submit_frame = self.host.encode_paste(
            "한글 😀\r\n둘째 줄\n",
            clear_before=True,
            submit_key="Tab",
        )
        self.assertTrue(
            paste_frame.startswith(self.host.CLEAR_COMPOSER + self.host.BRACKETED_PASTE_START)
        )
        self.assertIn("한글 😀\n둘째 줄".encode("utf-8"), paste_frame)
        self.assertTrue(paste_frame.endswith(self.host.BRACKETED_PASTE_END))
        self.assertEqual(submit_frame, b"\t")

    def test_host_paste_submit_split_into_two_delayed_frames(self) -> None:
        """T-260711-30: a same-frame submit \\r can be swallowed mid-paste."""
        paste_frame, submit_frame = self.host.encode_paste(
            "제출키 분리 검증", clear_before=False, submit_key="Enter"
        )
        self.assertTrue(paste_frame.endswith(self.host.BRACKETED_PASTE_END))
        self.assertNotIn(b"\r", paste_frame)
        self.assertEqual(submit_frame, b"\r")

        inputs = self.host.OrderedInputQueue()
        paste_sequence = inputs.put("ipc", paste_frame)
        submit_sequence = inputs.put(
            "ipc", submit_frame, delay_before_ms=self.host.PASTE_SUBMIT_DELAY_MS
        )
        self.assertLess(paste_sequence, submit_sequence)

        waits: list[float] = []

        class RecordingStop(threading.Event):
            def wait(self, timeout=None):  # noqa: ANN001 - threading.Event signature
                waits.append(timeout)
                return self.is_set()

        stop = RecordingStop()

        class FakePty:
            def __init__(self) -> None:
                self.writes: list[bytes] = []

            def alive(self) -> bool:
                return True

            def write(self, payload: bytes) -> None:
                self.writes.append(payload)
                if len(self.writes) == 2:
                    stop.set()

        pty = FakePty()
        worker = threading.Thread(target=self.host.writer_loop, args=(pty, inputs, stop))
        worker.start()
        worker.join(timeout=5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(pty.writes, [paste_frame, submit_frame])
        self.assertIn(self.host.PASTE_SUBMIT_DELAY_MS / 1000.0, waits)

    def test_host_protocol_paste_enqueues_submit_as_separate_frame(self) -> None:
        inputs = self.host.OrderedInputQueue()
        protocol = self.host.HostProtocol(
            "generation-1234567890",
            "capability-abcdefghijklmnopqrstuvwxyz123456",
            inputs,
            self.host.RawOutputBuffer(),
            lambda: None,
            lambda: True,
        )
        accepted = protocol.handle(
            {
                "schema": 1,
                "request_id": "request-1234",
                "generation": "generation-1234567890",
                "capability": "capability-abcdefghijklmnopqrstuvwxyz123456",
                "op": "paste",
                "text": "아니키 질문",
                "clear_before": False,
                "submit_key": "Enter",
                "enter_count": 1,
            }
        )
        first = inputs.get(timeout=1)
        second = inputs.get(timeout=1)

        self.assertTrue(accepted["ok"])
        self.assertEqual(accepted["sequence"], second.sequence)
        self.assertIn("아니키 질문".encode(), first.payload)
        self.assertTrue(first.payload.endswith(self.host.BRACKETED_PASTE_END))
        self.assertEqual(first.delay_before_ms, 0)
        self.assertEqual(second.payload, b"\r")
        self.assertEqual(second.delay_before_ms, self.host.PASTE_SUBMIT_DELAY_MS)

    def test_host_queue_serializes_five_concurrent_whole_frames(self) -> None:
        inputs = self.host.OrderedInputQueue()
        barrier = threading.Barrier(5)
        bodies = [f"메시지-{index}-😀".encode() for index in range(5)]

        def submit(body: bytes) -> int:
            barrier.wait()
            return inputs.put("test", body)

        with ThreadPoolExecutor(max_workers=5) as pool:
            sequences = list(pool.map(submit, bodies))
        frames = [inputs.get(timeout=1) for _ in range(5)]

        self.assertEqual(sorted(sequences), [1, 2, 3, 4, 5])
        self.assertEqual([frame.sequence for frame in frames], [1, 2, 3, 4, 5])
        self.assertEqual({frame.payload for frame in frames}, set(bodies))

    def test_host_protocol_requires_capability_and_generation(self) -> None:
        session = self.root / "rollout-test.jsonl"
        session.write_text("", encoding="utf-8")
        inputs = self.host.OrderedInputQueue()
        protocol = self.host.HostProtocol(
            "generation-1234567890",
            "capability-abcdefghijklmnopqrstuvwxyz123456",
            inputs,
            self.host.RawOutputBuffer(),
            lambda: session,
            lambda: True,
        )
        request = {
            "schema": 1,
            "request_id": "request-1234",
            "generation": "generation-1234567890",
            "capability": "wrong",
            "op": "paste",
            "text": "secret prompt must not echo",
        }
        denied = protocol.handle(request)
        self.assertEqual(denied["error"], "unauthorized")
        self.assertNotIn("secret", json.dumps(denied))

        request.update(
            capability="capability-abcdefghijklmnopqrstuvwxyz123456",
            clear_before=False,
            submit_key="Tab",
            enter_count=1,
        )
        accepted = protocol.handle(request)
        frame = inputs.get(timeout=1)
        self.assertTrue(accepted["ok"])
        self.assertIn(b"secret prompt must not echo", frame.payload)
        self.assertEqual(frame.source, "ipc")

    def test_host_protocol_bootstraps_owned_child_before_session_bind(self) -> None:
        inputs = self.host.OrderedInputQueue()
        protocol = self.host.HostProtocol(
            "generation-1234567890",
            "capability-abcdefghijklmnopqrstuvwxyz123456",
            inputs,
            self.host.RawOutputBuffer(),
            lambda: None,
            lambda: True,
        )
        base = {
            "schema": 1,
            "request_id": "request-1234",
            "generation": "generation-1234567890",
            "capability": "capability-abcdefghijklmnopqrstuvwxyz123456",
        }
        verified = protocol.handle({**base, "op": "verify"})
        accepted = protocol.handle(
            {
                **base,
                "op": "paste",
                "text": "첫 Telegram 턴",
                "clear_before": False,
                "submit_key": "Enter",
                "enter_count": 1,
            }
        )
        unbound = protocol.handle({**base, "op": "session"})

        self.assertTrue(verified["ok"])
        self.assertFalse(verified["session_bound"])
        self.assertTrue(accepted["ok"])
        self.assertIn("첫 Telegram 턴".encode(), inputs.get(timeout=1).payload)
        self.assertEqual(unbound["error"], "session_unbound")

    def test_session_binder_rejects_ambiguous_new_sessions(self) -> None:
        root = self.root / "sessions"
        root.mkdir()
        old = root / "rollout-old.jsonl"
        old.write_text("", encoding="utf-8")
        binder = self.host.SessionBinder([root], launched_ns=0)
        (root / "rollout-a.jsonl").write_text("", encoding="utf-8")
        (root / "rollout-b.jsonl").write_text("", encoding="utf-8")

        with self.assertRaisesRegex(self.host.AmbiguousSessionError, "multiple_new_sessions"):
            binder.bind_once()

    def test_native_p0_status_is_explicitly_deferred(self) -> None:
        class Telegram:
            def __init__(self):
                self.messages = []

            def send(self, text):
                self.messages.append(text)
                return True

        telegram = Telegram()
        repl = SimpleNamespace(supports_pane_features=False)
        bridge = self.bridge.Bridge(
            SimpleNamespace(typing_max_seconds=30), telegram, repl
        )

        self.assertTrue(bridge.handle_status_command("/status"))
        self.assertIn("ConPTY P0", telegram.messages[0])

    def test_native_bridge_starts_polling_before_first_session_bind(self) -> None:
        class NativeRepl:
            supports_pane_features = False

            def __init__(self):
                self.verified = 0
                self.session_calls = 0

            def verify(self):
                self.verified += 1

            def session_file(self):
                self.session_calls += 1
                raise AssertionError("startup must not require a pre-existing JSONL")

        repl = NativeRepl()
        bridge = self.bridge.Bridge(
            SimpleNamespace(signal_path=None),
            SimpleNamespace(),
            repl,
        )
        bridge.acquire_lock = lambda: None
        bridge.release_lock = lambda: None
        bridge.jsonl_loop = lambda: None
        bridge.telegram_loop = lambda: None

        bridge.run()

        self.assertEqual(repl.verified, 1)
        self.assertEqual(repl.session_calls, 0)


    def test_session_binder_ignores_files_older_than_first_input(self) -> None:
        """T-260711-32: a foreign Codex (e.g. deploy smoke) that turns before our
        lazy child must not be bindable once callers scope by first-input time."""
        root = self.root / "sessions"
        root.mkdir()
        binder = self.host.SessionBinder([root], launched_ns=0)
        foreign = root / "rollout-foreign.jsonl"
        foreign.write_text("{}\n", encoding="utf-8")
        first_input_ns = foreign.stat().st_mtime_ns + 1_000_000_000

        self.assertIsNone(binder.bind_once(min_mtime_ns=first_input_ns))

        child = root / "rollout-child.jsonl"
        child.write_text("{}\n", encoding="utf-8")
        os.utime(child, ns=(first_input_ns + 1, first_input_ns + 1))
        self.assertEqual(binder.bind_once(min_mtime_ns=first_input_ns), child.resolve())

    def test_wait_for_session_defers_binding_until_first_input(self) -> None:
        root = self.root / "sessions"
        root.mkdir()
        binder = self.host.SessionBinder([root], launched_ns=0)
        hijacker = root / "rollout-hijack.jsonl"
        hijacker.write_text("{}\n", encoding="utf-8")

        with self.assertRaises(TimeoutError):
            self.host.wait_for_session(
                binder, 0.3, input_activity={"first_ns": 0}
            )

        first_input_ns = hijacker.stat().st_mtime_ns + 1_000_000_000
        child = root / "rollout-child.jsonl"
        child.write_text("{}\n", encoding="utf-8")
        os.utime(child, ns=(first_input_ns + 1, first_input_ns + 1))
        bound = self.host.wait_for_session(
            binder, 1.0, input_activity={"first_ns": first_input_ns}
        )
        self.assertEqual(bound, child.resolve())

    def test_writer_loop_records_first_input_time(self) -> None:
        inputs = self.host.OrderedInputQueue()
        inputs.put("ipc", b"payload")
        activity: dict[str, int] = {"first_ns": 0}
        stop = threading.Event()

        class FakePty:
            def alive(self) -> bool:
                return True

            def write(self, payload: bytes) -> None:
                stop.set()

        worker = threading.Thread(
            target=self.host.writer_loop, args=(FakePty(), inputs, stop, activity)
        )
        worker.start()
        worker.join(timeout=5)
        self.assertFalse(worker.is_alive())
        self.assertGreater(activity["first_ns"], 0)

    def test_prepare_smoke_codex_home_isolates_sessions_and_copies_login(self) -> None:
        real_home = self.root / "real-codex"
        real_home.mkdir()
        (real_home / "auth.json").write_text("{}", encoding="utf-8")
        (real_home / "config.toml").write_text("model = 'x'", encoding="utf-8")
        smoke_home = self.root / "smoke-home"

        sessions = self.host.prepare_smoke_codex_home(smoke_home, real_home=real_home)

        self.assertEqual(sessions, smoke_home / "sessions")
        self.assertTrue(sessions.is_dir())
        self.assertTrue((smoke_home / "auth.json").is_file())
        self.assertTrue((smoke_home / "config.toml").is_file())

    def _native_bridge_for_loss(self, repl, telegram=None):
        telegram = telegram if telegram is not None else SimpleNamespace(send=lambda text: True)
        return self.bridge.Bridge(
            SimpleNamespace(native_turn_stale_seconds=60, signal_path=None),
            telegram,
            repl,
        )

    def test_native_turn_loss_reason_matrix(self) -> None:
        session = self.root / "rollout-live.jsonl"
        session.write_text("{}\n", encoding="utf-8")

        class HealthyRepl:
            supports_pane_features = False

            def verify(self):
                return None

            def session_file(self):
                return session

        bridge = self._native_bridge_for_loss(HealthyRepl())
        self.assertEqual(bridge.native_turn_loss_reason(10.0), "")
        self.assertEqual(bridge.native_turn_loss_reason(120.0), "")

        os.utime(session, (1, 1))
        self.assertIn("idle", bridge.native_turn_loss_reason(120.0))

        class RestartedRepl(HealthyRepl):
            def verify(self):
                raise RuntimeError("generation changed")

        bridge = self._native_bridge_for_loss(RestartedRepl())
        self.assertIn("generation changed", bridge.native_turn_loss_reason(10.0))

        class UnboundRepl(HealthyRepl):
            def session_file(self):
                raise RuntimeError("ConPTY host session is not bound")

        bridge = self._native_bridge_for_loss(UnboundRepl())
        self.assertIn("unbound", bridge.native_turn_loss_reason(120.0))

        class TmuxRepl:
            supports_pane_features = True

        bridge = self._native_bridge_for_loss(TmuxRepl())
        self.assertEqual(bridge.native_turn_loss_reason(9999.0), "")

    def test_abort_lost_native_turn_sends_one_error_and_releases_wait(self) -> None:
        class Telegram:
            def __init__(self):
                self.messages = []

            def send(self, text):
                self.messages.append(text)
                return True

        class RestartedRepl:
            supports_pane_features = False

            def verify(self):
                raise RuntimeError("generation changed")

        telegram = Telegram()
        bridge = self._native_bridge_for_loss(RestartedRepl(), telegram)
        released = []
        bridge.stop_repl_typing = lambda: released.append("typing")
        bridge.stop_telegram_fallback = lambda: released.append("fallback")
        bridge.resolve_midreport_obligation = lambda status, title: released.append(status)
        bridge.finish_duplicate_final_turn = lambda: released.append("finish")

        self.assertTrue(bridge.abort_lost_native_turn("ㅎㅇㅎㅇ", 120.0))
        self.assertEqual(len(telegram.messages), 1)
        self.assertIn("Turn lost", telegram.messages[0])
        self.assertIn("ㅎㅇㅎㅇ", telegram.messages[0])
        self.assertEqual(released, ["typing", "fallback", "failed", "finish"])

        class HealthyRepl:
            supports_pane_features = False

            def verify(self):
                return None

            def session_file(self):
                raise AssertionError("must not be called under cutoff")

        bridge = self._native_bridge_for_loss(HealthyRepl(), telegram)
        self.assertFalse(bridge.abort_lost_native_turn("q", 1.0))
        self.assertEqual(len(telegram.messages), 1)


if __name__ == "__main__":
    unittest.main()
