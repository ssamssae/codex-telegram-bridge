# Codex Telegram Bridge 0.9.20

- After /effort, /clear waits up to 60 seconds, catches a confirmation prompt it used to miss, and answers it automatically.
- Replies sent to the chat no longer include decorative emoji.
- Local /clear command records are no longer read as model turns.
- After a reboot, a reused process ID no longer makes the bridge think its control channel cannot be opened.
- The session-start hook is registered in the user-scope settings.json, and a failed-submit notice now attaches a masked capture of the screen.
- A new Codex conversation is write-locked before its first question too.
- Multiple-choice questions can be answered on the phone. Tick the options on the card and move to the next field.

Validation: public export with the internal-symbol audit, private-data and token scans with two independent scanners, manifest parity, 392 tests on Python 3.14, twine check, and an installed-wheel import check from a neutral folder in an isolated environment. Source bundle, wheel and source distribution are included.

This release is published on GitHub and PyPI. It does not restart an installed bridge.
