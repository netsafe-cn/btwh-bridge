# -*- coding: utf-8 -*-
"""test_profiles.py - keep spec/profiles.json honest

The profile table is the single source for every model-dependent number: setpoint range,
write-parameter encoding, identity. Supporting a second heater later is a new row in the
spec, and the generators hand every consumer its own slice of it. That only works if the
table cannot drift from the code and specs it feeds, so these tests fail when:

  * the table's shape is wrong (ids, ranges, hex write params)
  * a range copy in mqtt.json / api_contract.json / bosch_api.py disagrees with the row
  * the golden D3 vector no longer rebuilds from the profile's write_param
  * the A0 vector's frame_type or reported max_setpoint disagrees with the row

The profile is hand-written; this test is the mechanism that makes it a contract.
"""
from __future__ import annotations

import json
import pathlib
import unittest

import bosch_api as api

ROOT = pathlib.Path(__file__).resolve().parents[2]
PROFILES = ROOT / "spec" / "profiles.json"
MQTT = ROOT / "spec" / "mqtt.json"
API_CONTRACT = ROOT / "spec" / "api_contract.json"
VECTORS = ROOT / "spec" / "protocol_vectors.json"


class TestProfilesTable(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.doc = json.loads(PROFILES.read_text(encoding="utf-8"))

    def default_row(self) -> dict:
        rows = {p["id"]: p for p in self.doc["profiles"]}
        return rows[self.doc["default_profile"]]

    # ---- shape -----------------------------------------------------------------
    def test_schema_and_default_profile_are_declared(self):
        self.assertIsInstance(self.doc.get("schema_version"), int)
        self.assertGreaterEqual(self.doc["schema_version"], 1)
        self.assertGreaterEqual(len(self.doc["profiles"]), 1)
        ids = [p["id"] for p in self.doc["profiles"]]
        self.assertEqual(len(set(ids)), len(ids), "duplicate profile ids")
        self.assertIn(self.doc["default_profile"], ids)

    def test_every_row_is_complete_and_sane(self):
        for p in self.doc["profiles"]:
            self.assertIn(p["status"], ("verified", "expected"), p["id"])
            self.assertTrue(p["model"].strip(), p["id"])
            self.assertIn(p["a0_frame_type"], (0, 1), p["id"])
            sp = p["setpoint"]
            self.assertLess(sp["min"], sp["max"], p["id"])
            self.assertGreaterEqual(sp.get("step", 1), 1, p["id"])
            for key in ("type", "id", "index"):
                v = p["write_param"][key]
                self.assertIsInstance(v, str)
                self.assertTrue(v.startswith("0x"), "%s write_param.%s" % (p["id"], key))
                int(v, 16)                                     # parses

    # ---- cross-file lockstep ---------------------------------------------------
    def test_mqtt_json_range_copies_agree(self):
        row = self.default_row()
        lo, hi = row["setpoint"]["min"], row["setpoint"]["max"]
        mqtt = json.loads(MQTT.read_text(encoding="utf-8"))
        self.assertEqual(mqtt["topics"]["set_temperature"]["range"], [lo, hi])
        state = next(f for f in mqtt["state_fields"] if f["key"] == "setpoint")
        self.assertEqual(state["range"], [lo, hi])
        wh = mqtt["home_assistant"]["water_heater_entity"]
        self.assertEqual((wh["min_temp"], wh["max_temp"]), (lo, hi))

    def test_mqtt_json_model_matches_profile(self):
        row = self.default_row()
        mqtt = json.loads(MQTT.read_text(encoding="utf-8"))
        self.assertEqual(mqtt["home_assistant"]["device"]["model"], row["model"])

    def test_bosch_api_constants_agree(self):
        row = self.default_row()
        self.assertEqual((api.TEMP_MIN, api.TEMP_MAX),
                         (row["setpoint"]["min"], row["setpoint"]["max"]))

    def test_api_contract_constants_agree(self):
        row = self.default_row()
        contract = json.loads(API_CONTRACT.read_text(encoding="utf-8"))

        def find_constants(node):
            if isinstance(node, dict):
                if "TEMP_MIN" in node and "TEMP_MAX" in node:
                    return node
                for v in node.values():
                    found = find_constants(v)
                    if found is not None:
                        return found
            return None

        consts = find_constants(contract)
        self.assertIsNotNone(consts, "api_contract.json lost TEMP_MIN/TEMP_MAX")
        self.assertEqual((consts["TEMP_MIN"], consts["TEMP_MAX"]),
                         (row["setpoint"]["min"], row["setpoint"]["max"]))

    def test_d3_golden_vector_rebuilds_from_write_param(self):
        # The same lockstep the C++ proto self test enforces on the board: the profile's
        # write bytes must rebuild the golden D3 frame's data area.
        vectors = json.loads(VECTORS.read_text(encoding="utf-8"))
        d3 = next(c for c in vectors["cases"] if c["name"] == "d3_set_temperature_42")
        data = bytes.fromhex(d3["frame_hex"])[6:-3]          # header(6) .. checksum(1)+tail(2)
        for p in self.doc["profiles"]:
            wp = p["write_param"]
            prefix = bytes((int(wp["type"], 16), int(wp["id"], 16), int(wp["index"], 16)))
            if data[:3] == prefix:
                self.assertEqual(len(data), 4, "golden D3 carries no value byte")
                value = data[3]
                sp = p["setpoint"]
                self.assertTrue(sp["min"] <= value <= sp["max"],
                                "%s range excludes golden value %d" % (p["id"], value))
                return
        self.fail("no profile rebuilds the golden D3 vector; got data %s" % data.hex())

    def test_a0_vector_agrees_with_profile(self):
        # The heater reports its own max setpoint in the A0 frame; a profile claiming a
        # different max is wrong by definition.
        vectors = json.loads(VECTORS.read_text(encoding="utf-8"))
        a0 = [c for c in vectors["cases"] if c["name"].startswith("a0_state")]
        self.assertGreaterEqual(len(a0), 1)
        for p in self.doc["profiles"]:
            matching = [c for c in a0 if c["state"]["frame_type"] == p["a0_frame_type"]]
            self.assertGreaterEqual(len(matching), 1,
                                    "%s: no A0 vector for frame_type %d" % (p["id"], p["a0_frame_type"]))
            for c in matching:
                self.assertEqual(c["state"]["max_setpoint"], p["setpoint"]["max"],
                                 "%s max disagrees with the heater's own report" % p["id"])

    def test_generated_header_is_fresh(self):
        header = ROOT / "firmware" / "bridge" / "src" / "proto" / "profiles_gen.h"
        text = header.read_text(encoding="utf-8")
        for p in self.doc["profiles"]:
            self.assertIn('"%s"' % p["id"], text, "%s missing - rerun gen_profiles.py" % p["id"])
            wp = p["write_param"]
            self.assertIn("0x%02X, 0x%02X, 0x%02X"
                          % (int(wp["type"], 16), int(wp["id"], 16), int(wp["index"], 16)),
                          text, "%s write bytes missing - rerun gen_profiles.py" % p["id"])


if __name__ == "__main__":
    unittest.main()
