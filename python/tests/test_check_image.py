# -*- coding: utf-8 -*-
"""The image guard that runs in CI: the banner must match the tag, and the image must fit.

Both checks are one-line arithmetic, which is exactly why they were skipped for ten releases -
and exactly why a wrong one would be worse than none. The tag rule in particular is subtle: a
release tag names the line (`v2.9.0`) while the banner names the build (`2.9.0-ota11`), so an
equality test would fail every release and get deleted within a week.
"""
from __future__ import annotations

import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
TOOLS = ROOT / "firmware" / "bridge" / "tools"
HEADER = ROOT / "firmware" / "bridge" / "src" / "net" / "mqtt_client.h"

sys.path.insert(0, str(TOOLS))
import check_image                                             # noqa: E402


class TestVersionLiteral(unittest.TestCase):

    def test_reads_the_literal_out_of_the_real_header(self):
        version = check_image.read_version(HEADER.read_text(encoding="utf-8"))
        self.assertIsNotNone(version)
        self.assertRegex(version, r"^\d+\.\d+\.\d+")

    def test_no_literal_is_a_failure_not_an_empty_pass(self):
        self.assertIsNone(check_image.read_version("// nothing here\n"))
        ok, msg = check_image.check_version(None, "2.9.0")
        self.assertFalse(ok)
        self.assertIn("no kFirmwareVersion", msg)

    def test_a_branch_has_nothing_to_compare_against(self):
        ok, _ = check_image.check_version("2.9.0-ota11", check_image.tag_version("refs/heads/main"))
        self.assertTrue(ok)


class TestTagMatching(unittest.TestCase):

    def test_tag_spellings_all_land_on_the_bare_version(self):
        for ref in ("refs/tags/v2.9.0", "refs/tags/2.9.0", "v2.9.0"):
            self.assertEqual(check_image.tag_version(ref), "2.9.0", ref)

    def test_a_branch_is_not_a_tag(self):
        self.assertIsNone(check_image.tag_version("refs/heads/main"))
        self.assertIsNone(check_image.tag_version(""))

    def test_the_build_suffix_is_allowed_under_a_line_tag(self):
        self.assertTrue(check_image.check_version("2.9.0-ota11", "2.9.0")[0])

    def test_an_exact_tag_is_allowed(self):
        self.assertTrue(check_image.check_version("2.9.0-ota11", "2.9.0-ota11")[0])

    def test_a_line_tag_allows_any_build_under_it(self):
        # Deliberate: `v2.9.0` names the line, and refusing `2.9.0-ota11` under it would make the
        # check fail every release, which is how a check gets deleted. Pinning a build requires
        # tagging the build (`v2.9.0-ota11`).
        self.assertTrue(check_image.check_version("2.9.0-ota10", "2.9.0")[0])

    def test_a_build_tag_pins_that_build(self):
        ok, msg = check_image.check_version("2.9.0-ota10", "2.9.0-ota11")
        self.assertFalse(ok, "a build tag must not accept a neighbouring build")
        self.assertIn("2.9.0-ota11", msg)

    def test_a_different_line_fails(self):
        ok, msg = check_image.check_version("2.9.0-ota11", "2.10.0")
        self.assertFalse(ok)
        self.assertIn("2.10.0", msg)


class TestSizeGuard(unittest.TestCase):

    def test_current_image_is_inside_both_lines(self):
        ok, msg = check_image.check_size(1_181_545)
        self.assertTrue(ok, msg)
        self.assertIn("%", msg)

    def test_over_budget_still_builds_but_says_so(self):
        ok, msg = check_image.check_size(check_image.BUDGET_BYTES + 1)
        self.assertTrue(ok, "the budget is advice; only the slot is fatal")
        self.assertIn("comfort budget", msg)

    def test_past_the_slot_is_fatal(self):
        ok, msg = check_image.check_size(check_image.SLOT_BYTES + 1)
        self.assertFalse(ok)
        self.assertIn("larger than", msg)

    def test_the_slot_matches_the_pinned_partition_scheme(self):
        # default_8MB.csv: app0 @ 0x10000, app1 @ 0x340000. If gateway_cli.py ever changes the
        # scheme, this number has to move with it or the guard silently checks nothing.
        self.assertEqual(check_image.SLOT_BYTES, 3_342_336)


if __name__ == "__main__":
    unittest.main()
