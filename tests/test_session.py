# -*- coding: utf-8 -*-
"""test_session.py —— 会话层单元测试（全离线，用假 BLE 客户端）

覆盖 BoschDevice 的登录状态机、单飞收发、应答路由、坏帧过滤。
不连设备、不依赖 Windows 蓝牙。

运行：  python -m unittest test_session -v
"""
import asyncio
import unittest
from unittest import mock

import bosch_api
from bosch_api import (
    BoschDevice, BoschProtocol, Config, RECV_BIZ, RECV_LOGIN,
)


class FakeClient:
    """最小可用的假 BLE 客户端：记录写入，并按 responder 注入应答。"""

    def __init__(self):
        self.is_connected = True
        self.written = []
        self.dev = None
        self.responder = None      # callable(cmd, mid, data) -> payload|None

    async def connect(self):
        self.is_connected = True

    async def disconnect(self):
        self.is_connected = False

    async def start_notify(self, uuid, cb, **kw):
        pass

    async def write_gatt_char(self, uuid, data, response=True):
        self.written.append(bytes(data))
        if self.responder is None:
            return
        plain = BoschProtocol.unstuff(BoschProtocol.decrypt_chunk(bytes(data)))
        if len(plain) < 9:
            return
        cmd, mid, data_part = plain[5], plain[4], plain[6:-3]
        payload = self.responder(cmd, mid, data_part)
        if payload is None:
            return
        frame = BoschProtocol.build(0x00, 0x21, mid, cmd, payload)
        enc = BoschProtocol.xor(BoschProtocol.stuff(frame))
        if self.dev:
            self.dev._on_notify(None, bytearray(enc))


def make_cfg():
    cfg = Config()
    cfg.device_address = "AA:BB:CC:DD:EE:FF"
    cfg.login_mac = "C0335E0FF68A"
    cfg.pair_code = "831152"
    return cfg


class SessionBase(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.cfg = make_cfg()
        self.fake = FakeClient()
        self._patch_client = mock.patch.object(
            bosch_api, "BleakClient", lambda *a, **k: self.fake)
        self._patch_client.start()
        self._patch_sleep = mock.patch.object(bosch_api.asyncio, "sleep",
                                              new=mock.AsyncMock())
        self._patch_sleep.start()
        self.dev = BoschDevice(self.cfg)
        self.dev.client = self.fake
        self.fake.dev = self.dev

    async def asyncTearDown(self):
        self._patch_sleep.stop()
        self._patch_client.stop()

    def reply(self, mapping):
        """mapping: {(cmd, subcmd): payload} —— subcmd 取 data[0]。"""
        def responder(cmd, mid, data):
            sub = data[0] if data else None
            return mapping.get((cmd, sub))
        self.fake.responder = responder


class TestLogin(SessionBase):

    async def test_login_ok(self):
        """07→01（免密）+ 08→01 → 成功"""
        self.reply({(0xA2, 0x07): bytes([0x07, 0x01]),
                    (0xA2, 0x08): bytes([0x08, 0x01])})
        self.assertTrue(await self.dev.login())
        self.assertTrue(self.dev.login_state.startswith("已授权"))

    async def test_login_need_code(self):
        """07→02 表示要密码，有码则继续"""
        self.reply({(0xA2, 0x07): bytes([0x07, 0x02]),
                    (0xA2, 0x08): bytes([0x08, 0x01])})
        self.assertTrue(await self.dev.login())

    async def test_login_00_still_tries_code(self):
        """07→00（MAC 未授权）也应继续走密码流程（与 App 行为一致）"""
        self.reply({(0xA2, 0x07): bytes([0x07, 0x00]),
                    (0xA2, 0x08): bytes([0x08, 0x01])})
        self.assertTrue(await self.dev.login())

    async def test_login_no_code_configured(self):
        """07→02 但配置里没有配对码 → 失败并提示"""
        self.cfg.pair_code = ""
        self.reply({(0xA2, 0x07): bytes([0x07, 0x02])})
        self.assertFalse(await self.dev.login())
        self.assertIn("配对码", self.dev.login_state)

    async def test_login_wrong_code(self):
        """08→00 表示密码被拒（可能是码过期或窗口未开）"""
        self.reply({(0xA2, 0x07): bytes([0x07, 0x02]),
                    (0xA2, 0x08): bytes([0x08, 0x00])})
        self.assertFalse(await self.dev.login())
        self.assertIn("拒绝", self.dev.login_state)

    async def test_login_cooldown_3c(self):
        """3C = 设备冷却 30 秒"""
        self.reply({(0xA2, 0x07): bytes([0x07, 0x3C])})
        self.assertFalse(await self.dev.login())
        self.assertIn("冷却", self.dev.login_state)
        self.assertEqual(self.dev.cooldown_sec, 30.0)

    async def test_login_cooldown_5a(self):
        """5A = 要求等 90 秒"""
        self.reply({(0xA2, 0x07): bytes([0x07, 0x5A])})
        self.assertFalse(await self.dev.login())
        self.assertEqual(self.dev.cooldown_sec, 90.0)

    async def test_login_unknown_answer(self):
        """未知应答应失败而不是当作成功"""
        self.reply({(0xA2, 0x07): bytes([0x07, 0x77])})
        self.assertFalse(await self.dev.login())
        self.assertIn("未知应答", self.dev.login_state)

    async def test_login_sends_mac_and_code(self):
        """两帧都应发往登录地址 0xAB，且密码为 nibble 编码"""
        self.reply({(0xA2, 0x07): bytes([0x07, 0x01]),
                    (0xA2, 0x08): bytes([0x08, 0x01])})
        await self.dev.login()
        plains = [BoschProtocol.unstuff(BoschProtocol.decrypt_chunk(w))
                  for w in self.fake.written]
        receivers = [p[3] for p in plains]
        self.assertTrue(all(r == RECV_LOGIN for r in receivers))
        # 第二帧 data = 08 + 831152 的 nibble
        self.assertEqual(plains[-1][6:-3],
                         bytes([0x08, 0x08, 0x03, 0x01, 0x01, 0x05, 0x02]))

    async def test_login_no_response_retries(self):
        """无应答时按 5 次重发后失败"""
        self.fake.responder = None
        with mock.patch.object(self.dev, "_wait_login",
                               mock.AsyncMock(return_value=False)):
            self.assertFalse(await self.dev.login())
        # MAC 帧重发 5 次
        self.assertEqual(len(self.fake.written), 5)


class TestSendAndWait(SessionBase):

    async def test_receives_reply(self):
        """收到应答即返回，且按 mid 路由"""
        self.reply({(0xD8, 0x01): bytes([0x01, 0x01, 0x41, 0x13])})
        r = await self.dev.send_and_wait(0xD8, bytes([0x01]), RECV_BIZ,
                                         "版本", timeout=1.0, retries=1)
        self.assertEqual(r, bytes([0x01, 0x01, 0x41, 0x13]))

    async def test_timeout_raises(self):
        """无应答且重试耗尽 → TimeoutError"""
        self.fake.responder = None
        with self.assertRaises(TimeoutError):
            await self.dev.send_and_wait(0xD8, bytes([0x01]), RECV_BIZ,
                                         "版本", timeout=0.05, retries=2)

    async def test_retry_uses_new_msgid(self):
        """重发必须换 msgId（规格 §3.5），否则设备当同一请求去重"""
        self.fake.responder = None
        with self.assertRaises(TimeoutError):
            await self.dev.send_and_wait(0xD8, bytes([0x01]), RECV_BIZ,
                                         "版本", timeout=0.05, retries=3)
        mids = [BoschProtocol.unstuff(BoschProtocol.decrypt_chunk(w))[4]
                for w in self.fake.written]
        self.assertEqual(len(mids), 3)
        self.assertEqual(len(set(mids)), 3)      # 三个不同 msgId

    async def test_set_temperature_ok(self):
        """设温走 send_and_wait，拿到应答不抛"""
        self.reply({(0xD3, 0x02): bytes([0x02, 0x55, 0x01])})
        await self.dev.set_temperature(42)
        plain = BoschProtocol.unstuff(BoschProtocol.decrypt_chunk(
            self.fake.written[-1]))
        self.assertEqual(plain[5], 0xD3)
        self.assertEqual(plain[6:-3], bytes([0x02, 0x55, 0x00, 42]))

    async def test_set_temperature_clamped(self):
        """超出范围应被夹到 35~60"""
        self.reply({(0xD3, 0x02): bytes([0x02, 0x55, 0x01])})
        await self.dev.set_temperature(99)
        plain = BoschProtocol.unstuff(BoschProtocol.decrypt_chunk(
            self.fake.written[-1]))
        self.assertEqual(plain[-3 - 1], 60)

    async def test_set_temperature_timeout_raises(self):
        """未获确认时抛 TimeoutError（上层据此提示，但不断会话）"""
        self.fake.responder = None
        with self.assertRaises(TimeoutError):
            await self.dev.send_and_wait(0xD3, b"\x02\x55\x00\x2a", RECV_BIZ,
                                         "设置", timeout=0.05, retries=1)


class TestNotify(SessionBase):

    def feed(self, cmd, payload, mid=5):
        frame = BoschProtocol.build(0x00, 0x21, mid, cmd, payload)
        self.dev._on_notify(None, bytearray(BoschProtocol.xor(
            BoschProtocol.stuff(frame))))

    def test_state_frame_updates(self):
        """A0 帧应更新 state"""
        self.feed(0xA0, bytes.fromhex(
            "01 19 50 1a 5a 2a 1a 46 00 00 0f 00 00 00 00 00 00 00 00 00 00 00 "
            "00 00 00 00 00 00 19 50 00 00 00 00"))
        self.assertIsNotNone(self.dev.state)
        self.assertAlmostEqual(self.dev.state.temp_in, 25.8)
        self.assertEqual(self.dev.state.setpoint, 42)

    def test_bad_checksum_discarded(self):
        """校验和错误的帧应被丢弃，不污染 state"""
        frame = bytearray(BoschProtocol.build(0x00, 0x21, 5, 0xA0,
                                              b"\x01" * 34))
        frame[-3] ^= 0xFF                    # 故意破坏校验和
        self.dev._on_notify(None, bytearray(BoschProtocol.xor(
            BoschProtocol.stuff(bytes(frame)))))
        self.assertIsNone(self.dev.state)

    async def test_history_reply_routed(self):
        """B0 应答应被 send_and_wait 按 mid 拿到"""
        self.reply({(0xB0, 0x02): bytes([0x02, 0x00, 0x00, 0x12, 0x67,
                                         0x00, 0x00, 0x05, 0xB6])})
        r = await self.dev.send_and_wait(0xB0, bytes([0x02]), RECV_BIZ,
                                         "历史", 1.0, 1)
        self.assertEqual(r.hex(" "), "02 00 00 12 67 00 00 05 b6")


class TestQuery(SessionBase):

    async def test_read_history(self):
        self.reply({(0xB0, 0x02): bytes.fromhex("02 00 00 12 67 00 00 05 b6")})
        r = await self.dev.read_history(2)
        info = BoschProtocol.parse_history(r)
        self.assertEqual(info["a"], 4711)
        self.assertEqual(info["b"], 1462)

    async def test_read_errors(self):
        self.reply({(0xD7, 0x0A): bytes.fromhex(
            "ea 00 00 00 00 00 00 00 00 00")})
        r = await self.dev.read_errors(10)
        self.assertEqual(BoschProtocol.parse_errors(r), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
