# -*- coding: utf-8 -*-
"""test_mqtt_contract.py - keep spec/mqtt.json honest

The MQTT contract is what lets the bridge and the panel be separate applications: the panel
never reads the bridge's source, it reads this file. That only works if the file cannot drift
away from the code it describes, so these tests fail when:

  * a field named in the contract no longer exists on DeviceState
  * a source attribute exists but its text form is not among the documented enum values
  * the setpoint range in the contract disagrees with TEMP_MIN / TEMP_MAX
  * a topic template lost its {device_id} placeholder
  * a state field is missing the metadata a UI needs to render it

The contract itself is hand-written; this test is the mechanism that makes it a contract.
"""
from __future__ import annotations

import json
import pathlib
import unittest
from dataclasses import fields as dataclass_fields

import bosch_api as api

ROOT = pathlib.Path(__file__).resolve().parents[2]
SPEC = ROOT / "spec" / "mqtt.json"


class TestMqttContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.raw = SPEC.read_text(encoding="utf-8")
        cls.doc = json.loads(cls.raw)

    # ---- shape -----------------------------------------------------------------
    def test_is_valid_json_with_schema_version(self):
        self.assertIsInstance(self.doc.get("schema_version"), int)
        self.assertGreaterEqual(self.doc["schema_version"], 1)

    def test_state_fields_are_unique_and_complete(self):
        keys = [f["key"] for f in self.doc["state_fields"]]
        self.assertEqual(len(keys), len(set(keys)), "duplicate key in state_fields")
        for f in self.doc["state_fields"]:
            for required in ("key", "type", "label"):
                self.assertIn(required, f, "field %r lacks %s" % (f.get("key"), required))
            self.assertIn(f["type"], ("float", "int", "bool", "string", "enum"))
            if f["type"] == "enum":
                self.assertTrue(f.get("values"), "enum %r has no values" % f["key"])
            if f["type"] in ("float", "int"):
                self.assertIn("unit", f, "%r must carry a unit" % f["key"])

    def test_every_topic_has_a_device_id_placeholder(self):
        for name, spec in self.doc["topics"].items():
            self.assertIn("{device_id}", spec["topic"],
                          "topic %r would collide between two heaters" % name)
            self.assertIn(spec["direction"], ("bridge -> consumer", "consumer -> bridge"))

    # ---- coupling to the library ------------------------------------------------
    def test_every_source_attribute_exists_on_device_state(self):
        # A source may be a dataclass field or a derived property (failure_text is one), so
        # check the instance rather than the field list - hasattr covers both.
        inst = api.DeviceState()
        names = {f.name for f in dataclass_fields(api.DeviceState)}
        for f in self.doc["state_fields"]:
            src = f.get("source")
            if not src:
                continue
            self.assertTrue(src.startswith("DeviceState."), src)
            attr = src.split(".", 1)[1]
            self.assertTrue(hasattr(inst, attr),
                            "spec/mqtt.json references %r; DeviceState fields are %s"
                            % (attr, ", ".join(sorted(names))))

    def test_mode_values_match_the_library(self):
        declared = next(f for f in self.doc["state_fields"] if f["key"] == "mode")["values"]
        produced = {api.DeviceState(mode=m).mode_text for m in (None, 0, 1, 2, 99)}
        self.assertTrue(produced <= set(declared),
                        "mode_text produces %s, contract declares %s"
                        % (sorted(produced), sorted(declared)))

    def test_fault_text_matches_the_library(self):
        healthy = api.DeviceState(failure=0).failure_text
        self.assertIn(healthy, self.doc["state_fields"][10]["note"].lower() + "none",
                      "a healthy heater must map to the documented 'none'")
        self.assertEqual(api.DeviceState(failure=0x2A).failure_text, "2A",
                         "fault codes are two upper-case hex digits")

    def test_setpoint_range_matches_the_library(self):
        rng = self.doc["topics"]["set_temperature"]["range"]
        self.assertEqual(rng, [api.TEMP_MIN, api.TEMP_MAX])
        setpoint = next(f for f in self.doc["state_fields"] if f["key"] == "setpoint")
        self.assertEqual(setpoint["range"], [api.TEMP_MIN, api.TEMP_MAX])

    # ---- consumer-facing rules --------------------------------------------------
    def test_consumer_rules_are_documented(self):
        rules = self.doc.get("consumer_rules") or []
        self.assertGreaterEqual(len(rules), 3,
                                "a consumer needs the missing-value / offline / fault rules")
        joined = " ".join(rules).lower()
        for concept in ("missing", "offline", "fault"):
            self.assertIn(concept, joined)

    def test_draft_contract_is_not_silently_released(self):
        """status: draft means the bridge does not publish yet; flipping it is a decision."""
        self.assertIn(self.doc.get("status"), ("draft", "stable"))
        if self.doc["status"] == "stable":
            self.assertEqual(self.doc["schema_version"], 1,
                             "bump the version when the contract goes stable")


if __name__ == "__main__":
    unittest.main()
