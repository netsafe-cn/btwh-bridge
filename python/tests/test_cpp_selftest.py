# -*- coding: utf-8 -*-
"""The C++ selftest suites, compiled and run on the host.

The board runs the same suites (serial command `selftest`); this closes the
window where a broken check - like the wrong "expand fits exactly" expectation
that shipped with the discovery suite - could sit unexecuted until the first
flash. Skipped, not failed, when no host C++ compiler is available: a machine
without g++/clang++/ziglang still gets the full Python suite.
"""
import pathlib
import subprocess
import sys
import unittest

TOOLS = pathlib.Path(__file__).resolve().parents[2] / "firmware" / "bridge" / "tools"
RUNNER = TOOLS / "host_selftest.py"


class HostSelftest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.proc = subprocess.run(
            [sys.executable, str(RUNNER), "--force"],
            capture_output=True, text=True, timeout=300)
        if cls.proc.returncode == 2:
            raise unittest.SkipTest("no host C++ compiler (g++/clang++/ziglang)")

    def test_suites_pass(self):
        self.assertEqual(self.proc.returncode, 0,
                         "host selftest exited %d\n%s%s"
                         % (self.proc.returncode, self.proc.stdout, self.proc.stderr))

    def test_totals_match_the_board(self):
        # proto 64 + net 16 + mqtt 32 + discovery 73, printed by the runner's last line.
        # If these drift, update this number AND the board result in AGENTS.md - the two
        # must stay in lockstep or one of them is no longer running what it claims.
        self.assertIn("host selftest passed: 185 checks", self.proc.stdout)


if __name__ == "__main__":
    unittest.main()
