# Release Checklist

Maintainer-only: the export script lives in the maintainer's private
automation repo. Forks can skip the export step and run the scans/tests
directly against their working tree.

- Run `scripts/codex-bridge-oss-export.sh` (private repo).
- Run the exported package test — `scripts/tests/test_codex_bridge_oss_export.sh`
  runs the full exported suite via `unittest discover`, exactly as the public CI.
- `PUBLIC_TESTS.manifest` (packaging/codex-telegram-bridge) is the single source
  of truth for the public `tests/` directory; the export self-test asserts the
  exported tests/ equals it exactly (T-260721-003). Only `test_agent_runtime.py`
  is hand-maintained (POSIX FIFO / Windows process-check platform guards) — update
  it by hand on behavior change. Every other public test is export-produced and
  cannot drift. Never hand-edit a test directly in the public repo.
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
