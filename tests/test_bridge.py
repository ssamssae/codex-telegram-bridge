#!/usr/bin/env python3
import json
import subprocess
import stat
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
        "prefix_line": False,
        "workdir": Path(tmpdir),
        "workdir_lock": True,
        "timeout": 600,
        "telegram_chunk": 12,
        "codex_dangerous_bypass": False,
        "codex_extra_args": [],
        "local_input_path": None,
        "stdin_input": False,
        "typing_interval": 4,
    }
    base.update(overrides)
    return tab.Config(**base)


class FakeTelegram:
    def __init__(self):
        self.calls = []

    def call(self, method, **params):
        self.calls.append((method, params))
        return {"ok": True}


class CapturingBridge(tab.Bridge):
    def __init__(self, cfg, backend, telegram):
        super().__init__(cfg, backend, telegram)
        self.local_output = []

    def print_local(self, text):
        self.local_output.append(text)


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

    def test_bridge_prefix_can_use_own_line(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = config(tmpdir, prefix="BOT", prefix_line=True, telegram_chunk=12)
            bridge = tab.Bridge(cfg, tab.GenericBackend(cfg), FakeTelegram())

            self.assertEqual(bridge.telegram_chunks("answer"), ["BOT\nanswer"])

    def test_telegram_message_enqueues_without_running_agent(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = config(tmpdir)
            bridge = CapturingBridge(cfg, tab.GenericBackend(cfg), FakeTelegram())

            bridge.handle_update(
                {"update_id": 1, "message": {"chat": {"id": "1234"}, "text": "hello"}}
            )

            queued = bridge.jobs.get_nowait()
            self.assertEqual(queued, tab.BridgeJob(source="telegram", text="hello"))
            self.assertEqual(bridge.telegram.calls, [])

    def test_local_job_mirrors_prompt_and_answer_to_both_sides(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = config(tmpdir, telegram_chunk=4096)
            bridge = CapturingBridge(cfg, tab.GenericBackend(cfg), FakeTelegram())
            bridge.execute_with_session = lambda prompt: f"answer to {prompt}"

            bridge.process_job(tab.BridgeJob(source="local", text="hello"))

            sent = [
                params["text"]
                for method, params in bridge.telegram.calls
                if method == "sendMessage"
            ]
            self.assertEqual(sent, ["BOT local input:\nhello", "BOT answer to hello"])
            self.assertIn("agent answer (local):\nanswer to hello", bridge.local_output)

    def test_telegram_job_mirrors_prompt_to_terminal_and_answer_to_telegram(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = config(tmpdir, telegram_chunk=4096)
            bridge = CapturingBridge(cfg, tab.GenericBackend(cfg), FakeTelegram())
            bridge.execute_with_session = lambda prompt: "done"

            bridge.process_job(tab.BridgeJob(source="telegram", text="from phone"))

            sent = [
                params["text"]
                for method, params in bridge.telegram.calls
                if method == "sendMessage"
            ]
            self.assertEqual(sent, ["BOT done"])
            self.assertIn("telegram input:\nfrom phone", bridge.local_output)
            self.assertIn("agent answer (telegram):\ndone", bridge.local_output)

    def test_local_fifo_is_created(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            fifo_path = Path(tmpdir) / "input.fifo"
            cfg = config(tmpdir, local_input_path=fifo_path)
            bridge = CapturingBridge(cfg, tab.GenericBackend(cfg), FakeTelegram())

            bridge.ensure_local_fifo()

            self.assertTrue(stat.S_ISFIFO(fifo_path.stat().st_mode))

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

    def test_run_agent_turn_respects_workdir_lock(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = config(tmpdir, agent="generic", agent_cmd=["echo", "ok"])
            bridge = tab.Bridge(cfg, tab.GenericBackend(cfg), FakeTelegram())
            held = tab.WorkdirLock(cfg.workdir, cfg.state_dir, "other-head")
            held.acquire()
            try:
                with self.assertRaisesRegex(tab.AgentExecError, "workdir already locked"):
                    bridge.run_agent_turn("hello")
            finally:
                held.release()


if __name__ == "__main__":
    unittest.main()
