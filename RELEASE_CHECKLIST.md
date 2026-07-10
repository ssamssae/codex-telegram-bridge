# Release Checklist

Maintainer-only: the export script lives in the maintainer's private
automation repo. Forks can skip the export step and run the scans/tests
directly against their working tree.

- Run `scripts/codex-bridge-oss-export.sh` (private repo).
- Run the exported package test.
- Sync the public repo's `tests/` with any behavior change in this release —
  `tests/` is outside the export script's scope, so test ports are manual
  (v0.6.4 gap: 3 cases had to be ported by hand).
- Confirm the secret/internal scan has zero matches.
- Confirm `CRB_DIRECTIVE_SIGNAL_PATH` points to a public local-state path or is disabled.
- Build + validate the PyPI distribution before upload:

  ```bash
  python3 -m build dist/codex-telegram-bridge --outdir dist/codex-telegram-bridge/pypi-dist
  python3 -m twine check dist/codex-telegram-bridge/pypi-dist/*
  ```

  Expected: `twine check` reports `PASSED` for the sdist and the wheel.
- Public release (PyPI upload) requires the maintainer's PyPI API token and an
  explicit release gate:

  ```bash
  curl -s -o /dev/null -w '%{http_code}\n' https://pypi.org/pypi/codex-telegram-bridge/json  # 404 = unclaimed
  python3 -m twine upload dist/codex-telegram-bridge/pypi-dist/*
  ```

  The published name MUST equal `SELF_UPDATE_PACKAGE` in the bridge script, or the
  in-app update check (`pypi.org/pypi/<name>/json`) never sees the new version.
- Do not publish from the private operator repo without an explicit release gate.
