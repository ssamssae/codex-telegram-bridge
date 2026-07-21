import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path


def _load_host(alias):
    path = Path(__file__).resolve().parents[1] / "codex_repl_host_windows.py"
    spec = importlib.util.spec_from_file_location(alias, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[alias] = mod
    spec.loader.exec_module(mod)
    return mod


class TransportSessionDeferSmokeTest(unittest.TestCase):
    """New-generation smoke for the ConPTY session binder (replaces the drifted
    old-generation test_repl_transport suite). Binding is deferred until a
    session file appears *after* first input, so a foreign/hijack session that
    turned earlier is never bound. The host module loads on the public CI matrix
    (POSIX + Windows), matching the internal transport contract."""

    @classmethod
    def setUpClass(cls):
        cls.host = _load_host("codex_repl_host_transport_smoke")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "sessions"
        self.root.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def test_binder_defers_sessions_older_than_first_input(self):
        binder = self.host.SessionBinder([self.root], launched_ns=0)
        foreign = self.root / "rollout-foreign.jsonl"
        foreign.write_text("{}\n", encoding="utf-8")
        first_input_ns = foreign.stat().st_mtime_ns + 1_000_000_000
        # A session that turned before first input must not be bindable.
        self.assertIsNone(binder.bind_once(min_mtime_ns=first_input_ns))
        # A session that appears after first input binds.
        child = self.root / "rollout-child.jsonl"
        child.write_text("{}\n", encoding="utf-8")
        os.utime(child, ns=(first_input_ns + 1, first_input_ns + 1))
        self.assertEqual(binder.bind_once(min_mtime_ns=first_input_ns), child.resolve())

    def test_wait_for_session_defers_until_first_input(self):
        binder = self.host.SessionBinder([self.root], launched_ns=0)
        hijacker = self.root / "rollout-hijack.jsonl"
        hijacker.write_text("{}\n", encoding="utf-8")
        # No first input yet -> binding is deferred -> the wait times out.
        with self.assertRaises(TimeoutError):
            self.host.wait_for_session(binder, 0.3, input_activity={"first_ns": 0})


if __name__ == "__main__":
    unittest.main()
