# Release Checklist

Maintainer-only: the export script lives in the maintainer's private
automation repo. Forks can skip the export step and run the scans/tests
directly against their working tree.

- Run `scripts/codex-bridge-oss-export.sh` (private repo).
- Run the exported package test.
- Confirm the secret/internal scan has zero matches.
- Confirm `CRB_DIRECTIVE_SIGNAL_PATH` points to a public local-state path or is disabled.
- Do not publish from the private operator repo without an explicit release gate.
