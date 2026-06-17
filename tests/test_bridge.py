#!/usr/bin/env python3
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import telegram_agent_bridge as tab


def config(tmpdir, **overrides):
    base = {
        "bot_token": "token",
        "chat_id": "1234",
        "agent": "codex",
        "agent_cmd": ["/tmp/fake-codex"],
        "state_dir": Path(tmpdir),
        "prefix": "BOT",
        "workdir": Path(tmpdir),
        "timeout": 600,
        "telegram_chunk": 12,
        "codex_dangerous_bypass": False,
        "codex_extra_args": [],
    }
    base.update(overrides)
    return tab.Config(**base)


class FakeTelegram:
    def __init__(self):
        self.calls = []

    def call(self, method, **params):
        self.calls.append((method, params))
        return {"ok": True}


class BridgeTests(unittest.TestCase):
    def test_codex_exec_and_resume(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = config(tmpdir)
            backend = tab.CodexBackend(cfg)

            first = backend.build_exec_cmd("hello")
            self.assertEqual(first[0:4], ["/tmp/fake-codex", "exec", "--json", "-o"])
            self.assertEqual(first[5:], ["-C", tmpdir, "hello"])

            resumed = backend.build_resume_cmd("thread-1", "next")
            self.assertEqual(resumed[0:4], ["/tmp/fake-codex", "exec", "--json", "-o"])
            self.assertEqual(resumed[5:], ["-C", tmpdir, "resume", "thread-1", "next"])

    def test_codex_dangerous_flag_is_opt_in(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = config(tmpdir, codex_dangerous_bypass=True)
            cmd = tab.CodexBackend(cfg).build_exec_cmd("hello")
            self.assertIn("--dangerously-bypass-approvals-and-sandbox", cmd)

    def test_codex_parser_prefers_output_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            backend = tab.CodexBackend(config(tmpdir))
            output = backend._new_output_path()
            output.write_text("final answer\n", encoding="utf-8")
            events = [
                {"type": "thread.started", "thread_id": "thread-abc"},
                {"item": {"type": "agent_message", "text": "json answer"}},
            ]
            self.assertEqual(backend.parse_thread_id(events), "thread-abc")
            self.assertEqual(backend.parse_answer(events, "", ""), "final answer")
            backend.cleanup()

    def test_generic_prompt_placeholder(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = config(
                tmpdir,
                agent="generic",
                agent_cmd=["agent", "--message", "{prompt}"],
            )
            self.assertEqual(
                tab.GenericBackend(cfg).build_exec_cmd("hello world"),
                ["agent", "--message", "hello world"],
            )

    def test_bridge_chunks_and_allowlist(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = config(tmpdir)
            bridge = tab.Bridge(cfg, tab.GenericBackend(cfg), FakeTelegram())

            self.assertEqual(
                bridge.telegram_chunks("abcdefghijklmnopqrstuvwxy"),
                ["BOT abcdefgh", "ijklmnopqrst", "uvwxy"],
            )

            bridge.handle_update(
                {"update_id": 1, "message": {"chat": {"id": "9999"}, "text": "/ping"}}
            )
            self.assertEqual(bridge.telegram.calls, [])

    def test_session_retries_stale_thread_once(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = config(tmpdir)
            bridge = tab.Bridge(cfg, tab.CodexBackend(cfg), FakeTelegram())
            bridge.write_thread_id("stale-thread")
            attempts = []

            def fake_run(prompt, thread_id):
                attempts.append((prompt, thread_id))
                if thread_id == "stale-thread":
                    raise tab.AgentExecError("resume failed")
                return "fresh answer", "fresh-thread"

            bridge.run_agent_turn = fake_run
            self.assertEqual(bridge.execute_with_session("question"), "fresh answer")
            self.assertEqual(attempts, [("question", "stale-thread"), ("question", "")])
            self.assertEqual(bridge.read_thread_id(), "fresh-thread")

    def test_run_agent_turn_uses_stdout_json_fallback(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = config(tmpdir)
            bridge = tab.Bridge(cfg, tab.CodexBackend(cfg), FakeTelegram())
            original_run = subprocess.run

            def fake_run(cmd, **kwargs):
                self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
                self.assertEqual(kwargs["timeout"], 600)
                return SimpleNamespace(
                    returncode=0,
                    stdout="\n".join(
                        [
                            json.dumps({"type": "thread.started", "thread_id": "t1"}),
                            json.dumps(
                                {"item": {"type": "agent_message", "text": "hello"}}
                            ),
                        ]
                    ),
                    stderr="",
                )

            try:
                subprocess.run = fake_run
                answer, thread_id = bridge.run_agent_turn("hello")
            finally:
                subprocess.run = original_run

            self.assertEqual(answer, "hello")
            self.assertEqual(thread_id, "t1")

    def test_run_agent_turn_reads_codex_output_file_before_cleanup(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = config(tmpdir)
            bridge = tab.Bridge(cfg, tab.CodexBackend(cfg), FakeTelegram())
            original_run = subprocess.run
            output_paths = []

            def fake_run(cmd, **kwargs):
                output_path = Path(cmd[cmd.index("-o") + 1])
                output_paths.append(output_path)
                output_path.write_text("answer from file", encoding="utf-8")
                return SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps({"type": "thread.started", "thread_id": "t-file"}),
                    stderr="",
                )

            try:
                subprocess.run = fake_run
                answer, thread_id = bridge.run_agent_turn("hello")
            finally:
                subprocess.run = original_run

            self.assertEqual(answer, "answer from file")
            self.assertEqual(thread_id, "t-file")
            self.assertFalse(output_paths[0].exists())

    def test_run_agent_turn_reports_missing_executable(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = config(tmpdir)
            bridge = tab.Bridge(cfg, tab.CodexBackend(cfg), FakeTelegram())
            original_run = subprocess.run

            def fake_run(cmd, **kwargs):
                raise FileNotFoundError(cmd[0])

            try:
                subprocess.run = fake_run
                with self.assertRaisesRegex(tab.AgentExecError, "failed to start agent"):
                    bridge.run_agent_turn("hello")
            finally:
                subprocess.run = original_run


if __name__ == "__main__":
    unittest.main()
