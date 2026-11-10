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


class TestDiagnosticEntities(unittest.TestCase):
    """The attributes fields are the remote-troubleshooting surface: an international user
    with no serial access reads firmware, RSSI and the last disconnect through these.

    That is why they must be entities (not just topic payload): a diagnostic entity is
    visible in Home Assistant without the user subscribing to anything.
    """

    @classmethod
    def setUpClass(cls):
        cls.doc = json.loads((ROOT / "spec" / "mqtt.json").read_text(encoding="utf-8"))
        cls.entities = {e["key"]: e for e in
                        (gen_ha.ha_diag_entity(f, cls.doc) for f in cls.doc["attributes_fields"])}

    def test_every_attributes_field_is_a_diagnostic_entity(self):
        self.assertEqual(set(self.entities),
                         {f["key"] for f in self.doc["attributes_fields"]},
                         "an attributes field has no entity: %s"
                         % sorted(set(f["key"] for f in self.doc["attributes_fields"])
                                  - set(self.entities)))

    def test_diagnostics_read_the_attributes_topic_and_are_filed_as_diagnostics(self):
        want = "%s/%s/attributes" % (self.doc["base_topic"], gen_ha.DEV)
        for key, e in self.entities.items():
            cfg = e["config"]
            self.assertEqual(e["component"], "sensor", key)
            self.assertEqual(cfg["state_topic"], want, key)
            self.assertEqual(cfg["entity_category"], "diagnostic", key)

    def test_rssi_and_uptime_are_graphable(self):
        rssi = self.entities["rssi"]["config"]
        self.assertEqual(rssi["device_class"], "signal_strength")
        self.assertEqual(rssi["unit_of_measurement"], "dBm")
        self.assertEqual(rssi["state_class"], "measurement")
        up = self.entities["uptime"]["config"]
        self.assertEqual(up["device_class"], "duration")
        self.assertEqual(up["unit_of_measurement"], "s")
        # Not total_increasing: uptime resets on every reboot.
        self.assertEqual(up["state_class"], "measurement")

    def test_last_disconnect_epoch_zero_reads_as_unknown(self):
        cfg = self.entities["last_disconnect_at"]["config"]
        self.assertEqual(cfg["device_class"], "timestamp")
        # 0 = "never dropped". Without the guard HA renders 1970-01-01 and a healthy
        # bridge looks like it dropped during the epoch.
        self.assertEqual(cfg["value_template"],
                         "{% if value_json.last_disconnect_at %}"
                         "{{ value_json.last_disconnect_at }}{% endif %}")

    def test_pair_state_is_an_enum(self):
        cfg = self.entities["pair_state"]["config"]
        self.assertEqual(cfg["device_class"], "enum")
        self.assertEqual(cfg["options"], ["unpaired", "paired", "reconnecting"])

    def test_the_generated_header_contains_the_diagnostics(self):
        header = HEADER.read_text(encoding="utf-8")
        for key in self.entities:
            self.assertIn('"%s"' % key, header, "no diagnostic entity for %s" % key)


class TestWaterHeaterEntity(unittest.TestCase):
    """The water_heater domain entity: one device-level heater view over the v1 topics.

    These tests pin the mapping that makes the heater a first-class water heater in
    Home Assistant - and the discipline that it is an *adapter view*: every topic it
    touches must already exist in the v1 contract, so adding the entity cannot be a
    breaking change for any other consumer.
    """

    def setUp(self):
        self.spec = json.loads(gen_ha.SPEC.read_text(encoding="utf-8"))
        self.cfg = gen_ha.ha_water_heater_entity(self.spec)["config"]

    def test_current_temperature_reads_the_state_json(self):
        self.assertEqual(self.cfg["current_temperature_topic"], "btwh/@DEV@/state")
        self.assertEqual(self.cfg["current_temperature_template"], "{{ value_json.temp_out }}")

    def test_target_temperature_reads_setpoint(self):
        self.assertEqual(self.cfg["temperature_state_topic"], "btwh/@DEV@/state")
        self.assertEqual(self.cfg["temperature_state_template"], "{{ value_json.setpoint }}")

    def test_commands_reuse_the_contract_topic(self):
        # The existing command topic, unchanged - a new entity must not invent a new one.
        # The generator swaps the contract's {device_id} token for its own @DEV@ at write
        # time; comparing with the swap undone proves they are the same topic.
        self.assertEqual(self.cfg["temperature_command_topic"].replace("@DEV@", "{device_id}"),
                         self.spec["topics"]["set_temperature"]["topic"])

    def test_range_and_unit_follow_the_contract(self):
        self.assertEqual(self.cfg["min_temp"], 35)
        self.assertEqual(self.cfg["max_temp"], 60)
        self.assertEqual(self.cfg["precision"], 1.0)
        # water_heater.mqtt takes "C"/"F" - not the degree sign the sensor schema demands.
        self.assertEqual(self.cfg["temperature_unit"], "C")

    def test_mode_is_read_only_and_maps_flame(self):
        # A tankless heater has no switchable mode; 'gas' while firing, 'off' while idle.
        self.assertEqual(self.cfg["modes"], ["off", "gas"])
        self.assertEqual(self.cfg["mode_state_topic"], "btwh/@DEV@/state")
        self.assertEqual(self.cfg["mode_state_template"],
                         "{{ 'gas' if value_json.flame else 'off' }}")
        self.assertNotIn("mode_command_topic", self.cfg)

    def test_not_optimistic_and_same_availability(self):
        self.assertFalse(self.cfg["optimistic"])
        self.assertEqual(self.cfg["availability_topic"], "btwh/@DEV@/availability")
        self.assertEqual(self.cfg["payload_not_available"], "offline")

    def test_the_generated_header_carries_the_entity(self):
        header = HEADER.read_text(encoding="utf-8")
        self.assertIn('"water_heater", "water_heater"', header)


if __name__ == "__main__":
    unittest.main()
