#!/usr/bin/env python3
"""Transcribe Telegram audio files for the Codex REPL bridge."""

from __future__ import annotations

import os
import sys
from pathlib import Path


HOME = Path.home()
DEFAULT_TOOL_DIR = HOME / ".local" / "share" / "telegram-agent-bridge" / "asr-py"
DEFAULT_CACHE_DIR = HOME / ".cache" / "telegram-agent-bridge-whisper"


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: codex-audio-transcribe.py <audio-path>", file=sys.stderr)
        return 2

    audio_path = Path(sys.argv[1]).expanduser()
    if not audio_path.exists():
        print(f"audio file not found: {audio_path}", file=sys.stderr)
        return 2

    tool_dir = Path(os.environ.get("CRB_ASR_PYTHONPATH", str(DEFAULT_TOOL_DIR))).expanduser()
    if tool_dir.exists():
        sys.path.insert(0, str(tool_dir))

    try:
        from faster_whisper import WhisperModel
    except Exception as exc:  # noqa: BLE001
        print(f"failed to import faster_whisper: {exc}", file=sys.stderr)
        return 2

    model_name = os.environ.get("CRB_WHISPER_MODEL", "tiny")
    device = os.environ.get("CRB_WHISPER_DEVICE", "cpu")
    compute_type = os.environ.get("CRB_WHISPER_COMPUTE_TYPE", "int8")
    cache_dir = Path(os.environ.get("CRB_WHISPER_CACHE_DIR", str(DEFAULT_CACHE_DIR))).expanduser()
    cache_dir.mkdir(parents=True, exist_ok=True)

    try:
        model = WhisperModel(
            model_name,
            device=device,
            compute_type=compute_type,
            download_root=str(cache_dir),
            local_files_only=False,
        )
        segments, _info = model.transcribe(
            str(audio_path),
            beam_size=5,
            vad_filter=True,
            language=os.environ.get("CRB_WHISPER_LANGUAGE") or None,
        )
        text = " ".join(segment.text.strip() for segment in segments if segment.text.strip()).strip()
    except Exception as exc:  # noqa: BLE001
        print(f"transcription failed: {exc}", file=sys.stderr)
        return 1

    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
