#!/usr/bin/env python3
import contextlib
import io
import subprocess
import tempfile
import unittest
from pathlib import Path

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

            self.assertEqual(setup.file_mode(env_file), 0o600)
            values = setup.load_env_file(env_file)
            self.assertEqual(values["TAB_BOT_TOKEN"], "123:secret")
            self.assertEqual(values["TAB_CHAT_ID"], "12345")
            self.assertEqual(values["TAB_BRIDGE_MODE"], "repl")
            self.assertEqual(values["TAB_PREFIX_LINE"], "1")
            self.assertEqual(values["CRB_SIGNAL_PATH"], str(state_dir / "input.fifo"))
            self.assertEqual(values["CRB_TMUX_SOCKET"], "codex")

    def test_install_runner_sources_private_env(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            runner = Path(tmpdir) / "bridge-run"
            env_file = Path(tmpdir) / "bridge.env"
            setup.install_runner(runner, env_file)

            text = runner.read_text(encoding="utf-8")
            self.assertIn(str(env_file), text)
            self.assertIn(str(setup.REPL_BRIDGE_SCRIPT), text)
            self.assertIn(str(setup.EXEC_BRIDGE_SCRIPT), text)
            self.assertIn("TAB_BRIDGE_MODE", text)
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

    def test_install_service_windows_installs_startup_launcher_without_schtasks(self):
        commands = []

        def run(cmd, check=False):
            commands.append(tuple(cmd))
            return completed(cmd)

        with tempfile.TemporaryDirectory() as tmpdir:
            startup_dir = Path(tmpdir) / "Startup"
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
            self.assertIn("C:\\Program Files\\Git\\bin\\bash.exe", launcher_text)
            self.assertIn("/c/Users/gb/.local/bin/telegram-agent-bridge-run", launcher_text)
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
                self.assertEqual(cmd[:4], ["schtasks", "/Query", "/TN", "telegram-agent-bridge"])
                return completed(cmd, returncode=1, stderr="ERROR: Access is denied.\r\n")

            self.assertEqual(
                setup.service_status(
                    os_name="Windows",
                    run=missing_task,
                    task_name="telegram-agent-bridge",
                    windows_startup_dir=startup_dir,
                ),
                "startup-installed",
            )

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

    def test_setup_complete_message_treats_windows_startup_launcher_as_ready(self):
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
        self.assertIn("Windows Startup autostart is installed", params["text"])
        self.assertIn("/ping", params["text"])
        self.assertNotIn("Start it with", params["text"])

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
            with contextlib.redirect_stdout(buffer):
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


if __name__ == "__main__":
    unittest.main()
