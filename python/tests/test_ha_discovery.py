# -*- coding: utf-8 -*-
"""test_ha_discovery.py - the generated Home Assistant table must follow spec/mqtt.json

The firmware publishes discovery payloads from a generated header. Two failures are worth a
test rather than a review:

  * the header forgotten after a contract edit - Home Assistant would show stale entities, and
    nothing in the build would complain
  * an entity that points at the wrong state topic - it would appear in the UI and never update,
    which looks like a bridge fault rather than a config fault

Both are checked against the contract, not against a hand-written expectation, so adding a
field to spec/mqtt.json needs no edit here.
"""
from __future__ import annotations

import json
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
GEN_DIR = ROOT / "firmware" / "bridge" / "tools"
HEADER = ROOT / "firmware" / "bridge" / "src" / "net" / "ha_discovery_gen.h"

sys.path.insert(0, str(GEN_DIR))
import gen_ha                                                  # noqa: E402


class TestDiscoveryIsGenerated(unittest.TestCase):
    def test_header_matches_the_generator(self):
        want = gen_ha.build()
        got = HEADER.read_text(encoding="utf-8") if HEADER.exists() else ""
        self.assertEqual(got, want,
                         "ha_discovery_gen.h is stale; run: python "
                         "firmware/bridge/tools/gen_ha.py")

    def test_every_state_field_has_an_entity_except_metadata(self):
        doc = json.loads((ROOT / "spec" / "mqtt.json").read_text(encoding="utf-8"))
        keys = [f["key"] for f in doc["state_fields"] if f["key"] not in gen_ha.SKIP]
        header = HEADER.read_text(encoding="utf-8")
        for key in keys:
            self.assertIn('"%s"' % key, header, "no discovery entity for %s" % key)
        self.assertNotIn('"ts"', header,
                         "ts is bridge metadata; an entity for it would read as a measurement")


class TestEntityConfig(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.doc = json.loads((ROOT / "spec" / "mqtt.json").read_text(encoding="utf-8"))
        cls.entities = {e["key"]: e for e in
                        (gen_ha.ha_entity(f, cls.doc) for f in cls.doc["state_fields"])
                        if e}

    def test_all_entities_share_the_state_topic(self):
        want = "%s/%s/state" % (self.doc["base_topic"], gen_ha.DEV)
        for key, e in self.entities.items():
            self.assertEqual(e["config"]["state_topic"], want, key)

    def test_availability_is_wired_the_same_way_everywhere(self):
        want = "%s/%s/availability" % (self.doc["base_topic"], gen_ha.DEV)
        for key, e in self.entities.items():
            self.assertEqual(e["config"]["availability_topic"], want, key)
            self.assertEqual(e["config"]["payload_not_available"], "offline", key)

    def test_setpoint_is_a_number_bound_to_the_command_topic(self):
        e = self.entities["setpoint"]
        self.assertEqual(e["component"], "number")
        cfg = e["config"]
        self.assertEqual(cfg["command_topic"],
                         "%s/%s/set/temperature" % (self.doc["base_topic"], gen_ha.DEV))
        self.assertEqual((cfg["min"], cfg["max"]), (35, 60))
        self.assertFalse(cfg["optimistic"],
                         "a setpoint the heater refused must not look applied")

    def test_flame_is_a_binary_sensor_with_json_booleans(self):
        cfg = self.entities["flame"]["config"]
        self.assertEqual(cfg["payload_on"], "true")
        self.assertEqual(cfg["payload_off"], "false")
        self.assertEqual(cfg["device_class"], "running")
        # The state payload carries a JSON boolean. HA parses it into a Python bool, whose
        # template rendering is "True" - capital T - which never matches payload_on. The
        # template must therefore render the literals itself.
        self.assertEqual(cfg["value_template"],
                         "{{ 'true' if value_json.flame else 'false' }}")

    def test_temperature_sensors_declare_class_and_unit(self):
        for key in ("temp_out", "temp_in", "chamber", "temp_box"):
            cfg = self.entities[key]["config"]
            self.assertEqual(cfg["device_class"], "temperature", key)
            self.assertEqual(cfg["state_class"], "measurement", key)
            self.assertEqual(cfg["unit_of_measurement"], "°C", key)


if __name__ == "__main__":
    unittest.main()
