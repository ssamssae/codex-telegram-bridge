# Codex Telegram Bridge 0.9.19

- /clear identity confirmation is more robust. It accepts the YOLO permissions header, the native reset logo and cropped alternate screens, reads the tall cropped /status card, and confirms against the active session IDs.
- Narrow clear identity and long typing states are recovered instead of leaving the bridge waiting.
- A failed /clear probe keeps a masked screen capture next to the log, with 13 screen regression cases.
- Question panel wording and TUI marker patterns are consolidated into single vocabulary blocks. No behaviour change.
- AI handoff cards are unified and queued input is received.

Validation: public export, private-data/token scan with a positive control, manifest parity, and 392 tests on Python 3.14. Source bundle, wheel and source distribution are included.

This release is published on GitHub and PyPI. It does not restart an installed bridge.
