#!/usr/bin/env python3
"""Watch and recover the local Telegram Agent Bridge service."""

from __future__ import annotations

import argparse
import os
import platform
import subprocess
import time
from pathlib import Path
from typing import Callable


APP_NAME = "telegram-agent-bridge"
SERVICE_NAME = "telegram-agent-bridge.service"
LAUNCHD_LABEL = "com.user.telegram-agent-bridge"

RunCommand = Callable[[list[str]], subprocess.CompletedProcess[str]]


def default_state_dir() -> Path:
    configured = os.environ.get("TAB_STATE_DIR")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".local" / "state" / APP_NAME


def default_status_file() -> Path:
    configured = os.environ.get("TAB_WATCHDOG_STATUS")
    if configured:
        return Path(configured).expanduser()
    return default_state_dir() / "watchdog.status"


def run_command(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True)


def status_text(proc: subprocess.CompletedProcess[str]) -> str:
    return (proc.stdout or proc.stderr or "").strip()


def launchd_state(output: str) -> str:
    for line in output.splitlines():
        stripped = line.strip()
        if stripped.startswith("state = "):
            return stripped.removeprefix("state = ").strip()
    return "unknown"


def write_status(status_file: Path, status: str, detail: str) -> None:
    status_file.parent.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y-%m-%d %H:%M:%S %z")
    status_file.write_text(
        "\n".join(
            [
                f"ts={stamp}",
                f"status={status}",
                f"detail={detail}",
                "",
            ]
        ),
        encoding="utf-8",
    )


def linux_is_active(service_name: str, run: RunCommand) -> bool:
    proc = run(["systemctl", "--user", "is-active", service_name])
    return proc.returncode == 0 and status_text(proc) == "active"


def watch_linux(
    *,
    service_name: str,
    status_file: Path,
    run: RunCommand,
    settle_seconds: float,
) -> int:
    if linux_is_active(service_name, run):
        write_status(status_file, "active", f"systemd:{service_name}")
        return 0

    start = run(["systemctl", "--user", "start", service_name])
    if settle_seconds > 0:
        time.sleep(settle_seconds)
    if linux_is_active(service_name, run):
        write_status(status_file, "recovered", f"systemd:{service_name}")
        return 0

    detail = status_text(start) or f"systemd:{service_name}:start-failed"
    write_status(status_file, "failed", detail)
    return 1


def mac_service_state(label: str, run: RunCommand) -> tuple[bool, str]:
    proc = run(["launchctl", "print", f"gui/{os.getuid()}/{label}"])
    if proc.returncode != 0:
        return False, "not-loaded"
    state = launchd_state(status_text(proc))
    return state == "running", state


def watch_macos(
    *,
    label: str,
    plist_file: Path,
    status_file: Path,
    run: RunCommand,
    settle_seconds: float,
) -> int:
    running, state = mac_service_state(label, run)
    if running:
        write_status(status_file, "active", f"launchd:{label}")
        return 0

    domain = f"gui/{os.getuid()}"
    if state == "not-loaded" and plist_file.exists():
        run(["launchctl", "bootstrap", domain, str(plist_file)])

    run(["launchctl", "kickstart", "-k", f"{domain}/{label}"])
    if settle_seconds > 0:
        time.sleep(settle_seconds)
    running, state = mac_service_state(label, run)
    if running:
        write_status(status_file, "recovered", f"launchd:{label}")
        return 0

    write_status(status_file, "failed", f"launchd:{label}:{state}")
    return 1


def watch_once(
    *,
    os_name: str | None = None,
    service_name: str = SERVICE_NAME,
    launchd_label: str = LAUNCHD_LABEL,
    launchd_plist: Path | None = None,
    status_file: Path | None = None,
    run: RunCommand = run_command,
    settle_seconds: float = 2.0,
) -> int:
    os_name = os_name or platform.system()
    status_file = status_file or default_status_file()
    if os_name == "Linux":
        return watch_linux(
            service_name=service_name,
            status_file=status_file,
            run=run,
            settle_seconds=settle_seconds,
        )
    if os_name == "Darwin":
        return watch_macos(
            label=launchd_label,
            plist_file=launchd_plist
            or Path.home() / "Library" / "LaunchAgents" / f"{launchd_label}.plist",
            status_file=status_file,
            run=run,
            settle_seconds=settle_seconds,
        )

    write_status(status_file, "unsupported", os_name)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Recover a stopped Telegram Agent Bridge service")
    parser.add_argument("--service-name", default=SERVICE_NAME)
    parser.add_argument("--launchd-label", default=LAUNCHD_LABEL)
    parser.add_argument("--launchd-plist", type=Path)
    parser.add_argument("--status-file", type=Path, default=default_status_file())
    parser.add_argument(
        "--settle-seconds",
        type=float,
        default=float(os.environ.get("TAB_WATCHDOG_SETTLE_SECONDS", "2")),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return watch_once(
        service_name=args.service_name,
        launchd_label=args.launchd_label,
        launchd_plist=args.launchd_plist,
        status_file=args.status_file,
        settle_seconds=args.settle_seconds,
    )


if __name__ == "__main__":
    raise SystemExit(main())
