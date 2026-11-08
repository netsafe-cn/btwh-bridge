# -*- coding: utf-8 -*-
"""test_service.py - BoschService resident-layer tests

Fully offline: no asyncio thread, no Bluetooth, and the real config.json is never written.
Covers the synchronous surface only: queue semantics, live config reload, code validation.

Run:  python run_tests.py -v   (from the repository root)
"""
import threading
import unittest
from unittest import mock

from bosch_api import BoschService, Config, DeviceState


def make_cfg():
    return Config(device_address="AA:BB:CC:DD:EE:FF",
                  login_mac="C0335E0FF68A",
                  pair_code="123456")


class TestServiceInitialState(unittest.TestCase):

    def setUp(self):
        self.svc = BoschService(make_cfg())

    def test_not_running_before_start(self):
        self.assertFalse(self.svc.running)

    def test_initial_public_state(self):
        self.assertIsNone(self.svc.state)
        self.assertEqual(self.svc.status, "not started")
        self.assertEqual(self.svc.login_state, "not logged in")
        self.assertFalse(self.svc.connected)
        self.assertEqual(self.svc.results, {})


class TestServiceCommandQueues(unittest.TestCase):

    def setUp(self):
        self.svc = BoschService(make_cfg())

    def test_set_temperature_enqueues_without_blocking(self):
        self.svc.set_temperature(42)
        self.assertEqual(self.svc._cmd_q.get_nowait(), 42)

    def test_query_history_enqueues_kind(self):
        self.svc.query_history(3)
        self.assertEqual(self.svc._req_q.get_nowait(), ("history", 3))

    def test_query_history_default_kind(self):
        self.svc.query_history()
        self.assertEqual(self.svc._req_q.get_nowait(), ("history", 2))

    def test_query_errors_default_count(self):
        self.svc.query_errors()
        self.assertEqual(self.svc._req_q.get_nowait(), ("errors", 10))

    def test_queues_are_independent(self):
        """Temperature commands and read-only queries must not share one queue"""
        self.svc.set_temperature(45)
        self.svc.query_errors(5)
        self.assertEqual(self.svc._cmd_q.get_nowait(), 45)
        self.assertEqual(self.svc._req_q.get_nowait(), ("errors", 5))

    def test_take_logs_drains_completely(self):
        self.svc._log_q.put("a")
        self.svc._log_q.put("b")
        self.assertEqual(self.svc.take_logs(), ["a", "b"])
        self.assertEqual(self.svc.take_logs(), [])

    def test_log_is_timestamped(self):
        self.svc._log("hello")
        self.assertTrue(self.svc.take_logs()[0].endswith("hello"))


class TestServiceConfigHotReload(unittest.TestCase):

    def setUp(self):
        self.svc = BoschService(make_cfg())

    def test_update_config_swaps_and_flags_reload(self):
        cfg2 = make_cfg()
        cfg2.pair_code = "654321"
        self.assertFalse(self.svc._reload)
        self.svc.update_config(cfg2)
        self.assertIs(self.svc.cfg, cfg2)
        self.assertTrue(self.svc._reload)

    def test_bad_pair_code_rejected(self):
        for bad in ("", "12345", "abcdef", "1234567", "12 456", None, 123456):
            with self.subTest(code=bad):
                with self.assertRaises(ValueError):
                    self.svc.set_pair_code_and_reconnect(bad, save=False)

    def test_valid_pair_code_updates_and_reloads(self):
        self.svc.set_pair_code_and_reconnect("999999", save=False)
        self.assertEqual(self.svc.cfg.pair_code, "999999")
        self.assertTrue(self.svc._reload)
        self.assertTrue(any("reconnecting now" in x for x in self.svc.take_logs()))

    def test_pair_code_is_stripped(self):
        self.svc.set_pair_code_and_reconnect("  888888  ", save=False)
        self.assertEqual(self.svc.cfg.pair_code, "888888")

    def test_save_true_persists(self):
        with mock.patch.object(self.svc.cfg, "save") as save:
            self.svc.set_pair_code_and_reconnect("888888", save=True)
            save.assert_called_once_with()

    def test_save_false_never_writes_disk(self):
        """Tests must never touch the user's real config.json"""
        with mock.patch.object(self.svc.cfg, "save") as save:
            self.svc.set_pair_code_and_reconnect("888888", save=False)
            save.assert_not_called()


class TestServiceLifecycle(unittest.TestCase):

    def setUp(self):
        self.svc = BoschService(make_cfg())

    def test_stop_without_start_is_safe(self):
        self.svc.stop(timeout=0)
        self.assertTrue(self.svc._stop_flag)
        self.assertIsNone(self.svc._cmd_q.get_nowait())

    def test_start_is_idempotent(self):
        """A second start must not spawn a second thread, or two clients fight for the device"""
        gate = threading.Event()

        def fake_run(self):
            gate.wait(2)

        with mock.patch.object(BoschService, "_run", fake_run):
            try:
                self.svc.start()
                first = self.svc._thread
                self.assertTrue(self.svc.running)
                self.assertTrue(first.daemon)
                self.svc.start()
                self.assertIs(self.svc._thread, first)
            finally:
                gate.set()
                self.svc._thread.join(timeout=1)

    def test_set_state_invokes_callback_and_counts_frames(self):
        seen = []
        self.svc.on_state = lambda st: seen.append(st)
        st = DeviceState(temp_out=45.0)
        self.svc._set_state(st)
        self.assertIs(self.svc.state, st)
        self.assertEqual(self.svc._rx_frames, 1)
        self.assertEqual(seen, [st])


class TestServicePairFailure(unittest.TestCase):
    """The service must surface the pairing failure reason, not only log it."""

    def setUp(self):
        self.svc = BoschService(make_cfg())

    def test_initial_no_failure(self):
        self.assertIsNone(self.svc.pair_fail_reason)
        self.assertEqual(self.svc.pair_fail_hint(), "")

    def test_hint_for_window_closed(self):
        from bosch_api import PAIR_FAIL_WINDOW_CLOSED
        self.svc.pair_fail_reason = PAIR_FAIL_WINDOW_CLOSED
        self.assertIn("Window", self.svc.pair_fail_hint())

    def test_hint_for_cooldown(self):
        from bosch_api import PAIR_FAIL_COOLDOWN
        self.svc.pair_fail_reason = PAIR_FAIL_COOLDOWN
        self.assertIn("rate limit", self.svc.pair_fail_hint())

    def test_hint_empty_for_unknown_reason(self):
        self.svc.pair_fail_reason = "not_a_real_reason"
        self.assertEqual(self.svc.pair_fail_hint(), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
