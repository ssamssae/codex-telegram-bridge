# Codex Telegram Bridge 0.9.22

- Set up the bridge the way its maintainer runs it with `codex-telegram-bridge setup --like-author`. It turns on the live flow card with its Stop and send-now buttons, for prompts sent from your phone and for prompts typed in the Codex terminal, and drops the separate progress note that otherwise follows a slow phone prompt. It backs up an existing config before rewriting it, starts Codex in the bridge's tmux session when that session is missing (a running session is left as it is), and installs the usual background service and watchdog. Only creating the bot in @BotFather and sending /start still need you. The preset needs tmux and REPL mode (macOS, Linux, or WSL).
- The interactive wizard now asks first: `Use the author's setup (recommended)? [Y/n]`. Enter accepts it; answer `n` for the plain setup, which is unchanged. A run without a terminal to answer keeps the plain setup.
- After an author-preset install, a short self-check reports three lines in the terminal and in Telegram: bot to phone, phone to terminal and back (60 seconds), and terminal to phone (90 seconds). A line that times out says what to check and points to `codex-telegram-bridge doctor`.
- New `codex-tg` command, also available as `codex-telegram-bridge attach`: it attaches your terminal to the one Codex session the bridge controls and starts that session first when it is missing. A terminal that is already attached stays attached.
- `codex-telegram-bridge doctor` now warns when a Codex CLI runs outside the bridge's tmux session, because that window is not connected to your phone. When the session is missing, it also suggests `codex-tg`.
- `setup --dry-run` prints what setup would write and start without touching files, tmux, or Telegram.

Validation: public export with the undefined-name gate over every public module, private-data and token scans with two independent scanners, manifest parity, 433 tests on Python 3.14, twine check, and an isolated install that ran `codex-tg --help` and `setup --like-author --dry-run` from a neutral folder. Source bundle, wheel and source distribution are included.

This release is published on GitHub and PyPI. It does not restart an installed bridge.
