#!/usr/bin/env python3
"""Watch and recover the local Telegram Agent Bridge service."""

from __future__ import annotations

import argparse
import os
import platform
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable


APP_NAME = "telegram-agent-bridge"
SERVICE_NAME = "telegram-agent-bridge.service"
LAUNCHD_LABEL = "com.user.telegram-agent-bridge"
WINDOWS_TASK_NAME = APP_NAME
WINDOWS_BRIDGE_PROCESS_PATTERN = (
    r"telegram_agent_bridge\.py|codex_repl_bridge\.py|telegram-agent-bridge-run|"
    r"(?:^|\s)-m\s+(?:codex_repl_bridge|telegram_agent_bridge)\b"
)

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
    kwargs: dict[str, object] = {"capture_output": True, "text": True}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    return subprocess.run(cmd, **kwargs)


def status_text(proc: subprocess.CompletedProcess[str]) -> str:
    return (proc.stdout or proc.stderr or "").strip()


def default_windows_startup_dir() -> Path:
    appdata = os.environ.get("APPDATA")
    if appdata:
        return (
            Path(appdata)
            / "Microsoft"
            / "Windows"
            / "Start Menu"
            / "Programs"
            / "Startup"
        )
    return (
        Path.home()
        / "AppData"
        / "Roaming"
        / "Microsoft"
        / "Windows"
        / "Start Menu"
        / "Programs"
        / "Startup"
    )


def default_windows_startup_launcher_file(
    startup_dir: Path | None = None,
    *,
    app_name: str = APP_NAME,
) -> Path:
    return (startup_dir or default_windows_startup_dir()) / f"{app_name}.bat"


def powershell_single_quote(value: str | Path) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def windows_startup_launcher_start_command(launcher_file: Path) -> list[str]:
    script = f"Start-Process -WindowStyle Hidden -FilePath {powershell_single_quote(launcher_file)}"
    return [
        "powershell.exe",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-Command",
        script,
    ]


def parse_windows_scheduled_task_status(output: str) -> str:
    for line in output.replace("\r\n", "\n").splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        if key.strip().lower() == "status":
            return value.strip() or "unknown"
    return "unknown"


def windows_scheduled_task_status(*, task_name: str, run: RunCommand) -> str:
    proc = run(["schtasks", "/Query", "/TN", task_name, "/FO", "LIST", "/V"])
    if proc.returncode != 0:
        return "not-installed"
    return parse_windows_scheduled_task_status(proc.stdout or proc.stderr or "")


def windows_bridge_process_running(run: RunCommand) -> bool:
    script = (
        "$ErrorActionPreference='SilentlyContinue'; "
        "$p = Get-CimInstance Win32_Process | Where-Object { "
        "$_.ProcessId -ne $PID -and $_.CommandLine -and "
        f"$_.CommandLine -match '{WINDOWS_BRIDGE_PROCESS_PATTERN}' "
        "}; "
        "if ($p) { 'running'; exit 0 } else { 'missing'; exit 1 }"
    )
    proc = run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script])
    return proc.returncode == 0


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


def mac_gui_domain() -> str:
    getuid = getattr(os, "getuid", None)
    uid = getuid() if callable(getuid) else 0
    return f"gui/{uid}"


def mac_service_state(label: str, run: RunCommand) -> tuple[bool, str]:
    proc = run(["launchctl", "print", f"{mac_gui_domain()}/{label}"])
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

    domain = mac_gui_domain()
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


def watch_windows(
    *,
    service_task_name: str,
    startup_launcher: Path | None,
    status_file: Path,
    run: RunCommand,
    settle_seconds: float,
) -> int:
    if windows_bridge_process_running(run):
        write_status(status_file, "active", "windows-process")
        return 0

    task_status = windows_scheduled_task_status(task_name=service_task_name, run=run)
    if task_status != "not-installed":
        start = run(["schtasks", "/Run", "/TN", service_task_name])
        if settle_seconds > 0:
            time.sleep(settle_seconds)
        if windows_bridge_process_running(run):
            write_status(status_file, "recovered", f"windows-task:{service_task_name}")
            return 0
        detail = status_text(start) or f"windows-task:{service_task_name}:{task_status}:start-failed"
        write_status(status_file, "failed", detail)
        return 1

    launcher = startup_launcher or default_windows_startup_launcher_file()
    if launcher.exists():
        start = run(windows_startup_launcher_start_command(launcher))
        if settle_seconds > 0:
            time.sleep(settle_seconds)
        if windows_bridge_process_running(run):
            write_status(status_file, "recovered", f"windows-startup:{launcher}")
            return 0
        detail = status_text(start) or f"windows-startup:{launcher}:process-not-detected"
        write_status(status_file, "failed", detail)
        return 1

    write_status(status_file, "failed", "windows:not-installed")
    return 1


def watch_once(
    *,
    os_name: str | None = None,
    service_name: str = SERVICE_NAME,
    launchd_label: str = LAUNCHD_LABEL,
    launchd_plist: Path | None = None,
    windows_service_task_name: str = WINDOWS_TASK_NAME,
    windows_startup_launcher: Path | None = None,
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
    if os_name == "Windows":
        return watch_windows(
            service_task_name=windows_service_task_name,
            startup_launcher=windows_startup_launcher,
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
