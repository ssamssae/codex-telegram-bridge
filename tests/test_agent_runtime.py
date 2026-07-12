#!/usr/bin/env python3
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_runtime import (
    ApprovalOption,
    ApprovalRequest,
    CapabilityRegistry,
    HeadCapabilities,
    WorkdirLock,
    WorkdirLockError,
)
from agent_runtime import locks as lock_module
from agent_runtime import transport as transport_module
from agent_runtime.adapters.codex_repl import CodexReplAdapter
from agent_runtime.transport import QueueTransport
from agent_runtime.types import AgentMessage


class FakeRepl:
    def __init__(self, session_file=None):
        self.verified = False
        self.prompts = []
        self.approvals = []
        self._session_file = session_file

    def verify(self):
        self.verified = True

    def paste_prompt(self, text):
        self.prompts.append(text)

    def send_approval_key(self, key):
        self.approvals.append(key)

    def capture_pane(self, lines=80):
        return f"lines={lines}"

    def session_file(self):
        return self._session_file or Path("/tmp/session.jsonl")


class AgentRuntimeTests(unittest.TestCase):
    def test_approval_request_tracks_ttl_and_cancelled_state(self):
        request = ApprovalRequest.create(
            approval_id="abcdef1234567890",
            source_head="codex_repl",
            command="$ git status",
            reason="test",
            options=(ApprovalOption("1", "Yes", "y"),),
            ttl_seconds=10,
            now=100,
        )

        self.assertTrue(request.is_active(now=109))
        self.assertFalse(request.is_active(now=110))
        self.assertTrue(request.cancel().cancelled)

    def test_capability_registry_requires_known_heads(self):
        registry = CapabilityRegistry()
        registry.register(HeadCapabilities(head="codex_repl", vision=True, repl=True))

        self.assertTrue(registry.supports("codex_repl", "vision"))
        self.assertFalse(registry.supports("codex_repl", "audio"))
        with self.assertRaises(KeyError):
            registry.require("missing")

    def test_queue_transport_hides_local_input_mechanism(self):
        transport = QueueTransport()
        transport.send("hello")

        self.assertEqual(transport.recv(timeout=0.1), "hello")

    @unittest.skipIf(os.name == "nt", "FifoTransport requires POSIX named pipes")
    def test_fifo_transport_timeout_is_enforced(self):
        from agent_runtime.transport import FifoTransport

        with tempfile.TemporaryDirectory() as tmpdir:
            transport = FifoTransport(Path(tmpdir) / "input.fifo")

            with self.assertRaises(TimeoutError):
                transport.recv(timeout=0.01)

    def test_fifo_transport_fails_fast_on_windows(self):
        from agent_runtime.transport import FifoTransport

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "input.fifo"
            with mock.patch.object(transport_module.os, "name", "nt"):
                with self.assertRaisesRegex(NotImplementedError, "POSIX named pipes"):
                    FifoTransport(path)

    def test_workdir_lock_blocks_second_owner(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workdir = Path(tmpdir) / "repo"
            workdir.mkdir()
            state_dir = Path(tmpdir) / "state"
            first = WorkdirLock(workdir, state_dir, "codex")
            second = WorkdirLock(workdir, state_dir, "claude")

            first.acquire()
            try:
                with self.assertRaises(WorkdirLockError):
                    second.acquire()
            finally:
                first.release()

            second.acquire()
            second.release()

    def test_workdir_lock_removes_dead_stale_lock(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workdir = Path(tmpdir) / "repo"
            workdir.mkdir()
            state_dir = Path(tmpdir) / "state"
            first = WorkdirLock(workdir, state_dir, "dead-head", stale_seconds=0)
            first.lock_file.parent.mkdir(parents=True, exist_ok=True)
            first.lock_file.write_text(
                json.dumps(
                    {
                        "owner": "dead-head",
                        "pid": 99999999,
                        "workdir": str(workdir),
                        "created_at": 1,
                    }
                ),
                encoding="utf-8",
            )

            second = WorkdirLock(workdir, state_dir, "codex", stale_seconds=0)
            second.acquire()
            second.release()

    def test_windows_process_check_never_uses_os_kill(self):
        with (
            mock.patch.object(lock_module.os, "name", "nt"),
            mock.patch.object(lock_module, "_windows_process_alive", return_value=False) as check,
            mock.patch.object(lock_module.os, "kill") as kill,
        ):
            self.assertFalse(lock_module._process_alive(99999999))

        check.assert_called_once_with(99999999)
        kill.assert_not_called()

    def test_codex_repl_adapter_wraps_existing_repl(self):
        repl = FakeRepl()
        adapter = CodexReplAdapter(repl, workdir=Path("/tmp/project"))

        adapter.spawn()
        adapter.send(AgentMessage("hello"))
        adapter.inject_approval(ApprovalOption("1", "Yes", "y"))

        self.assertTrue(repl.verified)
        self.assertEqual(repl.prompts, ["hello"])
        self.assertEqual(repl.approvals, ["y"])
        self.assertTrue(adapter.capabilities().approval)

    def test_codex_repl_adapter_reads_jsonl_events(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            session = Path(tmpdir) / "rollout.jsonl"
            session.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "type": "event_msg",
                                "payload": {"type": "user_message", "message": "hello"},
                            }
                        ),
                        json.dumps(
                            {
                                "type": "event_msg",
                                "payload": {
                                    "type": "agent_message",
                                    "phase": "final_answer",
                                    "message": "answer",
                                },
                            }
                        ),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            adapter = CodexReplAdapter(FakeRepl(session))

            events = list(adapter.recv())

            self.assertEqual([event.kind for event in events], ["user", "assistant"])
            self.assertEqual([event.text for event in events], ["hello", "answer"])
            self.assertEqual(list(adapter.recv()), [])


if __name__ == "__main__":
    unittest.main()
