#!/usr/bin/env python3
import contextlib
import io
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import bridge_setup as setup


def completed(cmd, returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(cmd, returncode, stdout, stderr)


class FakeApi:
    def __init__(self):
        self.calls = []

    def __call__(self, token, method, timeout=60, **params):
        self.calls.append((token, method, timeout, params))
        if method == "getMe":
            return {"ok": True, "result": {"username": "test_bot"}}
        if method == "getUpdates":
            return {
                "ok": True,
                "result": [
                    {
                        "update_id": 42,
                        "message": {
                            "chat": {
                                "id": 12345,
                                "first_name": "Ada",
                                "username": "ada",
                            },
                            "text": "/start",
                        },
                    }
                ],
            }
        if method in {"sendMessage", "sendChatAction"}:
            return {"ok": True, "result": True}
        return {"ok": False}


class SetupWizardTests(unittest.TestCase):
    def test_validate_bot_token_returns_username(self):
        api = FakeApi()
        self.assertEqual(setup.validate_bot_token("token", api_call=api), "test_bot")
        self.assertEqual(api.calls[0][1], "getMe")

    def test_current_update_offset_uses_latest_update(self):
        api = FakeApi()
        self.assertEqual(setup.current_update_offset("token", api_call=api), 43)
        _token, method, _timeout, params = api.calls[0]
        self.assertEqual(method, "getUpdates")
        self.assertEqual(params["timeout_param"], 0)

    def test_wait_for_chat_id_extracts_message_chat(self):
        api = FakeApi()
        chat_id, label = setup.wait_for_chat_id(
            "token",
            offset=10,
            timeout_seconds=1,
            api_call=api,
        )
        self.assertEqual(chat_id, "12345")
        self.assertIn("@ada", label)

    def test_write_env_config_is_private_and_loadable(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            env_file = Path(tmpdir) / "bridge.env"
            state_dir = Path(tmpdir) / "state"
            setup.write_env_config(
                env_file,
                mode="repl",
                token="123:secret",
                chat_id="12345",
                agent="codex",
                agent_cmd="codex",
                workdir=Path(tmpdir),
                prefix="BOT",
                prefix_line=True,
                state_dir=state_dir,
                local_input=state_dir / "input.fifo",
                dangerous_bypass=False,
                tmux_socket="codex",
                tmux_session="codex",
                submit_key="Tab",
                audio_transcribe_cmd="/tmp/transcribe {path}",
            )

            self.assertTrue(env_file.is_file())
            if os.name != "nt":
                self.assertEqual(setup.file_mode(env_file), 0o600)
            values = setup.load_env_file(env_file)
            self.assertEqual(values["TAB_BOT_TOKEN"], "123:secret")
            self.assertEqual(values["TAB_CHAT_ID"], "12345")
            self.assertEqual(values["TAB_BRIDGE_MODE"], "repl")
            self.assertEqual(values["TAB_PREFIX_LINE"], "1")
            self.assertEqual(values["SUGGESTED_REPLY_BUBBLE"], "0")
            self.assertEqual(values["CRB_SUGGESTED_CONFIRM"], "0")
            self.assertEqual(values["CRB_SUGGESTED_TAIL_PROMPT"], "0")
            expected_signal_path = "off" if os.name == "nt" else str(state_dir / "input.fifo")
            self.assertEqual(values["CRB_SIGNAL_PATH"], expected_signal_path)
            self.assertEqual(values["CRB_TMUX_SOCKET"], "codex")

    def test_install_runner_sources_private_env(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            runner = Path(tmpdir) / "bridge-run"
            env_file = Path(tmpdir) / "bridge.env"
            setup.install_runner(runner, env_file)

            text = runner.read_text(encoding="utf-8")
            env_reference = repr(str(env_file)) if os.name == "nt" else str(env_file)
            repl_reference = (
                repr(str(setup.REPL_BRIDGE_SCRIPT)) if os.name == "nt" else str(setup.REPL_BRIDGE_SCRIPT)
            )
            exec_reference = (
                repr(str(setup.EXEC_BRIDGE_SCRIPT)) if os.name == "nt" else str(setup.EXEC_BRIDGE_SCRIPT)
            )
            self.assertIn(env_reference, text)
            self.assertIn(repl_reference, text)
            self.assertIn(exec_reference, text)
            self.assertIn("TAB_BRIDGE_MODE", text)
            self.assertTrue(runner.is_file())
            if os.name != "nt":
                self.assertEqual(setup.file_mode(runner), 0o755)

    def test_service_templates_do_not_embed_token(self):
        runner = Path("/tmp/telegram-agent-bridge-run")
        self.assertIn(str(runner), setup.systemd_unit_content(runner))
        self.assertIn(str(runner), setup.launchd_plist_content(runner))
        action = setup.windows_scheduled_task_action(
            setup.WindowsBashHost(kind="wsl", executable="wsl.exe", distro="Ubuntu"),
            Path(r"C:\Users\Ada\.local\bin\telegram-agent-bridge-run"),
        )
        self.assertIn("powershell.exe", action)
        self.assertIn("-WindowStyle Hidden", action)
        self.assertIn("wsl.exe", action)
        self.assertIn("Ubuntu", action)
        self.assertIn("/mnt/c/Users/Ada/.local/bin/telegram-agent-bridge-run", action)
        self.assertIn("telegram-agent-bridge-watchdog.service", setup.watchdog_systemd_timer_content())
        self.assertIn(str(setup.WATCHDOG_SCRIPT), setup.watchdog_systemd_service_content())
        self.assertIn(str(setup.WATCHDOG_SCRIPT), setup.watchdog_launchd_plist_content())
        self.assertNotIn("TAB_BOT_TOKEN", setup.systemd_unit_content(runner))
        self.assertNotIn("TAB_BOT_TOKEN", setup.launchd_plist_content(runner))
        self.assertNotIn("TAB_BOT_TOKEN", action)
        self.assertNotIn("TAB_BOT_TOKEN", setup.watchdog_systemd_service_content())
        self.assertNotIn("TAB_BOT_TOKEN", setup.watchdog_launchd_plist_content())

    def test_install_service_windows_creates_and_runs_scheduled_task(self):
        commands = []
        xml_text = ""

        def run(cmd, check=False):
            nonlocal xml_text
            commands.append(tuple(cmd))
            if "/XML" in cmd:
                xml_path = Path(cmd[cmd.index("/XML") + 1])
                xml_text = xml_path.read_text(encoding="utf-8")
            return completed(cmd)

        long_runner = Path(
            r"C:\Users\Ada\very-long-profile-segment\repos\codex-telegram-bridge"
            + (r"\nested" * 24)
            + r"\telegram-agent-bridge-run"
        )
        installed = setup.install_windows_scheduled_task(
            long_runner,
            start=True,
            run=run,
            task_name="tab-test-autostart",
            bash_host=setup.WindowsBashHost(
                kind="wsl",
                executable=r"C:\Windows\System32\wsl.exe",
                distro="Ubuntu",
            ),
        )

        self.assertEqual(installed, "Scheduled Task: tab-test-autostart")
        self.assertEqual(commands[0][:4], ("schtasks", "/Create", "/TN", "tab-test-autostart"))
        self.assertIn("/XML", commands[0])
        self.assertNotIn("/TR", commands[0])
        self.assertNotIn(str(long_runner), " ".join(commands[0]))
        self.assertIn("<Command>powershell.exe</Command>", xml_text)
        self.assertIn("Start-Process", xml_text)
        self.assertIn("C:\\Windows\\System32\\wsl.exe", xml_text)
        self.assertIn("/mnt/c/Users/Ada/very-long-profile-segment", xml_text)
        self.assertIn(("schtasks", "/Run", "/TN", "tab-test-autostart"), commands)

    def test_install_runner_windows_writes_python_runner_with_lf(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            runner = Path(tmpdir) / "telegram-agent-bridge-run"
            env_file = Path(r"C:\Users\gb\.config\telegram-agent-bridge.env")

            setup.install_runner(runner, env_file, os_name="Windows")

            raw = runner.read_bytes()
            text = raw.decode("utf-8")
            self.assertNotIn(b"\r", raw)
            self.assertIn("import os", text)
            self.assertIn("shlex.split", text)
            self.assertIn(repr(str(env_file)), text)
            self.assertIn("telegram_agent_bridge.py", text)
            self.assertNotIn("set -euo pipefail", text)

    def test_install_service_windows_installs_native_startup_launcher_without_bash(self):
        commands = []

        def run(cmd, check=False):
            commands.append(tuple(cmd))
            return completed(cmd)

        with tempfile.TemporaryDirectory() as tmpdir:
            startup_dir = Path(tmpdir) / "Startup"
            with mock.patch.object(setup.sys, "executable", r"C:\Python312\python.exe"):
                installed = setup.install_service(
                    Path(r"C:\Users\gb\.local\bin\telegram-agent-bridge-run"),
                    os_name="Windows",
                    run=run,
                    task_name="telegram-agent-bridge",
                    bash_host=setup.WindowsBashHost(kind="git-bash", executable=r"C:\Program Files\Git\bin\bash.exe"),
                    windows_startup_dir=startup_dir,
                )

            launcher = startup_dir / "telegram-agent-bridge.bat"
            self.assertEqual(installed, launcher)
            self.assertTrue(launcher.exists())
            launcher_text = launcher.read_text(encoding="utf-8")
            self.assertIn("Start-Process", launcher_text)
            self.assertIn(r"C:\Python312\python.exe", launcher_text)
            self.assertIn(r"C:\Users\gb\.local\bin\telegram-agent-bridge-run", launcher_text)
            self.assertNotIn("bash.exe", launcher_text)
            self.assertNotIn("wsl.exe", launcher_text)
            self.assertNotIn("/c/Users/gb", launcher_text)
            self.assertIn(">NUL 2>NUL", launcher_text)

        self.assertNotIn(("schtasks", "/Create"), [cmd[:2] for cmd in commands])
        self.assertNotIn(("schtasks", "/Run", "/TN", "telegram-agent-bridge"), commands)
        self.assertEqual(commands[-1][0], "powershell.exe")
        self.assertIn("Start-Process", commands[-1][-1])
        self.assertIn(str(launcher), commands[-1][-1])

    def test_windows_service_status_reports_startup_launcher_when_task_missing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            startup_dir = Path(tmpdir) / "Startup"
            launcher = startup_dir / "telegram-agent-bridge.bat"
            startup_dir.mkdir(parents=True)
            launcher.write_text("@echo off\n", encoding="utf-8")

            def missing_task(cmd, check=False):
                if cmd[:4] == ["schtasks", "/Query", "/TN", "telegram-agent-bridge"]:
                    return completed(cmd, returncode=1, stderr="ERROR: Access is denied.\r\n")
                if cmd[0] == "powershell.exe" and "Get-CimInstance" in cmd[-1]:
                    return completed(cmd, returncode=1, stdout="missing")
                self.fail(f"unexpected command: {cmd}")

            self.assertEqual(
                setup.service_status(
                    os_name="Windows",
                    run=missing_task,
                    task_name="telegram-agent-bridge",
                    windows_startup_dir=startup_dir,
                ),
                "startup-installed",
            )

            def running_process(cmd, check=False):
                if cmd[:4] == ["schtasks", "/Query", "/TN", "telegram-agent-bridge"]:
                    return completed(cmd, returncode=1, stderr="ERROR: Access is denied.\r\n")
                if cmd[0] == "powershell.exe" and "Get-CimInstance" in cmd[-1]:
                    return completed(cmd, returncode=0, stdout="running")
                self.fail(f"unexpected command: {cmd}")

            self.assertEqual(
                setup.service_status(
                    os_name="Windows",
                    run=running_process,
                    task_name="telegram-agent-bridge",
                    windows_startup_dir=startup_dir,
                ),
                "running",
            )

    def test_windows_startup_installed_is_not_running(self):
        self.assertFalse(setup.service_is_running("startup-installed"))

    def test_windows_service_status_parses_schtasks_query(self):
        def running(cmd, check=False):
            self.assertEqual(cmd[:4], ["schtasks", "/Query", "/TN", "tab-test-autostart"])
            return completed(cmd, stdout="TaskName: tab-test-autostart\r\nStatus: Running\r\n")

        def ready(cmd, check=False):
            return completed(cmd, stdout="TaskName: tab-test-autostart\r\nStatus: Ready\r\n")

        def missing(cmd, check=False):
            return completed(cmd, returncode=1, stderr="ERROR: The system cannot find the file specified.\r\n")

        self.assertEqual(
            setup.service_status(os_name="Windows", run=running, task_name="tab-test-autostart"),
            "Running",
        )
        self.assertEqual(
            setup.service_status(os_name="Windows", run=ready, task_name="tab-test-autostart"),
            "Ready",
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            self.assertEqual(
                setup.service_status(
                    os_name="Windows",
                    run=missing,
                    task_name="tab-test-autostart",
                    windows_startup_dir=Path(tmpdir) / "Startup",
                ),
                "not-installed",
            )

    def test_setup_complete_message_includes_manual_start_when_not_running(self):
        api = FakeApi()

        self.assertTrue(
            setup.send_test_message(
                "token",
                "12345",
                api_call=api,
                service_status_text="Ready",
                start_command="schtasks /Run /TN telegram-agent-bridge",
            )
        )

        _token, method, _timeout, params = api.calls[-1]
        self.assertEqual(method, "sendMessage")
        self.assertIn("/ping", params["text"])
        self.assertIn("schtasks /Run /TN telegram-agent-bridge", params["text"])

    def test_setup_complete_message_treats_windows_startup_launcher_as_not_confirmed_running(self):
        api = FakeApi()

        self.assertTrue(
            setup.send_test_message(
                "token",
                "12345",
                api_call=api,
                service_status_text="startup-installed",
                start_command='cmd.exe /c start "" "telegram-agent-bridge.bat"',
            )
        )

        _token, method, _timeout, params = api.calls[-1]
        self.assertEqual(method, "sendMessage")
        self.assertIn("Background service is not running (startup-installed)", params["text"])
        self.assertIn("/ping", params["text"])
        self.assertIn("Start it with", params["text"])

    def test_noninteractive_setup_writes_config_and_runner(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            api = FakeApi()
            rc = setup.setup_bridge(
                setup.SetupOptions(
                    config_file=base / "bridge.env",
                    runner_file=base / "bridge-run",
                    mode="repl",
                    token="token",
                    chat_id="12345",
                    agent="codex",
                    agent_cmd="codex",
                    workdir=base,
                    prefix="BOT",
                    prefix_line=False,
                    state_dir=base / "state",
                    dangerous_bypass=False,
                    tmux_socket="codex",
                    tmux_session="codex",
                    submit_key="Tab",
                    install_asr=False,
                    wait_timeout=1,
                    install_service=False,
                    start_service=False,
                    send_test=True,
                    non_interactive=True,
                    yes=True,
                ),
                api_call=api,
            )
            self.assertEqual(rc, 0)
            values = setup.load_env_file(base / "bridge.env")
            self.assertEqual(values["TAB_CHAT_ID"], "12345")
            self.assertEqual(values["TAB_BRIDGE_MODE"], "repl")
            self.assertTrue((base / "bridge-run").exists())
            self.assertIn("sendMessage", [call[1] for call in api.calls])

    def test_setup_output_guides_first_time_users(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            api = FakeApi()
            buffer = io.StringIO()
            # The wizard's first question offers the author preset; this test walks
            # the plain path, so it answers n.
            with contextlib.redirect_stdout(buffer), mock.patch("builtins.input", return_value="n"):
                rc = setup.setup_bridge(
                    setup.SetupOptions(
                        config_file=base / "bridge.env",
                        runner_file=base / "bridge-run",
                        mode="repl",
                        token="token",
                        chat_id="",
                        agent="codex",
                        agent_cmd="codex",
                        workdir=base,
                        prefix="BOT",
                        prefix_line=False,
                        state_dir=base / "state",
                        dangerous_bypass=False,
                        tmux_socket="codex",
                        tmux_session="codex",
                        submit_key="Tab",
                        install_asr=False,
                        wait_timeout=1,
                        install_service=False,
                        start_service=False,
                        send_test=False,
                        non_interactive=False,
                        yes=True,
                    ),
                    api_call=api,
                )

            output = buffer.getvalue()
            self.assertEqual(rc, 0)
            self.assertIn("[1/6] Paste the BotFather token", output)
            self.assertIn("Using the token provided by --token", output)
            self.assertIn("[2/6] Connect your Telegram chat", output)
            self.assertIn("Do not paste the bot token into Telegram", output)
            self.assertIn("Only send /start", output)
            self.assertIn("tmux -L codex new -s codex", output)
            self.assertIn("Setup complete.", output)

    def test_doctor_reports_ok_with_fake_api(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            env_file = base / "bridge.env"
            runner = base / "bridge-run"
            state_dir = base / "state"
            setup.write_env_config(
                env_file,
                mode="exec",
                token="token",
                chat_id="12345",
                agent="codex",
                agent_cmd="python3",
                workdir=base,
                prefix="BOT",
                prefix_line=False,
                state_dir=state_dir,
                local_input=state_dir / "input.fifo",
                dangerous_bypass=False,
                tmux_socket="codex",
                tmux_session="codex",
                submit_key="Tab",
                audio_transcribe_cmd="",
            )
            setup.install_runner(runner, env_file)

            original_status = setup.service_status
            setup.service_status = lambda: "active"
            try:
                self.assertEqual(setup.doctor(env_file, runner, api_call=FakeApi()), 0)
            finally:
                setup.service_status = original_status



class LikeAuthorSetupTests(unittest.TestCase):
    """`setup --like-author`, `attach` (codex-tg), doctor warning, self-check."""

    def options(self, base: Path, **overrides):
        values = dict(
            config_file=base / "bridge.env",
            runner_file=base / "bridge-run",
            mode="repl",
            token="token",
            chat_id="12345",
            agent="codex",
            agent_cmd="codex",
            workdir=base,
            prefix="BOT",
            prefix_line=False,
            state_dir=base / "state",
            dangerous_bypass=False,
            tmux_socket="codex",
            tmux_session="codex",
            submit_key="Tab",
            install_asr=False,
            wait_timeout=1,
            install_service=False,
            start_service=False,
            send_test=False,
            non_interactive=True,
            yes=True,
        )
        values.update(overrides)
        return setup.SetupOptions(**values)

    def test_like_author_setup_writes_preset_backs_up_config_and_starts_tmux_session(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            (base / "bridge.env").write_text("TAB_BOT_TOKEN='old'\n", encoding="utf-8")
            calls = []

            def run(cmd, check=False):
                calls.append(cmd)
                if "has-session" in cmd:
                    return completed(cmd, returncode=1, stderr="no server")
                return completed(cmd)

            output = io.StringIO()
            with mock.patch.object(setup.shutil, "which", return_value="/opt/bin/codex"), contextlib.redirect_stdout(output):
                rc = setup.setup_bridge(self.options(base, like_author=True), api_call=FakeApi(), run=run)
            self.assertEqual(rc, 0, output.getvalue())
            values = setup.load_env_file(base / "bridge.env")
            for key, value in setup.AUTHOR_PRESET_ENV:
                self.assertEqual(values.get(key), value, key)
            self.assertEqual(values["CRB_TMUX_SOCKET"], "codex")
            content = (base / "bridge.env").read_text(encoding="utf-8")
            self.assertEqual(content.count("CRB_FLOW_MIRROR="), 1)
            self.assertEqual(content.count("CRB_LONG_RUNNING_PROGRESS_SECONDS="), 1)
            backups = sorted(base.glob("bridge.env.bak-*"))
            self.assertEqual(len(backups), 1)
            self.assertIn("old", backups[0].read_text(encoding="utf-8"))
            self.assertIn(["tmux", "-L", "codex", "new-session", "-d", "-s", "codex", "-c", str(base), "codex"], calls)
            self.assertIn(setup.ATTACH_COMMAND, output.getvalue())

    def test_setup_without_preset_keeps_previous_config_shape(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            calls = []

            def run(cmd, check=False):
                calls.append(cmd)
                return completed(cmd)

            with contextlib.redirect_stdout(io.StringIO()):
                rc = setup.setup_bridge(self.options(base), api_call=FakeApi(), run=run)
            self.assertEqual(rc, 0)
            content = (base / "bridge.env").read_text(encoding="utf-8")
            self.assertNotIn("Author preset", content)
            self.assertIn("CRB_LONG_RUNNING_PROGRESS_SECONDS=0", content)
            self.assertFalse(any("new-session" in cmd for cmd in calls))

    def test_like_author_requires_repl_mode(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            with contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(setup.SetupError):
                    setup.setup_bridge(self.options(base, mode="exec", like_author=True), api_call=FakeApi())

    def test_interactive_wizard_asks_author_preset_and_defaults_to_yes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            prompts = []
            calls = []

            def fake_input(prompt=""):
                prompts.append(prompt)
                return ""

            def run(cmd, check=False):
                calls.append(cmd)
                return completed(cmd)

            with mock.patch("builtins.input", fake_input), contextlib.redirect_stdout(io.StringIO()):
                rc = setup.setup_bridge(self.options(base, non_interactive=False), api_call=FakeApi(), run=run)
            self.assertEqual(rc, 0)
            self.assertIn(setup.AUTHOR_PRESET_QUESTION, prompts)
            self.assertIn("Author preset", (base / "bridge.env").read_text(encoding="utf-8"))

    def test_interactive_wizard_author_preset_declined_keeps_plain_setup(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)

            def fake_input(prompt=""):
                return "n"

            with mock.patch("builtins.input", fake_input), contextlib.redirect_stdout(io.StringIO()):
                rc = setup.setup_bridge(self.options(base, non_interactive=False), api_call=FakeApi())
            self.assertEqual(rc, 0)
            self.assertNotIn("Author preset", (base / "bridge.env").read_text(encoding="utf-8"))

    def test_attach_reads_socket_and_session_from_config_and_execs_tmux(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = Path(tmpdir) / "bridge.env"
            config.write_text(
                "TAB_BRIDGE_MODE=repl\nCRB_TMUX_BIN=/usr/bin/tmux\nCRB_TMUX_SOCKET=sock1\nCRB_TMUX_SESSION=sess1\n",
                encoding="utf-8",
            )
            calls, execs = [], []

            def run(cmd, check=False):
                calls.append(cmd)
                return completed(cmd)

            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                rc = setup.attach_command(config, run=run, exec_fn=lambda binary, argv: execs.append((binary, argv)))
            self.assertEqual(rc, 0, output.getvalue())
            self.assertIn(["/usr/bin/tmux", "-L", "sock1", "has-session", "-t", "=sess1"], calls)
            self.assertEqual(execs, [("/usr/bin/tmux", ["/usr/bin/tmux", "-L", "sock1", "attach", "-t", "sess1"])])
            self.assertFalse(any("new-session" in cmd for cmd in calls))

    def test_attach_creates_missing_session_then_attaches(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = Path(tmpdir) / "bridge.env"
            config.write_text(
                "TAB_BRIDGE_MODE=repl\nTAB_AGENT_CMD=codex\nTAB_WORKDIR=" + tmpdir + "\nCRB_TMUX_SOCKET=codex\nCRB_TMUX_SESSION=codex\n",
                encoding="utf-8",
            )
            calls, execs = [], []

            def run(cmd, check=False):
                calls.append(cmd)
                if "has-session" in cmd:
                    return completed(cmd, returncode=1, stderr="no server")
                return completed(cmd)

            with mock.patch.object(setup.shutil, "which", return_value="/opt/bin/codex"), contextlib.redirect_stdout(io.StringIO()):
                rc = setup.attach_command(config, run=run, exec_fn=lambda binary, argv: execs.append(argv))
            self.assertEqual(rc, 0)
            self.assertIn(["tmux", "-L", "codex", "new-session", "-d", "-s", "codex", "-c", tmpdir, "codex"], calls)
            self.assertEqual(execs, [["tmux", "-L", "codex", "attach", "-t", "codex"]])

    def test_attach_with_another_client_warns_but_never_detaches_it(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = Path(tmpdir) / "bridge.env"
            config.write_text("TAB_BRIDGE_MODE=repl\nCRB_TMUX_SOCKET=codex\nCRB_TMUX_SESSION=codex\n", encoding="utf-8")
            execs = []

            def run(cmd, check=False):
                if "list-clients" in cmd:
                    return completed(cmd, stdout="/dev/ttys002: codex [107x55 xterm-256color]\n")
                return completed(cmd)

            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                rc = setup.attach_command(config, run=run, exec_fn=lambda binary, argv: execs.append(argv))
            self.assertEqual(rc, 0)
            self.assertIn("another terminal", output.getvalue())
            self.assertNotIn("-d", execs[0])

    def test_attach_refuses_exec_mode_config(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = Path(tmpdir) / "bridge.env"
            config.write_text("TAB_BRIDGE_MODE=exec\n", encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()):
                rc = setup.attach_command(config, run=lambda cmd, check=False: completed(cmd), exec_fn=lambda *a: self.fail("must not attach"))
            self.assertEqual(rc, 2)

    def test_codex_sessions_outside_bridge_matches_comm_basename_and_tty(self):
        ps_text = (
            "27835 ttys003 /opt/homebrew/bin/codex\n"
            "30001 ttys007 /opt/homebrew/bin/codex\n"
            "30002 ?? codex\n"
            "30003 ttys009 /usr/bin/node\n"
        )
        self.assertEqual(setup.codex_sessions_outside_bridge("/dev/ttys003", ps_text), [("30001", "ttys007")])
        self.assertEqual(setup.codex_sessions_outside_bridge("", ps_text), [])

    def test_doctor_warns_when_codex_runs_outside_bridge_tmux(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            env_file = base / "bridge.env"
            runner = base / "bridge-run"
            state_dir = base / "state"
            setup.write_env_config(
                env_file, mode="repl", token="token", chat_id="12345", agent="codex", agent_cmd="python3",
                workdir=base, prefix="BOT", prefix_line=False, state_dir=state_dir,
                local_input=state_dir / "input.fifo", dangerous_bypass=False, tmux_socket="codex",
                tmux_session="codex", submit_key="Tab", audio_transcribe_cmd="",
            )
            setup.install_runner(runner, env_file)

            def run(cmd, check=False):
                if cmd[0] == "tmux" and "display-message" in cmd:
                    return completed(cmd, stdout="/dev/ttys003\n")
                if cmd[0] == "tmux":
                    return completed(cmd)
                if cmd[0] == "ps":
                    return completed(cmd, stdout="27835 ttys003 /opt/homebrew/bin/python3\n30001 ttys007 /opt/homebrew/bin/python3\n")
                return completed(cmd, stdout="active\n")

            output = io.StringIO()
            # The REPL bridge module exists only in the built package; any file stands in.
            with mock.patch.object(setup, "service_status", return_value="active"), \
                    mock.patch.object(setup, "watchdog_status", return_value="active"), \
                    mock.patch.object(setup, "REPL_BRIDGE_SCRIPT", runner), \
                    mock.patch.object(setup.shutil, "which", return_value="/usr/bin/tmux"), \
                    mock.patch.object(setup.sys, "platform", "linux"), contextlib.redirect_stdout(output):
                rc = setup.doctor(env_file, runner, api_call=FakeApi(), run=run)
            text = output.getvalue()
            self.assertEqual(rc, 0, text)
            self.assertIn("outside the bridge tmux session", text)
            self.assertIn("pid 30001 on ttys007", text)
            self.assertNotIn("pid 27835", text)  # the bridge pane's own process is not a stray session
            self.assertIn(setup.ATTACH_COMMAND, text)
            self.assertIn("1 warning(s)", text)

    def self_check(self, *, log_texts=(), api_calls=None, **kwargs):
        clock = {"now": 1000.0}
        tmux_calls = []

        def api(token, method, timeout=60, **params):
            if api_calls is not None:
                api_calls.append((method, params))
            return {"ok": True, "result": {"message_id": 1}}

        def run(cmd, check=False):
            if cmd[0] == "tmux":
                tmux_calls.append(cmd)
            return completed(cmd)

        def sleep(seconds):
            clock["now"] += seconds

        class Tail:
            def __init__(self, texts):
                self.texts = list(texts)

            def read_new(self):
                return self.texts.pop(0) if self.texts else ""

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            lines = setup.author_self_check(
                token="token", chat_id="12345",
                config={"CRB_TMUX_SOCKET": "codex", "CRB_TMUX_SESSION": "codex", "CRB_TMUX_SUBMIT_KEY": "Tab"},
                os_name="Darwin", api_call=api, run=run, sleep=sleep, clock=lambda: clock["now"],
                log_tail=Tail(log_texts), phone_timeout=10, terminal_timeout=10, **kwargs,
            )
        return lines, output.getvalue(), tmux_calls

    def test_self_check_reports_both_timeouts_and_sends_summary(self):
        api_calls = []
        lines, text, tmux_calls = self.self_check(api_calls=api_calls)
        self.assertEqual(len(lines), 3)
        self.assertIn("no phone message", lines[1])
        self.assertIn("doctor", lines[1])
        self.assertIn("no terminal mirror", lines[2])
        # Pane target "=codex:" (tmux 3.6 rejects a bare "=codex" for send-keys:
        # "can't find pane"), the same target the bridge itself types into.
        self.assertIn(["tmux", "-L", "codex", "send-keys", "-t", "=codex:", "-l", setup.SELF_CHECK_PROBE], tmux_calls)
        self.assertIn(["tmux", "-L", "codex", "send-keys", "-t", "=codex:", "Tab"], tmux_calls)
        self.assertLess(
            tmux_calls.index(["tmux", "-L", "codex", "send-keys", "-t", "=codex:", "-l", setup.SELF_CHECK_PROBE]),
            tmux_calls.index(["tmux", "-L", "codex", "send-keys", "-t", "=codex:", "Tab"]),
        )
        summary = api_calls[-1]
        self.assertEqual(summary[0], "sendMessage")
        self.assertIn("no phone message", summary[1]["text"])
        self.assertIn("no terminal mirror", text)

    def test_self_check_passes_when_log_shows_both_directions(self):
        lines, _text, _calls = self.self_check(
            log_texts=[
                "[12:00:00] JSONL matched Telegram-origin prompt\n",
                "[12:00:03] SEND  Telegram mirror handled from telegram\n",
                "[12:00:10] SEND  sent terminal-origin directive card\n",
            ],
        )
        self.assertIn("phone -> terminal -> phone: ok", lines[1])
        self.assertIn("terminal -> phone: ok", lines[2])

    def test_self_check_reports_arrival_without_answer(self):
        lines, _text, _calls = self.self_check(log_texts=["[12:00:00] JSONL matched queued Telegram-origin prompt mid=3\n"])
        self.assertIn("reached the terminal", lines[1])

    def test_parser_and_pyproject_expose_like_author_attach_and_codex_tg(self):
        parser = setup.build_parser()
        args = parser.parse_args(["setup", "--like-author", "--create-session"])
        self.assertTrue(args.like_author)
        self.assertTrue(args.create_session)
        attach = parser.parse_args(["attach", "--no-create"])
        self.assertEqual(attach.command, "attach")
        self.assertTrue(attach.no_create)
        pyproject = (Path(setup.__file__).resolve().parent / "pyproject.toml").read_text(encoding="utf-8")
        self.assertIn('codex-tg = "bridge_setup:attach_main"', pyproject)

    def test_wizard_without_a_terminal_keeps_plain_setup(self):
        # A provisioning script that passes --token/--chat-id but not --non-interactive
        # has no terminal to answer the new first question; it must not crash.
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            with mock.patch("builtins.input", side_effect=EOFError), contextlib.redirect_stdout(io.StringIO()):
                rc = setup.setup_bridge(self.options(base, non_interactive=False), api_call=FakeApi())
            self.assertEqual(rc, 0)
            self.assertNotIn("Author preset", (base / "bridge.env").read_text(encoding="utf-8"))

    def test_self_check_first_line_reports_the_setup_message_result(self):
        for sent, expected in ((True, "1/3 bot -> phone: ok"), (False, "not delivered"), (None, "skipped")):
            with self.subTest(sent=sent):
                lines, _text, _calls = self.self_check(bot_message_sent=sent)
                self.assertIn(expected, lines[0])

    def test_dry_run_prints_the_plan_without_writing_or_contacting_telegram(self):
        for like_author in (True, False):
            with self.subTest(like_author=like_author), tempfile.TemporaryDirectory() as tmpdir:
                base = Path(tmpdir)
                api = FakeApi()
                calls = []

                def run(cmd, check=False):
                    calls.append(cmd)
                    return completed(cmd)

                output = io.StringIO()
                with contextlib.redirect_stdout(output), mock.patch("builtins.input", side_effect=AssertionError("no prompt")):
                    rc = setup.setup_bridge(
                        self.options(base, like_author=like_author, dry_run=True, token=None, chat_id=None,
                                     non_interactive=False),
                        api_call=api,
                        run=run,
                    )
                text = output.getvalue()
                self.assertEqual(rc, 0, text)
                self.assertEqual((api.calls, calls), ([], []))
                self.assertEqual(sorted(path.name for path in base.iterdir()), [])
                self.assertIn("Dry run", text)
                for key, value in setup.AUTHOR_PRESET_ENV:
                    self.assertEqual(f"{key}={value}" in text, like_author, key)
                self.assertEqual(setup.ATTACH_COMMAND in text, like_author)
                self.assertEqual("self-check" in text, like_author)

    def test_like_author_keeps_an_existing_tmux_session(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            calls = []

            def run(cmd, check=False):
                calls.append(cmd)
                return completed(cmd)

            with contextlib.redirect_stdout(io.StringIO()):
                rc = setup.setup_bridge(self.options(base, like_author=True), api_call=FakeApi(), run=run)
            self.assertEqual(rc, 0)
            self.assertIn(["tmux", "-L", "codex", "has-session", "-t", "=codex"], calls)
            self.assertFalse(any("new-session" in cmd or "kill-session" in cmd for cmd in calls))

    def test_like_author_refuses_native_windows(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            with mock.patch.object(setup.platform, "system", return_value="Windows"), \
                    contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(setup.SetupError):
                    setup.setup_bridge(self.options(base, like_author=True), api_call=FakeApi())
            self.assertFalse((base / "bridge.env").exists())

    def test_like_author_runs_self_check_only_after_the_service_started(self):
        for start, status, expected in ((True, "active", 1), (False, "active", 0), (True, "failed", 0)):
            with self.subTest(start=start, status=status), tempfile.TemporaryDirectory() as tmpdir:
                base = Path(tmpdir)
                output = io.StringIO()
                with mock.patch.object(setup, "install_service", return_value=base / "unit"), \
                        mock.patch.object(setup, "install_watchdog", return_value=base / "timer"), \
                        mock.patch.object(setup, "service_status", return_value=status), \
                        mock.patch.object(setup, "watchdog_status", return_value="active"), \
                        mock.patch.object(setup, "author_self_check", return_value=[]) as check, \
                        mock.patch.object(setup.shutil, "which", return_value="/opt/bin/codex"), \
                        contextlib.redirect_stdout(output):
                    rc = setup.setup_bridge(
                        self.options(base, like_author=True, install_service=True, start_service=start, send_test=True),
                        api_call=FakeApi(),
                        run=lambda cmd, check=False: completed(cmd),
                    )
                self.assertEqual(rc, 0, output.getvalue())
                self.assertEqual(check.call_count, expected)
                if expected:
                    self.assertIs(check.call_args.kwargs["bot_message_sent"], True)
                    self.assertEqual(check.call_args.kwargs["config"]["CRB_FLOW_MIRROR"], "1")
                else:
                    self.assertIn("Self-check skipped", output.getvalue())

    def test_main_and_codex_tg_dispatch_to_attach(self):
        with mock.patch.object(setup, "attach_command", return_value=0) as attach:
            self.assertEqual(setup.main(["attach", "--no-create"]), 0)
            self.assertEqual(setup.attach_main([]), 0)
        self.assertEqual(attach.call_count, 2)
        self.assertFalse(attach.call_args_list[0].kwargs["create_missing"])
        self.assertTrue(attach.call_args_list[1].kwargs["create_missing"])

    def test_attach_without_tmux_reports_instead_of_crashing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = Path(tmpdir) / "bridge.env"
            config.write_text("TAB_BRIDGE_MODE=repl\n", encoding="utf-8")

            def run(cmd, check=False):
                raise FileNotFoundError(cmd[0])

            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                rc = setup.attach_command(config, run=run, exec_fn=lambda *a: self.fail("must not attach"))
            self.assertEqual(rc, 2)
            self.assertIn("tmux", output.getvalue())

    def test_log_tail_returns_only_new_text(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            log = Path(tmpdir) / "bridge.log"
            log.write_text("old line\n", encoding="utf-8")
            tail = setup.BridgeLogTail("Darwin", path=log)
            self.assertEqual(tail.read_new(), "")
            with log.open("a", encoding="utf-8") as handle:
                handle.write("new line\n")
            self.assertEqual(tail.read_new(), "new line\n")
            self.assertEqual(tail.read_new(), "")
        journal = ["a\n", "a\nb\n", "a\nb\n"]
        commands = []

        def run(cmd, check=False):
            commands.append(cmd)
            return completed(cmd, stdout=journal.pop(0))

        tail = setup.BridgeLogTail("Linux", run=run, since=100.0)
        self.assertEqual([tail.read_new(), tail.read_new(), tail.read_new()], ["a\n", "b\n", ""])
        self.assertIn("--since", commands[0])
        self.assertIn("@100", commands[0])


if __name__ == "__main__":
    unittest.main()
