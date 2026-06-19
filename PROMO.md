# Codex Telegram Bridge

Codex Telegram Bridge turns Telegram into a lightweight control plane for Codex CLI.

Why it is useful:

- Send a prompt from Telegram and get the final Codex answer back in Telegram.
- Keep a local terminal input path too, so a prompt typed on the machine can be mirrored back to Telegram.
- Preserve Codex `exec` session context across turns.
- Avoid webhooks and exposed ports; it uses Telegram polling.
- Run with only the Python standard library.

The first public release is intentionally small: one trusted chat id, one bot token, one local machine, one terminal agent turn at a time. That shape keeps the safety boundary easy to understand while still making Codex reachable from a phone.

Repository:
https://github.com/ssamssae/codex-telegram-bridge

Release:
https://github.com/ssamssae/codex-telegram-bridge/releases/latest

Install sketch:

```bash
pipx install "git+https://github.com/ssamssae/codex-telegram-bridge.git@v0.3.0"
codex-telegram-bridge setup
codex-telegram-bridge doctor
```

This is free and MIT licensed.
