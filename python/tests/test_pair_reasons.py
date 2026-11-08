# -*- coding: utf-8 -*-
"""test_pair_reasons.py - contract tests for the pairing failure reasons

Semantics established on ESP32-S3 hardware, 2026-11-07: the A2/07 reply byte reports
the PAIRING WINDOW, not the MAC state. These tests pin that down, so it cannot drift
back to "00 = MAC unauthorised" and send users off to edit login_mac.

Run:  python run_tests.py -v   (from the repository root)
"""
import unittest
from unittest import mock

import bosch_api
from bosch_api import BoschDevice
from tests.test_session import SessionBase


class TestPairFailReasons(SessionBase):
    """Reuses the fake BLE client from test_session; fully offline."""

    async def test_success_clears_reason(self):
        self.reply({(0xA2, 0x07): bytes([0x07, 0x02]),
                    (0xA2, 0x08): bytes([0x08, 0x01])})
        self.assertTrue(await self.dev.login())
        self.assertIsNone(self.dev.pair_fail_reason)
        self.assertEqual(self.dev.pair_fail_hint(), "")

    async def test_window_closed_when_a2_07_is_00(self):
        """00 means the window is closed, not that the MAC is unauthorised"""
        self.reply({(0xA2, 0x07): bytes([0x07, 0x00]),
                    (0xA2, 0x08): bytes([0x08, 0x00])})
        self.assertFalse(await self.dev.login())
        self.assertEqual(self.dev.pair_fail_reason,
                         bosch_api.PAIR_FAIL_WINDOW_CLOSED)

    async def test_code_rejected_when_window_open(self):
        """Window open (02) but the code rejected (00) => this is code_rejected"""
        self.reply({(0xA2, 0x07): bytes([0x07, 0x02]),
                    (0xA2, 0x08): bytes([0x08, 0x00])})
        self.assertFalse(await self.dev.login())
        self.assertEqual(self.dev.pair_fail_reason,
                         bosch_api.PAIR_FAIL_CODE_REJECTED)

    async def test_a2_08_00_does_not_override_window_closed(self):
        """Key regression: with the window closed, A2/08=00 must not overwrite the cause"""
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
        """Measured 2026-11-07: A2/07=01 only means the MAC is registered; A2/08 can still

        The old code treated 01 as passwordless, so the 19:15:22 hardware failure could not be
        """
        self.reply({(0xA2, 0x07): bytes([0x07, 0x01]),
                    (0xA2, 0x08): bytes([0x08, 0x00])})
        self.assertFalse(await self.dev.login())
        self.assertEqual(self.dev.pair_fail_reason,
                         bosch_api.PAIR_FAIL_CODE_REJECTED)
        self.assertNotEqual(self.dev.pair_fail_reason,
                            bosch_api.PAIR_FAIL_WINDOW_CLOSED)

    async def test_a2_07_01_then_success(self):
        """Only A2/08=01 after the 01 counts as success"""
        self.reply({(0xA2, 0x07): bytes([0x07, 0x01]),
                    (0xA2, 0x08): bytes([0x08, 0x01])})
        self.assertTrue(await self.dev.login())
        self.assertIsNone(self.dev.pair_fail_reason)


class TestAuthState(SessionBase):
    """Structured auth state: the layers above decide on this, not on prose."""

    async def test_first_pair_when_a2_07_is_02(self):
        """02 = first time the device sees this MAC, so the code must come from the window"""
        self.reply({(0xA2, 0x07): bytes([0x07, 0x02]),
                    (0xA2, 0x08): bytes([0x08, 0x01])})
        await self.dev.login()
        self.assertTrue(self.dev.mac_registered)
        self.assertEqual(self.dev.pair_kind, "first_pair")

    async def test_rebind_when_a2_07_is_01(self):
        """01 = the MAC is registered, so this is a reconnection with a stored credential"""
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
        """A second failure must not keep the previous authorized=True"""
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
        """The previous failure reason must not survive into the next attempt"""
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
        """Every reason code needs one user-facing action attached to it"""
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
