import importlib.util
import os
import sys
import unittest
from pathlib import Path
from unittest import mock


def _load_bridge(alias):
    path = Path(__file__).resolve().parents[1] / "codex_repl_bridge.py"
    spec = importlib.util.spec_from_file_location(alias, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[alias] = mod
    spec.loader.exec_module(mod)
    return mod


class ViewerBudgetSmokeTest(unittest.TestCase):
    """New-generation smoke for the viewer send budget (replaces the drifted
    old-generation test_repl_bridge suite). A viewer-lane send must fast-fail on
    the first failed chunk under a bounded call budget so a broken viewer never
    holds the worker thread hostage."""

    def setUp(self):
        self.mod = _load_bridge("codex_repl_bridge_viewer_smoke")

    def test_viewer_send_fast_fails_after_first_chunk_with_bounded_budget(self):
        client = self.mod.TelegramClient("token", "123456789", "BOT", 4096)
        calls = []

        def fake_call(method, **params):
            calls.append((method, params))
            return {"ok": False}

        client.call = fake_call
        long_text = "x" * 9000  # spans several 4096 chunks

        calls.clear()
        self.assertFalse(client.send(long_text, viewer=True))
        viewer_calls = len(calls)
        # Fast-fail: gives up on the first failed chunk instead of walking all.
        self.assertEqual(viewer_calls, 1)
        # The bounded budget rides on the wire call (single attempt, no retry).
        self.assertEqual(calls[0][1].get("_attempts"), 1)
        self.assertEqual(calls[0][1].get("_retry_delay"), 0)

        calls.clear()
        self.assertFalse(client.send(long_text, viewer=False))
        # Non-viewer keeps trying every chunk (no fast-fail, no budget kwargs).
        self.assertGreater(len(calls), viewer_calls)
        self.assertNotIn("_attempts", calls[0][1])


class FlowMirrorToggleSmokeTest(unittest.TestCase):
    """New-generation smoke for the opt-in REPL flow mirror. It is default-OFF
    and the CRB_FLOW_MIRROR env override wins in both directions."""

    def setUp(self):
        self.mod = _load_bridge("codex_repl_bridge_flow_smoke")

    def test_flow_mirror_env_toggles_on_and_off(self):
        with mock.patch("os.path.exists", return_value=False):
            with mock.patch.dict(os.environ, {"CRB_FLOW_MIRROR": "1"}, clear=False):
                self.assertTrue(self.mod.flow_mirror_enabled())
            with mock.patch.dict(os.environ, {"CRB_FLOW_MIRROR": "0"}, clear=False):
                self.assertFalse(self.mod.flow_mirror_enabled())
            with mock.patch.dict(os.environ, {}, clear=True):
                self.assertFalse(self.mod.flow_mirror_enabled())


if __name__ == "__main__":
    unittest.main()
