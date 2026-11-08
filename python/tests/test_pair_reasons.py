# -*- coding: utf-8 -*-
"""test_pair_reasons.py —— 配对失败原因码契约测试

2026-11-07 ESP32-S3 真机实测确定的语义：`A2/07` 的应答位是「配对窗口状态」，
不是「MAC 授权状态」。本文件把这条语义钉死，防止以后又被改回
「00 = MAC 未授权」而误导用户去改 `login_mac`。

运行：  python -m unittest test_pair_reasons -v
"""
import unittest
from unittest import mock

import bosch_api
from bosch_api import BoschDevice
from tests.test_session import SessionBase


class TestPairFailReasons(SessionBase):
    """复用 test_session 的假 BLE 客户端，全离线、不连硬件。"""

    async def test_success_clears_reason(self):
        self.reply({(0xA2, 0x07): bytes([0x07, 0x02]),
                    (0xA2, 0x08): bytes([0x08, 0x01])})
        self.assertTrue(await self.dev.login())
        self.assertIsNone(self.dev.pair_fail_reason)
        self.assertEqual(self.dev.pair_fail_hint(), "")

    async def test_window_closed_when_a2_07_is_00(self):
        """00 = 窗口没开，而不是 MAC 未授权"""
        self.reply({(0xA2, 0x07): bytes([0x07, 0x00]),
                    (0xA2, 0x08): bytes([0x08, 0x00])})
        self.assertFalse(await self.dev.login())
        self.assertEqual(self.dev.pair_fail_reason,
                         bosch_api.PAIR_FAIL_WINDOW_CLOSED)

    async def test_code_rejected_when_window_open(self):
        """窗口开着(02) 但码失效(00) → 才是 code_rejected"""
        self.reply({(0xA2, 0x07): bytes([0x07, 0x02]),
                    (0xA2, 0x08): bytes([0x08, 0x00])})
        self.assertFalse(await self.dev.login())
        self.assertEqual(self.dev.pair_fail_reason,
                         bosch_api.PAIR_FAIL_CODE_REJECTED)

    async def test_a2_08_00_does_not_override_window_closed(self):
        """关键回归：窗口没开时，A2/08 的 00 不能覆盖根因"""
        self.reply({(0xA2, 0x07): bytes([0x07, 0x00]),
                    (0xA2, 0x08): bytes([0x08, 0x00])})
        await self.dev.login()
        self.assertEqual(self.dev.pair_fail_reason,
                         bosch_api.PAIR_FAIL_WINDOW_CLOSED)
        self.assertNotEqual(self.dev.pair_fail_reason,
                            bosch_api.PAIR_FAIL_CODE_REJECTED)

    async def test_code_mismatch(self):
        self.reply({(0xA2, 0x07): bytes([0x07, 0x02]),
                    (0xA2, 0x08): bytes([0x08, 0x02])})
        self.assertFalse(await self.dev.login())
        self.assertEqual(self.dev.pair_fail_reason,
                         bosch_api.PAIR_FAIL_CODE_MISMATCH)

    async def test_a2_07_01_still_needs_code(self):
        """实测 2026-11-07：A2/07=01 只表示 MAC 已登记，A2/08 仍可能被拒。

        原实现把 01 当「免密授权」，导致 19:15:22 那次真机失败无法归因。
        """
        self.reply({(0xA2, 0x07): bytes([0x07, 0x01]),
                    (0xA2, 0x08): bytes([0x08, 0x00])})
        self.assertFalse(await self.dev.login())
        self.assertEqual(self.dev.pair_fail_reason,
                         bosch_api.PAIR_FAIL_CODE_REJECTED)
        self.assertNotEqual(self.dev.pair_fail_reason,
                            bosch_api.PAIR_FAIL_WINDOW_CLOSED)

    async def test_a2_07_01_then_success(self):
        """01 之后 A2/08=01 才是真正的成功"""
        self.reply({(0xA2, 0x07): bytes([0x07, 0x01]),
                    (0xA2, 0x08): bytes([0x08, 0x01])})
        self.assertTrue(await self.dev.login())
        self.assertIsNone(self.dev.pair_fail_reason)


class TestAuthState(SessionBase):
    """结构化认证状态：上层据此决策，不解析中文文案。"""

    async def test_first_pair_when_a2_07_is_02(self):
        """02 = 设备第一次见这个 MAC，必须窗口内的码"""
        self.reply({(0xA2, 0x07): bytes([0x07, 0x02]),
                    (0xA2, 0x08): bytes([0x08, 0x01])})
        await self.dev.login()
        self.assertTrue(self.dev.mac_registered)
        self.assertEqual(self.dev.pair_kind, "first_pair")

    async def test_rebind_when_a2_07_is_01(self):
        """01 = 设备已登记该 MAC，属于复用凭证重连"""
        self.reply({(0xA2, 0x07): bytes([0x07, 0x01]),
                    (0xA2, 0x08): bytes([0x08, 0x01])})
        await self.dev.login()
        self.assertTrue(self.dev.mac_registered)
        self.assertEqual(self.dev.pair_kind, "rebind")

    async def test_not_registered_when_window_closed(self):
        self.reply({(0xA2, 0x07): bytes([0x07, 0x00]),
                    (0xA2, 0x08): bytes([0x08, 0x00])})
        await self.dev.login()
        self.assertFalse(self.dev.mac_registered)
        self.assertEqual(self.dev.pair_kind, "")

    async def test_authorized_and_timestamp_on_success(self):
        self.reply({(0xA2, 0x07): bytes([0x07, 0x02]),
                    (0xA2, 0x08): bytes([0x08, 0x01])})
        await self.dev.login()
        self.assertTrue(self.dev.authorized)
        self.assertIsNotNone(self.dev.pair_code_verified_at)

    async def test_not_authorized_on_failure(self):
        self.reply({(0xA2, 0x07): bytes([0x07, 0x02]),
                    (0xA2, 0x08): bytes([0x08, 0x00])})
        await self.dev.login()
        self.assertFalse(self.dev.authorized)
        self.assertIsNone(self.dev.pair_code_verified_at)

    async def test_state_reset_between_attempts(self):
        """第二次失败不能残留上一次成功的 authorized=True"""
        self.reply({(0xA2, 0x07): bytes([0x07, 0x02]),
                    (0xA2, 0x08): bytes([0x08, 0x01])})
        await self.dev.login()
        self.assertTrue(self.dev.authorized)

        self.reply({(0xA2, 0x07): bytes([0x07, 0x00]),
                    (0xA2, 0x08): bytes([0x08, 0x00])})
        await self.dev.login()
        self.assertFalse(self.dev.authorized)
        self.assertFalse(self.dev.mac_registered)

    async def test_pair_summary_shape_and_no_code(self):
        self.reply({(0xA2, 0x07): bytes([0x07, 0x02]),
                    (0xA2, 0x08): bytes([0x08, 0x01])})
        await self.dev.login()
        snap = self.dev.pair_summary()
        self.assertEqual(set(snap), {
            "mac_registered", "authorized", "pair_kind",
            "fail_reason", "fail_hint", "code_verified_at",
        })
        self.assertNotIn(self.cfg.pair_code, repr(snap))
        self.assertTrue(snap["authorized"])

    async def test_no_response(self):
        with mock.patch.object(BoschDevice, "_wait_login",
                               mock.AsyncMock(return_value=False)):
            self.assertFalse(await self.dev.login())
        self.assertEqual(self.dev.pair_fail_reason,
                         bosch_api.PAIR_FAIL_NO_RESPONSE)

    async def test_cooldown_3c(self):
        self.reply({(0xA2, 0x07): bytes([0x07, 0x3C])})
        self.assertFalse(await self.dev.login())
        self.assertEqual(self.dev.pair_fail_reason,
                         bosch_api.PAIR_FAIL_COOLDOWN)
        self.assertEqual(self.dev.cooldown_sec, 30.0)

    async def test_cooldown_5a(self):
        self.reply({(0xA2, 0x07): bytes([0x07, 0x5A])})
        self.assertFalse(await self.dev.login())
        self.assertEqual(self.dev.pair_fail_reason,
                         bosch_api.PAIR_FAIL_COOLDOWN)
        self.assertEqual(self.dev.cooldown_sec, 90.0)

    async def test_reason_reset_between_attempts(self):
        """上一次失败的原因不能残留到下一次尝试"""
        self.reply({(0xA2, 0x07): bytes([0x07, 0x00]),
                    (0xA2, 0x08): bytes([0x08, 0x00])})
        await self.dev.login()
        self.assertEqual(self.dev.pair_fail_reason,
                         bosch_api.PAIR_FAIL_WINDOW_CLOSED)

        self.reply({(0xA2, 0x07): bytes([0x07, 0x02]),
                    (0xA2, 0x08): bytes([0x08, 0x01])})
        self.assertTrue(await self.dev.login())
        self.assertIsNone(self.dev.pair_fail_reason)

    def test_every_reason_has_a_hint(self):
        """每个原因码都必须配一句可展示的动作建议"""
        found = [n for n in dir(bosch_api) if n.startswith("PAIR_FAIL_")]
        self.assertGreaterEqual(len(found), 6)
        for name in found:
            with self.subTest(const=name):
                reason = getattr(bosch_api, name)
                self.assertIn(reason, bosch_api.PAIR_HINTS, name)
                self.assertTrue(bosch_api.PAIR_HINTS[reason].strip(), name)

    async def test_hint_mentions_window_for_window_closed(self):
        self.reply({(0xA2, 0x07): bytes([0x07, 0x00]),
                    (0xA2, 0x08): bytes([0x08, 0x00])})
        await self.dev.login()
        self.assertIn("Window", self.dev.pair_fail_hint())


if __name__ == "__main__":
    unittest.main(verbosity=2)
