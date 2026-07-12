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

## Beginner Quick Start

### 1. What this does

Codex Telegram Bridge lets you use a private Telegram bot as a phone remote for
the Codex session running on your computer. A message sent to the bot appears in
your visible Codex terminal, and Codex's answer comes back to Telegram.

Private-chat replies omit configured `BOT`/node prefixes and leading decorative
emoji. Group chats keep their prefix and node emoji so senders remain clear;
reply quoting works the same on both surfaces.

The default `repl` mode keeps the terminal as the source of truth. The bridge
does not create a second hidden AI conversation.

### 2. What you need

- a personal Telegram account
- Python 3.10 or newer
- [Codex CLI](https://developers.openai.com/codex/cli/) installed and logged in
- `tmux` on Linux, WSL, or macOS for the full visible-session `repl` mode
  (if missing, install once with `sudo apt install tmux` on Debian/Ubuntu or
  `brew install tmux` on macOS)
- two terminal windows during first-time setup

Native Windows users can start with text-only `exec` mode without `tmux`. See
[Windows quickstart](#windows-quickstart-5-min) after this section.

Before continuing in Windows PowerShell, check the two commands the setup uses:

```powershell
py --version
codex --version
```

If `py` is not found, install Python 3.10 or newer from
[python.org](https://www.python.org/downloads/windows/) with the Python launcher,
then reopen PowerShell. If `codex` is not found, follow the
[Codex CLI installation guide](https://developers.openai.com/codex/cli/), reopen
PowerShell, run `codex` once, and complete sign-in before installing the bridge.

### 3. Create your Telegram bot with BotFather

1. Open Telegram and search for the official
   [@BotFather](https://t.me/BotFather) account.
2. Send this command to BotFather:

   ```text
   /newbot
   ```

3. BotFather asks for a display name. Any name is fine, for example
   `My Codex Remote`.
4. BotFather asks for a username. It must be unique and end in `bot`, for
   example `my_codex_remote_bot`.
5. BotFather sends you a bot token. Copy it, but treat it like a password.
   Never post it in an issue, commit it to Git, or send it to your new bot.
6. Open the new bot's chat and send one message. `/start` is recommended:

   ```text
   /start
   ```

Telegram bots cannot start a private conversation with you. Your `/start`
message creates the first update that setup can discover.

#### Find your numeric `chat_id` with `getUpdates`

The setup wizard in step 5 detects this automatically. The manual check below
is useful when learning the setup or diagnosing an empty result.

On Linux, WSL, or macOS, read the token without placing it in shell history:

```bash
read -rsp 'Bot token: ' BOT_TOKEN; echo
curl -sS "https://api.telegram.org/bot${BOT_TOKEN}/getUpdates"
unset BOT_TOKEN
```

In the JSON response, find the most recent private message and read:

```text
result -> message -> chat -> id
```

That integer is your `chat_id`. Keep it private even though the bot token is the
more sensitive value.

PowerShell users can run:

```powershell
$token = Read-Host 'Bot token'
$updates = Invoke-RestMethod "https://api.telegram.org/bot$token/getUpdates"
$updates.result[-1].message.chat.id
Remove-Variable token
```

If `result` is empty, return to the bot chat, send `/start` again, and rerun
`getUpdates`. If Telegram returns `401 Unauthorized`, copy a fresh token from
BotFather and check that no spaces were added.

### 4. Install from PyPI

Using a virtual environment keeps the bridge isolated from system Python.

```bash
python3 -m venv ~/.venvs/codex-telegram-bridge
source ~/.venvs/codex-telegram-bridge/bin/activate
python -m pip install --upgrade pip
pip install codex-telegram-bridge
```

Native Windows PowerShell users should use the single `pipx` path in the
[Windows quickstart](#windows-quickstart-5-min). It avoids virtual-environment
activation and the common `Activate.ps1` execution-policy error.

On Linux, WSL, or macOS, you can use `pipx install codex-telegram-bridge`
instead if you already use `pipx` for Python command-line applications.

### 5. Start Codex and run the minimal setup

In terminal 1, create the visible tmux session and start Codex:

```bash
tmux -L codex new -s codex
codex
```

Leave Codex running. In terminal 2, activate the virtual environment if needed,
then start the setup wizard:

```bash
source ~/.venvs/codex-telegram-bridge/bin/activate
codex-telegram-bridge setup
```

Native Windows users should skip these `tmux` and `source` commands and continue
with the [Windows quickstart](#windows-quickstart-5-min).

The wizard asks for the BotFather token, waits for the `/start` message, detects
your `chat_id`, writes the private configuration, and installs a background
user service. Paste the token only into this local terminal prompt.

Native Windows without tmux should use:

```powershell
codex-telegram-bridge setup --mode exec
```

`exec` mode handles one text-only Codex turn per Telegram message. Run Codex in
WSL with tmux if you want the full visible-session `repl` experience on Windows.

### 6. Verify your first run

Run the built-in health check:

```bash
codex-telegram-bridge doctor
```

Then open your bot chat in Telegram:

1. Send `/ping`. You should receive a bridge response.
2. Send `Reply with exactly: bridge works`.
3. In `repl` mode, confirm that the prompt appears in the visible Codex tmux
   session and that the final answer returns to Telegram.

If `doctor` reports that the tmux session is missing, start terminal 1 again or
switch to `exec` mode. If `codex-telegram-bridge` is not found, reactivate the
virtual environment and retry.

You now have the minimum working bridge. When you want to stop or remove it
later, run `codex-telegram-bridge uninstall` (add `--purge` to also delete the
private config) — details in [Setup Commands](#setup-commands).

The sections below explain the public export model, setup wizard internals,
services, media, approvals, security, and advanced configuration.

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
  -> run a local watchdog every 60 seconds
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

## Setup Wizard Details

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
  or a per-user Windows Startup launcher
- install a watchdog timer/LaunchAgent/Scheduled Task that recovers an inactive
  bridge service
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

## Windows quickstart (5 min)

### Option A — Native Windows exec (easiest, text-only)

On native Windows, use `exec` mode unless you are already running Codex inside
WSL with tmux. This is the recommended native Windows install path.

Open PowerShell and confirm the prerequisites:

```powershell
py --version
codex --version
```

Both commands must print a version. Run `codex` once and complete sign-in if you
have not already done so. Then install `pipx`:

```powershell
py -m pip install --user pipx
py -m pipx ensurepath
```

Close and reopen PowerShell if `pipx` was just added to PATH, then install and
run the setup wizard:

```powershell
py -m pipx install codex-telegram-bridge
codex-telegram-bridge setup --mode exec
codex-telegram-bridge doctor
```

If you installed from a clone instead, run the same flow from the clone folder:

```powershell
py bridge_setup.py setup --mode exec
py bridge_setup.py doctor
```

`doctor` should finish with zero failures. If it prints a warning, follow the
`Next steps` command printed above the summary, then run `doctor` again.

Common first-install fixes:

- `py` is not recognized: install Python 3.10 or newer from
  [python.org](https://www.python.org/downloads/windows/) with the Python
  launcher, then reopen PowerShell.
- `codex` is not recognized: install Codex CLI from the linked guide, reopen
  PowerShell, and run `codex` once to sign in.
- `codex-telegram-bridge` is not recognized: run `py -m pipx ensurepath`, close
  every PowerShell window, open a new one, and retry.
- PowerShell blocks `Activate.ps1`: do not weaken the execution policy for this
  install. Use the `pipx` commands above; they do not activate a virtual
  environment.

### Option B — WSL + tmux (full visible REPL)

Choose this path if you want Telegram messages to appear in one visible Codex
terminal, with the full `repl` features. Everything except the short PowerShell
commands runs inside Ubuntu on WSL. The official Codex
[WSL guide](https://developers.openai.com/codex/windows/wsl) also recommends
running Codex and your project files inside the Linux environment.

1. Open PowerShell **as Administrator** and install Ubuntu on WSL:

   ```powershell
   wsl --install -d Ubuntu
   ```

   Restart Windows if prompted. If WSL is already installed, check the distro
   name with `wsl -l -v` and skip this step.

2. Open a normal PowerShell window and enter Ubuntu:

   ```powershell
   WSL.exe -d Ubuntu
   ```

   The prompt now changes to a Linux prompt. Run these commands there:

   ```bash
   sudo apt update
   sudo apt install -y curl tmux python3 python3-venv pipx
   pipx ensurepath
   curl -fsSL https://chatgpt.com/codex/install.sh | sh
   exit
   ```

   Back in PowerShell, reopen Ubuntu so the new commands are on `PATH`:

   ```powershell
   WSL.exe -d Ubuntu
   ```

   Then finish the install at the Linux prompt:

   ```bash
   pipx install codex-telegram-bridge
   codex --version
   codex-telegram-bridge --help
   ```

   This installs Codex and the bridge inside WSL. Do not mix them with the
   native Windows copies from Option A.

3. Still inside WSL, create the visible tmux session:

   ```bash
   mkdir -p ~/code
   cd ~/code
   tmux -L codex new -s codex
   ```

   The screen changes because you are now inside tmux. At the new prompt, run:

   ```bash
   codex
   ```

   Complete the ChatGPT sign-in the first time Codex asks. Leave this window
   open while you finish setup.

4. Open a **second** PowerShell window, enter Ubuntu again, and run the bridge
   wizard in its default `repl` mode:

   ```powershell
   WSL.exe -d Ubuntu
   ```

   Then, at the Linux prompt:

   ```bash
   codex-telegram-bridge setup
   codex-telegram-bridge doctor
   ```

5. You can watch or rejoin the same Codex screen from any PowerShell window:

   ```powershell
   WSL.exe -d Ubuntu -- tmux -L codex attach -t codex
   ```

   To leave the screen without stopping Codex, press `Ctrl+B`, release both
   keys, then press `D`. The tmux session and bridge keep running. Use the same
   attach command whenever you want to return.

If your distro is not named `Ubuntu`, replace it in every `WSL.exe -d Ubuntu`
command with the name printed by `wsl -l -v`. If tmux says the `codex` session
already exists, use the attach command instead of creating it again.

### Native visible REPL P0 (manual foreground mode)

The default Windows path above remains `exec` mode. An opt-in P0 can instead
create one native visible Codex TUI that is owned by a foreground ConPTY host.
It does not attach to a Codex session that was started earlier, install a
service, edit Windows Terminal settings, or synthesize global keystrokes.

First make sure the bot token and chat id are already present in the local
environment. In Windows Terminal 1, start the host from the working directory
where Codex should run:

```powershell
python -m codex_repl_host_windows --workdir $PWD
```

The host launches native Codex itself. Keep that terminal open; local keyboard
input and Telegram input are serialized into the same TUI. Press `Ctrl+]` to
close the foreground host without reserving `Ctrl+C`, which remains available
to Codex. The first local or Telegram prompt creates the JSONL session; the
host then binds that exact new file to its current generation. It waits for the
first prompt instead of guessing an existing session.

An optional local check creates a short-lived `cmd.exe` ConPTY and a protected
IPC endpoint without contacting Codex or Telegram:

```powershell
python -m codex_repl_host_windows --self-test
```

In Windows Terminal 2, start the bridge against the host descriptor:

```powershell
$env:CRB_REPL_TRANSPORT = "conpty"
$env:CRB_CONPTY_STATE_PATH = "$env:LOCALAPPDATA\codex-telegram-bridge\repl-host.json"
$env:CRB_FLOW_MIRROR = "1"
python -m codex_repl_bridge
```

P0 supports text prompts, Korean/Unicode, multiline bracketed paste, and
final/public-flow mirroring from the JSONL session created by that host. Set
`CRB_FLOW_MIRROR=0` to disable the live-updating flow card.
Screen-model features are intentionally deferred: Telegram approval/selection
buttons and `/status` or `/context` TUI extraction still require tmux REPL mode.
If more than one new Codex JSONL session appears during startup, the host stops
instead of guessing. A restarted host uses a new generation and capability, so
an old bridge cannot redirect queued input into the new session.

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
| `TAB_CODEX_EXTRA_ARGS` | no | empty | Extra arguments inserted after `codex exec --json -o <tmp>`. `setup --mode exec` writes `--skip-git-repo-check` by default. |
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

On Windows, the wizard writes a per-user Startup launcher at
`%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\telegram-agent-bridge.bat`.
This does not require administrator rights. Setup also starts that launcher once
immediately so you can send `/ping` without logging out and back in. If an older
per-user `telegram-agent-bridge` Scheduled Task exists, `doctor` still reports
its `schtasks /query` status; otherwise it reports whether the Startup launcher
is installed.

The Windows watchdog is a separate per-user Scheduled Task named
`telegram-agent-bridge-watchdog`. It runs `bridge_watchdog.py` every minute
without administrator rights. If the bridge process is missing, it first restarts
an older service Scheduled Task when present, otherwise it re-runs the Startup
launcher and writes the same status file.

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
- Native ConPTY P0 uses a current-user-only Windows named pipe plus a random
  capability stored in a current-user-only descriptor file. Do not copy,
  print, or share that descriptor. Prompt bodies and capabilities are excluded
  from host logs.
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
