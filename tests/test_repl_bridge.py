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
        "telegram_chunk": 4096,
        "poll_timeout": 2,
        "start_at_end": True,
    }
    base.update(overrides)
    return repl.Config(**base)


class FakeTelegram:
    def __init__(self):
        self.downloads = []

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


if __name__ == "__main__":
    unittest.main()
