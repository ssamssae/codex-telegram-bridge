#!/usr/bin/env python3
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import bridge_watchdog as watchdog


def completed(cmd, returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(cmd, returncode, stdout, stderr)


class FakeSystemd:
    def __init__(self, active):
        self.active = active
        self.commands = []

    def __call__(self, cmd):
        self.commands.append(tuple(cmd))
        if cmd[:3] == ["systemctl", "--user", "is-active"]:
            if self.active:
                return completed(cmd, stdout="active\n")
            return completed(cmd, returncode=3, stdout="inactive\n")
        if cmd[:3] == ["systemctl", "--user", "start"]:
            self.active = True
            return completed(cmd)
        return completed(cmd)


class FakeLaunchd:
    def __init__(self, state):
        self.state = state
        self.commands = []

    def __call__(self, cmd):
        self.commands.append(tuple(cmd))
        if cmd[:2] == ["launchctl", "print"]:
            if self.state == "missing":
                return completed(cmd, returncode=113, stderr="Could not find service\n")
            return completed(cmd, stdout=f"\tstate = {self.state}\n")
        if cmd[:3] == ["launchctl", "kickstart", "-k"]:
            self.state = "running"
            return completed(cmd)
        if cmd[:2] == ["launchctl", "bootstrap"]:
            self.state = "not running"
            return completed(cmd)
        return completed(cmd)


class WatchdogTests(unittest.TestCase):
    def test_macos_gui_domain_falls_back_when_getuid_is_unavailable(self):
        with mock.patch.object(watchdog.os, "getuid", None, create=True):
            self.assertEqual(watchdog.mac_gui_domain(), "gui/0")

    def test_linux_inactive_service_is_started_and_marked_recovered(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            status_file = Path(tmpdir) / "status"
            run = FakeSystemd(active=False)

            rc = watchdog.watch_once(
                os_name="Linux",
                status_file=status_file,
                run=run,
                settle_seconds=0,
            )

            self.assertEqual(rc, 0)
            self.assertIn(("systemctl", "--user", "start", watchdog.SERVICE_NAME), run.commands)
            self.assertIn("status=recovered", status_file.read_text(encoding="utf-8"))

    def test_linux_active_service_is_not_restarted(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            status_file = Path(tmpdir) / "status"
            run = FakeSystemd(active=True)

            rc = watchdog.watch_once(
                os_name="Linux",
                status_file=status_file,
                run=run,
                settle_seconds=0,
            )

            self.assertEqual(rc, 0)
            self.assertNotIn(("systemctl", "--user", "start", watchdog.SERVICE_NAME), run.commands)
            self.assertIn("status=active", status_file.read_text(encoding="utf-8"))

    def test_macos_stopped_service_is_kickstarted(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            plist = base / "bridge.plist"
            plist.write_text("plist", encoding="utf-8")
            status_file = base / "status"
            run = FakeLaunchd(state="not running")

            rc = watchdog.watch_once(
                os_name="Darwin",
                launchd_plist=plist,
                status_file=status_file,
                run=run,
                settle_seconds=0,
            )

            self.assertEqual(rc, 0)
            self.assertIn("kickstart", [part for cmd in run.commands for part in cmd])
            self.assertIn("status=recovered", status_file.read_text(encoding="utf-8"))

    def test_macos_missing_service_bootstraps_when_plist_exists(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            plist = base / "bridge.plist"
            plist.write_text("plist", encoding="utf-8")
            status_file = base / "status"
            run = FakeLaunchd(state="missing")

            rc = watchdog.watch_once(
                os_name="Darwin",
                launchd_plist=plist,
                status_file=status_file,
                run=run,
                settle_seconds=0,
            )

            self.assertEqual(rc, 0)
            self.assertIn("bootstrap", [part for cmd in run.commands for part in cmd])
            self.assertIn("status=recovered", status_file.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
