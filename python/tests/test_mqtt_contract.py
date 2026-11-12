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
import re
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

    def test_contract_version_is_declared_with_rules(self):
        # The version is the compatibility promise: bump minor for additions, major for
        # anything that renames a key, moves a topic or changes a meaning.
        self.assertIsInstance(self.doc.get("contract_version"), int)
        self.assertGreaterEqual(self.doc["contract_version"], 1)
        self.assertIn("parallel", self.doc.get("contract_rules", ""),
                      "major changes must promise a parallel-publishing transition")

    def test_water_heater_entity_only_reuses_contract_topics(self):
        # The HA water_heater view is an adapter: every topic it names must exist in the
        # v1 topics block. That is what keeps it from being a breaking change.
        wh = self.doc["home_assistant"]["water_heater_entity"]
        cmd = self.doc["topics"]["set_temperature"]["topic"]
        self.assertEqual(cmd, "btwh/{device_id}/set/temperature")
        self.assertIn(wh["current_temperature"], [f["key"] for f in self.doc["state_fields"]])
        self.assertIn(wh["target_temperature"], [f["key"] for f in self.doc["state_fields"]])
        self.assertEqual(wh["min_temp"], self.doc["topics"]["set_temperature"]["range"][0])
        self.assertEqual(wh["max_temp"], self.doc["topics"]["set_temperature"]["range"][1])

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

    # ---- the OTA pair: live-only trigger, retained manifest --------------------
    def test_ota_delivery_is_live_only(self):
        # A retained ota URL would reflash the bridge on every reconnect - a boot loop
        # whose trigger is the broker replaying state. The topic must never gain
        # "retain": true; the guard belongs here because the failure mode is contractual,
        # not a firmware bug.
        spec = self.doc["topics"]["ota"]
        self.assertFalse(spec.get("retain"),
                         "the ota topic must stay live-only; a retained URL reflashes "
                         "on every reconnect")
        self.assertIn("http://host[:port]/path", spec.get("grammar", ""),
                      "the ota grammar is the one the firmware parser implements "
                      "(rules::parseOtaUrl); keep them in lockstep")

    def test_manifest_topics_are_retained(self):
        # The mirror of the rule above: the manifest (latest version + image URL) is
        # state, so it is retained on purpose. A rebooted bridge re-learns it from the
        # broker on the first subscribe instead of believing it is up to date, and HA
        # shows 'unknown' rather than a fake 'no update' until the bridge confirms.
        for name in ("update_manifest_latest", "update_manifest_url"):
            spec = self.doc["topics"][name]
            self.assertTrue(spec.get("retain"),
                            "%s must be retained: replaying the manifest is idempotent"
                            % name)
            self.assertEqual(spec["direction"], "consumer -> bridge", name)

    def test_update_state_is_bridge_published_and_live(self):
        # update/state is republished on connect, on manifest change and on the 60 s
        # heartbeat - all bridge-initiated - so it needs no retain flag to stay fresh,
        # and HA never has to infer it from the manifest topics directly.
        spec = self.doc["topics"]["update_state"]
        self.assertEqual(spec["direction"], "bridge -> consumer")
        self.assertFalse(spec.get("retain"))
        self.assertIn("installed_version", spec.get("purpose", ""))

    # ---- liveness vs freshness --------------------------------------------------
    def test_link_topic_is_retained_and_bounded(self):
        # Bench finding 2026-11-12: Home Assistant showed a 42.3 degree outlet and a burning
        # flame that were two days old, because availability only ever said "the bridge is
        # running". The freshness signal therefore has to be its own retained topic with a
        # stated window - not an inference from the heartbeat.
        spec = self.doc["topics"]["link"]
        self.assertEqual(spec["direction"], "bridge -> consumer")
        self.assertEqual(spec["values"], ["online", "offline"])
        self.assertTrue(self.doc["retain"]["link"],
                        "the link value must survive a consumer restart: it is state")
        self.assertEqual(spec["stale_after_s"], 90)
        self.assertTrue(self.doc["retain"]["availability"],
                        "availability stays retained as well; the two are separate claims")

    def test_firmware_stale_window_matches_the_contract(self):
        # The firmware owns the clock, the contract owns the promise. Two copies of "90 s"
        # that can drift silently is how an entity ends up quoting a freshness it cannot
        # deliver, so the number is read out of the source that enforces it.
        src = (ROOT / "firmware" / "bridge" / "src" / "main.cpp").read_text(encoding="utf-8")
        match = re.search(r"kLinkStaleMs\s*=\s*(\d+)", src)
        self.assertIsNotNone(match, "kLinkStaleMs is gone from main.cpp")
        self.assertEqual(int(match.group(1)),
                         self.doc["topics"]["link"]["stale_after_s"] * 1000)

    def test_availability_rule_separates_state_from_bridge(self):
        # The split is the whole design: a state-fed entity may only be available while the
        # heater answers, while a bridge-fed one must stay readable exactly then. Naming a
        # topic that does not exist would produce an entity that never comes back.
        rule = self.doc["home_assistant"]["availability_rule"]
        self.assertEqual(rule["mode"], "all")
        for key in rule["state_entities"] + rule["bridge_entities"]:
            self.assertIn(key, self.doc["topics"], key)
        self.assertIn("link", rule["state_entities"])
        self.assertNotIn("link", rule["bridge_entities"])
        self.assertIn("link", self.doc["topics"]["availability"]["purpose"],
                      "availability must point at the link topic, or the next reader "
                      "will wire freshness to the will again")

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
