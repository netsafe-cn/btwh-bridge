# -*- coding: utf-8 -*-
"""API-layer tests: configuration, state, cloud client parsing

Run:  python run_tests.py -v   (from the repository root)
Fully offline: no hardware, no network.
"""
import json
import os
import pathlib
import tempfile
import unittest
from unittest import mock          # stdlib, so CI needs no extra install

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
        """An empty config reports the three required fields"""
        errs = Config().validate()
        self.assertEqual(len(errs), 3)
        self.assertTrue(any("heater Bluetooth address" in e for e in errs))
        self.assertTrue(any("pairing code" in e for e in errs))

    def test_validate_ok(self):
        """A complete config reports nothing"""
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
        self.assertEqual(c.device_address, "AA:BB")   # the file value still applies

    def test_unknown_key_in_file_ignored(self):
        self.tmp.write_text(json.dumps({"device_address": "X", "unknown_key": 1}),
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
        self.assertEqual(DeviceState(mode=0).mode_text, "idle")
        self.assertEqual(DeviceState(mode=1).mode_text, "burning")
        self.assertEqual(DeviceState(mode=2).mode_text, "comfort")
        self.assertEqual(DeviceState(failure=0).failure_text, "none")
        self.assertEqual(DeviceState(failure=0xEA).failure_text, "EA")

    def test_compat_properties(self):
        """Legacy field names still work, for the UI and external callers"""
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
    """Parsing only; no connection is made"""

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
        """A message we published ourselves must be dropped"""
        import time
        self.cli._echo[("boschset004", "#38")] = time.time()
        self.cli._on_line("cmd=2&uid=uid&topic=boschset004&msg=#38")
        self.assertEqual(self.got, [])

    def test_echo_expired(self):
        """The same message after the window is processed normally"""
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
        cli.push("t", "x")          # must not raise
        self.assertEqual(cli._send_q.qsize(), 0)


class TestPairing(unittest.TestCase):
    """OS-level pairing: after Windows unpairs, the device silently drops plaintext
        sessions, so the pairing must be kept before connecting."""

    def test_parse_mac(self):
        from bosch_api import parse_mac
        self.assertEqual(parse_mac("AA:BB:CC:DD:EE:FF"), 0xAABBCCDDEEFF)
        self.assertEqual(parse_mac("aabbccddeeff"), 0xAABBCCDDEEFF)
        self.assertEqual(parse_mac("AA-BB-CC-DD-EE-FF"), 0xAABBCCDDEEFF)

    def test_ensure_paired_non_windows_degrades_true(self):
        """Non-Windows (no WinRT) degrades to True and never blocks the connection"""
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

    @unittest.skipUnless(bosch_api._WINRT_OK, "requires Windows WinRT")
    def test_ensure_paired_device_unreachable_is_ok(self):
        """Device not in the system cache (asleep): let the connect path handle it"""
        import bosch_api
        import asyncio
        fake = mock.MagicMock()
        fake.from_bluetooth_address_async = mock.AsyncMock(return_value=None)
        with mock.patch.object(bosch_api, "_WINRT_OK", True), \
             mock.patch.object(bosch_api, "BluetoothLEDevice", fake):
            self.assertTrue(
                asyncio.run(bosch_api.ensure_paired("AA:BB:CC:DD:EE:FF")))

    @unittest.skipUnless(bosch_api._WINRT_OK, "requires Windows WinRT")
    def test_ensure_paired_already_paired_short_circuit(self):
        """Already paired: True without starting a pairing"""
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

    @unittest.skipUnless(bosch_api._WINRT_OK, "requires Windows WinRT")
    def test_ensure_paired_unpaired_uses_confirm_only(self):
        """Unpaired: custom.pair_async(CONFIRM_ONLY), then accept the request"""
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
        # the registered handler must accept a CONFIRM_ONLY request
        args = mock.MagicMock()
        args.pairing_kind = DevicePairingKinds.CONFIRM_ONLY
        captured["fn"](None, args)
        args.accept.assert_called_once()


if __name__ == "__main__":
    unittest.main(verbosity=2)
