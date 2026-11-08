# -*- coding: utf-8 -*-
"""test_fsm.py —— 配网/配对状态机单元测试

重点验「正交性」：配网失败 / 重置配网，绝不能影响热水器配对状态与 bond。
全部确定性，不连硬件、不用真实时钟（now 由用例注入）。

运行：  python -m unittest test_fsm -v
"""
import unittest

from device_fsm import (
    BridgeFsm, DeviceMode, FsmConfig, NVS_LAYOUT, PairState, WifiState,
)


class TestBoot(unittest.TestCase):

    def test_no_credentials_goes_setup(self):
        f = BridgeFsm()
        self.assertEqual(f.on_boot(False, False, False), DeviceMode.SETUP)
        self.assertEqual(f.wifi, WifiState.UNPROVISIONED)
        self.assertEqual(f.pair, PairState.IDLE)

    def test_credentials_go_normal(self):
        f = BridgeFsm()
        self.assertEqual(f.on_boot(True, False, False), DeviceMode.NORMAL)
        self.assertEqual(f.wifi, WifiState.CONNECTING)

    def test_bond_and_pin_skip_smp(self):
        """重启后已绑定且码还在 → 直接走应用层登录，不必再去面板"""
        f = BridgeFsm()
        f.on_boot(True, True, True)
        self.assertEqual(f.pair, PairState.PIN_READY)

    def test_bond_without_pin_stays_idle(self):
        f = BridgeFsm()
        f.on_boot(True, True, False)
        self.assertEqual(f.pair, PairState.IDLE)


class TestWifiAxis(unittest.TestCase):

    def setUp(self):
        self.f = BridgeFsm(FsmConfig(wifi_fail_limit=3))
        self.f.on_boot(True, False, False)

    def test_happy_path(self):
        self.f.request_setup()
        self.assertEqual(self.f.wifi, WifiState.PROVISIONING)
        self.f.provisioning_done("HomeWiFi")
        self.assertEqual(self.f.wifi, WifiState.CONNECTING)
        self.f.wifi_result(True)
        self.assertEqual(self.f.wifi, WifiState.CONNECTED)
        self.assertEqual(self.f.mode, DeviceMode.NORMAL)

    def test_empty_ssid_rejected(self):
        with self.assertRaises(ValueError):
            self.f.provisioning_done("")

    def test_failures_enter_recovery_at_limit(self):
        self.assertEqual(self.f.wifi_result(False), DeviceMode.NORMAL)
        self.assertEqual(self.f.wifi_result(False), DeviceMode.NORMAL)
        self.assertEqual(self.f.wifi_result(False), DeviceMode.RECOVERY)
        self.assertEqual(self.f.wifi_fail, 3)

    def test_success_clears_recovery(self):
        for _ in range(3):
            self.f.wifi_result(False)
        self.assertEqual(self.f.mode, DeviceMode.RECOVERY)
        self.f.wifi_result(True)
        self.assertEqual(self.f.mode, DeviceMode.NORMAL)
        self.assertEqual(self.f.wifi_fail, 0)
        self.assertIsNone(self.f.last_error)

    def test_recovery_opens_provisioning(self):
        for _ in range(3):
            self.f.wifi_result(False)
        self.assertEqual(self.f.wifi, WifiState.PROVISIONING)

    def test_retry_blocked_in_recovery(self):
        for _ in range(3):
            self.f.wifi_result(False)
        self.assertEqual(self.f.wifi_retry(), WifiState.PROVISIONING)

    def test_retry_after_transient_failure(self):
        self.f.wifi_result(False)
        self.assertEqual(self.f.wifi_retry(), WifiState.CONNECTING)


class TestOrthogonality(unittest.TestCase):
    """本状态机最核心的设计约束：两条轴互不干扰。"""

    def setUp(self):
        self.f = BridgeFsm(FsmConfig(wifi_fail_limit=2))
        self.f.on_boot(True, True, True)
        self.f.submit_pin("311522", now=0)
        self.f.smp_result(True)
        self.f.login_result(True)
        self.assertEqual(self.f.pair, PairState.ONLINE)

    def test_wifi_failure_does_not_touch_pair(self):
        self.f.wifi_result(False)
        self.f.wifi_result(False)
        self.assertEqual(self.f.mode, DeviceMode.RECOVERY)
        self.assertEqual(self.f.pair, PairState.ONLINE)
        self.assertTrue(self.f.has_bond)
        self.assertTrue(self.f.has_pin)

    def test_reset_wifi_only_keeps_bond_and_pin(self):
        """换路由器不需要再去面板清除配对"""
        self.f.reset_wifi_only()
        self.assertEqual(self.f.mode, DeviceMode.SETUP)
        self.assertEqual(self.f.wifi, WifiState.PROVISIONING)
        self.assertEqual(self.f.pair, PairState.ONLINE)
        self.assertTrue(self.f.has_bond)
        self.assertTrue(self.f.has_pin)
        self.assertIsNone(self.f.ssid)

    def test_request_setup_does_not_reset_pair(self):
        self.f.request_setup()
        self.assertEqual(self.f.pair, PairState.ONLINE)

    def test_pairing_works_even_in_setup_mode(self):
        """没配 WiFi 时也能先跟热水器配对（串口调试阶段就是这么用的）"""
        f = BridgeFsm()
        f.on_boot(False, False, False)
        f.request_pairing(now=0)
        self.assertEqual(f.submit_pin("123456", now=1), PairState.BONDING)
        self.assertEqual(f.smp_result(True), PairState.LOGIN)

    def test_nvs_bond_managed_externally(self):
        """NimBLE bond 不能由配网流程删除"""
        self.assertEqual(NVS_LAYOUT["bond"]["managed_by"], "NimBLE")
        self.assertNotIn("bond", NVS_LAYOUT["wifi"]["keys"])
        self.assertNotIn("bond", NVS_LAYOUT["heater"]["keys"])


class TestPairAxis(unittest.TestCase):

    def setUp(self):
        self.f = BridgeFsm(FsmConfig(pair_window_sec=60))
        self.f.on_boot(True, False, False)

    def test_happy_path(self):
        self.assertEqual(self.f.request_pairing(now=0), PairState.WINDOW_WAIT)
        self.assertEqual(self.f.submit_pin("311522", now=1), PairState.BONDING)
        self.assertEqual(self.f.smp_result(True), PairState.LOGIN)
        self.assertEqual(self.f.login_result(True), PairState.ONLINE)
        self.assertTrue(self.f.has_bond)
        self.assertIsNone(self.f.pin_deadline)

    def test_bad_pin_rejected(self):
        self.f.request_pairing(now=0)
        for bad in ("", "12345", "abcdef", "1234567", None, 123456):
            with self.subTest(code=bad):
                with self.assertRaises(ValueError):
                    self.f.submit_pin(bad, now=1)

    def test_pin_is_stripped(self):
        self.f.request_pairing(now=0)
        self.f.submit_pin("  311522  ", now=1)
        self.assertEqual(self.f._pin, "311522")

    def test_smp_failure(self):
        self.f.request_pairing(now=0)
        self.f.submit_pin("311522", now=1)
        self.assertEqual(self.f.smp_result(False), PairState.FAILED)
        self.assertIn("SMP", self.f.last_error)

    def test_login_failure(self):
        self.f.request_pairing(now=0)
        self.f.submit_pin("311522", now=1)
        self.f.smp_result(True)
        self.assertEqual(self.f.login_result(False), PairState.FAILED)
        self.assertIn("A2/08", self.f.last_error)

    def test_retry_clears_pin(self):
        self.f.request_pairing(now=0)
        self.f.submit_pin("311522", now=1)
        self.f.smp_result(False)
        self.f.pair_retry()
        self.assertEqual(self.f.pair, PairState.IDLE)
        self.assertFalse(self.f.has_pin)
        self.assertIsNone(self.f._pin)

    def test_window_timeout(self):
        self.f.request_pairing(now=0)
        self.assertEqual(self.f.tick(now=59), PairState.WINDOW_WAIT)
        self.assertEqual(self.f.tick(now=61), PairState.FAILED)
        self.assertIn("expired", self.f.last_error)

    def test_expired_pin_rejected(self):
        self.f.request_pairing(now=0)
        self.assertEqual(self.f.submit_pin("311522", now=61), PairState.FAILED)

    def test_no_timeout_after_online(self):
        self.f.request_pairing(now=0)
        self.f.submit_pin("311522", now=1)
        self.f.smp_result(True)
        self.f.login_result(True)
        self.assertEqual(self.f.tick(now=99999), PairState.ONLINE)


class TestSafetyAndReset(unittest.TestCase):

    def setUp(self):
        self.f = BridgeFsm()
        self.f.on_boot(True, True, True)
        self.f.provisioning_done("HomeWiFi")
        self.f.submit_pin("311522", now=0)

    def test_snapshot_never_leaks_secret(self):
        snap = self.f.snapshot()
        text = repr(snap)
        self.assertNotIn("311522", text)
        self.assertIn("has_pin", snap)
        self.assertEqual(snap["has_pin"], True)

    def test_snapshot_shape_is_stable(self):
        self.assertEqual(set(self.f.snapshot()), {
            "mode", "wifi", "pair", "ssid", "has_bond",
            "has_pin", "wifi_fail", "error",
        })

    def test_factory_reset_clears_everything(self):
        self.assertEqual(self.f.factory_reset(), DeviceMode.SETUP)
        self.assertEqual(self.f.wifi, WifiState.UNPROVISIONED)
        self.assertEqual(self.f.pair, PairState.IDLE)
        self.assertFalse(self.f.has_bond)
        self.assertFalse(self.f.has_pin)
        self.assertIsNone(self.f.ssid)
        self.assertIsNone(self.f._pin)


if __name__ == "__main__":
    unittest.main(verbosity=2)
