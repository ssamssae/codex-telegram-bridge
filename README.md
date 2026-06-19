# Codex Telegram Bridge

Run Codex from Telegram, with the visible Codex CLI REPL and Telegram kept in
sync. Telegram text, images, video metadata/thumbnails, and audio transcripts
can be delivered into the Codex CLI transcript, while Codex final answers are
mirrored back to Telegram.

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
```

## Quickstart

1. Install and log in to Codex on the same machine. Start Codex in a named tmux
   session for full REPL mode:

```bash
tmux -L codex new -s codex
codex
```

If you only want text-only one-shot mode without a visible Codex TUI, you can use
`python3 bridge_setup.py setup --mode exec` instead.

2. Create a Telegram bot with [@BotFather](https://t.me/BotFather) and copy the bot token.
   Do not send this token in any Telegram chat. Paste it only into the local
   terminal setup wizard in the next step.

3. Run the setup wizard:

```bash
git clone https://github.com/ssamssae/codex-telegram-bridge.git
cd codex-telegram-bridge
python3 bridge_setup.py setup
```

The wizard will:

- validate the BotFather token with Telegram `getMe`
- ask you to send `/start` to the bot in Telegram
- detect your numeric `chat_id` automatically
- write a private `~/.config/telegram-agent-bridge.env` with mode `0600`
- install `~/.local/bin/telegram-agent-bridge-run`
- install and start a user service with systemd on Linux/WSL or launchd on macOS
- send a setup-complete test message

Default setup mode is `repl`, which supports the visible Codex CLI transcript,
Telegram text, image prompts, video thumbnails/metadata, audio-file delivery,
optional audio transcription, answer mirroring, and Telegram `typing...`.

4. Check the installation:

```bash
python3 bridge_setup.py doctor
```

5. Send `/ping` to the bot. Then send a normal prompt.

Token safety rule: BotFather shows the token in Telegram, but you should copy it
from BotFather and paste it into `python3 bridge_setup.py setup` in your local
terminal. In Telegram, send only `/start` or normal prompts to your bot.

For local terminal input without scraping an agent TUI, either type into the
foreground bridge process or write one prompt per line to the FIFO:

```bash
printf '%s\n' 'continue from the terminal' > ~/.local/state/telegram-agent-bridge/input.fifo
```

Prompts and final answers are mirrored to both Telegram and terminal output.

## Setup Commands

Interactive install:

```bash
python3 bridge_setup.py setup
```

Text-only one-shot install:

```bash
python3 bridge_setup.py setup --mode exec
```

Install optional local audio transcription dependencies for voice/audio files:

```bash
python3 bridge_setup.py setup --install-asr
```

Non-interactive install, useful for scripts:

```bash
python3 bridge_setup.py setup \
  --token '123456:BOT_TOKEN' \
  --chat-id '123456789' \
  --non-interactive \
  -y
```

Health check:

```bash
python3 bridge_setup.py doctor
```

Uninstall the service and runner while keeping your private config:

```bash
python3 bridge_setup.py uninstall
```

Remove the private config too:

```bash
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

## REPL Mode Media Support

`TAB_BRIDGE_MODE=repl` supports:

- text messages
- Telegram photos and image documents
- Telegram videos, video notes, animations, and video documents
- Telegram voice/audio files
- Telegram `typing...` while Codex is generating
- terminal-origin Codex prompts mirrored back to Telegram

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
