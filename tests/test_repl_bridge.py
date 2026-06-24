#!/usr/bin/env python3
import json
import os
import tempfile
import time
import unittest
from pathlib import Path

import codex_repl_bridge as repl


def config(tmpdir, **overrides):
    base = {
        "node": "testnode",
        "emoji": "BOT",
        "token_file": None,
        "chat_id": "1234",
        "state_dir": Path(tmpdir),
        "tmux_bin": "tmux",
        "tmux_socket": "codex",
        "tmux_session": "codex",
        "submit_key": "Tab",
        "enter_count": 5,
        "codex_bin": "codex",
        "codex_timeout": 30,
        "image_mode": "repl",
        "ffmpeg_bin": "ffmpeg",
        "ffprobe_bin": "ffprobe",
        "audio_transcribe_cmd": None,
        "video_frame_count": 3,
        "typing_max_seconds": 30,
        "typing_liveness_seconds": 10,
        "long_running_progress_seconds": 0,
        "approval_ttl_seconds": 300,
        "workdir": Path(tmpdir),
        "attachment_roots": (Path(tmpdir),),
        "max_attachment_bytes": 50 * 1024 * 1024,
        "telegram_chunk": 4096,
        "poll_timeout": 2,
        "start_at_end": True,
        "state_path": Path(tmpdir) / "bridge.state.json",
        "backfill_enabled": True,
        "backfill_max": 1,
        "backfill_window_sec": 600,
        "tail_scan_bytes": 65536,
        "state_ring_cap": 64,
        "bridge_kill": False,
    }
    base.update(overrides)
    return repl.Config(**base)


class ConfigDefaultsTest(unittest.TestCase):
    def test_default_long_running_progress_interval_is_ten_minutes(self):
        old_progress = os.environ.pop("CRB_LONG_RUNNING_PROGRESS_SECONDS", None)
        old_liveness = os.environ.pop("CRB_TYPING_LIVENESS_SECONDS", None)
        old_chat_id = os.environ.get("TAB_CHAT_ID")
        os.environ["TAB_CHAT_ID"] = "1234"
        try:
            cfg = repl.Config.from_env()
        finally:
            if old_progress is not None:
                os.environ["CRB_LONG_RUNNING_PROGRESS_SECONDS"] = old_progress
            if old_liveness is not None:
                os.environ["CRB_TYPING_LIVENESS_SECONDS"] = old_liveness
            if old_chat_id is None:
                os.environ.pop("TAB_CHAT_ID", None)
            else:
                os.environ["TAB_CHAT_ID"] = old_chat_id

        self.assertEqual(cfg.long_running_progress_seconds, 600)
        self.assertEqual(cfg.typing_liveness_seconds, 10)

    def test_emoji_prefix_strips_inline_node_emoji_from_answer_body(self):
        telegram = repl.TelegramClient("token", "1234", "🏭", 4096)

        self.assertEqual(
            telegram.with_emoji_prefix("🏭 ㅎㅇ 아니키, 대기 중입니다."),
            "🏭\nㅎㅇ 아니키, 대기 중입니다.",
        )

    def test_long_running_progress_message_includes_detail_fields(self):
        message = repl.format_long_running_progress_message(
            "T-260624-11 deploy bridge",
            630,
            task_id="T-260624-11",
            recent_progress="code deployed, checks running",
        )

        self.assertIn("Task: T-260624-11 deploy bridge", message)
        self.assertIn("Task ID: T-260624-11", message)
        self.assertIn("Elapsed: about 10 min", message)
        self.assertIn("Recent: code deployed, checks running", message)
        self.assertIn("Current: waiting for the final answer", message)
        self.assertIn("Next: I will send the final answer", message)
        self.assertIn("Blocked: no blocker reported", message)

    def test_busy_screen_detects_working_footer(self):
        self.assertTrue(
            repl.screen_has_repl_busy_marker(
                "ready\nWorking (5m 05s · esc to interrupt)\n"
            )
        )
        self.assertFalse(repl.screen_has_repl_busy_marker("ready\nNo active task\n"))


class FakeTelegram:
    def __init__(self):
        self.downloads = []
        self.sent = []
        self.calls = []
        self.attachments = []
        self.approval_updates = []
        self.choice_updates = []

    def download_file(
        self,
        file_id,
        output_dir,
        name_hint,
        default_suffix=".bin",
        allowed_extensions=None,
    ):
        output_dir.mkdir(parents=True, exist_ok=True)
        path = output_dir / f"{repl.safe_filename_part(name_hint)}{default_suffix}"
        path.write_bytes(b"fake-media")
        self.downloads.append((file_id, path, allowed_extensions))
        return path

    def send(self, text):
        self.sent.append(text)
        return True

    def send_typing(self):
        self.calls.append(("sendChatAction", {"action": "typing"}))

    def send_approval_prompt(self, prompt):
        self.sent.append(prompt.telegram_text())
        return 42

    def update_approval_prompt(self, message_id, prompt, status_text, selected=None):
        self.approval_updates.append((message_id, prompt, status_text, selected))

    def send_choice_prompt(self, prompt):
        self.sent.append(prompt.telegram_text())
        return 43

    def update_choice_prompt(self, message_id, prompt, status_text, selected=None):
        self.choice_updates.append((message_id, prompt, status_text, selected))

    def call(self, method, **params):
        self.calls.append((method, params))
        return {"ok": True, "result": True}

    def send_local_attachment(self, path, max_bytes):
        self.attachments.append((path, max_bytes))
        return True


class FailingAttachmentTelegram(FakeTelegram):
    def send_local_attachment(self, path, max_bytes):
        self.attachments.append((path, max_bytes))
        return False


class CaptureTelegram(repl.TelegramClient):
    def __init__(self, upload_ok=True):
        super().__init__("token", "1234", "BOT", 4096)
        self.upload_ok = upload_ok
        self.multipart = []

    def call_multipart(self, method, fields, file_field, file_path):
        self.multipart.append((method, file_field, file_path.name))
        return {"ok": self.upload_ok}


class FakeRepl:
    def __init__(self):
        self.approval_keys = []
        self.choice_options = []
        self.prompts = []
        self.screen = ""
        self.cleared = 0

    def send_approval_key(self, key):
        self.approval_keys.append(key)

    def send_choice_option(self, prompt, option):
        self.choice_options.append((prompt, option))

    def paste_prompt(self, prompt):
        self.prompts.append(prompt)

    def capture_pane(self, lines=80):
        return self.screen

    def clear_composer(self):
        self.cleared += 1


class RecordingCodexRepl(repl.CodexRepl):
    def __init__(self, cfg):
        super().__init__(cfg)
        self.calls = []

    def verify(self):
        self.calls.append((("verify",), None))

    def tmux(self, *args, input_text=None):
        self.calls.append((args, input_text))

    def sent_keys(self):
        return [args[-1] for args, _input_text in self.calls if args and args[0] == "send-keys"]


class ReplBridgeTests(unittest.TestCase):
    def test_load_token_prefers_environment_token(self):
        old_env = os.environ.copy()
        try:
            os.environ.clear()
            os.environ["TAB_BOT_TOKEN"] = "token-from-env"
            self.assertEqual(repl.load_token(None), "token-from-env")
        finally:
            os.environ.clear()
            os.environ.update(old_env)

    def test_config_from_env_uses_public_tab_settings(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            old_env = os.environ.copy()
            try:
                os.environ.clear()
                os.environ.update(
                    {
                        "TAB_CHAT_ID": "1234",
                        "TAB_BOT_TOKEN": "token",
                        "TAB_PREFIX": "BOT",
                        "TAB_STATE_DIR": tmpdir,
                        "CRB_NODE": "public-node",
                    }
                )
                cfg = repl.Config.from_env()
            finally:
                os.environ.clear()
                os.environ.update(old_env)

            self.assertEqual(cfg.chat_id, "1234")
            self.assertEqual(cfg.emoji, "BOT")
            self.assertEqual(cfg.state_dir, Path(tmpdir))
            self.assertIsNone(cfg.token_file)
            self.assertEqual(cfg.state_path, Path(tmpdir) / "codex-repl-bridge-public-node.state.json")
            self.assertTrue(cfg.backfill_enabled)
            self.assertEqual(cfg.backfill_max, 1)
            self.assertFalse(cfg.bridge_kill)

    def test_slash_command_uses_enter_submit_key(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            codex = RecordingCodexRepl(config(tmpdir, submit_key="Tab", enter_count=5))

            codex.paste_prompt("/model")

            self.assertEqual(codex.sent_keys(), ["Enter"])

    def test_clear_composer_clears_both_sides_of_cursor(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            codex = RecordingCodexRepl(config(tmpdir))

            codex.clear_composer()

            self.assertEqual(codex.sent_keys(), ["C-e", "C-u", "C-a", "C-k"])

    def test_slash_command_token_ignores_multiline_prompts(self):
        self.assertEqual(repl.slash_command_token("/model"), "/model")
        self.assertEqual(repl.slash_command_token("/model fast"), "/model")
        self.assertEqual(repl.slash_command_token("/model\nexplain"), "")

    def test_goal_slash_command_keeps_typing(self):
        self.assertTrue(repl.slash_command_keeps_typing("/goal"))
        self.assertTrue(repl.slash_command_keeps_typing("/goal run this"))
        self.assertTrue(repl.slash_command_keeps_typing("/goal@codex_bot run this"))
        self.assertFalse(repl.slash_command_keeps_typing("/model"))

    def test_slash_command_typing_stop_policy(self):
        self.assertFalse(repl.should_stop_typing_after_slash_command("/goal", had_error=False))
        self.assertTrue(repl.should_stop_typing_after_slash_command("/goal", had_error=True))
        self.assertTrue(repl.should_stop_typing_after_slash_command("/model", had_error=False))

    def test_status_command_aliases(self):
        self.assertTrue(repl.is_status_command("status"))
        self.assertTrue(repl.is_status_command("/status"))
        self.assertTrue(repl.is_status_command("/status@codex_bot"))
        self.assertFalse(repl.is_status_command("status please"))

    def test_context_command_aliases(self):
        self.assertTrue(repl.is_context_command("context"))
        self.assertTrue(repl.is_context_command("/context"))
        self.assertTrue(repl.is_context_command("/context@codex_bot"))
        self.assertFalse(repl.is_context_command("context please"))

    def test_extract_codex_status_text_from_tui_box(self):
        screen = """
/status

╭─────────────────────────────────────────────────────────────────────────────────────────╮
│  >_ OpenAI Codex (v0.141.0)                                                             │
│                                                                                         │
│ Visit https://chatgpt.com/codex/settings/usage for up-to-date                           │
│ information on rate limits and credits                                                  │
│                                                                                         │
│  Model:                       gpt-5.5 (reasoning xhigh, summaries auto)                 │
│  Directory:                   ~                                                         │
│  Permissions:                 Full Access                                               │
│  Session:                     019ee042-0a6e-76c1-9da8-ce73a0bd670a                      │
│                                                                                         │
│  Context window:              45% left (147K used / 258K)                               │
│  5h limit:                    [████████████████████] 100% left (resets 04:20 on 20 Jun) │
│  Weekly limit:                [████████████████░░░░] 81% left (resets 20:49 on 25 Jun)  │
╰─────────────────────────────────────────────────────────────────────────────────────────╯
"""
        status = repl.extract_codex_status_text(screen)

        self.assertIn("Codex status", status)
        self.assertIn("OpenAI Codex (v0.141.0)", status)
        self.assertIn("Model:  gpt-5.5", status)
        self.assertIn("Context window:  45% left", status)
        self.assertIn("Weekly limit:  [████████████████░░░░] 81% left", status)
        self.assertNotIn("Visit https://chatgpt.com", status)

    def test_extract_unrecognized_slash_error(self):
        screen = """
• Unrecognized command '/bad'. Type "/" for a list of supported commands.
"""
        self.assertEqual(
            repl.extract_unrecognized_slash_error(screen, "/bad"),
            "• Unrecognized command '/bad'. Type \"/\" for a list of supported commands.",
        )

    def test_unrecognized_slash_command_notifies_and_clears_composer(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            fake_repl = FakeRepl()
            fake_repl.screen = "• Unrecognized command '/bad'. Type \"/\" for a list of supported commands."
            telegram = FakeTelegram()
            bridge = repl.Bridge(config(tmpdir), telegram, fake_repl)

            had_error = bridge.handle_slash_command_result("/bad")

            self.assertTrue(had_error)
            self.assertEqual(fake_repl.cleared, 1)
            self.assertFalse(bridge.needs_composer_clear)
            self.assertIn("Codex slash command error", telegram.sent[0])
            self.assertIn("Unrecognized command '/bad'", telegram.sent[0])
            self.assertIn("바로 비웠습니다", telegram.sent[0])

    def test_recognized_slash_command_result_returns_no_error(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            fake_repl = FakeRepl()
            telegram = FakeTelegram()
            bridge = repl.Bridge(config(tmpdir), telegram, fake_repl)

            had_error = bridge.handle_slash_command_result("/goal")

            self.assertFalse(had_error)
            self.assertEqual(fake_repl.cleared, 0)
            self.assertEqual(telegram.sent, [])

    def test_backfill_events_after_latest_user_only(self):
        now = 1_800_000_000.0
        identity = repl.SessionIdentity("/tmp/rollout.jsonl", 1, 2, 100)
        state = repl.bridge_state_default(identity)
        repl.ring_push(state, "duplicate", 64)
        events = [
            repl.JsonlEvent("assistant", "old before user", now - 20, 0, 10, "old"),
            repl.JsonlEvent("user", "latest prompt", now - 15, 10, 20, "user"),
            repl.JsonlEvent("assistant", "stale", now - 700, 20, 30, "stale"),
            repl.JsonlEvent("assistant", "already sent", now - 10, 30, 40, "duplicate"),
            repl.JsonlEvent("assistant", "fresh", now - 5, 40, 50, "fresh"),
        ]

        candidates = repl.eligible_backfill_events(
            events,
            state,
            now_epoch=now,
            window_seconds=600,
            limit=3,
        )

        self.assertEqual([event.text for event in candidates], ["fresh"])

    def test_process_line_persists_assistant_dedup(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = config(tmpdir)
            telegram = FakeTelegram()
            bridge = repl.Bridge(cfg, telegram, FakeRepl())
            identity = repl.SessionIdentity(str(Path(tmpdir) / "rollout.jsonl"), 1, 2, 100)
            bridge.session_identity = identity
            bridge.bridge_state = repl.bridge_state_default(identity)
            record = {
                "timestamp": "2026-06-20T00:00:00Z",
                "type": "event_msg",
                "payload": {
                    "type": "agent_message",
                    "phase": "final_answer",
                    "message": "done",
                },
            }
            line = json.dumps(record) + "\n"

            self.assertTrue(bridge.process_line(line, 0, len(line)))
            self.assertEqual(telegram.sent, ["done"])
            state = repl.read_json(cfg.state_path)
            self.assertIsNotNone(state)
            assert state is not None
            self.assertEqual(state["offset"], len(line))
            self.assertEqual(len(state["coord_ring"]), 1)

            self.assertTrue(bridge.process_line(line, 0, len(line)))
            self.assertEqual(telegram.sent, ["done"])

    def test_slash_command_answer_still_mirrors_without_pending_match(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = config(tmpdir)
            telegram = FakeTelegram()
            bridge = repl.Bridge(cfg, telegram, FakeRepl())
            identity = repl.SessionIdentity(str(Path(tmpdir) / "rollout.jsonl"), 1, 2, 100)
            bridge.session_identity = identity
            bridge.bridge_state = repl.bridge_state_default(identity)
            user = {
                "timestamp": "2026-06-20T00:00:00Z",
                "type": "event_msg",
                "payload": {"type": "user_message", "message": "/goal"},
            }
            assistant = {
                "timestamp": "2026-06-20T00:00:01Z",
                "type": "event_msg",
                "payload": {
                    "type": "agent_message",
                    "phase": "final_answer",
                    "message": "goal updated",
                },
            }
            user_line = json.dumps(user) + "\n"
            assistant_line = json.dumps(assistant) + "\n"

            self.assertTrue(bridge.process_line(user_line, 0, len(user_line)))
            self.assertEqual(bridge.current_origin, "terminal")
            self.assertTrue(
                bridge.process_line(
                    assistant_line,
                    len(user_line),
                    len(user_line) + len(assistant_line),
                )
            )

            self.assertEqual(telegram.sent, ["goal updated"])

    def test_goal_copy_payload_commentary_sends_plain_before_final(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = config(tmpdir)
            telegram = FakeTelegram()
            bridge = repl.Bridge(cfg, telegram, FakeRepl())
            identity = repl.SessionIdentity(str(Path(tmpdir) / "rollout.jsonl"), 1, 2, 100)
            bridge.session_identity = identity
            bridge.bridge_state = repl.bridge_state_default(identity)
            goal = "/goal Codex Telegram bridge와 Claude Telegram bridge의 완성도 격차를 줄인다."
            spec = "상세스펙:\n\n문제:\n- 복붙용 메시지는 일반 메시지로 보내야 한다."
            commentary = {
                "timestamp": "2026-06-20T00:00:01Z",
                "type": "event_msg",
                "payload": {
                    "type": "agent_message",
                    "phase": "commentary",
                    "message": goal,
                },
            }
            assistant = {
                "timestamp": "2026-06-20T00:00:02Z",
                "type": "event_msg",
                "payload": {
                    "type": "agent_message",
                    "phase": "final_answer",
                    "message": spec,
                },
            }
            commentary_line = json.dumps(commentary) + "\n"
            assistant_line = json.dumps(assistant) + "\n"

            self.assertTrue(bridge.process_line(commentary_line, 0, len(commentary_line)))
            self.assertTrue(
                bridge.process_line(
                    assistant_line,
                    len(commentary_line),
                    len(commentary_line) + len(assistant_line),
                )
            )

            self.assertEqual(telegram.sent, [goal, spec])

    def test_telegram_prompt_tracking_sends_periodic_progress_update(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = config(tmpdir, long_running_progress_seconds=1)
            telegram = FakeTelegram()
            bridge = repl.Bridge(cfg, telegram, FakeRepl())

            try:
                bridge.begin_telegram_prompt_tracking("run the longer task")
                deadline = time.monotonic() + 2.5
                while time.monotonic() < deadline and not telegram.sent:
                    time.sleep(0.05)
            finally:
                bridge.stop_long_running_progress()

            self.assertTrue(telegram.sent)
            self.assertIn("Still working", telegram.sent[0])
            self.assertIn("run the longer task", telegram.sent[0])
            self.assertIn("final answer", telegram.sent[0])

    def test_progress_update_includes_latest_public_progress_and_current_task(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = config(tmpdir, long_running_progress_seconds=1)
            (Path(tmpdir) / "current-task").write_text("T-260624-11\n", encoding="utf-8")
            telegram = FakeTelegram()
            bridge = repl.Bridge(cfg, telegram, FakeRepl())

            try:
                bridge.begin_telegram_prompt_tracking("deploy bridge")
                bridge.handle_progress_event("copied to nodes\nchecking services")
                deadline = time.monotonic() + 2.5
                while time.monotonic() < deadline and not telegram.sent:
                    time.sleep(0.05)
            finally:
                bridge.stop_long_running_progress()

            self.assertTrue(telegram.sent)
            self.assertIn("Task ID: T-260624-11", telegram.sent[0])
            self.assertIn("Recent: copied to nodes checking services", telegram.sent[0])
            self.assertIn("Blocked: no blocker reported", telegram.sent[0])

    def test_liveness_recovers_typing_when_repl_is_busy(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            telegram = FakeTelegram()
            fake_repl = FakeRepl()
            fake_repl.screen = "Working (5m 05s · esc to interrupt)\n"
            bridge = repl.Bridge(config(tmpdir), telegram, fake_repl)

            try:
                self.assertTrue(bridge.recover_repl_liveness("test"))
                deadline = time.monotonic() + 1.0
                while time.monotonic() < deadline and not telegram.calls:
                    time.sleep(0.02)
                self.assertIn(("sendChatAction", {"action": "typing"}), telegram.calls)
            finally:
                bridge.stop_repl_typing()

    def test_liveness_recovers_progress_for_active_telegram_prompt(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            fake_repl = FakeRepl()
            fake_repl.screen = "Working (5m 05s · esc to interrupt)\n"
            bridge = repl.Bridge(
                config(tmpdir, long_running_progress_seconds=1),
                FakeTelegram(),
                fake_repl,
            )
            bridge.set_active_telegram_prompt("run the longer task")

            try:
                self.assertTrue(bridge.recover_repl_liveness("test"))
                self.assertTrue(bridge.has_long_running_progress())
            finally:
                bridge.stop_repl_typing()
                bridge.stop_long_running_progress()

    def test_final_answer_clears_active_telegram_prompt(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            bridge = repl.Bridge(config(tmpdir), FakeTelegram(), FakeRepl())

            bridge.begin_telegram_prompt_tracking("run the longer task")
            self.assertEqual(bridge.active_prompt_for_recovery(), "run the longer task")

            self.assertTrue(bridge.handle_assistant_event("done"))
            self.assertEqual(bridge.active_prompt_for_recovery(), "")

    def test_clear_composer_before_telegram_input_always_clears(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            fake_repl = FakeRepl()
            bridge = repl.Bridge(config(tmpdir), FakeTelegram(), fake_repl)
            bridge.needs_composer_clear = True

            bridge.clear_composer_before_telegram_input("telegram prompt")

            self.assertEqual(fake_repl.cleared, 1)
            self.assertFalse(bridge.needs_composer_clear)

    def test_stale_slash_composer_is_cleared_once(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            fake_repl = FakeRepl()
            bridge = repl.Bridge(config(tmpdir), FakeTelegram(), fake_repl)
            bridge.needs_composer_clear = True

            bridge.clear_stale_composer_if_needed()
            bridge.clear_stale_composer_if_needed()

            self.assertEqual(fake_repl.cleared, 1)
            self.assertFalse(bridge.needs_composer_clear)

    def test_extract_codex_context_text_from_tui_box(self):
        screen = """
╭─────────────────────────────────────────────────────────────────────────────────────────╮
│  >_ OpenAI Codex (v0.141.0)                                                             │
│  Model:                       gpt-5.5 (reasoning xhigh, summaries auto)                 │
│  Context window:              45% left (147K used / 258K)                               │
│  Weekly limit:                [████████████████░░░░] 81% left (resets 20:49 on 25 Jun)  │
╰─────────────────────────────────────────────────────────────────────────────────────────╯
"""
        context = repl.extract_codex_context_text(screen)

        self.assertEqual(
            context,
            "Codex context\nContext window:  45% left (147K used / 258K)",
        )

    def test_extract_codex_context_text_reports_missing_context(self):
        screen = """
╭─────────────────────────────╮
│  >_ OpenAI Codex (v0.141.0) │
│  Model: gpt-5.5             │
╰─────────────────────────────╯
"""
        self.assertEqual(repl.extract_codex_context_text(screen), "Codex context not visible yet.")

    def test_regular_prompt_keeps_configured_submit_key(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            codex = RecordingCodexRepl(config(tmpdir, submit_key="Tab", enter_count=5))

            codex.paste_prompt("hello")

            self.assertEqual(codex.sent_keys(), ["Tab"])

    def test_photo_prompt_includes_local_path_and_caption(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            bridge = repl.Bridge(config(tmpdir), FakeTelegram(), None)
            prompt = bridge.prompt_from_telegram_message(
                {
                    "caption": "what is this?",
                    "photo": [
                        {
                            "file_id": "photo-file",
                            "file_unique_id": "unique-photo",
                            "width": 640,
                            "height": 360,
                        }
                    ],
                },
                100,
            )
            self.assertEqual(prompt.kind, "photo")
            self.assertIn("[Telegram image received]", prompt.text)
            self.assertIn("caption: what is this?", prompt.text)
            self.assertIn("local_path:", prompt.text)

    def test_voice_prompt_reports_transcript_unavailable_without_backend(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            bridge = repl.Bridge(config(tmpdir), FakeTelegram(), None)
            prompt = bridge.prompt_from_telegram_message(
                {
                    "voice": {
                        "file_id": "voice-file",
                        "file_unique_id": "unique-voice",
                        "duration": 2,
                        "mime_type": "audio/ogg",
                    }
                },
                101,
            )
            self.assertEqual(prompt.kind, "voice")
            self.assertIn("[Telegram audio received]", prompt.text)
            self.assertIn("transcript_status: not_available", prompt.text)
            self.assertIn("media_kind: voice", prompt.text)

    def test_video_prompt_includes_thumbnail_and_metadata(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            bridge = repl.Bridge(config(tmpdir), FakeTelegram(), None)
            prompt = bridge.prompt_from_telegram_message(
                {
                    "caption": "last object?",
                    "video": {
                        "file_id": "video-file",
                        "file_unique_id": "unique-video",
                        "duration": 5,
                        "mime_type": "video/mp4",
                        "width": 1280,
                        "height": 720,
                        "thumbnail": {
                            "file_id": "thumb-file",
                            "file_unique_id": "unique-thumb",
                        },
                    },
                },
                102,
            )
            self.assertEqual(prompt.kind, "video")
            self.assertIn("[Telegram video received]", prompt.text)
            self.assertIn("thumbnail_path:", prompt.text)
            self.assertIn("metadata: duration=5; mime_type=video/mp4; width=1280; height=720", prompt.text)

    def test_generic_document_prompt_includes_local_path_caption_and_metadata(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            telegram = FakeTelegram()
            bridge = repl.Bridge(config(tmpdir), telegram, None)
            prompt = bridge.prompt_from_telegram_message(
                {
                    "caption": "please inspect",
                    "document": {
                        "file_id": "doc-file",
                        "file_unique_id": "unique-doc",
                        "file_name": "report.pdf",
                        "mime_type": "application/pdf",
                        "file_size": 4321,
                    },
                },
                103,
            )
            self.assertEqual(prompt.kind, "document")
            self.assertIn("[Telegram file received]", prompt.text)
            self.assertIn("local_path:", prompt.text)
            self.assertIn("media_kind: document", prompt.text)
            self.assertIn("caption: please inspect", prompt.text)
            self.assertIn(
                "metadata: mime_type=application/pdf; file_name=report.pdf; file_size=4321",
                prompt.text,
            )
            self.assertTrue(str(telegram.downloads[0][1]).endswith(".pdf"))
            self.assertIsNone(telegram.downloads[0][2])

    def test_parse_codex_approval_prompt_from_tmux_screen(self):
        screen = """
Would you like to run the following command?

Reason: Create an isolated git worktree for this branch.

$ rtk git worktree add .worktrees/raylib-survival-sample -b raylib-survival.sample

› 1. Yes, proceed (y)
  2. Yes, and don't ask again for commands that start with `rtk git worktree` (p)
  3. No, and tell Codex what to do differently (esc)

Press enter to confirm or esc to cancel
"""
        prompt = repl.parse_approval_prompt(screen)
        self.assertIsNotNone(prompt)
        assert prompt is not None
        self.assertIn("rtk git worktree add", prompt.command)
        self.assertEqual([option.number for option in prompt.options], ["1", "2", "3"])
        self.assertEqual([option.key for option in prompt.options], ["y", "p", "esc"])
        self.assertIn("Choose a button", prompt.telegram_text())

    def test_approval_signature_ignores_surrounding_screen_changes(self):
        base = """
Would you like to run the following command?

Reason: test

$ git status

› 1. Yes, proceed (y)
  2. Yes, and don't ask again (p)
  3. No (esc)
"""
        first = repl.parse_approval_prompt(base)
        second = repl.parse_approval_prompt(base + "\nWorking for 2s\n")

        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        assert first is not None and second is not None
        self.assertEqual(first.approval_id, second.approval_id)

    def test_parse_two_option_approval_prompt_with_wrapped_escape_label(self):
        screen = """
Would you like to run the following command?

Reason: Capture the desktop.

$ python3 screenshot.py

› 1. Yes, proceed (y)
  2. No, and tell Codex what to do
differently (esc)

Press enter to confirm or esc to cancel
"""
        prompt = repl.parse_approval_prompt(screen)

        self.assertIsNotNone(prompt)
        assert prompt is not None
        self.assertEqual([option.number for option in prompt.options], ["1", "2"])
        self.assertEqual([option.key for option in prompt.options], ["y", "esc"])
        self.assertIn("No, and tell Codex", prompt.options[1].label)

    def test_parse_general_numbered_choice_prompt(self):
        screen = """
Select model

› 1. GPT-5.5 xhigh fast
  2. GPT-5.5 high
  3. GPT-5 mini

Press enter to confirm or esc to cancel
"""
        prompt = repl.parse_choice_prompt(screen)

        self.assertIsNotNone(prompt)
        assert prompt is not None
        self.assertEqual(prompt.title, "Select model")
        self.assertEqual([option.value for option in prompt.options], ["1", "2", "3"])
        self.assertTrue(prompt.options[0].selected)

    def test_parse_inline_yes_no_choice_prompt(self):
        screen = """
Overwrite existing file? [y/N]
"""
        prompt = repl.parse_choice_prompt(screen)

        self.assertIsNotNone(prompt)
        assert prompt is not None
        self.assertEqual([option.value for option in prompt.options], ["y", "n"])
        self.assertEqual([option.key for option in prompt.options], ["y", "n"])
        self.assertTrue(prompt.options[1].selected)

    def test_approval_text_choice_sends_tmux_key(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            fake_repl = FakeRepl()
            bridge = repl.Bridge(config(tmpdir), FakeTelegram(), fake_repl)
            bridge.pending_approval = repl.ApprovalRequest.create(
                approval_id="abcdef1234567890",
                source_head="codex_repl",
                command="$ git status",
                reason="test",
                options=(
                    repl.ApprovalOption("1", "Yes", "y"),
                    repl.ApprovalOption("2", "Yes always", "p"),
                    repl.ApprovalOption("3", "No", "esc"),
                ),
                ttl_seconds=300,
            )

            self.assertTrue(bridge.handle_approval_text("2"))
            self.assertEqual(fake_repl.approval_keys, ["p"])

    def test_two_option_approval_no_text_maps_to_visible_no_option(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            fake_repl = FakeRepl()
            bridge = repl.Bridge(config(tmpdir), FakeTelegram(), fake_repl)
            bridge.pending_approval = repl.ApprovalRequest.create(
                approval_id="abcdef1234567890",
                source_head="codex_repl",
                command="$ screenshot",
                reason="test",
                options=(
                    repl.ApprovalOption("1", "Yes, proceed", "y"),
                    repl.ApprovalOption("2", "No, and tell Codex what to do differently", "esc"),
                ),
                ttl_seconds=300,
            )

            self.assertTrue(bridge.handle_approval_text("no"))
            self.assertEqual(fake_repl.approval_keys, ["esc"])

    def test_choice_text_sends_matching_option(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            fake_repl = FakeRepl()
            bridge = repl.Bridge(config(tmpdir), FakeTelegram(), fake_repl)
            bridge.pending_choice = repl.ChoicePrompt.create(
                choice_id="abcdef1234567890",
                source_head="codex_repl",
                title="Select model",
                options=(
                    repl.ChoiceOption("1", "GPT-5.5 xhigh fast", index=0, selected=True),
                    repl.ChoiceOption("2", "GPT-5.5 high", index=1),
                    repl.ChoiceOption("3", "GPT-5 mini", index=2),
                ),
                ttl_seconds=300,
            )

            self.assertTrue(bridge.handle_choice_text("2"))
            self.assertEqual(fake_repl.choice_options[0][1].value, "2")

    def test_approval_callback_checks_signature_and_sends_key(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            fake_repl = FakeRepl()
            telegram = FakeTelegram()
            bridge = repl.Bridge(config(tmpdir), telegram, fake_repl)
            bridge.pending_approval = repl.ApprovalRequest.create(
                approval_id="abcdef1234567890fedcba",
                source_head="codex_repl",
                command="$ git status",
                reason="test",
                options=(
                    repl.ApprovalOption("1", "Yes", "y"),
                    repl.ApprovalOption("2", "Yes always", "p"),
                    repl.ApprovalOption("3", "No", "esc"),
                ),
                ttl_seconds=300,
            )
            bridge.pending_approval_message_id = 42
            callback = {
                "id": "cb1",
                "data": "crb_approval:abcdef1234567890:3",
                "message": {"chat": {"id": "1234"}},
            }

            self.assertTrue(bridge.handle_callback_query(callback))
            self.assertEqual(fake_repl.approval_keys, ["esc"])
            self.assertEqual(telegram.approval_updates[0][0], 42)
            self.assertEqual(telegram.approval_updates[0][3].number, "3")
            self.assertIn("Selected 3", telegram.approval_updates[0][2])
            self.assertEqual(telegram.calls[0][0], "answerCallbackQuery")

    def test_choice_callback_checks_signature_and_marks_selection(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            fake_repl = FakeRepl()
            telegram = FakeTelegram()
            bridge = repl.Bridge(config(tmpdir), telegram, fake_repl)
            bridge.pending_choice = repl.ChoicePrompt.create(
                choice_id="abcdef1234567890fedcba",
                source_head="codex_repl",
                title="Select model",
                options=(
                    repl.ChoiceOption("1", "GPT-5.5 xhigh fast", index=0, selected=True),
                    repl.ChoiceOption("2", "GPT-5.5 high", index=1),
                ),
                ttl_seconds=300,
            )
            bridge.pending_choice_message_id = 43
            callback = {
                "id": "cb1",
                "data": "crb_choice:abcdef1234567890:2",
                "message": {"chat": {"id": "1234"}},
            }

            self.assertTrue(bridge.handle_callback_query(callback))
            self.assertEqual(fake_repl.choice_options[0][1].value, "2")
            self.assertEqual(telegram.choice_updates[0][0], 43)
            self.assertEqual(telegram.choice_updates[0][3].value, "2")
            self.assertIn("Selected 2", telegram.choice_updates[0][2])
            self.assertEqual(telegram.calls[0][0], "answerCallbackQuery")

    def test_done_approval_callback_only_acknowledges(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            telegram = FakeTelegram()
            bridge = repl.Bridge(config(tmpdir), telegram, FakeRepl())
            callback = {
                "id": "cb1",
                "data": "crb_approval:done:2",
                "message": {"chat": {"id": "1234"}},
            }

            self.assertTrue(bridge.handle_callback_query(callback))
            self.assertEqual(telegram.calls[0][0], "answerCallbackQuery")
            self.assertIn("already sent", telegram.calls[0][1]["text"])

    def test_done_choice_callback_only_acknowledges(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            telegram = FakeTelegram()
            bridge = repl.Bridge(config(tmpdir), telegram, FakeRepl())
            callback = {
                "id": "cb1",
                "data": "crb_choice:done:2",
                "message": {"chat": {"id": "1234"}},
            }

            self.assertTrue(bridge.handle_callback_query(callback))
            self.assertEqual(telegram.calls[0][0], "answerCallbackQuery")
            self.assertIn("already sent", telegram.calls[0][1]["text"])

    def test_resolved_approval_text_choice_is_not_sent_twice(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            fake_repl = FakeRepl()
            bridge = repl.Bridge(config(tmpdir), FakeTelegram(), fake_repl)
            approval = repl.ApprovalRequest.create(
                approval_id="abcdef1234567890fedcba",
                source_head="codex_repl",
                command="$ git status",
                reason="test",
                options=(
                    repl.ApprovalOption("1", "Yes", "y"),
                    repl.ApprovalOption("2", "Yes always", "p"),
                    repl.ApprovalOption("3", "No", "esc"),
                ),
                ttl_seconds=300,
            )
            bridge.pending_approval = approval
            bridge.resolved_approval_ids.add(approval.approval_id)

            self.assertTrue(bridge.handle_approval_text("2"))
            self.assertEqual(fake_repl.approval_keys, [])

    def test_expired_approval_callback_does_not_send_key(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            fake_repl = FakeRepl()
            telegram = FakeTelegram()
            bridge = repl.Bridge(config(tmpdir), telegram, fake_repl)
            bridge.pending_approval = repl.ApprovalRequest(
                approval_id="abcdef1234567890fedcba",
                source_head="codex_repl",
                command="$ git status",
                reason="test",
                options=(
                    repl.ApprovalOption("1", "Yes", "y"),
                    repl.ApprovalOption("2", "Yes always", "p"),
                    repl.ApprovalOption("3", "No", "esc"),
                ),
                expires_at=0,
            )
            callback = {
                "id": "cb1",
                "data": "crb_approval:abcdef1234567890:1",
                "message": {"chat": {"id": "1234"}},
            }

            self.assertTrue(bridge.handle_callback_query(callback))
            self.assertEqual(fake_repl.approval_keys, [])
            self.assertEqual(telegram.calls[0][1]["text"], "That approval prompt expired.")

    def test_extracts_and_sends_local_markdown_image_path(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            image = base / "screenshot.png"
            image.write_bytes(b"fake-png")
            telegram = FakeTelegram()
            bridge = repl.Bridge(config(tmpdir), telegram, None)

            bridge.send_answer(f"Here: [screenshot.png]({image})")

            self.assertEqual(len(telegram.attachments), 1)
            self.assertEqual(telegram.attachments[0][0], image)
            self.assertEqual(telegram.sent, ["Here:"])
            self.assertNotIn(str(image), telegram.sent[0])

    def test_hides_raw_local_image_path_after_sending_attachment(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            image = base / "screenshot.png"
            image.write_bytes(b"fake-png")
            telegram = FakeTelegram()
            bridge = repl.Bridge(config(tmpdir), telegram, None)

            bridge.send_answer(f"스크린샷입니다: {image}")

            self.assertEqual(len(telegram.attachments), 1)
            self.assertEqual(telegram.attachments[0][0], image)
            self.assertEqual(telegram.sent, ["스크린샷입니다:"])
            self.assertNotIn(str(image), telegram.sent[0])

    def test_attachment_only_answer_uses_human_fallback_text(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            image = base / "screenshot.png"
            image.write_bytes(b"fake-png")
            telegram = FakeTelegram()
            bridge = repl.Bridge(config(tmpdir), telegram, None)

            bridge.send_answer(f"![screenshot]({image})")

            self.assertEqual(len(telegram.attachments), 1)
            self.assertEqual(telegram.sent, ["파일을 첨부했어요."])

    def test_send_answer_returns_false_when_attachment_upload_fails(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            image = base / "screenshot.png"
            image.write_bytes(b"fake-png")
            telegram = FailingAttachmentTelegram()
            bridge = repl.Bridge(config(tmpdir), telegram, None)

            self.assertFalse(bridge.send_answer(f"Here: [screenshot.png]({image})"))

            self.assertEqual(telegram.sent, ["Here:"])
            self.assertEqual(len(telegram.attachments), 1)

    def test_hides_and_sends_local_video_path(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            video = base / "demo.mp4"
            video.write_bytes(b"fake-video")
            telegram = FakeTelegram()
            bridge = repl.Bridge(config(tmpdir), telegram, None)

            bridge.send_answer(f"영상입니다: {video}")

            self.assertEqual(len(telegram.attachments), 1)
            self.assertEqual(telegram.attachments[0][0], video)
            self.assertEqual(telegram.sent, ["영상입니다:"])
            self.assertNotIn(str(video), telegram.sent[0])

    def test_hides_and_sends_local_audio_markdown_path(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            audio = base / "voice.oga"
            audio.write_bytes(b"fake-audio")
            telegram = FakeTelegram()
            bridge = repl.Bridge(config(tmpdir), telegram, None)

            bridge.send_answer(f"음성입니다: [voice.oga]({audio})")

            self.assertEqual(len(telegram.attachments), 1)
            self.assertEqual(telegram.attachments[0][0], audio)
            self.assertEqual(telegram.sent, ["음성입니다:"])
            self.assertNotIn(str(audio), telegram.sent[0])

    def test_send_local_attachment_uses_media_specific_methods(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            video = base / "demo.mp4"
            voice = base / "voice.oga"
            audio = base / "track.mp3"
            image = base / "screenshot.png"
            for path in (video, voice, audio, image):
                path.write_bytes(b"fake-media")
            telegram = CaptureTelegram()

            self.assertTrue(telegram.send_local_attachment(video, 50 * 1024 * 1024))
            self.assertTrue(telegram.send_local_attachment(voice, 50 * 1024 * 1024))
            self.assertTrue(telegram.send_local_attachment(audio, 50 * 1024 * 1024))
            self.assertTrue(telegram.send_local_attachment(image, 50 * 1024 * 1024))

            self.assertEqual(
                telegram.multipart,
                [
                    ("sendVideo", "video", "demo.mp4"),
                    ("sendVoice", "voice", "voice.oga"),
                    ("sendAudio", "audio", "track.mp3"),
                    ("sendPhoto", "photo", "screenshot.png"),
                ],
            )

    def test_video_upload_falls_back_to_document(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            video = Path(tmpdir) / "demo.mov"
            video.write_bytes(b"fake-video")
            telegram = CaptureTelegram(upload_ok=False)

            self.assertFalse(telegram.send_local_attachment(video, 50 * 1024 * 1024))
            self.assertEqual(
                telegram.multipart,
                [
                    ("sendVideo", "video", "demo.mov"),
                    ("sendDocument", "document", "demo.mov"),
                ],
            )

    def test_local_image_attachment_respects_allowed_roots(self):
        with tempfile.TemporaryDirectory() as allowed, tempfile.TemporaryDirectory() as outside:
            outside_image = Path(outside) / "screenshot.png"
            outside_image.write_bytes(b"fake-png")
            found = repl.extract_local_image_paths(
                f"Here: [screenshot.png]({outside_image})",
                (Path(allowed),),
                50 * 1024 * 1024,
            )
            self.assertEqual(found, [])


if __name__ == "__main__":
    unittest.main()
