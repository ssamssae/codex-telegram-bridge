# Codex Telegram Bridge 0.9.21

- Unanswered questions stay pinned. When Codex asks you a question through the bridge, the question card is pinned quietly in your private chat until you answer it, so new progress cards cannot bury it. Once you answer, or the question expires or the session changes, the bridge unpins that card. Your other pinned messages are left alone. Pinning is only a visual anchor: it never answers a question and never grants a permission.
- Pinning is bounded. The bridge makes at most one pin or unpin call per check, retries a failed call at most three times a minute apart, keeps that state across restarts, and never resends the question card.
- The received-directive card now records in the bridge log whether its first send went out, the message id it received, or why the send failed, so a missing card can be told apart from a missing log line.

Validation: public export with the internal-symbol audit, private-data and token scans with two independent scanners, manifest parity, 409 tests on Python 3.14, twine check, and an installed-wheel import check from a neutral folder in an isolated environment. Source bundle, wheel and source distribution are included.

This release is published on GitHub and PyPI. It does not restart an installed bridge.
