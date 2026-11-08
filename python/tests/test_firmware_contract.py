# -*- coding: utf-8 -*-
"""test_firmware_contract.py - keep the firmware's auth layer in step with the Python API

`firmware/bridge/src/auth/` reimplements the two-step login, so its failure reasons are a second
copy of `bosch_api.PAIR_FAIL_*`. Two copies only stay equal if something checks, and here the
things that can silently drift are:

  * a reason renamed on one side (the serial log and an MQTT consumer would disagree)
  * the reply line printed in decimal instead of two-digit hex, which makes `log_diag.py` read
    `60` where the device said `3C` - a real bug that shipped and is why this test exists

The test reads C++ as text. That is deliberate: it is a contract check, not a compiler, and it
fails loudly when the shape it relies on changes.
"""
from __future__ import annotations

import pathlib
import re
import unittest

import bosch_api as api

ROOT = pathlib.Path(__file__).resolve().parents[2]
LOGIN_CPP = ROOT / "firmware" / "bridge" / "src" / "auth" / "login.cpp"

# Reasons the firmware defines that have no Python counterpart: "ok" is the success case, and
# "bad_input" means nothing was transmitted, so no PAIR_FAIL_* applies.
FIRMWARE_ONLY = {"ok", "bad_input"}


def _reason_strings() -> set[str]:
    """Every string literal returned from a `case` arm of auth::reasonText."""
    text = LOGIN_CPP.read_text(encoding="utf-8")
    body = re.search(r"reasonText\(Result r\)\s*\{(.*?)\n\}", text, re.S)
    assert body, "reasonText() not found in auth/login.cpp"
    # Only the case arms; the trailing "unknown" fallback is not a reason code.
    return set(re.findall(r'case\s+\w+:\s*return\s+"([a-z_]+)"', body.group(1)))


class TestAuthReasons(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fw = _reason_strings()
        cls.py = {v for k, v in vars(api).items()
                  if k.startswith("PAIR_FAIL_") and isinstance(v, str)}

    def test_firmware_reasons_are_found(self):
        self.assertTrue(self.fw, "no reason strings parsed; did reasonText() change shape?")

    def test_device_reasons_match_the_python_api(self):
        """The names a consumer sees must be identical on both sides, in both directions.

        `link_failed` is set by app/session.cpp rather than by classify(): it means the BLE link
        never came up or died mid-login, which is not something either reply byte can say.
        """
        fw_device = self.fw - FIRMWARE_ONLY
        self.assertEqual(fw_device, self.py,
                         "reason codes drifted: firmware %s vs python %s"
                         % (sorted(fw_device), sorted(self.py)))

    def test_firmware_only_reasons_still_exist(self):
        missing = FIRMWARE_ONLY - self.fw
        self.assertFalse(missing, "reasonText() no longer returns %s" % sorted(missing))


class TestReplyLineFormat(unittest.TestCase):
    """log_diag.py parses `A2/07 reply = 3C`, so the value must be two hex digits.

    It was `%d` once: the device answered 3C (60), the log said `60`, and the diagnosis script
    read an undefined value. Nobody noticed because the happy path is 01, which renders as `1`
    and simply fails to match - the log looked fine and the advice was silently skipped.
    """

    def test_replies_are_logged_as_two_hex_digits(self):
        text = LOGIN_CPP.read_text(encoding="utf-8")
        self.assertIn('reply = %02X', text,
                      "the reply line must print two hex digits; log_diag.py reads them")

    def test_log_diag_pattern_matches_what_the_firmware_prints(self):
        """Pair the pattern in log_diag.py with the format in login.cpp, value by value."""
        diag = (ROOT / "firmware" / "bridge" / "tools" / "log_diag.py").read_text(encoding="utf-8")
        pattern = r"A2/07\s*reply\s*=\s*([0-9A-Fa-f]{2})"
        self.assertIn(pattern, diag,
                      "log_diag.py no longer uses this pattern; update both sides together")

        for code in (0x00, 0x01, 0x02, 0x3C, 0x5A):
            line = "[AUTH] A2/%02X reply = %02X" % (0x07, code)
            self.assertEqual(re.findall(pattern, line), ["%02X" % code],
                             "log_diag would miss %s" % line)


if __name__ == "__main__":
    unittest.main()
