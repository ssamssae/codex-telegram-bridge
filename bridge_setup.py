#!/usr/bin/env python3
"""Setup, doctor, and uninstall helper for telegram-agent-bridge."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import platform
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


APP_NAME = "telegram-agent-bridge"
SERVICE_NAME = "telegram-agent-bridge.service"
LAUNCHD_LABEL = "com.user.telegram-agent-bridge"
WINDOWS_TASK_NAME = APP_NAME
WINDOWS_WATCHDOG_TASK_NAME = f"{APP_NAME}-watchdog"
WATCHDOG_SERVICE_NAME = "telegram-agent-bridge-watchdog.service"
WATCHDOG_TIMER_NAME = "telegram-agent-bridge-watchdog.timer"
WATCHDOG_LAUNCHD_LABEL = "com.user.telegram-agent-bridge-watchdog"
REPO_DIR = Path(__file__).resolve().parent
EXEC_BRIDGE_SCRIPT = REPO_DIR / "telegram_agent_bridge.py"
REPL_BRIDGE_SCRIPT = REPO_DIR / "codex_repl_bridge.py"
AUDIO_TRANSCRIBE_SCRIPT = REPO_DIR / "codex_audio_transcribe.py"
WATCHDOG_SCRIPT = REPO_DIR / "bridge_watchdog.py"
SETUP_TOTAL_STEPS = 6
WINDOWS_BATCH_EXTENSIONS = {".bat", ".cmd"}


class SetupError(RuntimeError):
    """Expected setup error."""


@dataclass(frozen=True)
class WindowsBashHost:
    kind: str
    executable: str
    distro: str | None = None


@dataclass(frozen=True)
class AgentCommandCheck:
    ok: bool
    message: str


def is_windows_platform(os_name: str | None = None) -> bool:
    return (os_name or platform.system()) == "Windows" or os.name == "nt" or sys.platform == "win32"


def agent_command_spawn_check(
    agent_cmd: str,
    *,
    os_name: str | None = None,
    which: Callable[[str], str | None] | None = None,
    environ: dict[str, str] | None = None,
) -> AgentCommandCheck:
    which = which or shutil.which
    try:
        first = shlex.split(agent_cmd)[0]
    except (ValueError, IndexError):
        return AgentCommandCheck(False, f"Codex command is invalid: {agent_cmd}")

    resolved = which(first)
    if not resolved:
        return AgentCommandCheck(False, f"Codex command not found on PATH: {agent_cmd}")

    if is_windows_platform(os_name) and Path(resolved).suffix.lower() in WINDOWS_BATCH_EXTENSIONS:
        env_map = environ or os.environ
        cmd_exe = env_map.get("COMSPEC") or which("cmd.exe") or which("cmd")
        if not cmd_exe:
            return AgentCommandCheck(
                False,
                f"Codex command is a Windows batch shim but cmd.exe was not found: {resolved}",
            )
        return AgentCommandCheck(True, f"Codex command spawnable: {first} -> {resolved} via {cmd_exe}")

    return AgentCommandCheck(True, f"Codex command spawnable: {first} -> {resolved}")


def expand_path(raw: str | Path) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(str(raw)))).resolve()


def default_config_file() -> Path:
    return Path.home() / ".config" / f"{APP_NAME}.env"


def default_runner_file() -> Path:
    return Path.home() / ".local" / "bin" / f"{APP_NAME}-run"


def default_state_dir() -> Path:
    return Path.home() / ".local" / "state" / APP_NAME


def default_systemd_unit_file() -> Path:
    return Path.home() / ".config" / "systemd" / "user" / SERVICE_NAME


def default_watchdog_systemd_service_file() -> Path:
    return Path.home() / ".config" / "systemd" / "user" / WATCHDOG_SERVICE_NAME


def default_watchdog_systemd_timer_file() -> Path:
    return Path.home() / ".config" / "systemd" / "user" / WATCHDOG_TIMER_NAME


def default_launchd_plist_file() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"


def default_watchdog_launchd_plist_file() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{WATCHDOG_LAUNCHD_LABEL}.plist"


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


def out(message: str = "") -> None:
    print(message, flush=True)


def warn(message: str) -> None:
    out(f"[warn] {message}")


def fail(message: str) -> None:
    out(f"[fail] {message}")


def ok(message: str) -> None:
    out(f"[ok] {message}")


def setup_step(number: int, title: str) -> None:
    out("")
    out(f"[{number}/{SETUP_TOTAL_STEPS}] {title}")


def setup_note(message: str) -> None:
    out(f"    {message}")


def setup_command(command: str) -> None:
    out(f"      {command}")


def doctor_command_hint() -> str:
    invoked = Path(sys.argv[0]).name
    if invoked == "bridge_setup.py":
        python_cmd = windows_command_quote(sys.executable or "python") if platform.system() == "Windows" else "python3"
        return f"{python_cmd} bridge_setup.py doctor"
    if invoked in {"codex-telegram-bridge", "telegram-agent-bridge"}:
        return f"{invoked} doctor"
    return "codex-telegram-bridge doctor"


def setup_command_hint(*, mode: str | None = None) -> str:
    invoked = Path(sys.argv[0]).name
    if invoked in {"codex-telegram-bridge", "telegram-agent-bridge"}:
        command = f"{invoked} setup"
    elif platform.system() == "Windows":
        command = f"{windows_command_quote(sys.executable or 'python')} bridge_setup.py setup"
    else:
        command = "python bridge_setup.py setup"
    if mode:
        command += f" --mode {mode}"
    return command


def shell_quote(value: str | Path | int | bool) -> str:
    return shlex.quote(str(value))


def file_mode(path: Path) -> int | None:
    try:
        return stat.S_IMODE(path.stat().st_mode)
    except OSError:
        return None


def chmod_private(path: Path) -> None:
    try:
        path.chmod(0o600)
    except OSError:
        pass


def write_text_atomic(path: Path, value: str, mode: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(value, encoding="utf-8", newline="\n")
    if mode is not None:
        tmp.chmod(mode)
    tmp.replace(path)
    if mode is not None:
        path.chmod(mode)


def load_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return values

    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, raw_value = stripped.split("=", 1)
        key = key.strip()
        raw_value = raw_value.strip()
        try:
            parts = shlex.split(raw_value)
            value = parts[0] if parts else ""
        except ValueError:
            value = raw_value.strip("'\"")
        if key:
            values[key] = value
    return values


def telegram_call(
    token: str,
    method: str,
    timeout: int = 60,
    **params: Any,
) -> dict[str, Any] | None:
    if "timeout_param" in params:
        params["timeout"] = params.pop("timeout_param")
    data = urllib.parse.urlencode(params).encode()
    url = f"https://api.telegram.org/bot{token}/{method}"
    request = urllib.request.Request(url, data=data)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.load(response)
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


ApiCall = Callable[..., dict[str, Any] | None]


def validate_bot_token(token: str, api_call: ApiCall = telegram_call) -> str:
    payload = api_call(token, "getMe", timeout=30)
    if not payload or not payload.get("ok"):
        raise SetupError("Telegram token validation failed")
    result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
    username = str(result.get("username") or "").strip()
    if not username:
        raise SetupError("Telegram getMe returned no bot username")
    return username


def current_update_offset(token: str, api_call: ApiCall = telegram_call) -> int:
    payload = api_call(token, "getUpdates", timeout=10, timeout_param=0)
    if not payload or not payload.get("ok"):
        return 0
    updates = payload.get("result")
    if not isinstance(updates, list) or not updates:
        return 0
    max_id = 0
    for update in updates:
        if isinstance(update, dict) and isinstance(update.get("update_id"), int):
            max_id = max(max_id, int(update["update_id"]))
    return max_id + 1 if max_id else 0


def extract_chat_id(update: dict[str, Any]) -> tuple[str, str] | None:
    message = update.get("message") or update.get("edited_message")
    if not isinstance(message, dict):
        return None
    chat = message.get("chat") if isinstance(message.get("chat"), dict) else {}
    chat_id = chat.get("id")
    if chat_id is None:
        return None

    label_parts = []
    first_name = chat.get("first_name")
    last_name = chat.get("last_name")
    username = chat.get("username")
    if first_name:
        label_parts.append(str(first_name))
    if last_name:
        label_parts.append(str(last_name))
    label = " ".join(label_parts).strip()
    if username:
        label = f"{label} (@{username})".strip()
    return str(chat_id), label or str(chat_id)


def wait_for_chat_id(
    token: str,
    offset: int = 0,
    timeout_seconds: int = 180,
    api_call: ApiCall = telegram_call,
) -> tuple[str, str]:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        payload = api_call(token, "getUpdates", timeout=20, offset=offset, timeout_param=10)
        if not payload or not payload.get("ok"):
            time.sleep(2)
            continue
        updates = payload.get("result")
        if not isinstance(updates, list):
            time.sleep(2)
            continue
        for update in updates:
            if not isinstance(update, dict):
                continue
            if isinstance(update.get("update_id"), int):
                offset = int(update["update_id"]) + 1
            extracted = extract_chat_id(update)
            if extracted:
                return extracted
    raise SetupError(
        "Timed out waiting for /start. Open your bot chat in Telegram, send /start, "
        "and keep this terminal setup running."
    )


def service_is_running(status: str | None) -> bool:
    return (status or "").strip().lower() in {"active", "running"}


def send_test_message(
    token: str,
    chat_id: str,
    api_call: ApiCall = telegram_call,
    *,
    service_status_text: str | None = None,
    start_command: str | None = None,
) -> bool:
    if service_status_text and service_is_running(service_status_text):
        text = "telegram-agent-bridge setup complete. Background service is running. Send /ping to test the bridge."
    elif start_command:
        status = service_status_text or "unknown"
        text = (
            "telegram-agent-bridge setup complete. Background service is not running "
            f"({status}). Start it with: {start_command}. Then send /ping to test the bridge."
        )
    else:
        text = "telegram-agent-bridge setup complete. Send /ping to test the bridge."
    payload = api_call(
        token,
        "sendMessage",
        timeout=30,
        chat_id=chat_id,
        text=text,
    )
    return bool(payload and payload.get("ok"))


def write_env_config(
    path: Path,
    *,
    mode: str,
    token: str,
    chat_id: str,
    agent: str,
    agent_cmd: str,
    workdir: Path,
    prefix: str,
    prefix_line: bool,
    state_dir: Path,
    local_input: Path,
    dangerous_bypass: bool,
    tmux_socket: str,
    tmux_session: str,
    submit_key: str,
    audio_transcribe_cmd: str,
) -> None:
    if agent != "codex":
        raise SetupError("agent supports only codex")
    local_input_value = "off" if is_windows_platform() else str(local_input)
    codex_extra_args = "--skip-git-repo-check" if mode == "exec" else ""
    lines = [
        "# telegram-agent-bridge private config",
        "# Keep this file out of git. It contains your Telegram bot token.",
        f"TAB_BRIDGE_MODE={shell_quote(mode)}",
        f"TAB_BOT_TOKEN={shell_quote(token)}",
        f"TAB_CHAT_ID={shell_quote(chat_id)}",
        f"TAB_AGENT={shell_quote(agent)}",
        f"TAB_AGENT_CMD={shell_quote(agent_cmd)}",
        f"TAB_PREFIX={shell_quote(prefix)}",
        f"TAB_PREFIX_LINE={'1' if prefix_line else '0'}",
        f"TAB_STATE_DIR={shell_quote(state_dir)}",
        f"TAB_WORKDIR={shell_quote(workdir)}",
        "TAB_TIMEOUT=600",
        "TAB_TG_CHUNK=4096",
        "TAB_TYPING_INTERVAL=4",
        f"TAB_LOCAL_INPUT={shell_quote(local_input_value)}",
        "TAB_STDIN_INPUT=0",
        f"TAB_CODEX_DANGEROUS_BYPASS={'1' if dangerous_bypass else '0'}",
        f"TAB_CODEX_EXTRA_ARGS={shell_quote(codex_extra_args)}",
    ]
    if mode == "repl":
        lines.extend(
            [
                "",
                "# REPL mode: paste Telegram prompts into an existing Codex TUI tmux session",
                "# and mirror final answers from Codex JSONL session logs.",
                "# BYO signal: local FIFO for cron/webhook/task-queue prompt injection.",
                f"CRB_SIGNAL_PATH={shell_quote(local_input_value)}",
                f"CRB_TMUX_SOCKET={shell_quote(tmux_socket)}",
                f"CRB_TMUX_SESSION={shell_quote(tmux_session)}",
                f"CRB_TMUX_SUBMIT_KEY={shell_quote(submit_key)}",
                "CRB_START_AT_END=1",
                "CRB_IMAGE_MODE=repl",
                "CRB_FLOW_MIRROR=1",
                "CRB_LONG_RUNNING_PROGRESS_SECONDS=0",
                f"CRB_AUDIO_TRANSCRIBE_CMD={shell_quote(audio_transcribe_cmd)}",
            ]
        )
    lines.append("")
    content = "\n".join(lines)
    write_text_atomic(path, content, mode=0o600)


def python_runner_content(env_file: Path) -> str:
    return "\n".join(
        [
            "#!/usr/bin/env python3",
            "import os",
            "import shlex",
            "import subprocess",
            "import sys",
            "from pathlib import Path",
            "",
            f"ENV_FILE = Path({str(env_file)!r})",
            f"REPL_SCRIPT = Path({str(REPL_BRIDGE_SCRIPT)!r})",
            f"EXEC_SCRIPT = Path({str(EXEC_BRIDGE_SCRIPT)!r})",
            "",
            "if not ENV_FILE.exists():",
            "    print(f\"config file not found: {ENV_FILE}\", file=sys.stderr)",
            "    raise SystemExit(2)",
            "",
            "for raw_line in ENV_FILE.read_text(encoding='utf-8').splitlines():",
            "    line = raw_line.strip()",
            "    if not line or line.startswith('#') or '=' not in line:",
            "        continue",
            "    key, raw_value = line.split('=', 1)",
            "    key = key.strip()",
            "    raw_value = raw_value.strip()",
            "    try:",
            "        parts = shlex.split(raw_value)",
            "        value = parts[0] if parts else ''",
            "    except ValueError:",
            "        value = raw_value.strip(\"'\\\"\")",
            "    if key:",
            "        os.environ[key] = value",
            "",
            "mode = os.environ.get('TAB_BRIDGE_MODE', 'repl')",
            "if mode == 'repl':",
            "    script = REPL_SCRIPT",
            "elif mode == 'exec':",
            "    script = EXEC_SCRIPT",
            "else:",
            "    print(f\"unknown TAB_BRIDGE_MODE: {mode}\", file=sys.stderr)",
            "    raise SystemExit(2)",
            "",
            "os.environ.setdefault('PYTHONUNBUFFERED', '1')",
            "os.environ.setdefault('PYTHONUTF8', '1')",
            "raise SystemExit(subprocess.call([sys.executable, str(script)]))",
            "",
        ]
    )


def install_runner(runner_file: Path, env_file: Path, *, os_name: str | None = None) -> None:
    os_name = os_name or platform.system()
    if os_name == "Windows":
        write_text_atomic(runner_file, python_runner_content(env_file), mode=0o755)
        return

    runner = "\n".join(
        [
            "#!/usr/bin/env bash",
            "set -euo pipefail",
            f"ENV_FILE={shell_quote(env_file)}",
            "if [ ! -f \"$ENV_FILE\" ]; then",
            "  echo \"config file not found: $ENV_FILE\" >&2",
            "  exit 2",
            "fi",
            "set -a",
            ". \"$ENV_FILE\"",
            "set +a",
            "case \"${TAB_BRIDGE_MODE:-repl}\" in",
            f"  repl) SCRIPT={shell_quote(REPL_BRIDGE_SCRIPT)} ;;",
            f"  exec) SCRIPT={shell_quote(EXEC_BRIDGE_SCRIPT)} ;;",
            "  *) echo \"unknown TAB_BRIDGE_MODE: ${TAB_BRIDGE_MODE}\" >&2; exit 2 ;;",
            "esac",
            f"exec {shell_quote(sys.executable or 'python3')} \"$SCRIPT\"",
            "",
        ]
    )
    write_text_atomic(runner_file, runner, mode=0o755)


def default_asr_tool_dir() -> Path:
    return Path.home() / ".local" / "share" / APP_NAME / "asr-py"


def install_asr_dependencies(tool_dir: Path | None = None) -> bool:
    tool_dir = tool_dir or default_asr_tool_dir()
    python_bin = sys.executable or shutil.which("python3") or "python3"
    check = subprocess.run(
        [
            python_bin,
            "-c",
            "import faster_whisper, imageio_ffmpeg",
        ],
        env={**os.environ, "PYTHONPATH": str(tool_dir)},
        capture_output=True,
        text=True,
    )
    if check.returncode == 0:
        return True
    pip = subprocess.run(
        [python_bin, "-m", "pip", "--version"],
        capture_output=True,
        text=True,
    )
    if pip.returncode != 0:
        warn("pip is unavailable; audio files will be received but not transcribed")
        return False
    tool_dir.mkdir(parents=True, exist_ok=True)
    out(f"Installing optional audio transcription dependencies into {tool_dir}")
    proc = subprocess.run(
        [
            python_bin,
            "-m",
            "pip",
            "install",
            "--quiet",
            "--target",
            str(tool_dir),
            "faster-whisper",
            "imageio-ffmpeg",
        ],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        warn("ASR dependency install failed; audio files will be received but not transcribed")
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        if detail:
            warn(detail[-1][:200])
        return False
    return True


def systemd_unit_content(runner_file: Path) -> str:
    return "\n".join(
        [
            "[Unit]",
            "Description=Telegram Agent Bridge",
            "After=network-online.target",
            "Wants=network-online.target",
            "",
            "[Service]",
            "Type=simple",
            "Environment=PYTHONUNBUFFERED=1",
            "Environment=PATH=%h/.local/bin:%h/.npm-global/bin:/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin",
            f"ExecStart={runner_file}",
            "Restart=always",
            "RestartSec=5",
            "",
            "[Install]",
            "WantedBy=default.target",
            "",
        ]
    )


def launchd_plist_content(runner_file: Path) -> str:
    log_file = Path("/tmp") / f"{APP_NAME}.log"
    return "\n".join(
        [
            '<?xml version="1.0" encoding="UTF-8"?>',
            '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
            '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">',
            '<plist version="1.0">',
            '<dict>',
            '    <key>Label</key>',
            f'    <string>{LAUNCHD_LABEL}</string>',
            '    <key>ProgramArguments</key>',
            '    <array>',
            f'        <string>{runner_file}</string>',
            '    </array>',
            '    <key>RunAtLoad</key>',
            '    <true/>',
            '    <key>KeepAlive</key>',
            '    <true/>',
            '    <key>StandardOutPath</key>',
            f'    <string>{log_file}</string>',
            '    <key>StandardErrorPath</key>',
            f'    <string>{log_file}</string>',
            '</dict>',
            '</plist>',
            '',
        ]
    )


def watchdog_python_command() -> str:
    python_bin = sys.executable or shutil.which("python3") or "python3"
    return f"{shell_quote(python_bin)} {shell_quote(WATCHDOG_SCRIPT)}"


def watchdog_systemd_service_content() -> str:
    return "\n".join(
        [
            "[Unit]",
            "Description=Telegram Agent Bridge watchdog",
            "After=network-online.target",
            "Wants=network-online.target",
            "",
            "[Service]",
            "Type=oneshot",
            "Environment=PYTHONUNBUFFERED=1",
            "Environment=PATH=%h/.local/bin:%h/.npm-global/bin:/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin",
            f"ExecStart={watchdog_python_command()}",
            "",
        ]
    )


def watchdog_systemd_timer_content() -> str:
    return "\n".join(
        [
            "[Unit]",
            "Description=Run Telegram Agent Bridge watchdog",
            "",
            "[Timer]",
            "OnBootSec=30s",
            "OnUnitActiveSec=60s",
            "AccuracySec=15s",
            f"Unit={WATCHDOG_SERVICE_NAME}",
            "",
            "[Install]",
            "WantedBy=timers.target",
            "",
        ]
    )


def watchdog_launchd_plist_content() -> str:
    log_file = Path("/tmp") / f"{APP_NAME}-watchdog.log"
    python_bin = sys.executable or shutil.which("python3") or "python3"
    return "\n".join(
        [
            '<?xml version="1.0" encoding="UTF-8"?>',
            '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
            '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">',
            '<plist version="1.0">',
            '<dict>',
            '    <key>Label</key>',
            f'    <string>{WATCHDOG_LAUNCHD_LABEL}</string>',
            '    <key>ProgramArguments</key>',
            '    <array>',
            f'        <string>{python_bin}</string>',
            f'        <string>{WATCHDOG_SCRIPT}</string>',
            '    </array>',
            '    <key>RunAtLoad</key>',
            '    <true/>',
            '    <key>StartInterval</key>',
            '    <integer>60</integer>',
            '    <key>StandardOutPath</key>',
            f'    <string>{log_file}</string>',
            '    <key>StandardErrorPath</key>',
            f'    <string>{log_file}</string>',
            '</dict>',
            '</plist>',
            '',
        ]
    )


def run_command(cmd: list[str], check: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, check=check)


def command_output(proc: subprocess.CompletedProcess[str]) -> str:
    return (proc.stdout or proc.stderr or "").strip()


def powershell_single_quote(value: str | Path) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def windows_path_for_wsl(path: Path) -> str:
    raw = str(path).replace("\\", "/")
    if len(raw) >= 2 and raw[1] == ":" and raw[0].isalpha():
        tail = raw[2:]
        if not tail.startswith("/"):
            tail = "/" + tail
        return f"/mnt/{raw[0].lower()}{tail}"
    return raw


def windows_path_for_git_bash(path: Path) -> str:
    raw = str(path).replace("\\", "/")
    if len(raw) >= 2 and raw[1] == ":" and raw[0].isalpha():
        tail = raw[2:]
        if not tail.startswith("/"):
            tail = "/" + tail
        return f"/{raw[0].lower()}{tail}"
    return raw


def parse_wsl_distro_name(output: str) -> str | None:
    cleaned = output.replace("\x00", "")
    for line in cleaned.splitlines():
        distro = line.strip()
        if distro:
            return distro
    return None


def detect_windows_bash_host(
    *,
    run: Callable[..., subprocess.CompletedProcess[str]] = run_command,
    environ: dict[str, str] | None = None,
    which: Callable[[str], str | None] = shutil.which,
) -> WindowsBashHost:
    environ = environ or os.environ
    wsl_exe = which("wsl.exe") or which("wsl")
    if wsl_exe:
        distro = environ.get("WSL_DISTRO_NAME", "").strip()
        if not distro:
            proc = run([wsl_exe, "-l", "-q"])
            if proc.returncode == 0:
                distro = parse_wsl_distro_name(proc.stdout or proc.stderr or "") or ""
        return WindowsBashHost(kind="wsl", executable=wsl_exe, distro=distro or None)

    bash_exe = which("bash.exe") or which("bash")
    if bash_exe:
        return WindowsBashHost(kind="git-bash", executable=bash_exe)

    raise SetupError("Windows service install requires WSL or Git Bash on PATH")


def powershell_hidden_start_action(executable: str, args: list[str]) -> str:
    command, arguments = windows_scheduled_task_exec(executable, args)
    return f"{command} {arguments}"


def windows_scheduled_task_exec(executable: str, args: list[str]) -> tuple[str, str]:
    quoted_args = ", ".join(powershell_single_quote(arg) for arg in args)
    script = f"Start-Process -WindowStyle Hidden -FilePath {powershell_single_quote(executable)}"
    if quoted_args:
        script += f" -ArgumentList @({quoted_args})"
    script_arg = '"' + script.replace('"', '`"') + '"'
    return "powershell.exe", f"-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -Command {script_arg}"


def windows_scheduled_task_action(host: WindowsBashHost, runner_file: Path) -> str:
    command, arguments = windows_scheduled_task_command(host, runner_file)
    return f"{command} {arguments}"


def windows_bash_host_runner_command(host: WindowsBashHost, runner_file: Path) -> tuple[str, list[str]]:
    if host.kind == "wsl":
        args: list[str] = []
        if host.distro:
            args.extend(["-d", host.distro])
        args.extend(["--", "bash", "-lc", f"exec {shell_quote(windows_path_for_wsl(runner_file))}"])
        return host.executable, args

    if host.kind == "git-bash":
        args = ["-lc", f"exec {shell_quote(windows_path_for_git_bash(runner_file))}"]
        return host.executable, args

    raise SetupError(f"unsupported Windows bash host: {host.kind}")


def windows_scheduled_task_command(host: WindowsBashHost, runner_file: Path) -> tuple[str, str]:
    executable, args = windows_bash_host_runner_command(host, runner_file)
    return windows_scheduled_task_exec(executable, args)


def windows_scheduled_task_xml(host: WindowsBashHost, runner_file: Path) -> str:
    command, arguments = windows_scheduled_task_command(host, runner_file)
    ns = "http://schemas.microsoft.com/windows/2004/02/mit/task"
    ET.register_namespace("", ns)
    task = ET.Element(f"{{{ns}}}Task", {"version": "1.4"})
    registration = ET.SubElement(task, f"{{{ns}}}RegistrationInfo")
    ET.SubElement(registration, f"{{{ns}}}Author").text = "codex-telegram-bridge setup"
    ET.SubElement(registration, f"{{{ns}}}Description").text = (
        "Codex Telegram Bridge logon autostart"
    )
    triggers = ET.SubElement(task, f"{{{ns}}}Triggers")
    logon = ET.SubElement(triggers, f"{{{ns}}}LogonTrigger")
    ET.SubElement(logon, f"{{{ns}}}Enabled").text = "true"
    principals = ET.SubElement(task, f"{{{ns}}}Principals")
    principal = ET.SubElement(principals, f"{{{ns}}}Principal", {"id": "Author"})
    ET.SubElement(principal, f"{{{ns}}}LogonType").text = "InteractiveToken"
    ET.SubElement(principal, f"{{{ns}}}RunLevel").text = "LeastPrivilege"
    settings = ET.SubElement(task, f"{{{ns}}}Settings")
    ET.SubElement(settings, f"{{{ns}}}MultipleInstancesPolicy").text = "IgnoreNew"
    ET.SubElement(settings, f"{{{ns}}}DisallowStartIfOnBatteries").text = "false"
    ET.SubElement(settings, f"{{{ns}}}StopIfGoingOnBatteries").text = "false"
    ET.SubElement(settings, f"{{{ns}}}AllowHardTerminate").text = "true"
    ET.SubElement(settings, f"{{{ns}}}StartWhenAvailable").text = "true"
    ET.SubElement(settings, f"{{{ns}}}RunOnlyIfNetworkAvailable").text = "false"
    ET.SubElement(settings, f"{{{ns}}}Enabled").text = "true"
    ET.SubElement(settings, f"{{{ns}}}Hidden").text = "true"
    ET.SubElement(settings, f"{{{ns}}}ExecutionTimeLimit").text = "PT0S"
    actions = ET.SubElement(task, f"{{{ns}}}Actions", {"Context": "Author"})
    exec_el = ET.SubElement(actions, f"{{{ns}}}Exec")
    ET.SubElement(exec_el, f"{{{ns}}}Command").text = command
    ET.SubElement(exec_el, f"{{{ns}}}Arguments").text = arguments
    return ET.tostring(task, encoding="unicode", xml_declaration=True)


def windows_command_quote(value: str) -> str:
    if not value or any(ch.isspace() for ch in value) or '"' in value:
        return '"' + value.replace('"', r'\"') + '"'
    return value


def windows_python_executable() -> str:
    return sys.executable or shutil.which("python") or "python"


def windows_python_runner_command(runner_file: Path) -> tuple[str, list[str]]:
    return windows_python_executable(), [str(runner_file)]


def windows_manual_start_command(host: WindowsBashHost, runner_file: Path) -> str:
    if host.kind == "wsl":
        args: list[str] = []
        if host.distro:
            args.extend(["-d", host.distro])
        args.extend(["--", "bash", "-lc", f"exec {shell_quote(windows_path_for_wsl(runner_file))}"])
        return " ".join([windows_command_quote(host.executable), *[windows_command_quote(arg) for arg in args]])
    if host.kind == "git-bash":
        args = ["-lc", f"exec {shell_quote(windows_path_for_git_bash(runner_file))}"]
        return " ".join([windows_command_quote(host.executable), *[windows_command_quote(arg) for arg in args]])
    return str(runner_file)


def manual_start_command_for_runner(
    runner_file: Path,
    *,
    os_name: str | None = None,
    run: Callable[..., subprocess.CompletedProcess[str]] = run_command,
) -> str:
    os_name = os_name or platform.system()
    if os_name == "Windows":
        executable, args = windows_python_runner_command(runner_file)
        return " ".join(windows_command_quote(part) for part in [executable, *args])
    return str(runner_file)


def install_windows_scheduled_task(
    runner_file: Path,
    *,
    start: bool,
    task_name: str,
    run: Callable[..., subprocess.CompletedProcess[str]],
    bash_host: WindowsBashHost | None = None,
) -> str | None:
    try:
        host = bash_host or detect_windows_bash_host(run=run)
    except SetupError as exc:
        warn(str(exc))
        return None

    xml_file: Path | None = None
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".xml", delete=False, encoding="utf-8") as tmp:
            tmp.write(windows_scheduled_task_xml(host, runner_file))
            xml_file = Path(tmp.name)
        create = run(["schtasks", "/Create", "/TN", task_name, "/XML", str(xml_file), "/F"])
        if create.returncode != 0:
            detail = command_output(create) or "unknown error"
            warn(f"failed to create Windows Scheduled Task {task_name}: {detail}")
            return None
    finally:
        if xml_file is not None:
            try:
                xml_file.unlink()
            except OSError:
                pass

    if start:
        started = run(["schtasks", "/Run", "/TN", task_name])
        if started.returncode != 0:
            warn(f"could not start Windows Scheduled Task {task_name}: {command_output(started)}")
    return f"Scheduled Task: {task_name}"


def windows_watchdog_task_action() -> str:
    python_bin = sys.executable or shutil.which("python") or shutil.which("python3") or "python"
    return " ".join(windows_command_quote(part) for part in [str(python_bin), str(WATCHDOG_SCRIPT)])


def install_windows_watchdog_task(
    *,
    start: bool,
    task_name: str,
    run: Callable[..., subprocess.CompletedProcess[str]],
) -> str | None:
    create = run(
        [
            "schtasks",
            "/Create",
            "/TN",
            task_name,
            "/SC",
            "MINUTE",
            "/MO",
            "1",
            "/TR",
            windows_watchdog_task_action(),
            "/F",
        ]
    )
    if create.returncode != 0:
        detail = command_output(create) or "unknown error"
        warn(f"failed to create Windows watchdog Scheduled Task {task_name}: {detail}")
        return None

    if start:
        started = run(["schtasks", "/Run", "/TN", task_name])
        if started.returncode != 0:
            warn(f"could not start Windows watchdog Scheduled Task {task_name}: {command_output(started)}")
    return f"Scheduled Task: {task_name}"


def powershell_invoke_command(executable: str, args: list[str]) -> str:
    return " ".join(["&", powershell_single_quote(executable), *[powershell_single_quote(arg) for arg in args]])


def windows_startup_launcher_command(runner_file: Path) -> tuple[str, str]:
    executable, args = windows_python_runner_command(runner_file)
    return windows_scheduled_task_exec(executable, args)


def windows_startup_launcher_content(runner_file: Path) -> str:
    command, arguments = windows_startup_launcher_command(runner_file)
    return "\r\n".join(
        [
            "@echo off",
            "rem Installed by codex-telegram-bridge setup. Runs at Windows logon.",
            f"{windows_command_quote(command)} {arguments} >NUL 2>NUL",
            "",
        ]
    )


def install_windows_startup_launcher(
    runner_file: Path,
    *,
    start: bool,
    run: Callable[..., subprocess.CompletedProcess[str]],
    bash_host: WindowsBashHost | None = None,
    startup_dir: Path | None = None,
    app_name: str = APP_NAME,
) -> Path | None:
    _ = bash_host

    launcher_file = default_windows_startup_launcher_file(startup_dir, app_name=app_name)
    write_text_atomic(
        launcher_file,
        windows_startup_launcher_content(runner_file),
        mode=0o644,
    )
    if start:
        started = run(windows_startup_launcher_start_command(launcher_file))
        if started.returncode != 0:
            warn(f"could not launch Windows Startup bridge launcher: {command_output(started)}")
    return launcher_file


def parse_windows_scheduled_task_status(output: str) -> str:
    for line in output.replace("\r\n", "\n").splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        if key.strip().lower() == "status":
            return value.strip() or "unknown"
    return "unknown"


def windows_scheduled_task_status(
    *,
    task_name: str,
    run: Callable[..., subprocess.CompletedProcess[str]],
) -> str:
    proc = run(["schtasks", "/Query", "/TN", task_name, "/FO", "LIST", "/V"])
    if proc.returncode != 0:
        return "not-installed"
    return parse_windows_scheduled_task_status(proc.stdout or proc.stderr or "")


def windows_bridge_process_running(run: Callable[..., subprocess.CompletedProcess[str]]) -> bool:
    script = (
        "$ErrorActionPreference='SilentlyContinue'; "
        "$p = Get-CimInstance Win32_Process | Where-Object { "
        "$_.ProcessId -ne $PID -and $_.CommandLine -and "
        "$_.CommandLine -match 'telegram_agent_bridge\\.py|codex_repl_bridge\\.py|telegram-agent-bridge-run' "
        "} | Select-Object -First 1; "
        "if ($p) { 'running'; exit 0 } else { 'missing'; exit 1 }"
    )
    proc = run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script])
    return proc.returncode == 0


def windows_startup_launcher_status(
    startup_dir: Path | None = None,
    *,
    app_name: str = APP_NAME,
) -> str:
    return (
        "startup-installed"
        if default_windows_startup_launcher_file(startup_dir, app_name=app_name).exists()
        else "not-installed"
    )


def windows_startup_launcher_start_command(launcher_file: Path) -> list[str]:
    script = f"Start-Process -WindowStyle Hidden -FilePath {powershell_single_quote(launcher_file)}"
    return [
        "powershell.exe",
        "-NoProfile",
        "-WindowStyle",
        "Hidden",
        "-ExecutionPolicy",
        "Bypass",
        "-Command",
        script,
    ]


def service_start_command(
    *,
    os_name: str | None = None,
    runner_file: Path | None = None,
    task_name: str = WINDOWS_TASK_NAME,
    windows_startup_dir: Path | None = None,
) -> str:
    os_name = os_name or platform.system()
    if os_name == "Windows":
        launcher_file = default_windows_startup_launcher_file(windows_startup_dir, app_name=task_name)
        return " ".join(windows_command_quote(part) for part in windows_startup_launcher_start_command(launcher_file))
    if os_name == "Linux":
        return f"systemctl --user start {SERVICE_NAME}"
    if os_name == "Darwin":
        return f"launchctl kickstart -k gui/$(id -u)/{LAUNCHD_LABEL}"
    return str(runner_file) if runner_file else ""


def install_service(
    runner_file: Path,
    *,
    start: bool = True,
    os_name: str | None = None,
    run: Callable[..., subprocess.CompletedProcess[str]] = run_command,
    task_name: str = WINDOWS_TASK_NAME,
    bash_host: WindowsBashHost | None = None,
    windows_startup_dir: Path | None = None,
) -> Path | str | None:
    os_name = os_name or platform.system()
    if os_name == "Linux":
        unit_file = default_systemd_unit_file()
        write_text_atomic(unit_file, systemd_unit_content(runner_file), mode=0o644)
        run(["systemctl", "--user", "daemon-reload"])
        run(["systemctl", "--user", "enable", SERVICE_NAME])
        if start:
            run(["systemctl", "--user", "restart", SERVICE_NAME])
        return unit_file

    if os_name == "Darwin":
        plist_file = default_launchd_plist_file()
        write_text_atomic(plist_file, launchd_plist_content(runner_file), mode=0o644)
        if start:
            domain = f"gui/{os.getuid()}/{LAUNCHD_LABEL}"
            run(["launchctl", "bootout", domain])
            run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(plist_file)])
        return plist_file

    if os_name == "Windows":
        return install_windows_startup_launcher(
            runner_file,
            start=start,
            run=run,
            bash_host=bash_host,
            startup_dir=windows_startup_dir,
            app_name=task_name,
        )

    warn(f"service install is not automated for {os_name}; use the runner manually")
    return None


def install_watchdog(
    *,
    start: bool = True,
    os_name: str | None = None,
    run: Callable[..., subprocess.CompletedProcess[str]] = run_command,
) -> Path | None:
    os_name = os_name or platform.system()
    if os_name == "Linux":
        service_file = default_watchdog_systemd_service_file()
        timer_file = default_watchdog_systemd_timer_file()
        write_text_atomic(service_file, watchdog_systemd_service_content(), mode=0o644)
        write_text_atomic(timer_file, watchdog_systemd_timer_content(), mode=0o644)
        run(["systemctl", "--user", "daemon-reload"])
        run(["systemctl", "--user", "enable", WATCHDOG_TIMER_NAME])
        if start:
            run(["systemctl", "--user", "start", WATCHDOG_TIMER_NAME])
            run(["systemctl", "--user", "start", WATCHDOG_SERVICE_NAME])
        return timer_file

    if os_name == "Darwin":
        plist_file = default_watchdog_launchd_plist_file()
        write_text_atomic(plist_file, watchdog_launchd_plist_content(), mode=0o644)
        if start:
            domain = f"gui/{os.getuid()}/{WATCHDOG_LAUNCHD_LABEL}"
            run(["launchctl", "bootout", domain])
            run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(plist_file)])
        return plist_file

    if os_name == "Windows":
        return install_windows_watchdog_task(
            start=start,
            task_name=WINDOWS_WATCHDOG_TASK_NAME,
            run=run,
        )

    warn(f"watchdog install is not automated for {os_name}")
    return None


def uninstall_service(
    *,
    os_name: str | None = None,
    run: Callable[..., subprocess.CompletedProcess[str]] = run_command,
) -> None:
    os_name = os_name or platform.system()
    if os_name == "Linux":
        run(["systemctl", "--user", "disable", "--now", SERVICE_NAME])
        unit_file = default_systemd_unit_file()
        try:
            unit_file.unlink()
        except OSError:
            pass
        run(["systemctl", "--user", "daemon-reload"])
        return

    if os_name == "Darwin":
        domain = f"gui/{os.getuid()}/{LAUNCHD_LABEL}"
        run(["launchctl", "bootout", domain])
        try:
            default_launchd_plist_file().unlink()
        except OSError:
            pass
        return

    if os_name == "Windows":
        run(["schtasks", "/End", "/TN", WINDOWS_TASK_NAME])
        run(["schtasks", "/Delete", "/TN", WINDOWS_TASK_NAME, "/F"])
        try:
            default_windows_startup_launcher_file().unlink()
        except OSError:
            pass
        return

    warn(f"service uninstall is not automated for {os_name}")


def uninstall_watchdog(
    *,
    os_name: str | None = None,
    run: Callable[..., subprocess.CompletedProcess[str]] = run_command,
) -> None:
    os_name = os_name or platform.system()
    if os_name == "Linux":
        run(["systemctl", "--user", "disable", "--now", WATCHDOG_TIMER_NAME])
        for path in (default_watchdog_systemd_service_file(), default_watchdog_systemd_timer_file()):
            try:
                path.unlink()
            except OSError:
                pass
        run(["systemctl", "--user", "daemon-reload"])
        return

    if os_name == "Darwin":
        domain = f"gui/{os.getuid()}/{WATCHDOG_LAUNCHD_LABEL}"
        run(["launchctl", "bootout", domain])
        try:
            default_watchdog_launchd_plist_file().unlink()
        except OSError:
            pass
        return

    if os_name == "Windows":
        run(["schtasks", "/End", "/TN", WINDOWS_WATCHDOG_TASK_NAME])
        run(["schtasks", "/Delete", "/TN", WINDOWS_WATCHDOG_TASK_NAME, "/F"])
        return

    warn(f"watchdog uninstall is not automated for {os_name}")


def service_status(
    *,
    os_name: str | None = None,
    run: Callable[..., subprocess.CompletedProcess[str]] = run_command,
    task_name: str = WINDOWS_TASK_NAME,
    windows_startup_dir: Path | None = None,
) -> str:
    os_name = os_name or platform.system()
    if os_name == "Linux":
        proc = run(["systemctl", "--user", "is-active", SERVICE_NAME])
        return (proc.stdout or proc.stderr or "unknown").strip()
    if os_name == "Darwin":
        proc = run(["launchctl", "print", f"gui/{os.getuid()}/{LAUNCHD_LABEL}"])
        text = (proc.stdout or proc.stderr or "").splitlines()
        for line in text:
            stripped = line.strip()
            if stripped.startswith("state = "):
                return stripped.removeprefix("state = ").strip()
        return "unknown"
    if os_name == "Windows":
        task_status = windows_scheduled_task_status(task_name=task_name, run=run)
        if task_status != "not-installed":
            return task_status
        if windows_bridge_process_running(run):
            return "running"
        return windows_startup_launcher_status(windows_startup_dir, app_name=task_name)
    return "manual"


def watchdog_status(
    *,
    os_name: str | None = None,
    run: Callable[..., subprocess.CompletedProcess[str]] = run_command,
    task_name: str = WINDOWS_WATCHDOG_TASK_NAME,
) -> str:
    os_name = os_name or platform.system()
    if os_name == "Linux":
        proc = run(["systemctl", "--user", "is-active", WATCHDOG_TIMER_NAME])
        return (proc.stdout or proc.stderr or "unknown").strip()
    if os_name == "Darwin":
        proc = run(["launchctl", "print", f"gui/{os.getuid()}/{WATCHDOG_LAUNCHD_LABEL}"])
        if proc.returncode != 0:
            return "missing"
        text = (proc.stdout or proc.stderr or "").splitlines()
        for line in text:
            stripped = line.strip()
            if stripped.startswith("state = "):
                state = stripped.removeprefix("state = ").strip()
                return "loaded" if state == "not running" else state
        return "loaded"
    if os_name == "Windows":
        return windows_scheduled_task_status(task_name=task_name, run=run)
    return "manual"


def prompt_value(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    value = input(f"{prompt}{suffix}: ").strip()
    return value or default


def prompt_yes_no(prompt: str, default: bool = True) -> bool:
    suffix = "Y/n" if default else "y/N"
    value = input(f"{prompt} [{suffix}]: ").strip().lower()
    if not value:
        return default
    return value in {"y", "yes"}


@dataclass(frozen=True)
class SetupOptions:
    config_file: Path
    runner_file: Path
    mode: str
    token: str | None
    chat_id: str | None
    agent: str
    agent_cmd: str
    workdir: Path
    prefix: str
    prefix_line: bool
    state_dir: Path
    dangerous_bypass: bool
    tmux_socket: str
    tmux_session: str
    submit_key: str
    install_asr: bool
    wait_timeout: int
    install_service: bool
    start_service: bool
    send_test: bool
    non_interactive: bool
    yes: bool


def setup_bridge(options: SetupOptions, api_call: ApiCall = telegram_call) -> int:
    out("Codex Telegram Bridge setup")
    out("This wizard connects one Telegram bot to this local machine.")
    out("")
    out("You will need:")
    setup_note("a Telegram bot token from @BotFather")
    setup_note("the Telegram app open so you can send /start to your bot")
    setup_note("Codex installed and logged in locally")
    if options.mode == "repl":
        setup_note("for full REPL mode, a Codex tmux session: tmux -L codex new -s codex")

    if options.mode not in {"repl", "exec"}:
        raise SetupError("--mode must be repl or exec")
    if options.agent != "codex":
        raise SetupError("--agent supports only codex")

    setup_step(1, "Paste the BotFather token")
    token = (options.token or "").strip()
    if token:
        setup_note("Using the token provided by --token.")
    else:
        setup_note("Paste the token here in the terminal. Do not send it in any Telegram chat.")
    setup_note(f"It will be stored only in your private config: {options.config_file}")
    if not token:
        if options.non_interactive:
            raise SetupError("--token is required in non-interactive mode")
        token = getpass.getpass("BotFather token (hidden): ").strip()
    try:
        username = validate_bot_token(token, api_call=api_call)
    except SetupError as exc:
        raise SetupError(
            f"{exc}. Copy the token from @BotFather and paste it into this terminal, "
            "not into your Telegram chat."
        ) from exc
    ok(f"token valid: @{username}")

    setup_step(2, "Connect your Telegram chat")
    chat_id = (options.chat_id or "").strip()
    if not chat_id:
        if options.non_interactive:
            raise SetupError("--chat-id is required in non-interactive mode")
        offset = current_update_offset(token, api_call=api_call)
        setup_note(f"Open Telegram and send /start to @{username}.")
        setup_note("Do not paste the bot token into Telegram. Only send /start.")
        setup_note(f"Waiting up to {options.wait_timeout} seconds for that message...")
        try:
            chat_id, label = wait_for_chat_id(
                token,
                offset=offset,
                timeout_seconds=options.wait_timeout,
                api_call=api_call,
            )
        except SetupError as exc:
            raise SetupError(
                f"{exc} If this keeps failing, make sure you opened the bot chat itself "
                "and pressed Start or sent /start."
            ) from exc
        ok(f"chat id detected: {chat_id} ({label})")
    else:
        setup_note("Using the chat id provided on the command line.")
        ok(f"chat id configured: {chat_id}")

    setup_step(3, "Check the local Codex mode")
    if options.mode == "repl":
        setup_note("REPL mode mirrors Telegram into an existing Codex CLI tmux session.")
        setup_note("If you have not started that session yet, open another terminal and run:")
        setup_command(f"tmux -L {options.tmux_socket} new -s {options.tmux_session}")
        setup_command(options.agent_cmd)
    else:
        setup_note("Exec mode runs one text-only Codex turn per Telegram prompt.")
    agent_check = agent_command_spawn_check(options.agent_cmd, os_name=platform.system())
    if agent_check.ok:
        ok(agent_check.message)
    else:
        warn(agent_check.message)
    if options.mode == "repl" and not shutil.which("tmux"):
        warn("tmux was not found on PATH. REPL mode requires an existing tmux Codex session.")
    if options.mode == "repl":
        ok("mode selected: repl (visible Codex transcript, media prompts, Telegram typing)")
    else:
        ok("mode selected: exec (one-shot codex exec text bridge)")

    setup_step(4, "Write the private config")
    local_input = options.state_dir / "input.fifo"
    if options.config_file.exists() and not options.yes and not options.non_interactive:
        setup_note("An existing config was found. Keeping it is safest if this is already working.")
        if not prompt_yes_no(f"Overwrite existing config {options.config_file}?", default=False):
            raise SetupError("setup cancelled")

    audio_transcribe_cmd = ""
    if options.mode == "repl" and AUDIO_TRANSCRIBE_SCRIPT.exists():
        audio_transcribe_cmd = f"{AUDIO_TRANSCRIBE_SCRIPT} {{path}}"
        if options.install_asr:
            if install_asr_dependencies():
                ok("audio transcription dependencies ready")
            else:
                warn("audio transcription command will be configured, but dependencies are missing")

    write_env_config(
        options.config_file,
        mode=options.mode,
        token=token,
        chat_id=chat_id,
        agent=options.agent,
        agent_cmd=options.agent_cmd,
        workdir=options.workdir,
        prefix=options.prefix,
        prefix_line=options.prefix_line,
        state_dir=options.state_dir,
        local_input=local_input,
        dangerous_bypass=options.dangerous_bypass,
        tmux_socket=options.tmux_socket,
        tmux_session=options.tmux_session,
        submit_key=options.submit_key,
        audio_transcribe_cmd=audio_transcribe_cmd,
    )
    ok(f"wrote private config: {options.config_file}")
    setup_note("The config file is chmod 600 and should stay out of git.")

    current_os = platform.system()
    install_runner(options.runner_file, options.config_file, os_name=current_os)
    ok(f"installed runner: {options.runner_file}")

    setup_step(5, "Install the background service")
    service_state: str | None = None
    start_command: str | None = None
    if options.install_service:
        installed = install_service(options.runner_file, start=options.start_service)
        if installed:
            start_command = service_start_command(os_name=current_os, runner_file=options.runner_file)
            ok(f"installed service: {installed}")
            service_state = service_status()
            if options.start_service:
                if service_is_running(service_state):
                    ok(f"service status: {service_state}")
                else:
                    warn(f"service status: {service_state}")
                    if current_os == "Windows" and service_state == "startup-installed":
                        setup_note("Windows Startup autostart exists, but the bridge process is not confirmed running.")
        else:
            start_command = manual_start_command_for_runner(options.runner_file, os_name=current_os)
            warn("service install did not complete; run the bridge manually")
            setup_command(start_command)
        watchdog = install_watchdog(start=options.start_service)
        if watchdog:
            ok(f"installed watchdog: {watchdog}")
            if options.start_service:
                ok(f"watchdog status: {watchdog_status()}")
    else:
        warn("service install skipped; run the runner manually")
        start_command = str(options.runner_file)
        setup_command(str(options.runner_file))

    setup_step(6, "Send a setup-complete test message")
    if options.send_test:
        if send_test_message(
            token,
            chat_id,
            api_call=api_call,
            service_status_text=service_state,
            start_command=None if service_is_running(service_state) else start_command,
        ):
            ok("sent setup-complete test message")
        else:
            warn("could not send setup-complete test message")
            setup_note("Run doctor next; it will test token, chat_id, service, and local command paths.")
    else:
        setup_note("Test message skipped by option.")

    out("")
    out("Setup complete.")
    out("Try it now:")
    if start_command and not service_is_running(service_state):
        out("  1. Start the background bridge:")
        out(f"     {start_command}")
        out("  2. In Telegram, send /ping to your bot.")
        out("  3. If /ping works, send a normal Codex prompt.")
        out(f"  4. Run a health check any time: {doctor_command_hint()}")
    else:
        out("  1. In Telegram, send /ping to your bot.")
        out("  2. If /ping works, send a normal Codex prompt.")
        out(f"  3. Run a health check any time: {doctor_command_hint()}")
    return 0


def doctor(config_file: Path, runner_file: Path, api_call: ApiCall = telegram_call) -> int:
    failures = 0
    warnings = 0
    next_steps: list[str] = []
    current_os = platform.system()

    def add_next_step(command: str) -> None:
        if command not in next_steps:
            next_steps.append(command)

    if config_file.exists():
        ok(f"config exists: {config_file}")
        mode = file_mode(config_file)
        if sys.platform != "win32" and mode is not None and mode & 0o077:
            warn(f"config permissions are too open: {oct(mode)}; run chmod 600")
            add_next_step(f"chmod 600 {shell_quote(config_file)}")
            warnings += 1
    else:
        fail(f"config missing: {config_file}")
        return 2

    config = load_env_file(config_file)
    for key in ("TAB_BOT_TOKEN", "TAB_CHAT_ID", "TAB_BRIDGE_MODE", "TAB_AGENT", "TAB_AGENT_CMD"):
        if config.get(key):
            ok(f"{key} configured")
        else:
            fail(f"{key} missing")
            failures += 1

    token = config.get("TAB_BOT_TOKEN", "")
    chat_id = config.get("TAB_CHAT_ID", "")
    if token:
        try:
            username = validate_bot_token(token, api_call=api_call)
            ok(f"token valid: @{username}")
        except SetupError as exc:
            fail(str(exc))
            failures += 1

    if token and chat_id:
        payload = api_call(token, "sendChatAction", timeout=10, chat_id=chat_id, action="typing")
        if payload and payload.get("ok"):
            ok("Telegram chat_id accepted sendChatAction")
        else:
            warn("Telegram sendChatAction failed; chat_id may be wrong or bot was not started")
            warnings += 1

    agent_cmd = config.get("TAB_AGENT_CMD", "")
    if agent_cmd:
        agent_check = agent_command_spawn_check(agent_cmd, os_name=current_os)
        if agent_check.ok:
            ok(agent_check.message)
        else:
            warn(agent_check.message)
            warnings += 1

    if runner_file.exists():
        ok(f"runner exists: {runner_file}")
    else:
        warn(f"runner missing: {runner_file}")
        warnings += 1

    mode = config.get("TAB_BRIDGE_MODE", "repl")
    bridge_script = REPL_BRIDGE_SCRIPT if mode == "repl" else EXEC_BRIDGE_SCRIPT
    if bridge_script.exists():
        ok(f"bridge script exists: {bridge_script}")
    else:
        fail(f"bridge script missing: {bridge_script}")
        failures += 1

    if mode == "repl":
        tmux_socket = config.get("CRB_TMUX_SOCKET", "codex")
        tmux_session = config.get("CRB_TMUX_SESSION", "codex")
        if sys.platform == "win32":
            add_next_step(f"Windows native mode has no tmux; use exec mode: {setup_command_hint(mode='exec')}")
        elif shutil.which("tmux"):
            proc = run_command(["tmux", "-L", tmux_socket, "has-session", "-t", f"={tmux_session}"])
            if proc.returncode == 0:
                ok(f"tmux Codex session found: {tmux_socket}/{tmux_session}")
            else:
                warn(f"tmux Codex session not found: {tmux_socket}/{tmux_session}")
                codex_command = config.get("TAB_AGENT_CMD", "codex") or "codex"
                add_next_step(
                    " ".join(
                        [
                            "tmux",
                            "-L",
                            shell_quote(tmux_socket),
                            "new",
                            "-d",
                            "-s",
                            shell_quote(tmux_session),
                            codex_command,
                        ]
                    )
                )
                add_next_step(f"To use it without tmux, switch to exec mode: {setup_command_hint(mode='exec')}")
                warnings += 1
        else:
            warn("tmux not found on PATH; REPL mode requires tmux")
            add_next_step(f"To use it without tmux, switch to exec mode: {setup_command_hint(mode='exec')}")
            warnings += 1

    status = service_status()
    if service_is_running(status):
        ok(f"service status: {status}")
    else:
        warn(f"service status: {status}")
        if status == "not-installed":
            if current_os == "Windows":
                add_next_step(f"Install Windows Startup autostart: {setup_command_hint()}")
            else:
                add_next_step(f"Run setup again to install service/watchdog: {setup_command_hint()}")
        elif current_os == "Windows" and status == "startup-installed":
            add_next_step(f"Start Windows Startup launcher: {service_start_command(os_name=current_os, runner_file=runner_file)}")
        warnings += 1

    wd_status = watchdog_status()
    if wd_status.strip().lower() in {"active", "running", "loaded", "ready"}:
        ok(f"watchdog status: {wd_status}")
    else:
        warn(f"watchdog status: {wd_status}")
        if wd_status == "not-installed":
            if current_os == "Windows":
                add_next_step(f"Run setup again to install Windows watchdog: {setup_command_hint()}")
            else:
                add_next_step(f"Run setup again to install service/watchdog: {setup_command_hint()}")
        warnings += 1

    out("")
    if next_steps:
        out("Next steps:")
        for command in next_steps:
            out(f"  {command}")
        out("")
    out(f"doctor complete: {failures} failure(s), {warnings} warning(s)")
    return 2 if failures else 0


def uninstall(
    config_file: Path,
    runner_file: Path,
    *,
    purge: bool,
    yes: bool,
) -> int:
    if not yes and not prompt_yes_no("Stop and remove telegram-agent-bridge service?", default=True):
        raise SetupError("uninstall cancelled")
    uninstall_watchdog()
    ok("watchdog removed or stopped")
    uninstall_service()
    ok("service removed or stopped")

    try:
        runner_file.unlink()
        ok(f"removed runner: {runner_file}")
    except OSError:
        warn(f"runner not found: {runner_file}")

    if purge:
        try:
            config_file.unlink()
            ok(f"removed config: {config_file}")
        except OSError:
            warn(f"config not found: {config_file}")
    else:
        out(f"kept config: {config_file}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Setup helper for Codex Telegram Bridge")
    subparsers = parser.add_subparsers(dest="command", required=True)

    setup_parser = subparsers.add_parser("setup", help="interactive setup wizard")
    setup_parser.add_argument("--config", type=expand_path, default=default_config_file())
    setup_parser.add_argument("--runner", type=expand_path, default=default_runner_file())
    setup_parser.add_argument(
        "--mode",
        choices=["repl", "exec"],
        default="repl",
        help="repl uses a visible Codex tmux session; exec runs one-shot codex exec",
    )
    setup_parser.add_argument("--token")
    setup_parser.add_argument("--chat-id")
    setup_parser.add_argument("--agent", choices=["codex"], default="codex", help=argparse.SUPPRESS)
    setup_parser.add_argument("--agent-cmd", default="codex")
    setup_parser.add_argument("--workdir", type=expand_path, default=Path.home())
    setup_parser.add_argument("--prefix", default="BOT")
    setup_parser.add_argument("--prefix-line", action="store_true")
    setup_parser.add_argument("--state-dir", type=expand_path, default=default_state_dir())
    setup_parser.add_argument("--dangerous-bypass", action="store_true")
    setup_parser.add_argument("--tmux-socket", default="codex")
    setup_parser.add_argument("--tmux-session", default="codex")
    setup_parser.add_argument("--submit-key", default="Tab")
    setup_parser.add_argument(
        "--install-asr",
        action="store_true",
        help="install optional faster-whisper dependencies for Telegram voice/audio transcription",
    )
    setup_parser.add_argument("--wait-timeout", type=int, default=180)
    setup_parser.add_argument("--no-service", action="store_true")
    setup_parser.add_argument("--no-start", action="store_true")
    setup_parser.add_argument("--no-test-message", action="store_true")
    setup_parser.add_argument("--non-interactive", action="store_true")
    setup_parser.add_argument("-y", "--yes", action="store_true")

    doctor_parser = subparsers.add_parser("doctor", help="check configuration and service")
    doctor_parser.add_argument("--config", type=expand_path, default=default_config_file())
    doctor_parser.add_argument("--runner", type=expand_path, default=default_runner_file())

    uninstall_parser = subparsers.add_parser("uninstall", help="remove service and runner")
    uninstall_parser.add_argument("--config", type=expand_path, default=default_config_file())
    uninstall_parser.add_argument("--runner", type=expand_path, default=default_runner_file())
    uninstall_parser.add_argument("--purge", action="store_true", help="also delete private config")
    uninstall_parser.add_argument("-y", "--yes", action="store_true")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "setup":
            return setup_bridge(
                SetupOptions(
                    config_file=args.config,
                    runner_file=args.runner,
                    mode=args.mode,
                    token=args.token,
                    chat_id=args.chat_id,
                    agent=args.agent,
                    agent_cmd=args.agent_cmd,
                    workdir=args.workdir,
                    prefix=args.prefix,
                    prefix_line=args.prefix_line,
                    state_dir=args.state_dir,
                    dangerous_bypass=args.dangerous_bypass,
                    tmux_socket=args.tmux_socket,
                    tmux_session=args.tmux_session,
                    submit_key=args.submit_key,
                    install_asr=args.install_asr,
                    wait_timeout=args.wait_timeout,
                    install_service=not args.no_service,
                    start_service=not args.no_start,
                    send_test=not args.no_test_message,
                    non_interactive=args.non_interactive,
                    yes=args.yes,
                )
            )
        if args.command == "doctor":
            return doctor(args.config, args.runner)
        if args.command == "uninstall":
            return uninstall(args.config, args.runner, purge=args.purge, yes=args.yes)
    except SetupError as exc:
        fail(str(exc))
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
