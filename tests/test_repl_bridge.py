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
        "telegram_fallback_seconds": 0,
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
        "flow_mirror": True,
        "reasoning_mirror": True,
        "signal_path": None,
    }
    base.update(overrides)
    return repl.Config(**base)


class ConfigDefaultsTest(unittest.TestCase):
    def test_suggested_reply_confirmation_defaults_on_and_can_disable(self):
        old_env = os.environ.copy()
        try:
            os.environ["TAB_CHAT_ID"] = "1234"
            os.environ.pop("CRB_SUGGESTED_REPLY_EYES", None)
            self.assertTrue(repl.Config.from_env().suggested_reply_confirmation_enabled)
            os.environ["CRB_SUGGESTED_REPLY_EYES"] = "0"
            self.assertFalse(repl.Config.from_env().suggested_reply_confirmation_enabled)
        finally:
            os.environ.clear()
            os.environ.update(old_env)

    def test_default_long_running_progress_interval_is_enabled(self):
        old_progress = os.environ.pop("CRB_LONG_RUNNING_PROGRESS_SECONDS", None)
        old_fallback = os.environ.pop("CRB_TELEGRAM_FALLBACK_SECONDS", None)
        old_liveness = os.environ.pop("CRB_TYPING_LIVENESS_SECONDS", None)
        old_chat_id = os.environ.get("TAB_CHAT_ID")
        os.environ["TAB_CHAT_ID"] = "1234"
        try:
            cfg = repl.Config.from_env()
        finally:
            if old_progress is not None:
                os.environ["CRB_LONG_RUNNING_PROGRESS_SECONDS"] = old_progress
            if old_fallback is not None:
                os.environ["CRB_TELEGRAM_FALLBACK_SECONDS"] = old_fallback
            if old_liveness is not None:
                os.environ["CRB_TYPING_LIVENESS_SECONDS"] = old_liveness
            if old_chat_id is None:
                os.environ.pop("TAB_CHAT_ID", None)
            else:
                os.environ["TAB_CHAT_ID"] = old_chat_id

        self.assertEqual(cfg.long_running_progress_seconds, 600)
        self.assertEqual(cfg.telegram_fallback_seconds, 90)
        self.assertEqual(cfg.typing_liveness_seconds, 10)
        self.assertTrue(cfg.flow_mirror)
        self.assertTrue(cfg.reasoning_mirror)

    def test_emoji_prefix_strips_inline_node_emoji_from_answer_body(self):
        telegram = repl.TelegramClient("token", "1234", "🏭", 4096)

        self.assertEqual(
            telegram.with_emoji_prefix("🏭 ㅎㅇ 잘 지내, 대기 중입니다."),
            "ㅎㅇ 잘 지내, 대기 중입니다.",
        )

    def test_private_chat_removes_leading_decorative_emoji_and_keeps_reply_quote(self):
        telegram = repl.TelegramClient("token", "1234", "BOT", 4096)
        calls = []
        telegram.call = lambda method, **params: calls.append((method, params)) or {"ok": True}

        self.assertEqual(telegram.with_emoji_prefix("🙂😄👋 안녕하세요"), "안녕하세요")
        self.assertEqual(telegram.with_emoji_prefix("🍎"), "🍎")
        self.assertTrue(telegram.send("답변", reply_to_message_id=42))

        self.assertEqual(calls[-1][1]["reply_to_message_id"], 42)

    def test_group_chat_keeps_node_emoji_and_reply_quote(self):
        telegram = repl.TelegramClient("token", "-1234", "BOT", 4096)
        calls = []
        telegram.call = lambda method, **params: calls.append((method, params)) or {"ok": True}

        self.assertEqual(telegram.with_emoji_prefix("답변"), "BOT\n답변")
        self.assertTrue(telegram.send("답변", reply_to_message_id=42))

        self.assertEqual(calls[-1][1]["reply_to_message_id"], 42)

    def test_code_fence_sends_native_pre_entity(self):
        telegram = repl.TelegramClient("token", "1234", "BOT", 4096)
        calls = []
        telegram.call = lambda method, **params: calls.append((method, params)) or {"ok": True}

        self.assertTrue(telegram.send("before\n```text\na😀b\n```\nafter"))

        messages = [
            params
            for method, params in calls
            if method == "sendMessage"
        ]
        self.assertEqual(
            [params["text"] for params in messages],
            ["before", "a😀b", "after"],
        )
        self.assertNotIn("entities", messages[0])
        self.assertEqual(
            json.loads(messages[1]["entities"]),
            [{"type": "pre", "offset": 0, "length": 4, "language": "text"}],
        )
        self.assertNotIn("parse_mode", messages[1])
        self.assertNotIn("entities", messages[2])

    def test_long_running_progress_message_is_prose_card(self):
        message = repl.format_long_running_progress_message(
            "T-260624-11 deploy bridge",
            630,
            task_id="T-260624-11",
            recent_progress="code deployed, checks running",
        )

        # Prose card, not a labelled status table.
        self.assertTrue(message.startswith("Progress update\n\n✓ "))
        self.assertIn("T-260624-11", message)
        self.assertIn("deploy bridge", message)
        self.assertIn("10 min", message)
        self.assertIn("code deployed, checks running", message)
        self.assertNotIn("Task ID:", message)
        self.assertNotIn("Blocked:", message)

    def test_busy_screen_detects_working_footer(self):
        self.assertTrue(
            repl.screen_has_repl_busy_marker(
                "ready\nWorking (5m 05s · esc to interrupt)\n"
            )
        )
        self.assertFalse(repl.screen_has_repl_busy_marker("ready\nNo active task\n"))

    def test_footer_status_parsed_without_touching_composer(self):
        screen = (
            "some output\n"
            "  gpt-5.5 xhigh · ~/project · Context 54% used · 5h 81% left · w 92% left\n"
        )
        footer = repl.parse_codex_footer_status(screen)
        self.assertEqual(footer["context_used"], "54")
        self.assertEqual(footer["five_hour_left"], "81")
        self.assertEqual(footer["weekly_left"], "92")

        status_text = repl.extract_codex_footer_status_text(screen)
        self.assertIn("Context: 54% used", status_text)
        self.assertEqual(
            repl.extract_codex_footer_context_text(screen),
            "Codex context\nContext: 54% used",
        )
        # No footer -> empty so callers fall back to the /status injection path.
        self.assertEqual(repl.parse_codex_footer_status("no footer here\n"), {})
        self.assertEqual(repl.extract_codex_footer_context_text("no footer\n"), "")


class FakeTelegram:
    def __init__(self):
        self.downloads = []
        self.sent = []
        self.calls = []
        self.attachments = []
        self.approval_updates = []
        self.choice_updates = []
        self.message_ids = []
        self.edits = []
        self.reactions = []
        self.fail_reactions = False
        self.next_copy_message_id = 1001

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

    def send(self, text, reply_to_message_id=None):
        self.sent.append(text)
        return True

    def send_copy_content(self, text):
        self.sent.append(text)
        message_id = self.next_copy_message_id
        self.next_copy_message_id += 1
        return [message_id]

    def set_message_reaction(self, message_id, emoji):
        self.reactions.append((message_id, emoji))
        return not self.fail_reactions

    def send_message_id(self, text):
        self.sent.append(text)
        message_id = 1000 + len(self.message_ids)
        self.message_ids.append(message_id)
        return message_id

    def edit(self, message_id, text):
        self.edits.append((message_id, text))
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
    supports_pane_features = True

    def __init__(self):
        self.approval_keys = []
        self.choice_options = []
        self.prompts = []
        self.screen = ""
        self.cleared = 0

    def send_approval_key(self, key):
        self.approval_keys.append(key)

    def send_key(self, key):
        self.approval_keys.append(key)

    def send_choice_option(self, prompt, option):
        self.choice_options.append((prompt, option))

    def send_choice(self, option):
        self.choice_options.append((None, option))

    def paste_prompt(self, prompt):
        self.prompts.append(prompt)

    def capture_pane(self, lines=80):
        return self.screen

    capture_screen = capture_pane

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


def without_sent_directive_cards(messages):
    # Terminal-origin prompts mirror one echo card (⌨️ for self-typed input,
    # 📤 for cross-node directives); filter it out when a test only cares
    # about the messages that follow.
    return [
        message
        for message in messages
        if not message.startswith(("📤 보낸 지시\n", "⌨️ 터미널 입력\n"))
    ]


class ReplBridgeTests(unittest.TestCase):
    def test_suggested_reply_classes_match_claude_rendering(self):
        for declared_class in ("auto-ok", "hold"):
            with self.subTest(declared_class=declared_class):
                answer = (
                    "본문 답변\n"
                    f'<추천답변 class="{declared_class}">바로 이어서 할게</추천답변>'
                )
                self.assertEqual(
                    repl.parse_suggested_reply(answer).declared_class,
                    declared_class,
                )
                self.assertEqual(
                    repl.suggested_reply_messages(answer, True, "aniki_dm"),
                    ["본문 답변", "바로 이어서 할게"],
                )
                self.assertEqual(
                    repl.suggested_reply_messages(answer, False, "aniki_dm"),
                    [answer],
                )

    def test_suggested_reply_confirmation_gate_matches_claude_reference(self):
        self.assertEqual(
            repl.suggested_reply_confirmation([701, 702], "aniki_dm", True, "telegram"),
            {"message_id": 701, "emoji": "👀"},
        )
        self.assertIsNone(
            repl.suggested_reply_confirmation([703], "aniki_dm", False, "telegram")
        )
        self.assertIsNone(
            repl.suggested_reply_confirmation([704], "mesh_group", True, "telegram")
        )
        self.assertIsNone(
            repl.suggested_reply_confirmation([705], "aniki_dm", True, "node")
        )
        self.assertIsNone(
            repl.suggested_reply_confirmation([], "aniki_dm", True, "telegram")
        )

    def test_suggested_reply_bubble_gets_private_eyes_confirmation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            telegram = FakeTelegram()
            bridge = repl.Bridge(
                config(
                    tmpdir,
                    suggested_reply_bubble=True,
                    chat_id="1234",
                ),
                telegram,
                None,
            )
            bridge.current_origin = "telegram"
            bridge.active_telegram_message_id = 42

            self.assertTrue(
                bridge.send_answer(
                    '본문 답변\n<추천답변 class="auto-ok">바로 이어서 할게</추천답변>'
                )
            )

            self.assertEqual(telegram.sent, ["본문 답변", "바로 이어서 할게"])
            self.assertEqual(telegram.reactions, [(1001, "👀")])

    def test_private_node_origin_uses_output_chat_for_eyes_gate(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            telegram = FakeTelegram()
            bridge = repl.Bridge(
                config(
                    tmpdir,
                    suggested_reply_bubble=True,
                    chat_id="1234",
                ),
                telegram,
                None,
            )
            bridge.current_origin = "terminal"

            self.assertTrue(
                bridge.send_answer(
                    '노드 답변\n<추천답변 class="hold">확인하고 이어서 할게</추천답변>'
                )
            )

            self.assertEqual(telegram.sent, ["노드 답변", "확인하고 이어서 할게"])
            self.assertEqual(telegram.reactions, [(1001, "👀")])

    def test_suggested_reply_confirmation_is_gated_off_for_group(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            telegram = FakeTelegram()
            bridge = repl.Bridge(
                config(
                    tmpdir,
                    suggested_reply_bubble=True,
                    chat_id="-1001234567890",
                ),
                telegram,
                None,
            )
            bridge.current_origin = "terminal"

            self.assertTrue(
                bridge.send_answer("그룹 답변\n<추천답변>그룹 후속</추천답변>")
            )

            self.assertEqual(telegram.sent, ["그룹 답변"])
            self.assertEqual(telegram.reactions, [])

    def test_suggested_reply_reaction_failure_is_non_fatal(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            telegram = FakeTelegram()
            telegram.fail_reactions = True
            bridge = repl.Bridge(
                config(
                    tmpdir,
                    suggested_reply_bubble=True,
                    chat_id="1234",
                ),
                telegram,
                None,
            )
            bridge.current_origin = "telegram"

            self.assertTrue(
                bridge.send_answer("본문 답변\n<추천답변>계속 진행해</추천답변>")
            )
            self.assertEqual(telegram.sent, ["본문 답변", "계속 진행해"])
            self.assertEqual(telegram.reactions, [(1001, "👀")])

    def test_suggested_reply_transport_returns_ids_and_sets_standard_eyes(self):
        captured = []
        client = repl.TelegramClient("token", "1234", "BOT", 4096)
        client.call = lambda method, **params: (
            captured.append((method, params))
            or {"ok": True, "result": {"message_id": 702}}
        )

        self.assertEqual(client.send_copy_content("이대로 보내줘"), [702])
        client.call = lambda method, **params: (
            captured.append((method, params)) or {"ok": True, "result": True}
        )
        self.assertTrue(client.set_message_reaction(702, "👀"))
        self.assertEqual(captured[-1][0], "setMessageReaction")
        self.assertEqual(captured[-1][1]["message_id"], 702)
        self.assertEqual(
            json.loads(captured[-1][1]["reaction"]),
            [{"type": "emoji", "emoji": "👀"}],
        )

    def test_flow_live_card_matches_claude_reference(self):
        payloads = [
            {"name": "mcp__plugin_playwright_playwright__browser_snapshot", "arguments": "{}"},
            {
                "name": "mcp__plugin_playwright_playwright__browser_navigate",
                "arguments": json.dumps({"url": "https://substack.com/publish/post"}),
            },
            {
                "name": "mcp__plugin_playwright_playwright__browser_click",
                "arguments": json.dumps({"element": "발행"}, ensure_ascii=False),
            },
            {
                "name": "mcp__plugin_playwright_playwright__browser_click",
                "arguments": json.dumps({"element": "확인"}, ensure_ascii=False),
            },
            {"name": "read_file", "arguments": json.dumps({"path": "/repo/newsletter/issue-66.md"})},
            {"name": "exec_command", "arguments": json.dumps({"cmd": "python publish.py"})},
        ]
        steps = "\n".join(repl.function_call_flow_summary(payload) for payload in payloads)
        card = repl.format_flow_mirror(
            steps,
            node="macmini",
            emoji="🏭",
            context="뉴스레터 발행해줘",
            now=repl.datetime(2026, 7, 12, 22, 1, tzinfo=repl.KST),
        )

        self.assertEqual(
            card,
            "🏭 macmini · 뉴스레터 발행 · 22:01\n\n"
            "🌐 브라우저\n"
            "🔗 이동 · substack.com\n"
            "🖱 클릭 ×2 · 확인\n"
            "📄 읽기 · issue-66.md\n"
            "▶ 실행 · python publish.py\n\n"
            "→ 진행중 · 현재: ▶ 실행",
        )
        self.assertEqual(
            repl.function_call_flow_summary(
                {
                    "name": "mcp__vendor__mystery_probe",
                    "arguments": json.dumps({"query": "안전"}, ensure_ascii=False),
                }
            ),
            "🔧 mystery_probe · 안전",
        )

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
            self.assertIsNone(cfg.signal_path)

    def test_config_from_env_accepts_signal_path(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            old_env = os.environ.copy()
            try:
                os.environ.clear()
                os.environ.update(
                    {
                        "TAB_CHAT_ID": "1234",
                        "TAB_BOT_TOKEN": "token",
                        "TAB_STATE_DIR": tmpdir,
                        "CRB_SIGNAL_PATH": str(Path(tmpdir) / "signals.fifo"),
                    }
                )
                cfg = repl.Config.from_env()
            finally:
                os.environ.clear()
                os.environ.update(old_env)

            self.assertEqual(cfg.signal_path, Path(tmpdir) / "signals.fifo")

    def test_config_from_env_uses_tab_local_input_as_signal_fallback(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            old_env = os.environ.copy()
            try:
                os.environ.clear()
                os.environ.update(
                    {
                        "TAB_CHAT_ID": "1234",
                        "TAB_BOT_TOKEN": "token",
                        "TAB_STATE_DIR": tmpdir,
                        "TAB_LOCAL_INPUT": str(Path(tmpdir) / "input.fifo"),
                    }
                )
                cfg = repl.Config.from_env()
            finally:
                os.environ.clear()
                os.environ.update(old_env)

            self.assertEqual(cfg.signal_path, Path(tmpdir) / "input.fifo")

    def test_config_from_env_signal_off_disables_tab_local_input_fallback(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            old_env = os.environ.copy()
            try:
                os.environ.clear()
                os.environ.update(
                    {
                        "TAB_CHAT_ID": "1234",
                        "TAB_BOT_TOKEN": "token",
                        "TAB_STATE_DIR": tmpdir,
                        "TAB_LOCAL_INPUT": str(Path(tmpdir) / "input.fifo"),
                        "CRB_SIGNAL_PATH": "off",
                    }
                )
                cfg = repl.Config.from_env()
            finally:
                os.environ.clear()
                os.environ.update(old_env)

            self.assertIsNone(cfg.signal_path)

    def test_parse_signal_prompt_accepts_plain_text_and_json(self):
        self.assertEqual(repl.parse_signal_prompt("ship this\n"), "ship this")
        self.assertEqual(repl.parse_signal_prompt('{"prompt":"run audit"}'), "run audit")
        self.assertEqual(repl.parse_signal_prompt('{"text":"run tests"}'), "run tests")
        self.assertEqual(repl.parse_signal_prompt('{"message":"summarize"}'), "summarize")
        self.assertEqual(repl.parse_signal_prompt('{"other":"ignored"}'), "")

    def test_signal_prompt_injects_through_repl_path(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            fake_repl = FakeRepl()
            bridge = repl.Bridge(
                config(
                    tmpdir,
                    signal_path=Path(tmpdir) / "signal.fifo",
                    telegram_fallback_seconds=0,
                    long_running_progress_seconds=0,
                ),
                FakeTelegram(),
                fake_repl,
            )

            self.assertTrue(bridge.process_signal_text('{"prompt":"T-260630-99 do work"}\n'))
            bridge.stop_repl_typing()
            bridge.stop_event.set()

            self.assertEqual(fake_repl.prompts, ["T-260630-99 do work"])
            self.assertEqual(fake_repl.cleared, 1)
            self.assertEqual(bridge.current_origin, "telegram")
            self.assertEqual(bridge.active_prompt_for_recovery(), "T-260630-99 do work")

    @unittest.skipUnless(hasattr(os, "mkfifo"), "requires POSIX FIFO")
    def test_signal_fifo_is_created_private(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "signal.fifo"
            bridge = repl.Bridge(config(tmpdir, signal_path=path), FakeTelegram(), FakeRepl())

            bridge.ensure_signal_fifo()

            self.assertTrue(path.is_fifo())

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

            self.assertEqual(without_sent_directive_cards(telegram.sent), ["goal updated"])

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

    def test_title_content_copy_payload_sends_plain_two_messages(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = config(tmpdir)
            telegram = FakeTelegram()
            bridge = repl.Bridge(cfg, telegram, FakeRepl())
            identity = repl.SessionIdentity(str(Path(tmpdir) / "rollout.jsonl"), 1, 2, 100)
            bridge.session_identity = identity
            bridge.bridge_state = repl.bridge_state_default(identity)
            prompt = "제목 내용 따로보내줘 두번"
            title = "제목: 클로드를 텔레그램에 연결했다 | 폰에서 Claude Code 자동화 실행하기"
            content = (
                "내용: Claude Telegram Bridge로 Claude Code 세션을 텔레그램에서 직접 호출하고, "
                "폰에서 작업 지시와 응답 확인까지 할 수 있게 만든 구조입니다."
            )
            records = [
                {
                    "timestamp": "2026-06-20T00:00:00Z",
                    "type": "event_msg",
                    "payload": {"type": "user_message", "message": prompt},
                },
                {
                    "timestamp": "2026-06-20T00:00:01Z",
                    "type": "event_msg",
                    "payload": {"type": "agent_message", "phase": "commentary", "message": title},
                },
                {
                    "timestamp": "2026-06-20T00:00:02Z",
                    "type": "event_msg",
                    "payload": {"type": "agent_message", "phase": "final_answer", "message": content},
                },
            ]
            bridge.begin_telegram_prompt_tracking(prompt)
            cursor = 0
            for record in records:
                line = json.dumps(record) + "\n"
                self.assertTrue(bridge.process_line(line, cursor, cursor + len(line)))
                cursor += len(line)

            self.assertEqual(telegram.sent, [title, content])
            state = json.loads(Path(cfg.state_path).read_text(encoding="utf-8"))
            self.assertNotIn("copy_payload_pair_contract", state)

    def test_duplicate_goal_copy_payload_commentary_dedups_by_body(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = config(tmpdir)
            telegram = FakeTelegram()
            bridge = repl.Bridge(cfg, telegram, FakeRepl())
            identity = repl.SessionIdentity(str(Path(tmpdir) / "rollout.jsonl"), 1, 2, 100)
            bridge.session_identity = identity
            bridge.bridge_state = repl.bridge_state_default(identity)
            goal = "/goal Codex Telegram bridge와 Claude Telegram bridge의 완성도 격차를 줄인다."
            first = {
                "timestamp": "2026-06-20T00:00:01Z",
                "type": "event_msg",
                "payload": {
                    "type": "agent_message",
                    "phase": "commentary",
                    "message": goal,
                },
            }
            second = {
                "timestamp": "2026-06-20T00:00:02Z",
                "type": "event_msg",
                "payload": {
                    "type": "agent_message",
                    "phase": "commentary",
                    "message": goal,
                },
            }
            first_line = json.dumps(first) + "\n"
            second_line = json.dumps(second) + "\n"

            self.assertTrue(bridge.process_line(first_line, 0, len(first_line)))
            self.assertTrue(
                bridge.process_line(
                    second_line,
                    len(first_line),
                    len(first_line) + len(second_line),
                )
            )

            self.assertEqual(telegram.sent, [goal])

    def test_duplicate_goal_copy_payload_final_answer_is_skipped(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = config(tmpdir)
            telegram = FakeTelegram()
            bridge = repl.Bridge(cfg, telegram, FakeRepl())
            identity = repl.SessionIdentity(str(Path(tmpdir) / "rollout.jsonl"), 1, 2, 100)
            bridge.session_identity = identity
            bridge.bridge_state = repl.bridge_state_default(identity)
            goal = "/goal Codex Telegram bridge와 Claude Telegram bridge의 완성도 격차를 줄인다."
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
                    "message": goal,
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

            self.assertEqual(telegram.sent, [goal])

    def test_same_copy_payload_body_can_be_resent_for_new_telegram_prompt(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = config(tmpdir)
            telegram = FakeTelegram()
            bridge = repl.Bridge(cfg, telegram, FakeRepl())
            identity = repl.SessionIdentity(str(Path(tmpdir) / "rollout.jsonl"), 1, 2, 100)
            bridge.session_identity = identity
            bridge.bridge_state = repl.bridge_state_default(identity)
            goal = "/goal T-260624-19 Claude 텔레그램 브릿지 route-health 재발방지를 마무리한다."
            first = {
                "timestamp": "2026-06-20T00:00:01Z",
                "type": "event_msg",
                "payload": {"type": "agent_message", "phase": "final_answer", "message": goal},
            }
            second = {
                "timestamp": "2026-06-20T00:00:02Z",
                "type": "event_msg",
                "payload": {"type": "agent_message", "phase": "final_answer", "message": goal},
            }

            bridge.begin_telegram_prompt_tracking("골 명령어 + 상세설명 나눠서 2개로 보내줘")
            first_line = json.dumps(first) + "\n"
            self.assertTrue(bridge.process_line(first_line, 0, len(first_line)))

            bridge.begin_telegram_prompt_tracking("왜 안와? 골 명령어 다시 보내")
            second_line = json.dumps(second) + "\n"
            self.assertTrue(bridge.process_line(second_line, len(first_line), len(first_line) + len(second_line)))

            self.assertEqual(telegram.sent, [goal, goal])

    def test_copy_payload_pair_contract_allows_goal_and_spec_two_messages(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = config(tmpdir)
            telegram = FakeTelegram()
            bridge = repl.Bridge(cfg, telegram, FakeRepl())
            identity = repl.SessionIdentity(str(Path(tmpdir) / "rollout.jsonl"), 1, 2, 100)
            bridge.session_identity = identity
            bridge.bridge_state = repl.bridge_state_default(identity)
            prompt = "골 명령어랑 상세스펙 두 통 따로 보내줘"
            goal = "/goal Codex Telegram bridge recurrence guard를 구현한다."
            spec = "상세스펙:\n\n- /goal 한 통과 상세스펙 한 통을 각각 보낸다."
            user = {
                "timestamp": "2026-06-20T00:00:00Z",
                "type": "event_msg",
                "payload": {"type": "user_message", "message": prompt},
            }
            commentary = {
                "timestamp": "2026-06-20T00:00:01Z",
                "type": "event_msg",
                "payload": {"type": "agent_message", "phase": "commentary", "message": goal},
            }
            assistant = {
                "timestamp": "2026-06-20T00:00:02Z",
                "type": "event_msg",
                "payload": {"type": "agent_message", "phase": "final_answer", "message": spec},
            }
            records = [user, commentary, assistant]
            bridge.begin_telegram_prompt_tracking(prompt)
            cursor = 0
            for record in records:
                line = json.dumps(record) + "\n"
                self.assertTrue(bridge.process_line(line, cursor, cursor + len(line)))
                cursor += len(line)

            self.assertEqual(telegram.sent, [goal, spec])
            state = json.loads(Path(cfg.state_path).read_text(encoding="utf-8"))
            self.assertNotIn("copy_payload_pair_contract", state)
            self.assertNotIn("last_copy_payload_pair_missing", state)

    def test_copy_payload_pair_contract_warns_when_spec_is_missing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = config(tmpdir)
            telegram = FakeTelegram()
            fake_repl = FakeRepl()
            bridge = repl.Bridge(cfg, telegram, fake_repl)
            identity = repl.SessionIdentity(str(Path(tmpdir) / "rollout.jsonl"), 1, 2, 100)
            bridge.session_identity = identity
            bridge.bridge_state = repl.bridge_state_default(identity)
            prompt = "그거 골 명령어 상세스펙 2개로 보내줘"
            goal = "/goal Codex Telegram bridge recurrence guard를 구현한다."
            user = {
                "timestamp": "2026-06-20T00:00:00Z",
                "type": "event_msg",
                "payload": {"type": "user_message", "message": prompt},
            }
            assistant = {
                "timestamp": "2026-06-20T00:00:01Z",
                "type": "event_msg",
                "payload": {"type": "agent_message", "phase": "final_answer", "message": goal},
            }
            bridge.begin_telegram_prompt_tracking(prompt)
            user_line = json.dumps(user) + "\n"
            assistant_line = json.dumps(assistant) + "\n"

            self.assertTrue(bridge.process_line(user_line, 0, len(user_line)))
            self.assertTrue(
                bridge.process_line(
                    assistant_line,
                    len(user_line),
                    len(user_line) + len(assistant_line),
                )
            )

            self.assertEqual(telegram.sent[0], goal)
            self.assertEqual(len(telegram.sent), 1)
            self.assertEqual(len(fake_repl.prompts), 1)
            self.assertIn("상세스펙", fake_repl.prompts[0])
            self.assertIn("누락", fake_repl.prompts[0])
            state = json.loads(Path(cfg.state_path).read_text(encoding="utf-8"))
            self.assertEqual(state["last_copy_payload_pair_missing"]["missing"], ["spec"])
            self.assertTrue(state["last_copy_payload_pair_missing"]["auto_repair_requested"])

    def test_copy_payload_pair_contract_accepts_detail_description_synonym(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = config(tmpdir)
            telegram = FakeTelegram()
            fake_repl = FakeRepl()
            bridge = repl.Bridge(cfg, telegram, fake_repl)
            identity = repl.SessionIdentity(str(Path(tmpdir) / "rollout.jsonl"), 1, 2, 100)
            bridge.session_identity = identity
            bridge.bridge_state = repl.bridge_state_default(identity)
            prompt = "골 명령어 + 상세설명 나눠서 2개로 보내줘"
            goal = "/goal Codex Telegram bridge recurrence guard를 구현한다."
            spec = "상세설명:\n\n- 두 번째 메시지는 상세 설명이다."
            records = [
                {
                    "timestamp": "2026-06-20T00:00:00Z",
                    "type": "event_msg",
                    "payload": {"type": "user_message", "message": prompt},
                },
                {
                    "timestamp": "2026-06-20T00:00:01Z",
                    "type": "event_msg",
                    "payload": {"type": "agent_message", "phase": "commentary", "message": goal},
                },
                {
                    "timestamp": "2026-06-20T00:00:02Z",
                    "type": "event_msg",
                    "payload": {"type": "agent_message", "phase": "final_answer", "message": spec},
                },
            ]
            bridge.begin_telegram_prompt_tracking(prompt)
            cursor = 0
            for record in records:
                line = json.dumps(record) + "\n"
                self.assertTrue(bridge.process_line(line, cursor, cursor + len(line)))
                cursor += len(line)

            self.assertEqual(telegram.sent, [goal, spec])
            self.assertEqual(fake_repl.prompts, [])

    def test_copy_payload_pair_contract_repairs_bare_gol_dubeon_request(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = config(tmpdir)
            telegram = FakeTelegram()
            fake_repl = FakeRepl()
            bridge = repl.Bridge(cfg, telegram, fake_repl)
            identity = repl.SessionIdentity(str(Path(tmpdir) / "rollout.jsonl"), 1, 2, 100)
            bridge.session_identity = identity
            bridge.bridge_state = repl.bridge_state_default(identity)
            prompt = "다시 그러면 아까 요청한 골 + 상세스펙 두번 보내줘"
            goal = "/goal Codex Telegram bridge recurrence guard를 구현한다."
            user = {
                "timestamp": "2026-06-20T00:00:00Z",
                "type": "event_msg",
                "payload": {"type": "user_message", "message": prompt},
            }
            assistant = {
                "timestamp": "2026-06-20T00:00:01Z",
                "type": "event_msg",
                "payload": {"type": "agent_message", "phase": "final_answer", "message": goal},
            }
            bridge.begin_telegram_prompt_tracking(prompt)
            user_line = json.dumps(user) + "\n"
            assistant_line = json.dumps(assistant) + "\n"

            self.assertTrue(bridge.process_line(user_line, 0, len(user_line)))
            self.assertTrue(
                bridge.process_line(
                    assistant_line,
                    len(user_line),
                    len(user_line) + len(assistant_line),
                )
            )

            self.assertEqual(telegram.sent, [goal])
            self.assertEqual(len(fake_repl.prompts), 1)
            self.assertIn("상세스펙", fake_repl.prompts[0])
            self.assertIn("누락", fake_repl.prompts[0])
            state = json.loads(Path(cfg.state_path).read_text(encoding="utf-8"))
            self.assertEqual(state["last_copy_payload_pair_missing"]["missing"], ["spec"])
            self.assertTrue(state["last_copy_payload_pair_missing"]["auto_repair_requested"])

    def test_copy_payload_pair_classifier_handles_korean_split_variants(self):
        self.assertTrue(repl.prompt_requires_copy_payload_pair("골 보내고 상세스펙 보내줘"))
        self.assertTrue(repl.prompt_requires_copy_payload_pair("/goal 상세스팩 2통"))
        self.assertTrue(repl.prompt_requires_copy_payload_pair("골명령어랑 상세설명 두 번 보내"))
        self.assertTrue(repl.prompt_requires_copy_payload_pair("제목 내용 따로보내줘 두번"))
        self.assertTrue(repl.prompt_requires_copy_payload_pair("제목이랑 본문을 두 통으로 보내줘"))

    def test_copy_payload_pair_classifier_rejects_false_positive_and_single_message(self):
        self.assertFalse(repl.prompt_requires_copy_payload_pair("해골 골라줘 상세설명 따로"))
        self.assertFalse(repl.prompt_requires_copy_payload_pair("골치 아픈 상세설명 따로"))
        self.assertFalse(repl.prompt_requires_copy_payload_pair("/goal 상세스펙 한 통에 같이"))
        self.assertFalse(repl.prompt_requires_copy_payload_pair("/goal 상세스펙 합쳐서 보내"))
        self.assertFalse(repl.prompt_requires_copy_payload_pair("제목 내용 한 통에 같이"))

    def test_combined_goal_and_spec_final_answer_splits_into_two_messages(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = config(tmpdir)
            telegram = FakeTelegram()
            fake_repl = FakeRepl()
            bridge = repl.Bridge(cfg, telegram, fake_repl)
            identity = repl.SessionIdentity(str(Path(tmpdir) / "rollout.jsonl"), 1, 2, 100)
            bridge.session_identity = identity
            bridge.bridge_state = repl.bridge_state_default(identity)
            prompt = "골 명령어랑 상세스펙 두 통 따로 보내줘"
            goal = "/goal Codex Telegram bridge recurrence guard를 구현한다."
            spec = "상세스펙:\n\n- 두 번째 메시지는 상세 스펙이다."
            combined = f"{goal}\n\n{spec}"
            records = [
                {
                    "timestamp": "2026-06-20T00:00:00Z",
                    "type": "event_msg",
                    "payload": {"type": "user_message", "message": prompt},
                },
                {
                    "timestamp": "2026-06-20T00:00:01Z",
                    "type": "event_msg",
                    "payload": {"type": "agent_message", "phase": "final_answer", "message": combined},
                },
            ]
            bridge.begin_telegram_prompt_tracking(prompt)
            cursor = 0
            for record in records:
                line = json.dumps(record) + "\n"
                self.assertTrue(bridge.process_line(line, cursor, cursor + len(line)))
                cursor += len(line)

            self.assertEqual(telegram.sent, [goal, spec])
            self.assertEqual(fake_repl.prompts, [])

    def test_progress_event_sends_flow_mirror_before_final_answer(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = config(tmpdir)
            telegram = FakeTelegram()
            bridge = repl.Bridge(cfg, telegram, FakeRepl())
            identity = repl.SessionIdentity(str(Path(tmpdir) / "rollout.jsonl"), 1, 2, 100)
            bridge.session_identity = identity
            bridge.bridge_state = repl.bridge_state_default(identity)
            records = [
                {
                    "timestamp": "2026-06-27T00:00:00Z",
                    "type": "event_msg",
                    "payload": {"type": "user_message", "message": "진행해"},
                },
                {
                    "timestamp": "2026-06-27T00:00:01Z",
                    "type": "event_msg",
                    "payload": {"type": "agent_message", "message": "서비스 확인 중"},
                },
                {
                    "timestamp": "2026-06-27T00:00:02Z",
                    "type": "event_msg",
                    "payload": {"type": "agent_message", "phase": "final_answer", "message": "완료"},
                },
            ]

            cursor = 0
            for record in records:
                line = json.dumps(record, ensure_ascii=False) + "\n"
                self.assertTrue(bridge.process_line(line, cursor, cursor + len(line)))
                cursor += len(line)

            messages = without_sent_directive_cards(telegram.sent)
            self.assertEqual(len(messages), 2)
            self.assertRegex(messages[0].splitlines()[0], r"^BOT testnode · 진행해 · \d{2}:\d{2}$")
            self.assertIn("\n\n서비스 확인 중\n\n", messages[0])
            self.assertTrue(messages[0].endswith("→ 진행중 · 현재: 서비스 확인 중"))
            self.assertEqual(messages[-1], "완료")

    def test_duplicate_progress_text_sends_flow_mirror_once_per_turn(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = config(tmpdir)
            telegram = FakeTelegram()
            bridge = repl.Bridge(cfg, telegram, FakeRepl())
            identity = repl.SessionIdentity(str(Path(tmpdir) / "rollout.jsonl"), 1, 2, 100)
            bridge.session_identity = identity
            bridge.bridge_state = repl.bridge_state_default(identity)
            progress = "스킬 제약 확인 완료했습니다. 이제 PR #231의 최신 커밋/diff/check만 다시 확인합니다."
            records = [
                {
                    "timestamp": "2026-06-27T00:00:00Z",
                    "type": "event_msg",
                    "payload": {"type": "user_message", "message": "231 머지해"},
                },
                {
                    "timestamp": "2026-06-27T00:00:01Z",
                    "type": "event_msg",
                    "payload": {"type": "agent_message", "message": progress},
                },
                {
                    "timestamp": "2026-06-27T00:00:02Z",
                    "type": "event_msg",
                    "payload": {"type": "agent_message", "message": progress},
                },
                {
                    "timestamp": "2026-06-27T00:00:03Z",
                    "type": "event_msg",
                    "payload": {"type": "agent_message", "phase": "final_answer", "message": "머지 라우팅 GO"},
                },
            ]

            cursor = 0
            for record in records:
                line = json.dumps(record, ensure_ascii=False) + "\n"
                self.assertTrue(bridge.process_line(line, cursor, cursor + len(line)))
                cursor += len(line)

            messages = without_sent_directive_cards(telegram.sent)
            self.assertEqual(len(messages), 2)
            self.assertRegex(messages[0].splitlines()[0], r"^BOT testnode · 231 머지해 · \d{2}:\d{2}$")
            self.assertIn(f"\n\n{progress}\n\n", messages[0])
            self.assertTrue(messages[0].endswith(f"→ 진행중 · 현재: {progress}"))
            self.assertEqual(messages[-1], "머지 라우팅 GO")

    def test_progress_flow_mirror_can_be_disabled(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = config(tmpdir, flow_mirror=False)
            telegram = FakeTelegram()
            bridge = repl.Bridge(cfg, telegram, FakeRepl())
            identity = repl.SessionIdentity(str(Path(tmpdir) / "rollout.jsonl"), 1, 2, 100)
            bridge.session_identity = identity
            bridge.bridge_state = repl.bridge_state_default(identity)
            records = [
                {
                    "timestamp": "2026-06-27T00:00:00Z",
                    "type": "event_msg",
                    "payload": {"type": "user_message", "message": "진행해"},
                },
                {
                    "timestamp": "2026-06-27T00:00:01Z",
                    "type": "event_msg",
                    "payload": {"type": "agent_message", "message": "서비스 확인 중"},
                },
                {
                    "timestamp": "2026-06-27T00:00:02Z",
                    "type": "event_msg",
                    "payload": {"type": "agent_message", "phase": "final_answer", "message": "완료"},
                },
            ]

            cursor = 0
            for record in records:
                line = json.dumps(record, ensure_ascii=False) + "\n"
                self.assertTrue(bridge.process_line(line, cursor, cursor + len(line)))
                cursor += len(line)

            self.assertEqual(without_sent_directive_cards(telegram.sent), ["완료"])

    def test_reasoning_summary_mirrors_after_final_answer(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = config(tmpdir)
            telegram = FakeTelegram()
            bridge = repl.Bridge(cfg, telegram, FakeRepl())
            identity = repl.SessionIdentity(str(Path(tmpdir) / "rollout.jsonl"), 1, 2, 100)
            bridge.session_identity = identity
            bridge.bridge_state = repl.bridge_state_default(identity)
            records = [
                {
                    "timestamp": "2026-06-28T00:00:00Z",
                    "type": "event_msg",
                    "payload": {"type": "user_message", "message": "진행해"},
                },
                {
                    "timestamp": "2026-06-28T00:00:01Z",
                    "type": "response_item",
                    "payload": {
                        "type": "reasoning",
                        "summary": [{"type": "summary_text", "text": "옵션을 비교해 가장 빠른 경로를 택함"}],
                    },
                },
                {
                    "timestamp": "2026-06-28T00:00:02Z",
                    "type": "event_msg",
                    "payload": {"type": "agent_message", "phase": "final_answer", "message": "완료"},
                },
            ]

            cursor = 0
            for record in records:
                line = json.dumps(record, ensure_ascii=False) + "\n"
                self.assertTrue(bridge.process_line(line, cursor, cursor + len(line)))
                cursor += len(line)

            # Answer first, then the 🧠 reasoning mirror.
            self.assertEqual(
                without_sent_directive_cards(telegram.sent),
                ["완료", f"{repl.REASONING_HEADER}\n옵션을 비교해 가장 빠른 경로를 택함"],
            )

    def test_reasoning_mirror_can_be_disabled(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = config(tmpdir, reasoning_mirror=False)
            telegram = FakeTelegram()
            bridge = repl.Bridge(cfg, telegram, FakeRepl())
            identity = repl.SessionIdentity(str(Path(tmpdir) / "rollout.jsonl"), 1, 2, 100)
            bridge.session_identity = identity
            bridge.bridge_state = repl.bridge_state_default(identity)
            records = [
                {
                    "timestamp": "2026-06-28T00:00:00Z",
                    "type": "event_msg",
                    "payload": {"type": "user_message", "message": "진행해"},
                },
                {
                    "timestamp": "2026-06-28T00:00:01Z",
                    "type": "response_item",
                    "payload": {
                        "type": "reasoning",
                        "summary": [{"type": "summary_text", "text": "내부 요약"}],
                    },
                },
                {
                    "timestamp": "2026-06-28T00:00:02Z",
                    "type": "event_msg",
                    "payload": {"type": "agent_message", "phase": "final_answer", "message": "완료"},
                },
            ]

            cursor = 0
            for record in records:
                line = json.dumps(record, ensure_ascii=False) + "\n"
                self.assertTrue(bridge.process_line(line, cursor, cursor + len(line)))
                cursor += len(line)

            self.assertEqual(without_sent_directive_cards(telegram.sent), ["완료"])

    def test_telegram_prompt_tracking_sends_periodic_progress_update(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = config(tmpdir, long_running_progress_seconds=1, flow_mirror=False)
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
            self.assertIn("Progress update", telegram.sent[0])
            self.assertIn("run the longer task", telegram.sent[0])
            self.assertIn("final answer", telegram.sent[0])

    def test_progress_update_includes_latest_public_progress_and_current_task(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = config(tmpdir, long_running_progress_seconds=1, flow_mirror=False)
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
            self.assertIn("Progress update", telegram.sent[0])
            self.assertIn("T-260624-11", telegram.sent[0])
            self.assertIn("copied to nodes checking services", telegram.sent[0])

    def test_telegram_fallback_sends_once_before_delayed_final_answer(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = config(tmpdir, telegram_fallback_seconds=0, flow_mirror=False)
            telegram = FakeTelegram()
            bridge = repl.Bridge(cfg, telegram, FakeRepl())

            bridge.begin_telegram_prompt_tracking("why is desktop bridge silent")
            bridge.handle_progress_event("checking logs\nwaiting for final_answer")
            bridge.send_telegram_fallback("why is desktop bridge silent", 95)
            bridge.send_telegram_fallback("why is desktop bridge silent", 120)

            self.assertEqual(len(telegram.sent), 1)
            self.assertIn("Progress update", telegram.sent[0])
            self.assertIn("checking logs waiting for final_answer", telegram.sent[0])

            self.assertTrue(bridge.handle_assistant_event("final result"))
            self.assertEqual(telegram.sent[-1], "final result")

    def test_telegram_fallback_does_not_send_for_terminal_origin(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = config(tmpdir, telegram_fallback_seconds=0)
            telegram = FakeTelegram()
            bridge = repl.Bridge(cfg, telegram, FakeRepl())

            bridge.handle_user_event("typed directly in terminal")
            bridge.send_telegram_fallback("typed directly in terminal", 95)

            # Terminal-origin prompts mirror a single terminal-input echo card;
            # the fallback itself must not add anything on top of it.
            self.assertEqual(
                telegram.sent,
                ["⌨️ 터미널 입력\ntyped directly in terminal"],
            )

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

    def test_liveness_stops_recovered_typing_when_repl_is_idle_without_active_prompt(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            telegram = FakeTelegram()
            fake_repl = FakeRepl()
            fake_repl.screen = "Working (5m 05s · esc to interrupt)\n"
            bridge = repl.Bridge(config(tmpdir), telegram, fake_repl)

            try:
                self.assertTrue(bridge.recover_repl_liveness("test"))
                self.assertTrue(bridge.has_repl_typing())

                fake_repl.screen = "ready\nNo active task\n  model · ~ · Context 54% used\n"
                self.assertTrue(bridge.recover_repl_liveness("test-idle"))
                self.assertFalse(bridge.has_repl_typing())
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
            self.assertEqual(telegram.attachments[0][0].resolve(), image.resolve())
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
            self.assertEqual(telegram.attachments[0][0].resolve(), image.resolve())
            self.assertEqual(telegram.sent, ["스크린샷입니다:"])
            self.assertNotIn(str(image), telegram.sent[0])

    def test_raw_windows_attachment_path_pattern_is_recognized(self):
        match = repl.RAW_LOCAL_ATTACHMENT_PATH_RE.search(
            r"영상입니다: C:\Users\test_user\Downloads\demo.mp4"
        )

        self.assertIsNotNone(match)
        self.assertEqual(match.group("path"), r"C:\Users\test_user\Downloads\demo.mp4")

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

            # Attachment failure no longer fails the whole turn — the body is
            # delivered and the failure is surfaced as a one-line notice, so the
            # cursor advances and the same answer is not resent forever.
            self.assertTrue(bridge.send_answer(f"Here: [screenshot.png]({image})"))

            self.assertEqual(telegram.sent[0], "Here:")
            self.assertIn("첨부 전송 실패", telegram.sent[-1])
            self.assertIn(str(image.resolve()), telegram.sent[-1])
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
            self.assertEqual(telegram.attachments[0][0].resolve(), video.resolve())
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
            self.assertEqual(telegram.attachments[0][0].resolve(), audio.resolve())
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
