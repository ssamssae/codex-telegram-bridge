# Codex Telegram Bridge

Run Codex from Telegram, with local terminal input mirrored back to Telegram.
Generic one-shot command backends can also adapt Claude Code, Aider, Gemini CLI,
or your own terminal agent.

This is not MCP. It is a small standalone relay daemon:

```text
Telegram message
  -> getUpdates polling
  -> shared turn queue
  -> single-user chat_id allowlist
  -> <agent> exec one turn
  -> optional session resume
  -> final answer only
  -> Telegram sendMessage

Local terminal input
  -> stdin or FIFO
  -> same shared turn queue
  -> same resumable agent thread
  -> final answer to both Telegram and terminal
```

## Quickstart

1. Create a Telegram bot with [@BotFather](https://t.me/BotFather) and copy the bot token.
2. Find your numeric Telegram chat id. A common way is to send a message to your bot, then call `getUpdates` once with the bot token.
3. Install and log in to your terminal agent on the same machine. For Codex, make sure `codex exec` works in a terminal first.
4. Create a private env file:

```bash
cp config.example.env ~/.config/telegram-agent-bridge.env
chmod 600 ~/.config/telegram-agent-bridge.env
$EDITOR ~/.config/telegram-agent-bridge.env
```

5. Run the daemon:

```bash
set -a
. ~/.config/telegram-agent-bridge.env
set +a
python3 ~/telegram-agent-bridge/telegram_agent_bridge.py
```

Send `/ping` to the bot. Then send a normal prompt.

For local terminal input without scraping an agent TUI, either type into the
foreground bridge process or write one prompt per line to the FIFO:

```bash
printf '%s\n' 'continue from the terminal' > ~/.local/state/telegram-agent-bridge/input.fifo
```

Prompts and final answers are mirrored to both Telegram and terminal output.

## Configuration

Required settings are intentionally small and explicit.

| Variable | Required | Default | Description |
| --- | --- | --- | --- |
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
