# -*- coding: utf-8 -*-
"""Import boundaries: importing the library must not require Bluetooth.

Why this file exists (2026-11-12): on a fresh CPython 3.13 without ``bleak``, 12 of the 13
test modules failed during collection - including the signature and MQTT contract tests,
none of which touch a heater. The cause was ``from bleak import BleakClient`` at module
level in ``bosch_api``. ADR-012 makes bleak a connection-time dependency, and these tests
keep it that way.

The Bluetooth modules are blocked inside a subprocess rather than assumed absent, so the
tests are meaningful on a developer machine where bleak *is* installed, and on CI.
"""
from __future__ import annotations

import pathlib
import subprocess
import sys
import textwrap
import unittest
from unittest import mock

import bosch_api


PYTHON_DIR = pathlib.Path(__file__).resolve().parents[1]
REPO_ROOT = PYTHON_DIR.parent


# Raising from find_spec aborts the import instead of falling through to site-packages,
# which is what makes this work even when bleak is installed.
BLOCK_BLUETOOTH = """
import sys


class _NoBluetooth:
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split(".")[0] in ("bleak", "winrt"):
            raise ImportError("blocked by test_import_boundaries: " + fullname)
        return None


sys.meta_path.insert(0, _NoBluetooth())
sys.path.insert(0, {python_dir!r})
"""


def run_without_bluetooth(body: str) -> subprocess.CompletedProcess:
    """Run `body` in a fresh interpreter where bleak and winrt cannot be imported."""
    script = BLOCK_BLUETOOTH.format(python_dir=str(PYTHON_DIR)) + textwrap.dedent(body)
    return subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True, text=True, encoding="utf-8", timeout=120,
        cwd=str(REPO_ROOT))


class TestNoBluetoothAtImportTime(unittest.TestCase):

    def test_import_and_codec_run_without_bluetooth(self):
        """The pure layers must work on a machine with no Bluetooth runtime at all."""
        proc = run_without_bluetooth("""
            import bosch_api

            frame = bosch_api.BoschProtocol.build(
                0x21, 0x00, 0x10, 0xD3, bytes([0x02, 0x55, 0x00, 42]))
            wire = bosch_api.BoschProtocol.xor(
                bosch_api.BoschProtocol.stuff(frame))
            back = bosch_api.BoschProtocol.unstuff(
                bosch_api.BoschProtocol.decrypt_chunk(wire))
            assert back == frame, back.hex()
            assert len(bosch_api.Config().validate()) == 3
            print("OK")
        """)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("OK", proc.stdout)

    def test_import_pulls_in_no_bluetooth_module(self):
        """A successful import is not enough; the modules must stay unloaded."""
        proc = run_without_bluetooth("""
            import sys
            import bosch_api

            leaked = [m for m in ("bleak", "winrt") if m in sys.modules]
            assert not leaked, "importing bosch_api loaded %s" % leaked
            print("OK")
        """)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("OK", proc.stdout)

    def test_missing_runtime_fails_at_connect_with_the_fix(self):
        """Connecting without bleak must say what to install, not raise ModuleNotFoundError."""
        proc = run_without_bluetooth("""
            import bosch_api

            try:
                bosch_api.BleakClient("AA:BB:CC:DD:EE:FF")
            except RuntimeError as exc:
                message = str(exc)
                assert "bleak" in message, message
                assert "requirements.txt" in message, message
                print("OK")
            else:
                raise SystemExit("constructing a client without bleak did not raise")
        """)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("OK", proc.stdout)


class TestPatchPoint(unittest.TestCase):
    """test_session.py replaces the client through mock.patch.object; keep that working."""

    def test_lazy_class_is_a_real_module_attribute(self):
        self.assertIsInstance(bosch_api.BleakClient, type)

    def test_patch_replaces_and_restores_the_client(self):
        sentinel = object()
        with mock.patch.object(bosch_api, "BleakClient", lambda *a, **k: sentinel):
            self.assertIs(bosch_api.BleakClient("AA:BB:CC:DD:EE:FF"), sentinel)
        self.assertIsInstance(bosch_api.BleakClient, type)


if __name__ == "__main__":
    unittest.main()
