# Codex Telegram Bridge — Run Codex from Telegram

Codex Telegram Bridge turns Telegram into a phone remote for your local Codex CLI.

Why it is useful:

- Send a prompt from Telegram and get the final Codex answer back in Telegram.
- Send screenshots, videos, and voice notes without leaving Telegram.
- Keep a local terminal input path too, so a prompt typed on the machine can be mirrored back to Telegram.
- Keep the visible Codex REPL transcript readable while Telegram mirrors final answers.
- Avoid webhooks and exposed ports; it uses Telegram polling.
- Run with only the Python standard library.

The product is Codex-first today, with an adapter foundation for other terminal AI agents later. The safety boundary stays simple: one trusted chat id, one bot token, one local machine, and no public web server.

Repository:
https://github.com/ssamssae/codex-telegram-bridge

Release:
https://github.com/ssamssae/codex-telegram-bridge/releases/latest

Install sketch:

```bash
pipx install "git+https://github.com/ssamssae/codex-telegram-bridge.git@v0.3.3"
codex-telegram-bridge setup
codex-telegram-bridge doctor
```

This is free and MIT licensed.
