#!/usr/bin/env python3
import os
import tempfile
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
        "approval_ttl_seconds": 300,
        "workdir": Path(tmpdir),
        "attachment_roots": (Path(tmpdir),),
        "max_attachment_bytes": 50 * 1024 * 1024,
        "telegram_chunk": 4096,
        "poll_timeout": 2,
        "start_at_end": True,
    }
    base.update(overrides)
    return repl.Config(**base)


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

    def send_approval_key(self, key):
        self.approval_keys.append(key)

    def send_choice_option(self, prompt, option):
        self.choice_options.append((prompt, option))


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

    def test_parse_generic_numbered_choice_prompt(self):
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
