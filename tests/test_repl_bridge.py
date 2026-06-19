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

    def call(self, method, **params):
        self.calls.append((method, params))
        return {"ok": True, "result": True}

    def send_local_attachment(self, path, max_bytes):
        self.attachments.append((path, max_bytes))
        return True


class FakeRepl:
    def __init__(self):
        self.approval_keys = []

    def send_approval_key(self, key):
        self.approval_keys.append(key)


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

    def test_approval_text_choice_sends_tmux_key(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            fake_repl = FakeRepl()
            bridge = repl.Bridge(config(tmpdir), FakeTelegram(), fake_repl)
            bridge.pending_approval = repl.ApprovalPrompt(
                signature="abcdef1234567890",
                command="$ git status",
                reason="test",
                options=(
                    repl.ApprovalOption("1", "Yes", "y"),
                    repl.ApprovalOption("2", "Yes always", "p"),
                    repl.ApprovalOption("3", "No", "esc"),
                ),
            )

            self.assertTrue(bridge.handle_approval_text("2"))
            self.assertEqual(fake_repl.approval_keys, ["p"])

    def test_approval_callback_checks_signature_and_sends_key(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            fake_repl = FakeRepl()
            telegram = FakeTelegram()
            bridge = repl.Bridge(config(tmpdir), telegram, fake_repl)
            bridge.pending_approval = repl.ApprovalPrompt(
                signature="abcdef1234567890fedcba",
                command="$ git status",
                reason="test",
                options=(
                    repl.ApprovalOption("1", "Yes", "y"),
                    repl.ApprovalOption("2", "Yes always", "p"),
                    repl.ApprovalOption("3", "No", "esc"),
                ),
            )
            callback = {
                "id": "cb1",
                "data": "crb_approval:abcdef1234567890:3",
                "message": {"chat": {"id": "1234"}},
            }

            self.assertTrue(bridge.handle_callback_query(callback))
            self.assertEqual(fake_repl.approval_keys, ["esc"])
            self.assertEqual(telegram.calls[0][0], "answerCallbackQuery")

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
