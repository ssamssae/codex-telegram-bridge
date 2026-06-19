# Codex Telegram Bridge

[![Release](https://img.shields.io/github/v/release/ssamssae/codex-telegram-bridge)](https://github.com/ssamssae/codex-telegram-bridge/releases)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

Run Codex from Telegram, with the visible Codex CLI REPL and Telegram kept in
sync. Telegram text, images, video metadata/thumbnails, and audio transcripts
can be delivered into the Codex CLI transcript, while Codex final answers are
mirrored back to Telegram.

Install with `pipx`, then run the setup wizard:

```bash
pipx install "git+https://github.com/ssamssae/codex-telegram-bridge.git@v0.2.4"
codex-telegram-bridge setup
codex-telegram-bridge doctor
```

Or install from a clone:

```bash
git clone https://github.com/ssamssae/codex-telegram-bridge.git
cd codex-telegram-bridge
python3 bridge_setup.py setup
python3 bridge_setup.py doctor
```

Token safety rule: BotFather shows the bot token in Telegram, but paste it only
into the local terminal setup wizard. In Telegram, send only `/start`, `/ping`,
or normal prompts to your bot.

Release: <https://github.com/ssamssae/codex-telegram-bridge/releases/latest>

Promo video:
<https://github.com/ssamssae/codex-telegram-bridge/releases/download/v0.2.4/codex-telegram-bridge-promo-v0.2.4.mp4>

The repo also includes a simpler one-shot `codex exec` mode. Generic one-shot
command backends can adapt Claude Code, Aider, Gemini CLI, or your own terminal
agent.

This is not MCP. It is a small standalone relay daemon. Default `repl` mode:

```text
Telegram message/media
  -> getUpdates polling
  -> single-user chat_id allowlist
  -> paste a prompt into tmux -L codex / Codex TUI
  -> watch Codex JSONL session logs
  -> mirror final answers to Telegram

Codex CLI input
  -> JSONL user event
  -> Telegram "typing..." while Codex is working
  -> final answer mirrored to Telegram

Codex approval prompt
  -> detect "Would you like to run..." in the tmux pane
  -> send Telegram buttons for 1/2/3
  -> inject the selected key back into the Codex TUI

Answer attachments
  -> detect local image paths in final answers
  -> send the actual image to Telegram with sendPhoto/sendDocument
```

## Quickstart

The setup wizard is designed for first-time users. It shows six steps:

```text
[1/6] Paste the BotFather token
[2/6] Connect your Telegram chat
[3/6] Check the local Codex mode
[4/6] Write the private config
[5/6] Install the background service
[6/6] Send a setup-complete test message
```

Important: paste the BotFather token only into the terminal when the setup
wizard asks for it. In Telegram, send only `/start`, `/ping`, or normal prompts.

1. Install and log in to Codex on the same machine. Start Codex in a named tmux
   session for full REPL mode:

```bash
tmux -L codex new -s codex
codex
```

If you only want text-only one-shot mode without a visible Codex TUI, you can use
`codex-telegram-bridge setup --mode exec` instead.

2. Create a Telegram bot with [@BotFather](https://t.me/BotFather) and copy the bot token.
   Do not send this token in any Telegram chat. Paste it only into the local
   terminal setup wizard in the next step.

3. Run the setup wizard. If you used `pipx`, run:

```bash
codex-telegram-bridge setup
```

If you installed from a clone, run:

```bash
git clone https://github.com/ssamssae/codex-telegram-bridge.git
cd codex-telegram-bridge
python3 bridge_setup.py setup
```

The wizard will:

- validate the BotFather token with Telegram `getMe`
- ask you to send `/start` to the bot in Telegram
- detect your numeric `chat_id` automatically
- show what it is doing at each step
- write a private `~/.config/telegram-agent-bridge.env` with mode `0600`
- install `~/.local/bin/telegram-agent-bridge-run`
- install and start a user service with systemd on Linux/WSL or launchd on macOS
- send a setup-complete test message

Default setup mode is `repl`, which supports the visible Codex CLI transcript,
Telegram text, image prompts, video thumbnails/metadata, audio-file delivery,
optional audio transcription, answer mirroring, Telegram `typing...`, and Codex
approval prompts. If a final answer contains a local image path or markdown
image/link to an allowed image file, the bridge also sends the actual image to
Telegram.

4. Check the installation:

```bash
codex-telegram-bridge doctor
# or, from a clone:
python3 bridge_setup.py doctor
```

5. Send `/ping` to the bot. Then send a normal prompt.

Token safety rule: BotFather shows the token in Telegram, but you should copy it
from BotFather and paste it into the local setup wizard in your terminal. In
Telegram, send only `/start` or normal prompts to your bot.

For local terminal input without scraping an agent TUI, either type into the
foreground bridge process or write one prompt per line to the FIFO:

```bash
printf '%s\n' 'continue from the terminal' > ~/.local/state/telegram-agent-bridge/input.fifo
```

Prompts and final answers are mirrored to both Telegram and terminal output.

## Setup Commands

Interactive install:

```bash
codex-telegram-bridge setup
# or:
python3 bridge_setup.py setup
```

Text-only one-shot install:

```bash
codex-telegram-bridge setup --mode exec
# or:
python3 bridge_setup.py setup --mode exec
```

Install optional local audio transcription dependencies for voice/audio files:

```bash
codex-telegram-bridge setup --install-asr
# or:
python3 bridge_setup.py setup --install-asr
```

Non-interactive install, useful for scripts:

```bash
codex-telegram-bridge setup \
  --token '123456:BOT_TOKEN' \
  --chat-id '123456789' \
  --non-interactive \
  -y

# or, from a clone:
python3 bridge_setup.py setup \
  --token '123456:BOT_TOKEN' \
  --chat-id '123456789' \
  --non-interactive \
  -y
```

Health check:

```bash
codex-telegram-bridge doctor
# or:
python3 bridge_setup.py doctor
```

Uninstall the service and runner while keeping your private config:

```bash
codex-telegram-bridge uninstall
# or:
python3 bridge_setup.py uninstall
```

Remove the private config too:

```bash
codex-telegram-bridge uninstall --purge
# or:
python3 bridge_setup.py uninstall --purge
```

## Manual Setup

If you do not want the setup wizard to install a service, create a private env
file manually:

```bash
cp config.example.env ~/.config/telegram-agent-bridge.env
chmod 600 ~/.config/telegram-agent-bridge.env
$EDITOR ~/.config/telegram-agent-bridge.env
```

Run the daemon in the foreground:

```bash
set -a
. ~/.config/telegram-agent-bridge.env
set +a
python3 telegram_agent_bridge.py
```

## Configuration

Required settings are intentionally small and explicit.

| Variable | Required | Default | Description |
| --- | --- | --- | --- |
| `TAB_BRIDGE_MODE` | no | `repl` | `repl` for visible Codex CLI sync, or `exec` for text-only one-shot mode. |
| `TAB_BOT_TOKEN` | yes | none | Telegram bot token from BotFather. Keep it secret. |
| `TAB_CHAT_ID` | yes | none | The only Telegram chat id allowed to control the agent. Other chats are ignored. |
| `TAB_AGENT` | no | `codex` | `codex` or `generic`. |
| `TAB_AGENT_CMD` | depends | `codex` for Codex | Base command. Split like shell arguments. Required for `generic`. |
| `TAB_STATE_DIR` | no | `~/.local/state/telegram-agent-bridge` | Offset and thread-id state directory. |
| `TAB_PREFIX` | no | empty | Prefix shown on the first Telegram reply chunk, for example an emoji or node label. |
| `TAB_PREFIX_LINE` | no | `0` | When `1`, puts `TAB_PREFIX` on its own first line. |
| `TAB_WORKDIR` | no | `~` | Working directory for the agent process. Codex also receives `-C TAB_WORKDIR`. |
| `TAB_TIMEOUT` | no | `600` | Per-turn timeout in seconds. |
| `TAB_TG_CHUNK` | no | `4096` | Telegram message chunk size. |
| `TAB_TYPING_INTERVAL` | no | `4` | Seconds between repeated Telegram `typing` actions while the agent is running. |
| `TAB_LOCAL_INPUT` | no | `~/.local/state/telegram-agent-bridge/input.fifo` on POSIX | FIFO path for local terminal prompts. Set to `0`/`off` to disable. |
| `TAB_STDIN_INPUT` | no | auto | Read local prompts from stdin. Defaults to on only when stdin is a TTY. |
| `TAB_CODEX_DANGEROUS_BYPASS` | no | `0` | When `1`, adds `--dangerously-bypass-approvals-and-sandbox` to Codex. |
| `TAB_CODEX_EXTRA_ARGS` | no | empty | Extra arguments inserted after `codex exec --json -o <tmp>`. |
| `CRB_TMUX_SOCKET` | repl only | `codex` | tmux socket for the visible Codex TUI. |
| `CRB_TMUX_SESSION` | repl only | `codex` | tmux session or target for the visible Codex TUI. |
| `CRB_TMUX_SUBMIT_KEY` | repl only | `Tab` | key sent after pasting Telegram prompts into Codex. |
| `CRB_AUDIO_TRANSCRIBE_CMD` | no | empty | Optional command template for audio transcription. Use `{path}` for the media file. |
| `CRB_ATTACHMENT_ROOTS` | no | state dir, workdir, `/tmp` | `:`-separated roots where answer-referenced local image files may be uploaded from. |
| `CRB_MAX_ATTACHMENT_BYTES` | no | `52428800` | Maximum size for local answer attachments. |

## REPL Mode Media Support

`TAB_BRIDGE_MODE=repl` supports:

- text messages
- Telegram photos and image documents
- Telegram videos, video notes, animations, and video documents
- Telegram voice/audio files
- Telegram `typing...` while Codex is generating
- terminal-origin Codex prompts mirrored back to Telegram
- Codex command approval prompts mirrored to Telegram with 1/2/3 buttons
- local image paths in final answers sent as Telegram attachments

Images are saved under `TAB_STATE_DIR` and passed to Codex as local paths in the
prompt. Video messages include the local video path, Telegram thumbnail when
available, and metadata. If `ffmpeg` is available, the bridge can extract video
frames. Audio messages include the local audio path and, when
`CRB_AUDIO_TRANSCRIBE_CMD` is configured, a transcript.

The setup wizard can install a local `faster-whisper` transcription environment:

```bash
python3 bridge_setup.py setup --install-asr
```

This is optional because it downloads Python packages and Whisper model files.

## REPL Mode Approval Prompts

When Codex is not running in a bypass/YOLO approval mode, it may pause inside the
terminal with a prompt such as:

```text
Would you like to run the following command?
1. Yes, proceed (y)
2. Yes, and don't ask again... (p)
3. No, and tell Codex what to do differently (esc)
```

In `repl` mode the bridge watches the tmux pane for that prompt and sends a
Telegram message with buttons:

- `1. Yes`
- `2. Yes, don't ask again`
- `3. No`

You can also reply with `1`, `2`, `3`, `y`, `p`, `esc`, or `/approve 1`. The
bridge injects the matching key back into the Codex TUI.

## Answer Image Attachments

If Codex answers with a local image path, the bridge sends the text answer and
then uploads the actual image to Telegram. This works for raw paths and markdown
links such as:

```text
Here is the screenshot: [screenshot.png](/home/user/project/screenshot.png)
```

For safety, only image files under `CRB_ATTACHMENT_ROOTS` are uploaded. By
default those roots are the bridge state directory, `TAB_WORKDIR`, and `/tmp`.
Large files are skipped according to `CRB_MAX_ATTACHMENT_BYTES`.

## Backends

Backends implement four methods:

```python
build_exec_cmd(prompt)
build_resume_cmd(thread_id, prompt)
parse_thread_id(json_events)
parse_answer(json_events, stdout, stderr)
```

### Codex

`TAB_AGENT=codex` is the full backend.

It runs:

```text
codex exec --json -o <tmp-answer-file> -C <TAB_WORKDIR> <prompt>
codex exec --json -o <tmp-answer-file> -C <TAB_WORKDIR> resume <thread_id> <prompt>
```

The bridge stores the first `thread.started.thread_id` and resumes it on later messages. If a stored thread id is stale, the bridge clears it and retries once as a fresh Codex thread.

### Generic

`TAB_AGENT=generic` is a no-session fallback for tools that can answer from one command.

Examples:

```bash
TAB_AGENT=generic
TAB_AGENT_CMD='my-agent --prompt {prompt}'
```

If `{prompt}` appears in any argument, it is replaced in place. Otherwise the prompt is appended as the final argument. Generic mode returns stdout as the answer and does not preserve context.

### Stub templates for other agents

These are command-shape starting points only. Verify each tool's current CLI before using it.

```bash
# Claude Code style one-shot
TAB_AGENT=generic
TAB_AGENT_CMD='claude -p {prompt}'

# Aider style one-shot
TAB_AGENT=generic
TAB_AGENT_CMD='aider --message {prompt}'

# Gemini CLI style one-shot
TAB_AGENT=generic
TAB_AGENT_CMD='gemini -p {prompt}'
```

To add a real resumable backend, create a class like `CodexBackend`, set `supports_resume = True`, and implement the four backend methods against that agent's machine-readable output.

## Service Examples

Examples live in:

- `examples/systemd/telegram-agent-bridge.service`
- `examples/launchd/com.user.telegram-agent-bridge.plist`

Review the `PATH` in each file. Services often start with a smaller environment than your shell, so include the directory where `codex`, `claude`, `aider`, or `gemini` is installed.

## Security Notes

- Treat `TAB_BOT_TOKEN` like a password. Do not commit it, paste it into logs, or share it.
- Do not paste the BotFather token into your Telegram bot chat. Paste it only
  into the local terminal setup wizard. The only Telegram message needed during
  setup is `/start`.
- Keep `TAB_CHAT_ID` set to your own chat id. This single-user allowlist is the main safety boundary.
- Run this only on a trusted personal machine or trusted server. Telegram messages become terminal agent prompts.
- Protect `TAB_LOCAL_INPUT`. Anyone who can write to the FIFO can send prompts to the agent.
- Be careful with `TAB_CODEX_DANGEROUS_BYPASS=1`. It adds `--dangerously-bypass-approvals-and-sandbox`, allowing Codex to act without normal approval and sandbox protections.
- Prefer a limited working directory in `TAB_WORKDIR` when possible.
- This daemon uses Telegram polling, not a public inbound webhook. You do not need to expose a local port.

## Development

The runtime uses only the Python standard library.

```bash
python3 -m unittest discover -s tests
```
