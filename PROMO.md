# Codex Telegram Bridge - Control your live Codex CLI from Telegram

Codex Telegram Bridge turns Telegram into a phone remote for your already-running
Codex TUI.

Why it is useful:

- Send a prompt from Telegram and get the final Codex answer back on your phone.
- Send screenshots, videos, and voice notes without leaving Telegram.
- Keep the visible tmux Codex transcript as the source of truth.
- Mirror terminal-origin prompts too, so Telegram stays informed even when work starts locally.
- Handle visible Codex approvals and selection prompts with Telegram buttons.
- Backfill the latest eligible final answer after a bridge service restart.
- Avoid webhooks and exposed ports; it uses Telegram polling.
- Run the core bridge with only the Python standard library.

This is REPL sync, not a separate hidden chat. Telegram controls the same Codex
session you can see in the terminal.

The product is Codex-first today, with an adapter foundation for other terminal
AI agents later. The safety boundary stays simple: one trusted chat id, one bot
token, one local machine, and no public web server.

Repository:
https://github.com/ssamssae/codex-telegram-bridge

Release:
https://github.com/ssamssae/codex-telegram-bridge/releases/latest

Install sketch:

```bash
pipx install "git+https://github.com/ssamssae/codex-telegram-bridge.git@v0.3.10"
codex-telegram-bridge setup
codex-telegram-bridge doctor
```

This is free and MIT licensed.
