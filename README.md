# Codex Telegram Bridge

[![Release](https://img.shields.io/github/v/release/ssamssae/codex-telegram-bridge)](https://github.com/ssamssae/codex-telegram-bridge/releases)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

Control your live Codex CLI session from Telegram. Only Codex is supported.
Other AI CLIs are intentionally out of scope; a Claude bridge should be a
separate Claude-specific program, not a shared mode in this repository.

Codex Telegram Bridge is a phone remote for your already-running Codex TUI. Send
prompts, screenshots, videos, voice notes, and files from Telegram; the bridge
pastes them into your visible tmux Codex session, watches Codex's structured
JSONL session log, then mirrors final answers and generated media back to
Telegram.

Default `repl` mode is REPL sync, not a separate hidden chat. Your terminal
stays the source of truth, the transcript remains readable, and Telegram becomes
the remote control when you are away from the keyboard.

Install with `pipx`, then run the setup wizard:

```bash
pipx install "git+https://github.com/ssamssae/codex-telegram-bridge.git@v0.5.1"
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

## Public Export Model

This public repository is maintained from a private operator source through a
sanitized export step. The export keeps the reusable bridge behavior, setup
wizard, and BYO signal contract, while stripping private chat ids, token paths,
hostnames, node labels, and local automation paths before release.

Do not copy another operator's private wrapper scripts into your setup. Treat
`CRB_SIGNAL_PATH` / `TAB_LOCAL_INPUT` as the public integration boundary: your
cron job, local queue, or orchestrator writes one prompt line to the FIFO, and
the bridge owns only local delivery into the visible Codex session plus Telegram
mirroring.

Release: <https://github.com/ssamssae/codex-telegram-bridge/releases/latest>

Promo video from the v0.3 demo release:
<https://github.com/ssamssae/codex-telegram-bridge/releases/download/v0.3.10/codex-telegram-bridge-promo-v0.3.10.mp4>

The repo also includes a simpler one-shot `codex exec` mode. Both modes are
Codex-only by design so maintenance stays focused and predictable.

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
  -> recover Telegram "typing..." if the bridge restarts while the Codex pane is still busy
  -> send a one-shot fallback progress reply if final_answer is delayed
  -> periodic detailed progress updates for long Telegram-origin turns
  -> final answer mirrored to Telegram

Codex approval prompt
  -> detect "Would you like to run..." in the tmux pane
  -> send Telegram buttons for the visible approval choices
  -> mark the selected button and remove stale choices
  -> inject the selected key back into the Codex TUI

Codex selection prompt
  -> detect numbered/lettered menus and y/n confirmations in the tmux pane
  -> send Telegram buttons for the visible options
  -> mark the selected button and remove stale choices
  -> inject shortcut keys or arrow+Enter navigation back into the Codex TUI

Codex slash command
  -> detect single-line commands such as /model from Telegram
  -> submit them with Enter instead of the normal queued prompt key
  -> mirror "Unrecognized command" errors back to Telegram
  -> clear the Codex composer before Telegram input so stale typo commands cannot be appended
  -> keep Telegram typing and progress updates active for long-running commands such as /goal
  -> auto-request a missing second /goal + 상세스펙/상세설명 copy-paste payload once, and split combined payloads into two Telegram messages
  -> scope copy-paste deduplication to the current Telegram prompt so an explicit resend can send the same /goal body again
  -> classify Korean two-message copy-paste requests with bare "골", 두번/두 번, repeated 보내 verbs, 상세스팩 typo, and single-message override words

Answer media attachments
  -> detect local image/video/audio paths in final answers
  -> hide the local path in Telegram
  -> send the actual media with sendPhoto/sendVideo/sendVoice/sendAudio

Service restart
  -> run a local watchdog every 60 seconds when installed as a service
  -> restart or kickstart the bridge if the user service is inactive
  -> load a persistent JSONL cursor and final-answer dedup ring
  -> resume from the cursor when it still matches the current session file
  -> otherwise tail-scan recent JSONL after the latest user event
  -> backfill the latest eligible final answer once, then resume live watching
```

## Why REPL Sync

- You can keep using the local Codex TUI normally while Telegram mirrors the
  final answers.
- Telegram-origin prompts are pasted into the same visible transcript instead of
  disappearing into a separate hidden thread.
- Terminal-origin prompts can still show Telegram `typing...` and final-answer
  mirrors, so the phone stays informed even when the work started locally.
- Long-running Telegram-origin turns send progress reports with the task label,
  optional task id, elapsed time, latest public progress note, next step, and
  blocker status.
- Telegram-origin turns also send one early fallback progress reply after 90
  seconds by default, so a turn that is producing commentary/tool activity but
  has not emitted `final_answer` yet does not look silent from Telegram.
- If the daemon restarts or joins mid-turn, it checks the visible Codex pane
  every 10 seconds by default and restarts Telegram `typing...` while Codex is
  still working. Set `CRB_TYPING_LIVENESS_SECONDS=0` to disable this recovery
  loop.
- Approval and selection prompts remain real Codex TUI prompts; the bridge sends
  Telegram buttons for the visible options and injects the selected key back
  into tmux.
- The daemon uses polling and a single `chat_id` allowlist, so no public webhook
  or inbound port is required.

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
- install and start a user service with systemd on Linux/WSL, launchd on macOS,
  or a per-user Windows Scheduled Task
- install a watchdog timer/LaunchAgent that recovers an inactive bridge service
- send a setup-complete test message

Default setup mode is `repl`, which supports the visible Codex CLI transcript,
Telegram text, image prompts, video thumbnails/metadata, audio-file delivery,
generic file delivery, optional audio transcription, answer mirroring, Telegram
`typing...`, and Codex approval prompts. It also stores a JSONL cursor so a
daemon restart can resume watching the current Codex session and backfill the
latest eligible final answer that was produced while the bridge was down. If a
final answer contains a local image, video, or audio path or markdown link to an
allowed media file, the bridge hides that local path from the Telegram text and
sends the actual media attachment.

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

For local terminal input without scraping the Codex TUI, either type into the
foreground bridge process or write one prompt per line to the FIFO:

```bash
printf '%s\n' 'continue from the terminal' > ~/.local/state/telegram-agent-bridge/input.fifo
```

Prompts and final answers are mirrored to both Telegram and terminal output.

## BYO Signal Contract

Scripts that push work into a live agent usually contain local SSH aliases,
node names, chat ids, token paths, and tmux assumptions. This project only needs
a small local input contract: write one UTF-8 line to the configured signal
FIFO.

Enable a signal FIFO for `repl` mode:

```bash
export CRB_SIGNAL_PATH="$HOME/.local/state/telegram-agent-bridge/input.fifo"
codex-telegram-bridge
```

The bridge creates the FIFO if it does not already exist. Any local process that
can write to this path can inject a prompt into Codex, so keep it under your
private state directory and do not expose it through a network share.

Signal payloads can be plain text:

```bash
printf '%s\n' 'review the latest failing test' > "$CRB_SIGNAL_PATH"
```

Or one JSON object per line with `prompt`, `text`, or `message`:

```bash
printf '%s\n' '{"prompt":"run the smoke checks and summarize failures"}' > "$CRB_SIGNAL_PATH"
```

A minimal generic trigger wrapper is included:

```bash
examples/triggers/byo-signal-submit.sh "summarize the current git diff"
```

Use this as the boundary for your own cron job, webhook receiver, task queue, or
multi-node orchestrator. Keep site-specific dispatch logic outside this repo;
the public bridge only owns the local signal contract and Codex/Telegram flow.

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
| `TAB_CHAT_ID` | yes | none | The only Telegram chat id allowed to control Codex. Other chats are ignored. |
| `TAB_AGENT` | no | `codex` | Compatibility setting. Only `codex` is supported; other values are rejected. |
| `TAB_AGENT_CMD` | no | `codex` | Codex command or wrapper command. Split like shell arguments. |
| `TAB_STATE_DIR` | no | `~/.local/state/telegram-agent-bridge` | Offset and thread-id state directory. |
| `TAB_PREFIX` | no | empty | Prefix shown on the first Telegram reply chunk, for example an emoji or node label. |
| `TAB_PREFIX_LINE` | no | `0` | When `1`, puts `TAB_PREFIX` on its own first line. |
| `TAB_WORKDIR` | no | `~` | Working directory for the Codex process. Codex also receives `-C TAB_WORKDIR`. |
| `TAB_WORKDIR_LOCK` | no | `1` | Acquire a local workdir lock around one-shot Codex turns. |
| `TAB_TIMEOUT` | no | `600` | Per-turn timeout in seconds. |
| `TAB_TG_CHUNK` | no | `4096` | Telegram message chunk size. |
| `TAB_TYPING_INTERVAL` | no | `4` | Seconds between repeated Telegram `typing` actions while Codex is running. |
| `CRB_SIGNAL_PATH` | no | `TAB_LOCAL_INPUT` when set | FIFO path for external/local signal prompts in `repl` mode. Set to `0`/`off` to disable. |
| `TAB_LOCAL_INPUT` | no | `~/.local/state/telegram-agent-bridge/input.fifo` on POSIX | Compatibility FIFO path for local terminal prompts and `CRB_SIGNAL_PATH` fallback. Set to `0`/`off` to disable. |
| `TAB_STDIN_INPUT` | no | auto | Read local prompts from stdin. Defaults to on only when stdin is a TTY. |
| `TAB_CODEX_DANGEROUS_BYPASS` | no | `0` | When `1`, adds `--dangerously-bypass-approvals-and-sandbox` to Codex. |
| `TAB_CODEX_EXTRA_ARGS` | no | empty | Extra arguments inserted after `codex exec --json -o <tmp>`. |
| `CRB_TMUX_SOCKET` | repl only | `codex` | tmux socket for the visible Codex TUI. |
| `CRB_TMUX_SESSION` | repl only | `codex` | tmux session or target for the visible Codex TUI. |
| `CRB_TMUX_SUBMIT_KEY` | repl only | `Tab` | key sent after pasting Telegram prompts into Codex. |
| `CRB_TYPING_MAX_SECONDS` | no | `7200` | Maximum lifetime for repeated Telegram `typing` actions during one visible Codex turn. |
| `CRB_TELEGRAM_FALLBACK_SECONDS` | no | `90` | One-shot fallback progress reply delay for Telegram-origin REPL prompts when `final_answer` is delayed. Set `0` to disable. |
| `CRB_FLOW_MIRROR` | no | `1` | Mirror public Codex progress/commentary steps to Telegram with the `⚙️ 작업 흐름` header. |
| `CRB_REASONING_MIRROR` | no | `1` | Mirror Codex's public reasoning summary to Telegram with the `🧠 코덱스 사고` header, sent right after the final answer. Only the runtime-public summary is sent (never raw chain-of-thought); copy-payload replies do not emit a reasoning mirror. Set `0` to disable. |
| `CRB_LONG_RUNNING_PROGRESS_SECONDS` | no | `0` | Legacy periodic progress interval for long-running Telegram-origin REPL prompts. The flow mirror replaces it by default; set a positive second value to re-enable. |
| `CRB_AUDIO_TRANSCRIBE_CMD` | no | empty | Optional command template for audio transcription. Use `{path}` for the media file. |
| `CRB_APPROVAL_TTL_SECONDS` | no | `300` | Seconds before a Telegram approval button is treated as stale. |
| `CRB_STATE_PATH` | no | `TAB_STATE_DIR/codex-repl-bridge-<node>.state.json` | Persistent JSONL cursor and final-answer dedup state for `repl` mode. |
| `CRB_BACKFILL` | no | `1` | When enabled, startup without a valid cursor tail-scans the current session for a fresh missed final answer. |
| `CRB_BACKFILL_MAX` | no | `1` | Maximum missed final answers to backfill on startup. Clamped to `1`-`3`. |
| `CRB_BACKFILL_WINDOW_SEC` | no | `600` | Maximum age for startup backfill candidates. |
| `CRB_TAIL_SCAN_BYTES` | no | `65536` | Bytes to scan from the end of the Codex JSONL session when cursorless backfill is needed. |
| `CRB_STATE_RING_CAP` | no | `64` | Number of mirrored final-answer keys retained for deduplication. |
| `CRB_KILL` | no | `0` | Emergency switch that blocks Telegram answer sends while keeping the process alive. |
| `CRB_ATTACHMENT_ROOTS` | no | state dir, workdir, `/tmp` | `:`-separated roots where answer-referenced local media files may be uploaded from. |
| `CRB_MAX_ATTACHMENT_BYTES` | no | `52428800` | Maximum size for local answer attachments. |

## REPL Mode Media Support

`TAB_BRIDGE_MODE=repl` supports:

- text messages
- Telegram photos and image documents
- Telegram videos, video notes, animations, and video documents
- Telegram voice/audio files
- generic Telegram document files
- Telegram `typing...` while Codex is generating
- terminal-origin Codex prompts mirrored back to Telegram
- Codex command approval prompts mirrored to Telegram with buttons
- Codex numbered/lettered selection prompts and y/n confirmations mirrored to
  Telegram with buttons
- local image/video/audio paths in final answers hidden from Telegram text and
  sent as Telegram attachments

Images are saved under `TAB_STATE_DIR` and passed to Codex as local paths in the
prompt. Video messages include the local video path, Telegram thumbnail when
available, and metadata. If `ffmpeg` is available, the bridge can extract video
frames. Audio messages include the local audio path and, when
`CRB_AUDIO_TRANSCRIBE_CMD` is configured, a transcript. Generic document files
are saved under the same local media directory and passed to Codex with
`local_path`, caption, MIME type, file name, and file size metadata.

The setup wizard can install a local `faster-whisper` transcription environment:

```bash
python3 bridge_setup.py setup --install-asr
```

This is optional because it downloads Python packages and Whisper model files.

## REPL Mode Restart Backfill

In `repl` mode the bridge stores a JSONL cursor and a small final-answer dedup
ring in `CRB_STATE_PATH`. On restart, if that cursor still matches the current
Codex session file, the bridge resumes reading from the saved offset.

If there is no valid cursor and `CRB_START_AT_END=1`, the bridge scans the tail
of the current Codex JSONL session, finds the latest user event, and backfills
up to `CRB_BACKFILL_MAX` fresh final answers after that user event. Candidates
must be inside `CRB_BACKFILL_WINDOW_SEC` and absent from the dedup ring. This
reduces the common failure mode where Codex finished while the Telegram bridge
service was restarting.

## Service Watchdog

When the setup wizard installs a background service, it also installs a small
watchdog.

On Windows, the wizard writes a per-user `telegram-agent-bridge` Scheduled Task
that runs at logon and starts once immediately after setup. `doctor` reports the
task status from `schtasks /query`.

On Linux and WSL, the wizard writes:

- `~/.config/systemd/user/telegram-agent-bridge-watchdog.service`
- `~/.config/systemd/user/telegram-agent-bridge-watchdog.timer`

The timer runs every 60 seconds. If `telegram-agent-bridge.service` is inactive,
the watchdog starts it and writes a status file at
`~/.local/state/telegram-agent-bridge/watchdog.status`.

On macOS, the wizard writes:

- `~/Library/LaunchAgents/com.user.telegram-agent-bridge-watchdog.plist`

The LaunchAgent runs every 60 seconds. If
`com.user.telegram-agent-bridge` is not running, the watchdog kickstarts it and
writes the same status file.

This covers the failure mode where the bridge was explicitly stopped or left
inactive. The normal `Restart=always` and launchd `KeepAlive` settings still
handle ordinary crashes.

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

You can also reply with the visible number or shortcut, for example `1`, `2`,
`y`, `p`, `esc`, or `/approve 1`. The bridge injects the matching key back into
the Codex TUI, edits the original Telegram approval message to show the selected
button, and removes stale choices.

## REPL Mode Selection Prompts

The bridge also watches for non-approval Codex TUI choices, for example model
pickers, mode pickers, numbered/lettered menus, and inline confirmations such as
`[y/N]` or `(yes/no)`. When a selection prompt is visible, Telegram receives
buttons for the visible options.

If a visible option has a shortcut such as `(y)`, `(n)`, `(esc)`, or `(enter)`,
the bridge sends that shortcut back to Codex. If there is no explicit shortcut,
it uses the currently highlighted `›` row and sends Up/Down plus Enter. Text
replies such as `1`, `2`, `a`, `y`, `yes`, `n`, `no`, and `/choose 2` are also
accepted while the prompt is active.

## Answer Media Attachments

If Codex answers with a local image, video, or audio path, the bridge removes
that local path from the Telegram text and uploads the actual media. This works
for raw paths and markdown links such as:

```text
Here is the screenshot: [screenshot.png](/path/to/project/screenshot.png)
Here is the clip: /path/to/project/demo.mp4
Here is the audio: [voice.oga](/path/to/project/voice.oga)
```

The bridge uses Telegram's media-specific upload methods when possible:
`sendPhoto`, `sendVideo`, `sendVoice`, and `sendAudio`; unsupported media falls
back to `sendDocument`.

For safety, only media files under `CRB_ATTACHMENT_ROOTS` are uploaded and
hidden from the Telegram text. By default those roots are the bridge state
directory, `TAB_WORKDIR`, and `/tmp`. Large files are skipped according to
`CRB_MAX_ATTACHMENT_BYTES`.

## Codex Execution

It runs:

```text
codex exec --json -o <tmp-answer-file> -C <TAB_WORKDIR> <prompt>
codex exec --json -o <tmp-answer-file> -C <TAB_WORKDIR> resume <thread_id> <prompt>
```

The bridge stores the first `thread.started.thread_id` and resumes it on later messages. If a stored thread id is stale, the bridge clears it and retries once as a fresh Codex thread.

## Service Examples

Examples live in:

- `examples/systemd/telegram-agent-bridge.service`
- `examples/systemd/telegram-agent-bridge-watchdog.service`
- `examples/systemd/telegram-agent-bridge-watchdog.timer`
- `examples/launchd/com.user.telegram-agent-bridge.plist`
- `examples/launchd/com.user.telegram-agent-bridge-watchdog.plist`

Review the `PATH` in each file. Services often start with a smaller environment than your shell, so include the directory where `codex` is installed.

## Roadmap

These are product directions, not promises in the current release:

- queue controls for safe queueing, interruption, and side tasks
- inline Telegram settings for mode and delivery preferences
- richer Markdown fallback when Telegram rejects formatted messages
- optional multi-user and topic allowlists for small private groups
- richer service supervision dashboards and remote health summaries
- idle cleanup and maintenance commands for long-running bridge installs

## Security Notes

- Treat `TAB_BOT_TOKEN` like a password. Do not commit it, paste it into logs, or share it.
- Do not paste the BotFather token into your Telegram bot chat. Paste it only
  into the local terminal setup wizard. The only Telegram message needed during
  setup is `/start`.
- Keep `TAB_CHAT_ID` set to the intended chat id. This single-user allowlist is the main safety boundary.
- Run this only on a trusted personal machine or trusted server. Telegram messages become Codex prompts.
- Protect `CRB_SIGNAL_PATH`/`TAB_LOCAL_INPUT`. Anyone who can write to the FIFO can send prompts to Codex.
- Be careful with `TAB_CODEX_DANGEROUS_BYPASS=1`. It adds `--dangerously-bypass-approvals-and-sandbox`, allowing Codex to act without normal approval and sandbox protections.
- Prefer a limited working directory in `TAB_WORKDIR` when possible.
- This daemon uses Telegram polling, not a public inbound webhook. You do not need to expose a local port.

## Advanced Settings

Beyond the keys documented above, the bridges read further tuning knobs
(direct `CRB_BOT_TOKEN`/`CRB_CHAT_ID` overrides, `CRB_TOKEN_FILE`, media
helper binaries, generated-image autosend, watchdog probe tuning, and the
exec-backend tmux fallbacks). Every key ships with a safe default; the full
annotated list lives at the bottom of `config.example.env`.

## Development

The core runtime uses only the Python standard library. The optional `asr`
extra installs local audio transcription dependencies.

```bash
python3 -m unittest discover -s tests
```
