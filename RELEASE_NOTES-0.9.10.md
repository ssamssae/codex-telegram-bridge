# codex-telegram-bridge 0.9.10

Synchronizes the public bridge with the current maintained runtime.

- Adds persisted handling for asynchronous questions and verifies native question submission before reporting completion.
- Improves selection prompt recovery and stale callback handling.
- Uses a static activity notice to reduce cosmetic Telegram edits.
- Documents the suggested-reply display switch and its distinction from generation instructions.
- Registers the delivery regression suite in the public test manifest.

Existing credentials and terminal sessions remain compatible. GitHub release only; this release does not claim a PyPI upload.
