# 0.9.16 — Bilingual controls and reliable progress

## Added

- English/Korean bridge messages and buttons, with `/language en`, `/language ko`,
  and a persisted chat preference. Setup supports `--language`; prompts, model
  answers, code, and question choices retain their original text.
- A Stop button bound to the current progress card, live session, and native
  turn. Stale cards, drafts, pending questions, and duplicate clicks are rejected.
- An Apply now button for native pending-steer inputs after ten seconds. It uses
  Codex's own interrupt-and-send action and preserves receipt/queue tracking.
- Korean setup documentation and the existing English/Korean web guide.

## Fixed

- Keep the Telegram typing indicator alive during quiet native turns and slow
  screen reads; stop it after the matching completion or interruption.
- Recognize custom tool calls and narrow background-wait rows in progress cards.
- Restore Stop targets after session attachment without replaying an old action.
- Keep unsupported free-text answers on choice cards separate from ordinary
  messages; explain recovery and uncertain delivery without automatic resends.

Stop and Apply now require the POSIX tmux transport and live flow cards
(`CRB_FLOW_MIRROR=1`). They are unavailable in native Windows and `exec` mode.
WSL with tmux is supported. Existing clear acknowledgements remain included.

## Install this release

```bash
pipx install "git+https://github.com/ssamssae/codex-telegram-bridge.git@v0.9.16"
```

This is a GitHub source release. It does not publish to PyPI or restart existing
bridge installations.
