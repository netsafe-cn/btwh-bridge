# -*- coding: utf-8 -*-
"""test_contract.py - the interface contract test

Turns the "additive only" promise in docs/API.md into an assertion that turns CI red.

Covers:
    1. Signature freeze: compare against the spec/api_contract.json snapshot
          - removing a symbol, renaming or dropping a parameter, changing a default, or
            making an optional parameter required. Only a trailing default may be added.
    2. Constant freeze: UUIDs, XOR_KEY, command bytes, chunk size. Changing one needs
       a major version bump.
       3. Golden vectors: Python and the ESP32 C++ firmware compared byte for byte.

Refresh the snapshot only after an intentional, reviewed compatibility extension:
    python python/tests/test_contract.py --update
"""
from __future__ import annotations

# Allow `python tests/test_contract.py --update` from the repository root.
import pathlib as _pathlib
import sys as _sys
_sys.path.insert(0, str(_pathlib.Path(__file__).resolve().parent.parent))

import inspect
import json
import pathlib
import sys
import unittest
from dataclasses import asdict

import bosch_api as api
from bosch_api import BoschProtocol

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parents[1]                  # python/tests -> repository root
SPEC = ROOT / "spec"                    # cross-language contracts live here
CONTRACT = SPEC / "api_contract.json"
VECTORS = SPEC / "protocol_vectors.json"

# Every public symbol docs/API.md declares. Adding one means updating this list too.
API_SYMBOLS = [
    "parse_mac", "paired_state", "ensure_paired", "unpair_device", "local_bt_mac",
    "Config.load", "Config.save", "Config.validate",
    "BoschProtocol.xor", "BoschProtocol.encode_chunks", "BoschProtocol.build",
    "BoschProtocol.stuff", "BoschProtocol.unstuff", "BoschProtocol.decrypt_chunk",
    "BoschProtocol.decode_stream", "BoschProtocol.parse_state",
    "BoschProtocol.parse_history", "BoschProtocol.parse_errors",
    "BoschProtocol.encode_pair_code",
    "BoschDevice.connect", "BoschDevice.close", "BoschDevice.login",
    "BoschDevice.send", "BoschDevice.send_and_wait",
    "BoschDevice.set_temperature", "BoschDevice.idle_seconds",
    "BoschDevice.read_history", "BoschDevice.read_errors",
    "BoschDevice.pair_fail_hint", "BoschDevice.pair_summary",
    "BoschService.pair_fail_hint",
    "BoschService.start", "BoschService.stop",
    "BoschService.set_temperature", "BoschService.query_history",
    "BoschService.query_errors", "BoschService.take_logs",
    "BoschService.update_config", "BoschService.set_pair_code_and_reconnect",
]

API_PROPERTIES = [
    "Config.login_mac_bytes",
    "BoschDevice.pair_fail_reason",
    "DeviceState.outlet", "DeviceState.inlet", "DeviceState.exchanger",
    "DeviceState.power", "DeviceState.burning", "DeviceState.mode_text",
    "DeviceState.failure_text", "BoschService.running",
]

CONSTANT_NAMES = [
    "SERVICE_BASE", "CHAR_MODE", "CHAR_FIFO", "CHAR_CREDITS", "CHAR_COUNTER",
    "XOR_KEY", "FRAME_START", "FRAME_END", "SENDER", "RECV_BIZ", "RECV_LOGIN",
    "CMD_STATE", "CMD_BCAST_CFG", "CMD_LOGIN", "CMD_READ_PARAM",
    "CMD_WRITE_PARAM", "CMD_HISTORY", "CMD_READ_ERROR", "CHUNK",
    "TEMP_MIN", "TEMP_MAX",
]


def resolve(dotted):
    """'BoschDevice.send_and_wait' -> the callable; a missing symbol raises AttributeError."""
    obj = api
    for part in dotted.split("."):
        obj = getattr(obj, part)
    return obj


def shape(fn) -> list:
    """Signature -> comparable structure. Type annotations and self/cls are ignored."""
    out = []
    for p in inspect.signature(fn).parameters.values():
        if p.name in ("self", "cls"):
            continue
        has_default = p.default is not inspect.Parameter.empty
        out.append({
            "name": p.name,
            "kind": p.kind.name,
            "has_default": has_default,
            "default": repr(p.default) if has_default else None,
        })
    return out


def freeze(v):
    """Recursively mark bytes as hex so json.dumps can survive DeviceState.raw."""
    if isinstance(v, bytes):
        return {"__bytes_hex__": v.hex()}
    if isinstance(v, dict):
        return {k: freeze(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [freeze(x) for x in v]
    return v


def build_vectors() -> dict:
    """Build the golden vectors the ESP32 C++ unit tests read directly."""
    mac = bytes.fromhex("C0335E0FF68A")
    code = "311522"

    def one(name, frame, note):
        stuffed = BoschProtocol.stuff(frame)
        return {
            "name": name,
            "note": note,
            "frame_hex": frame.hex(),
            "stuffed_hex": stuffed.hex(),
            "chunks_hex": [c.hex() for c in BoschProtocol.encode_chunks(stuffed)],
        }

    cases = [
        one("a2_07_submit_mac",
            BoschProtocol.build(0x21, 0xAB, 0x05, 0xA2, b"\x07" + mac),
            "Login step 1: submit the authorised host MAC (receiver=0xAB)"),
        one("a2_08_submit_code",
            BoschProtocol.build(0x21, 0xAB, 0x06, 0xA2,
                                b"\x08" + BoschProtocol.encode_pair_code(code)),
            "Login step 2: submit the six-digit panel code, one byte per digit"),
        one("d3_set_temperature_42",
            BoschProtocol.build(0x21, 0x00, 0x10, 0xD3, bytes([0x02, 0x55, 42])),
            "Write the 42 C setpoint"),
        one("b0_read_history",
            BoschProtocol.build(0x21, 0x00, 0x11, 0xB0, bytes([2])),
            "Read the run statistics"),
        one("stuff_7b_7d",
            BoschProtocol.build(0x21, 0x00, 0x12, 0xA0,
                                bytes([0x7B, 0x7D, 0x00, 0x01])),
            "Stuffing boundary: a 7B/7D inside the data area gets a 00 after it"),
    ]

    # A0 34-byte Type1 state frame; pins the offsets measured on 2026-11-01.
    # Temperatures are [integer][hundredths]: payload[i] + payload[i+1]/100.
    payload = bytearray(34)
    payload[0] = 1                              # frame type: 1 = Type1
    payload[1], payload[2] = 20, 0             # inlet 20.00
    payload[3], payload[4] = 30, 0             # chamber 30.00
    payload[5] = 42                            # setpoint 42
    payload[6], payload[7] = 26, 0             # chassis 26.00
    payload[10] = 0x10                         # ionisation (flame present)
    payload[11] = 0                            # no fault
    payload[12] = 35                           # fan power
    payload[13] = 35                           # burner power 35%
    payload[15], payload[16] = 8, 0            # fan 800 rpm
    payload[21] = 80                           # flow 8.0 L/min
    payload[27] = 0                            # pressure
    payload[28], payload[29] = 45, 0           # outlet 45.00
    payload[32] = 1                            # mode: burning
    payload[33] = 60                           # bathroom controller ceiling 60
    state = freeze(asdict(BoschProtocol.parse_state(bytes(payload))))
    state.pop("ts", None)                      # the parse timestamp is not deterministic
    cases.append({
        "name": "a0_state_type1",
        "note": "A0 34-byte Type1; offsets from the 2026-11-01 capture, ts excluded",
        "payload_hex": bytes(payload).hex(),
        "state": state,
    })

    return {
        "contract_version": "v1.0",
        "generated_by": "python test_contract.py --update",
        "usage": "the ESP32 C++ tests read these hex strings and compare their own framing",
        "cases": cases,
    }


class TestSignatureContract(unittest.TestCase):
    """Signature freeze: any breaking change must go red here."""

    @classmethod
    def setUpClass(cls):
        if not CONTRACT.exists():
            raise unittest.SkipTest("snapshot missing; run python python/tests/test_contract.py --update")
        cls.snap = json.loads(CONTRACT.read_text(encoding="utf-8"))

    def test_snapshot_covers_all_declared_symbols(self):
        missing = set(API_SYMBOLS) - set(self.snap["signatures"])
        self.assertFalse(missing, "snapshot is missing symbols: %s" % sorted(missing))

    def test_no_symbol_removed(self):
        for name in self.snap["signatures"]:
            with self.subTest(symbol=name):
                try:
                    resolve(name)
                except AttributeError:
                    self.fail("public symbol removed: %s" % name)

    def test_signatures_backward_compatible(self):
        for name, old in self.snap["signatures"].items():
            with self.subTest(symbol=name):
                new = shape(resolve(name))
                self.assertGreaterEqual(len(new), len(old),
                                        "%s lost a parameter (breaking)" % name)
                self.assertEqual(new[:len(old)], old,
                                 "%s had a parameter renamed / default changed / made required (breaking)" % name)
                for extra in new[len(old):]:
                    self.assertTrue(extra["has_default"],
                                    "%s gained a required parameter %s (breaking)" %
                                    (name, extra["name"]))

    def test_properties_exist(self):
        for name in API_PROPERTIES:
            with self.subTest(prop=name):
                try:
                    resolve(name)
                except AttributeError:
                    self.fail("compatibility property removed: %s" % name)

    def test_constants_frozen(self):
        for name, old in self.snap["constants"].items():
            with self.subTest(const=name):
                self.assertEqual(freeze(getattr(api, name)), old,
                                 "protocol constant %s changed; this needs a major version bump" % name)


class TestGoldenVectors(unittest.TestCase):
    """Cross-platform agreement: these hex strings are also the ESP32 test inputs."""

    @classmethod
    def setUpClass(cls):
        if not VECTORS.exists():
            raise unittest.SkipTest("vectors missing; run python python/tests/test_contract.py --update")
        cls.v = json.loads(VECTORS.read_text(encoding="utf-8"))

    def test_frames_roundtrip(self):
        for case in self.v["cases"]:
            if "frame_hex" not in case:
                continue
            with self.subTest(case=case["name"]):
                frame = bytes.fromhex(case["frame_hex"])
                stuffed = BoschProtocol.stuff(frame)
                self.assertEqual(stuffed.hex(), case["stuffed_hex"],
                                 "stuffing output differs from the golden vector")
                self.assertEqual(
                    [c.hex() for c in BoschProtocol.encode_chunks(stuffed)],
                    case["chunks_hex"], "chunking / per-fragment XOR differs from the golden vector")
                # Simulate the device side: decrypt each fragment -> concatenate -> deframe
                plain = b"".join(BoschProtocol.decrypt_chunk(bytes.fromhex(c))
                                 for c in case["chunks_hex"])
                frames, rest = BoschProtocol.decode_stream(plain)
                self.assertEqual(rest, b"", "leftover bytes after deframing")
                self.assertEqual([f.hex() for f in frames], [frame.hex()],
                                 "deframing did not recover the original logical frame")

    def test_a0_state_stable(self):
        case = next(c for c in self.v["cases"] if c["name"] == "a0_state_type1")
        st = freeze(asdict(
            BoschProtocol.parse_state(bytes.fromhex(case["payload_hex"]))))
        st.pop("ts", None)                     # differs every run, so it is not compared
        self.assertEqual(st, case["state"], "A0 field parsing drifted")
        # the vector itself must stay a meaningful Type1 sample, not degenerate to zeros
        self.assertEqual(st["frame_type"], 1)
        self.assertEqual(st["temp_out"], 45.0)
        self.assertEqual(st["water_flow"], 8.0)
        self.assertEqual(st["fan_speed"], 800)


def main():
    if "--update" in sys.argv:
        snap = {
            "contract_version": "v1.0",
            "note": "docs/API.md v1.0 signature snapshot; only a trailing default may be added",
            "signatures": {n: shape(resolve(n)) for n in API_SYMBOLS},
            "properties": API_PROPERTIES,
            "constants": {n: freeze(getattr(api, n)) for n in CONSTANT_NAMES},
        }
        CONTRACT.write_text(json.dumps(snap, ensure_ascii=False, indent=2),
                            encoding="utf-8")
        VECTORS.write_text(json.dumps(build_vectors(), ensure_ascii=False, indent=2),
                           encoding="utf-8")
        print("wrote spec/api_contract.json / spec/protocol_vectors.json")
        return 0
    unittest.main(verbosity=2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
