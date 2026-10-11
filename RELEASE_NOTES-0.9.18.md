# Codex Telegram Bridge 0.9.18

- Show Codex's Other choice as the last button on question cards. It opens a reply prompt for a typed answer, with a cancel button.
- Enter typed answers into Codex's Other input, including the inline Other input in Codex 0.154, only after re-checking the question, every option, the selected Other row and an empty input. A changed screen is not submitted.
- Mark the original card "Answer delivered" with the chosen row checked. A failed card edit never re-sends the answer.
- Ignore replies to cancelled or replaced input requests. A typed reply sent while the bridge is paused stays pending instead of being dropped.

Validation: public export, private-data/token scan, manifest parity, and 328 tests on Python 3.10 and 3.14. Source bundle, wheel and source distribution are included.

This is a GitHub release. It does not upload to PyPI or restart an installed bridge.
