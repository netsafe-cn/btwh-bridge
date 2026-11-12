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

    def test_state_entities_require_a_fresh_link(self):
        # Neither signal is enough on its own. availability says the bridge process is running,
        # and on 2026-11-12 it was running while it replayed a two-day-old snapshot; link says
        # the heater answered within the contract window. mode all means either one failing
        # greys the entity out, which is the honest rendering of "no measurement".
        rule = self.doc["home_assistant"]["availability_rule"]
        want = ["%s/%s/%s" % (self.doc["base_topic"], gen_ha.DEV, key)
                for key in rule["state_entities"]]
        for key, e in self.entities.items():
            cfg = e["config"]
            # The scalar and list forms are mutually exclusive in Home Assistant.
            self.assertNotIn("availability_topic", cfg, key)
            self.assertEqual([a["topic"] for a in cfg["availability"]], want, key)
            self.assertEqual(cfg["availability_mode"], "all", key)
            for entry in cfg["availability"]:
                self.assertEqual((entry["payload_available"], entry["payload_not_available"]),
                                 ("online", "offline"), key)

    def test_every_payload_names_its_origin(self):
        # Recommended for single-component discovery; it is what the core log prints when the
        # entity is discovered, so a user with three MQTT integrations can tell them apart.
        want = self.doc["home_assistant"]["origin"]["name"]
        for key, e in self.entities.items():
            self.assertEqual(e["config"]["origin"]["name"], want, key)
            self.assertNotIn("sw_version", e["config"]["origin"],
                             "the version already travels in attributes.fw and update/state")

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

    def test_diagnostics_survive_a_dead_heater_link(self):
        # These are the entities a remote user needs *while* the heater is unreachable:
        # pair_state and rssi explain why, and merging in the link topic would hide exactly the
        # evidence. They describe the bridge, so the bridge's availability is the right gate.
        for key, e in self.entities.items():
            cfg = e["config"]
            self.assertEqual(cfg["availability_topic"], "btwh/@DEV@/availability", key)
            self.assertNotIn("availability", cfg, key)

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

    def test_last_disconnect_epoch_is_formatted_not_passed_through(self):
        cfg = self.entities["last_disconnect_at"]["config"]
        self.assertEqual(cfg["device_class"], "timestamp")
        # Two traps, both measured in the log. `device_class: timestamp` parses ISO 8601, so a
        # bare epoch integer produces "Invalid state message '1794460577'" - the template has to
        # format. And 0 means "never dropped", where the entity should read unknown rather than
        # 1970-01-01; the else branch says "None" (HA's PAYLOAD_NONE), not an empty string,
        # which is unparseable too and logged a warning per heartbeat before 2026-11-12.
        self.assertEqual(cfg["value_template"],
                         "{% if value_json.last_disconnect_at %}"
                         "{{ value_json.last_disconnect_at | "
                         "timestamp_custom('%Y-%m-%dT%H:%M:%S%z', true) }}"
                         "{% else %}None{% endif %}")
        # The label is the same claim in prose. It said "(epoch)" until 2.9.0-ota8, which was true
        # of the wire value and false of the one thing a user actually sees - the rendered state.
        self.assertEqual(cfg["name"], "Last MQTT disconnect time")
        self.assertNotIn("epoch", cfg["name"].lower())

    def test_pair_state_is_an_enum(self):
        cfg = self.entities["pair_state"]["config"]
        self.assertEqual(cfg["device_class"], "enum")
        self.assertEqual(cfg["options"], ["unpaired", "paired", "reconnecting"])

    def test_unknown_values_are_null_in_the_contract(self):
        # rssi 0 and last_disconnect_at 0 were numbers nobody measured: a real RSSI is negative
        # and 0 only means "never dropped". The payload now sends JSON null for both, and the
        # contract says so, so a consumer cannot read the sentinel as a value. Their templates
        # need no guard: Home Assistant renders null (or the string "None") as unknown.
        for key in ("rssi", "last_disconnect_at"):
            field = next(f for f in self.doc["attributes_fields"] if f["key"] == key)
            self.assertTrue(field.get("nullable"),
                            "%s must be marked nullable; 0 is not a reading" % key)
            self.assertNotIn("0 means", field.get("note", ""),
                             "%s still documents 0 as a sentinel" % key)

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

    def test_not_optimistic_and_depends_on_a_fresh_link(self):
        self.assertFalse(self.cfg["optimistic"])
        # The heater card is the most damaging place to show a stale number: it is the thing a
        # household reads before stepping into the shower.
        self.assertEqual([a["topic"] for a in self.cfg["availability"]],
                         ["btwh/@DEV@/availability", "btwh/@DEV@/link"])
        self.assertEqual(self.cfg["availability_mode"], "all")
        self.assertNotIn("availability_topic", self.cfg)

    def test_the_generated_header_carries_the_entity(self):
        header = HEADER.read_text(encoding="utf-8")
        self.assertIn('"water_heater", "water_heater"', header)


class TestUpdateEntity(unittest.TestCase):
    """The update entity: HA's 'newer firmware available' card over the existing OTA path.

    These tests pin the design rule from spec home_assistant.update_entity: there is
    exactly one flasher and one grammar. The entity's command topic must be the plain
    live-only ota topic - the same path the bench tool drives - so the HA Install button
    and `mqtt_watch --ota` can never diverge. Its state is bridge-mediated JSON rather
    than HA reading the manifest topics directly, because the install command needs the
    URL cached on the board anyway.
    """

    def setUp(self):
        self.spec = json.loads(gen_ha.SPEC.read_text(encoding="utf-8"))
        self.entity = gen_ha.ha_update_entity(self.spec)
        self.cfg = self.entity["config"]

    def test_component_is_update_and_reads_the_update_state_topic(self):
        # The state comes from the bridge (installed_version it is running, latest_version
        # it cached from the manifest), not from HA's own view of the manifest topics.
        self.assertEqual(self.entity["component"], "update")
        self.assertEqual(self.cfg["state_topic"].replace("@DEV@", "{device_id}"),
                         self.spec["topics"]["update_state"]["topic"])

    def test_commands_reuse_the_live_only_ota_topic(self):
        # Same discipline as the water_heater entity: a new entity must not invent a new
        # command topic. Rebinding this to update/url would make a retained URL flash on
        # every reconnect - the exact boot loop the ota topic's live-only rule prevents.
        self.assertEqual(self.cfg["command_topic"].replace("@DEV@", "{device_id}"),
                         self.spec["topics"]["ota"]["topic"])
        self.assertNotEqual(self.cfg["command_topic"].replace("@DEV@", "{device_id}"),
                            self.spec["topics"]["update_manifest_url"]["topic"])
        self.assertEqual(self.cfg["payload_install"],
                         self.spec["home_assistant"]["update_entity"]["payload_install"])

    def test_title_class_and_category_follow_the_contract(self):
        # HA string-compares versions; the title is what the update card shows next to them.
        self.assertEqual(self.cfg["title"],
                         self.spec["home_assistant"]["update_entity"]["title"])
        self.assertEqual(self.cfg["device_class"], "firmware")
        # No entity_category: an update is a primary control. 'config' would file the Install
        # button under the section of the device page users do not open, which is where it was
        # until 2026-11-12.
        self.assertNotIn("entity_category", self.cfg)
        self.assertNotIn("entity_category", self.spec["home_assistant"]["update_entity"])

    def test_update_stays_available_when_the_heater_is_unreachable(self):
        # Installing firmware has nothing to do with the BLE link. Gating this card on a fresh
        # heater frame would hide the one recovery action available when the bridge is deaf.
        self.assertEqual(self.cfg["availability_topic"], "btwh/@DEV@/availability")
        self.assertEqual(self.cfg["payload_available"], "online")
        self.assertEqual(self.cfg["payload_not_available"], "offline")
        self.assertNotIn("availability", self.cfg)

    def test_the_generated_header_carries_the_entity(self):
        header = HEADER.read_text(encoding="utf-8")
        self.assertIn('"update", "update"', header)
        self.assertIn(r'"payload_install\":\"install', header)


class TestFaultBinarySensor(unittest.TestCase):
    """One field, two views: the code for a human, a problem bit for the UI and for triggers.

    A sensor cannot also carry device_class problem, and an automation that asks "is something
    wrong" should not have to template a string comparison against 'none'.
    """

    def setUp(self):
        self.spec = json.loads(gen_ha.SPEC.read_text(encoding="utf-8"))
        self.entity = gen_ha.ha_fault_binary_sensor(self.spec)
        self.cfg = self.entity["config"]

    def test_is_a_problem_binary_sensor_over_a_real_field(self):
        ha = self.spec["home_assistant"]["fault_binary_sensor"]
        self.assertEqual(self.entity["component"], "binary_sensor")
        self.assertEqual(self.cfg["device_class"], "problem")
        self.assertEqual(self.cfg["state_topic"],
                         "%s/%s/state" % (self.spec["base_topic"], gen_ha.DEV))
        self.assertIn(ha["state_field"], [f["key"] for f in self.spec["state_fields"]])

    def test_healthy_code_reads_as_off(self):
        # 'none' is healthy; any other value is a fault code. The template renders the literals
        # itself, like flame, because a Python bool renders as "True" and never matches
        # payload_on "true".
        self.assertEqual(self.cfg["value_template"],
                         "{{ 'true' if value_json.fault | default('none') != 'none' "
                         "else 'false' }}")
        self.assertEqual((self.cfg["payload_on"], self.cfg["payload_off"]), ("true", "false"))

    def test_no_branch_can_render_an_unmatchable_value(self):
        # binary_sensor.mqtt matches payload_on/payload_off only; anything else - None, an
        # empty string - is logged as "Invalid state message" on every frame, which is the
        # class of bug the last_disconnect_at template had. Hence default() rather than a
        # second branch.
        self.assertNotIn("{% else %}", self.cfg["value_template"])
        self.assertNotIn("None", self.cfg["value_template"])

    def test_is_gated_on_a_fresh_link_and_not_filed_as_diagnostic(self):
        # A fault bit is a measurement of the heater, not of the bridge: it must grey out with
        # the rest when frames stop, because a stale "no fault" is worse than no answer.
        self.assertEqual([a["topic"] for a in self.cfg["availability"]],
                         ["btwh/@DEV@/availability", "btwh/@DEV@/link"])
        self.assertEqual(self.cfg["availability_mode"], "all")
        self.assertNotIn("entity_category", self.cfg)

    def test_rides_next_to_the_field_it_reads(self):
        # Publish order is the table order; keeping the pair adjacent keeps the generated
        # header readable and makes the two views impossible to miss when editing one.
        keys = [e["key"] for e in gen_ha.entities(self.spec)]
        self.assertEqual(keys[keys.index("fault") + 1], "fault_active")
        self.assertIn('"fault_active"',
                      HEADER.read_text(encoding="utf-8"))


class TestDeviceMap(unittest.TestCase):
    """The `device` map is identity, and Home Assistant merges entities by it.

    Two entities that disagree about `identifiers` split into two devices on the device page;
    one that omits `sw_version` leaves the page showing an empty firmware field forever. Both
    failures are invisible until someone looks at the UI, so they are pinned here.
    """

    @classmethod
    def setUpClass(cls):
        cls.doc = json.loads(gen_ha.SPEC.read_text(encoding="utf-8"))
        cls.entities = gen_ha.entities(cls.doc)

    def test_every_entity_carries_one_device_map(self):
        maps = {json.dumps(e["config"]["device"], sort_keys=True) for e in self.entities}
        self.assertEqual(len(maps), 1,
                         "entities disagree about the device: %s" % sorted(maps))

    def test_sw_version_is_the_firmware_constant(self):
        # gen_ha reads kFirmwareVersion out of the C++ header rather than taking a copy in the
        # contract; the test holds the other end of that decision.
        version = gen_ha.firmware_version()
        self.assertRegex(version, r"^\d+\.\d+\.\d+",
                         "not a version the update entity can compare: %r" % version)
        for e in self.entities:
            self.assertEqual(e["config"]["device"]["sw_version"], version, e["key"])

    def test_hw_version_names_the_build_target(self):
        # Fixed for this firmware: the bridge is an ESP32-S3 and there is no second target.
        for e in self.entities:
            self.assertEqual(e["config"]["device"]["hw_version"], "ESP32-S3", e["key"])

    def test_the_header_ships_the_same_version(self):
        # --check catches a stale header, but only when someone runs it. This catches the same
        # drift inside the regression suite, which CI runs on every push.
        self.assertIn(gen_ha.firmware_version(), HEADER.read_text(encoding="utf-8"))


class TestDiscoveryFitsTheWire(unittest.TestCase):
    """A discovery payload that does not fit the transaction buffer is skipped in silence.

    On 2026-11-12 that is exactly what happened: the table grew to 1082 bytes and the
    water_heater packet needed 1143 in a 1152-byte buffer, so the entity was simply never
    published - the connect log said "20 of 21". The on-board self test now builds the real
    packet; this is the same check without hardware, so the failure surfaces in CI.
    """

    @classmethod
    def setUpClass(cls):
        import re
        cls.doc = json.loads(gen_ha.SPEC.read_text(encoding="utf-8"))
        src = (ROOT / "firmware" / "bridge" / "src" / "net" / "mqtt_client.cpp").read_text(
            encoding="utf-8")
        match = re.search(r"kTxCap\s*=\s*(\d+)", src)
        assert match, "kTxCap is gone from mqtt_client.cpp"
        cls.cap = int(match.group(1))
        cls.entities = gen_ha.entities(cls.doc)

    def test_the_largest_packet_fits_with_room(self):
        dev = "btwh_eb6f5f"                     # 11 characters, always
        worst = ("", 0)
        for e in self.entities:
            payload = gen_ha.render_payload(e["config"]).replace(gen_ha.DEV, dev)
            topic = gen_ha.discovery_topic(e["component"], e["key"], self.doc).replace(
                gen_ha.DEV, dev)
            total = len(payload) + len(topic) + 3   # 0x30 + 2-byte remaining length
            worst = max(worst, (e["key"], total))
        self.assertLess(worst[1], self.cap,
                        "%s needs %d bytes in a %d-byte buffer" % (worst[0], worst[1], self.cap))
        # Headroom, not just a pass: the next field someone adds should not silently overflow.
        self.assertLess(worst[1], self.cap - 64,
                        "%s leaves under 64 bytes of headroom (%d of %d)"
                        % (worst[0], worst[1], self.cap))


class TestEntityIdentity(unittest.TestCase):
    """Cross-entity checks that only make sense over the whole table at once."""

    @classmethod
    def setUpClass(cls):
        cls.header = HEADER.read_text(encoding="utf-8")

    def test_unique_ids_do_not_collide(self):
        # Two entities with the same unique_id overwrite each other in Home Assistant
        # silently: the device page shows one of them and no error anywhere. The
        # generated table is flattened C-escaped JSON, so the ids read straight out of it.
        import re
        ids = re.findall(r'unique_id\\":\\"([^\\]+)', self.header)
        self.assertGreater(len(ids), 1)
        self.assertEqual(len(ids), len(set(ids)),
                         "duplicate unique_id in the discovery table: %s"
                         % sorted(i for i in ids if ids.count(i) > 1))


class TestReferencePublisher(unittest.TestCase):
    """python/tools/mqtt_watch.py --demo must advertise the same device the bridge does.

    The demo is what a panel or a Node-RED flow is developed against. Its discovery list used to
    be rebuilt by hand and had fallen two entities behind the firmware (the update entity and the
    fault binary sensor) - a demo whose device differs from the real one teaches the wrong thing,
    and nothing in the suite compared them.
    """

    @classmethod
    def setUpClass(cls):
        cls.spec = json.loads(gen_ha.SPEC.read_text(encoding="utf-8"))
        tools = ROOT / "python" / "tools"
        sys.path.insert(0, str(tools))
        try:
            import mqtt_watch                                   # noqa: F401 - needs paho
        except ImportError as exc:                              # pragma: no cover
            raise unittest.SkipTest("paho-mqtt is a tool dependency: %s" % exc)

    def test_it_emits_exactly_the_generated_entity_set(self):
        import mqtt_watch

        class Recorder:
            def __init__(self):
                self.sent = []

            def publish(self, topic, payload, qos=0, retain=False):
                self.sent.append((topic, payload, retain))

        device = "btwh_demo01"
        rec = Recorder()
        count = mqtt_watch.publish_discovery(rec, self.spec, device)
        want = {gen_ha.discovery_topic(e["component"], e["key"], self.spec).replace(
                    gen_ha.DEV, device)
                for e in gen_ha.entities(self.spec)}
        self.assertEqual({t for t, _, _ in rec.sent}, want)
        self.assertEqual(count, len(want))
        # Discovery is retained on both sides: it is idempotent state, not an action.
        self.assertTrue(all(retain for _, _, retain in rec.sent))


if __name__ == "__main__":
    unittest.main()
