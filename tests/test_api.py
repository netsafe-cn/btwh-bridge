# -*- coding: utf-8 -*-
"""API 层单元测试（配置、状态、云客户端解析）

运行：  python -m unittest test_api -v
全部离线，不碰硬件、不连网络。
"""
import json
import os
import pathlib
import tempfile
import unittest
from unittest import mock          # 标准库，CI 无需 pip install mock

import bosch_api
from bemfa import BemfaClient
from bosch_api import Config, DeviceState


class TestConfig(unittest.TestCase):

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp()) / "config.json"
        self._env = dict(os.environ)
        for k in list(os.environ):
            if k.startswith("BOSCH_"):
                del os.environ[k]

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)
        if self.tmp.exists():
            self.tmp.unlink()

    def test_validate_empty(self):
        """空配置应报三个必填项"""
        errs = Config().validate()
        self.assertEqual(len(errs), 3)
        self.assertTrue(any("heater Bluetooth address" in e for e in errs))
        self.assertTrue(any("pairing code" in e for e in errs))

    def test_validate_ok(self):
        """完整配置应无报错"""
        c = Config(device_address="AA:BB:CC:DD:EE:FF",
                   login_mac="112233445566",
                   pair_code="123456")
        self.assertEqual(c.validate(), [])

    def test_login_mac_bytes(self):
        c = Config(login_mac="11:22:33:44:55:66")
        self.assertEqual(c.login_mac_bytes, bytes.fromhex("112233445566"))

    def test_login_mac_with_dash_and_case(self):
        c = Config(login_mac="c0-33-5e-0f-f6-8a")
        self.assertEqual(c.login_mac_bytes, bytes.fromhex("C0335E0FF68A"))

    def test_login_mac_invalid(self):
        for bad in ("", "ABC", "ZZZZZZZZZZZZ", "11223344556677"):
            with self.assertRaises(ValueError):
                _ = Config(login_mac=bad).login_mac_bytes

    def test_save_load_roundtrip(self):
        c = Config(device_address="AA:BB:CC:DD:EE:FF",
                   login_mac="112233445566",
                   pair_code="654321",
                   bemfa_uid="deadbeef")
        c.save(self.tmp)
        back = Config.load(self.tmp)
        self.assertEqual(back.device_address, c.device_address)
        self.assertEqual(back.pair_code, "654321")
        self.assertEqual(back.bemfa_uid, "deadbeef")

    def test_env_overrides_file(self):
        Config(device_address="AA:BB", pair_code="1").save(self.tmp)
        os.environ["BOSCH_PAIR_CODE"] = "999999"
        os.environ["BOSCH_BEMFA_UID"] = "envuid"
        c = Config.load(self.tmp)
        self.assertEqual(c.pair_code, "999999")
        self.assertEqual(c.bemfa_uid, "envuid")
        self.assertEqual(c.device_address, "AA:BB")   # 文件值仍生效

    def test_unknown_key_in_file_ignored(self):
        self.tmp.write_text(json.dumps({"device_address": "X", "不存在的键": 1}),
                            encoding="utf-8")
        c = Config.load(self.tmp)
        self.assertEqual(c.device_address, "X")

    def test_bad_json_falls_back(self):
        self.tmp.write_text("{ not json", encoding="utf-8")
        c = Config.load(self.tmp)
        self.assertEqual(c.device_address, "")


class TestDeviceState(unittest.TestCase):

    def test_burning_flag(self):
        self.assertFalse(DeviceState(burner_power=0).burning)
        self.assertTrue(DeviceState(burner_power=30).burning)
        self.assertTrue(DeviceState(burner_power=80).burning)

    def test_mode_and_failure_text(self):
        self.assertEqual(DeviceState(mode=0).mode_text, "待机")
        self.assertEqual(DeviceState(mode=1).mode_text, "燃烧")
        self.assertEqual(DeviceState(mode=2).mode_text, "舒适")
        self.assertEqual(DeviceState(failure=0).failure_text, "无")
        self.assertEqual(DeviceState(failure=0xEA).failure_text, "EA")

    def test_compat_properties(self):
        """旧字段名仍可用（界面/外部代码兼容）"""
        st = DeviceState(temp_out=45.5, temp_in=20.1, chamber=30.0, burner_power=50)
        self.assertEqual(st.outlet, 45.5)
        self.assertEqual(st.inlet, 20.1)
        self.assertEqual(st.exchanger, 30.0)
        self.assertEqual(st.power, 50)

    def test_defaults(self):
        st = DeviceState()
        self.assertIsNone(st.temp_out)
        self.assertIsNone(st.setpoint)
        self.assertEqual(st.burner_power, 0)
        self.assertEqual(st.frame_type, 0)


class TestBemfaParsing(unittest.TestCase):
    """只测纯解析逻辑，不建立连接"""

    def setUp(self):
        self.cli = BemfaClient("uid", ["boschset004", "boschup006"])
        self.got = []
        self.cli.on_command = lambda t, m: self.got.append((t, m))

    def test_parse_command(self):
        self.cli._on_line("cmd=2&uid=uid&topic=boschset004&msg=#45")
        self.assertEqual(self.got, [("boschset004", "#45")])

    def test_ignore_pong_and_noise(self):
        for line in ("pong", "", "cmd=1&res=1", "cmd=0&res=1"):
            self.cli._on_line(line)
        self.assertEqual(self.got, [])

    def test_echo_suppressed(self):
        """自己刚发的消息回环应该被丢弃"""
        import time
        self.cli._echo[("boschset004", "#38")] = time.time()
        self.cli._on_line("cmd=2&uid=uid&topic=boschset004&msg=#38")
        self.assertEqual(self.got, [])

    def test_echo_expired(self):
        """超过窗口期的相同消息应正常处理"""
        import time
        from bemfa import ECHO_WINDOW
        self.cli._echo[("boschset004", "#38")] = time.time() - ECHO_WINDOW - 1
        self.cli._on_line("cmd=2&uid=uid&topic=boschset004&msg=#38")
        self.assertEqual(self.got, [("boschset004", "#38")])

    def test_msg_with_special_chars(self):
        self.cli._on_line("cmd=2&uid=u&topic=boschup006&msg=on#1#1")
        self.assertEqual(self.got, [("boschup006", "on#1#1")])

    def test_push_without_uid_ignored(self):
        cli = BemfaClient("", ["t"])
        cli.push("t", "x")          # 不应抛异常
        self.assertEqual(cli._send_q.qsize(), 0)


class TestPairing(unittest.TestCase):
    """系统配对：2026-11-01 教训——Windows 解绑后设备会静默丢弃明文会话，
    连接前必须保持配对。"""

    def test_parse_mac(self):
        from bosch_api import parse_mac
        self.assertEqual(parse_mac("AA:BB:CC:DD:EE:FF"), 0xAABBCCDDEEFF)
        self.assertEqual(parse_mac("aabbccddeeff"), 0xAABBCCDDEEFF)
        self.assertEqual(parse_mac("AA-BB-CC-DD-EE-FF"), 0xAABBCCDDEEFF)

    def test_ensure_paired_non_windows_degrades_true(self):
        """非 Windows（无 WinRT）应降级为 True，不阻塞连接流程"""
        import bosch_api
        import asyncio
        with mock.patch.object(bosch_api, "_WINRT_OK", False):
            self.assertTrue(
                asyncio.run(bosch_api.ensure_paired("AA:BB:CC:DD:EE:FF")))

    def test_paired_state_non_windows_unknown(self):
        import bosch_api
        import asyncio
        with mock.patch.object(bosch_api, "_WINRT_OK", False):
            self.assertEqual(
                asyncio.run(bosch_api.paired_state("AA:BB:CC:DD:EE:FF")),
                "unknown")

    @unittest.skipUnless(bosch_api._WINRT_OK, "需要 Windows WinRT")
    def test_ensure_paired_device_unreachable_is_ok(self):
        """设备不在系统缓存（休眠）→ 放行，交给连接流程处理"""
        import bosch_api
        import asyncio
        fake = mock.MagicMock()
        fake.from_bluetooth_address_async = mock.AsyncMock(return_value=None)
        with mock.patch.object(bosch_api, "_WINRT_OK", True), \
             mock.patch.object(bosch_api, "BluetoothLEDevice", fake):
            self.assertTrue(
                asyncio.run(bosch_api.ensure_paired("AA:BB:CC:DD:EE:FF")))

    @unittest.skipUnless(bosch_api._WINRT_OK, "需要 Windows WinRT")
    def test_ensure_paired_already_paired_short_circuit(self):
        """已配对 → 直接 True，不发起配对"""
        import bosch_api
        import asyncio
        pairing = mock.MagicMock()
        pairing.is_paired = True
        info = mock.MagicMock()
        info.pairing = pairing
        dev = mock.MagicMock()
        dev.device_information = info
        fake = mock.MagicMock()
        fake.from_bluetooth_address_async = mock.AsyncMock(return_value=dev)
        with mock.patch.object(bosch_api, "_WINRT_OK", True), \
             mock.patch.object(bosch_api, "BluetoothLEDevice", fake):
            self.assertTrue(
                asyncio.run(bosch_api.ensure_paired("AA:BB:CC:DD:EE:FF")))
        pairing.custom.pair_async.assert_not_called()

    @unittest.skipUnless(bosch_api._WINRT_OK, "需要 Windows WinRT")
    def test_ensure_paired_unpaired_uses_confirm_only(self):
        """未配对 → 走 custom.pair_async(CONFIRM_ONLY) 并接受请求"""
        import bosch_api
        import asyncio

        result = mock.MagicMock()
        result.status = 0                  # DevicePairingResultStatus.PAIRED = 0
        custom = mock.MagicMock()
        custom.pair_async = mock.AsyncMock(return_value=result)
        pairing = mock.MagicMock()
        pairing.is_paired = False
        pairing.can_pair = True
        pairing.custom = custom
        info = mock.MagicMock()
        info.pairing = pairing
        dev = mock.MagicMock()
        dev.device_information = info
        fake = mock.MagicMock()
        fake.from_bluetooth_address_async = mock.AsyncMock(return_value=dev)

        captured = {}

        def add_handler(fn):
            captured["fn"] = fn
        custom.add_pairing_requested.side_effect = add_handler

        import types
        from winrt.windows.devices.enumeration import (
            DevicePairingKinds, DevicePairingResultStatus,
        )

        with mock.patch.object(bosch_api, "_WINRT_OK", True), \
             mock.patch.object(bosch_api, "BluetoothLEDevice", fake):
            ok = asyncio.run(bosch_api.ensure_paired("AA:BB:CC:DD:EE:FF"))
        self.assertTrue(ok)
        custom.pair_async.assert_awaited_once()
        # 验证注册的回调会接受 CONFIRM_ONLY 请求
        args = mock.MagicMock()
        args.pairing_kind = DevicePairingKinds.CONFIRM_ONLY
        captured["fn"](None, args)
        args.accept.assert_called_once()


if __name__ == "__main__":
    unittest.main(verbosity=2)
