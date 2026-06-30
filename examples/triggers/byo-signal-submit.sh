#!/usr/bin/env bash
set -euo pipefail

signal_path="${CRB_SIGNAL_PATH:-${TAB_LOCAL_INPUT:-$HOME/.local/state/telegram-agent-bridge/input.fifo}}"

if [[ $# -eq 0 ]]; then
  printf 'usage: CRB_SIGNAL_PATH=%s %s "prompt for Codex"\n' "$signal_path" "$0" >&2
  exit 2
fi

if [[ ! -p "$signal_path" ]]; then
  printf 'signal FIFO not found: %s\n' "$signal_path" >&2
  printf 'start codex-telegram-bridge with CRB_SIGNAL_PATH pointing to this FIFO first.\n' >&2
  exit 1
fi

prompt="$*"

python3 - "$prompt" <<'PY' > "$signal_path"
import json
import sys

print(
    json.dumps(
        {
            "prompt": sys.argv[1],
            "source": "byo-signal-submit",
        },
        ensure_ascii=False,
    ),
    flush=True,
)
PY
