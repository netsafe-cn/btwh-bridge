# -*- coding: utf-8 -*-
"""run_tests.py - regression entry point

One command runs the whole suite, fully offline (no hardware, no network):
    python run_tests.py           # summary only
    python run_tests.py -v        # list every test

Conventions:
  * every test_*.py is discovered automatically; adding a test needs no edit here
  * scripts that talk to real hardware must not use the test_ prefix (this project uses
    probe_* names)
  * tests must pass, or explicitly skip, on CI without WinRT or Bluetooth (Linux / macOS)

Run this after any change (code, docs, configuration) and commit only when it is green.
"""
import pathlib
import sys
import unittest


HERE = pathlib.Path(__file__).resolve().parent


def _walk(suite):
    """Flatten nested TestSuites down to individual cases."""
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from _walk(item)
        else:
            yield item


def main():
    verbosity = 2 if "-v" in sys.argv else 1
    loader = unittest.TestLoader()
    suite = loader.discover(start_dir=str(HERE / "tests"), pattern="test_*.py",
                            top_level_dir=str(HERE))

    # Guard against a false pass when the directory or pattern is wrong
    if suite.countTestCases() == 0:
        print("No test cases discovered - check the directory and the pattern")
        return 1

    # Per-module counts, so CI shows at a glance which module broke
    mods = {}
    for case in _walk(suite):
        m = case.__class__.__module__
        mods[m] = mods.get(m, 0) + 1
    print("Included in this run: %s" %
          ", ".join("%s(%d)" % (k, v) for k, v in sorted(mods.items())))
    print()

    result = unittest.TextTestRunner(verbosity=verbosity).run(suite)
    total = result.testsRun
    skipped = len(result.skipped)
    bad = len(result.failures) + len(result.errors)
    print()
    print("=" * 56)
    if bad == 0:
        print("Regression passed: %d tests, %d skipped as platform-specific"
              % (total, skipped))
    else:
        print("Regression FAILED: %d of %d tests" % (bad, total))
        for case, _ in result.failures + result.errors:
            print("  - %s" % case)
    print("=" * 56)
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
