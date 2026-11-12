#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""gen_ha.py - spec/mqtt.json -> src/net/ha_discovery_gen.h

Home Assistant discovery payloads, generated from the same contract that drives the state
payload. Nothing here is retyped by hand: rename a field in spec/mqtt.json, run this, and the
entity list follows.

Why generate instead of publishing `homeassistant/...` by hand from the firmware:

  * the entity list and `state_fields` cannot drift (one source, checked by --check in CI)
  * the firmware keeps a table of (topic, payload) pairs, so the client stays a byte pump
  * a consumer that is not Home Assistant (the touch panel, Node-RED) can read the same table

The `@DEV@` placeholder is replaced at runtime with the device id (`btwh_<last6>`), because the
address is not known until the heater is paired. Topic and payload both carry it.

Entities: one per state field except `ts`, which is bridge metadata rather than a measurement.
`setpoint` becomes a number entity bound to the command topic, `flame` a binary sensor.
`fault` gets two views - the code as a sensor and a `problem` binary sensor, because a sensor
cannot also be one and an automation should not have to template a string comparison.
Every attributes field becomes a diagnostic sensor on the attributes topic - the remote
troubleshooting surface: firmware, uptime, RSSI, pairing state, last disconnect.

Availability is two signals, not one, and the split follows the data: an entity fed by state
frames is available only while those frames are fresh (availability + link, mode all), while
an entity fed by attributes or by the firmware itself stays readable when the heater is
unreachable - that is exactly when a remote user needs the pairing state and the Install
button. spec home_assistant.availability_rule is the single place that decides.

On top of those, one `water_heater` domain entity: current outlet temperature, the setpoint
bound to the existing command topic, and a read-only mode ('gas' while the burner fires,
'off' while idle). It reuses the v1 contract topics; the heater becomes a first-class
water heater card instead of a pile of sensors. And one `update` entity over the OTA
path: state from update/state (bridge-mediated), install via the existing ota topic.

Usage:
    python firmware/bridge/tools/gen_ha.py            # write the header
    python firmware/bridge/tools/gen_ha.py --check    # fail if it is stale (CI)
"""
from __future__ import annotations

import json
import pathlib
import re
import sys

HERE = pathlib.Path(__file__).resolve().parent            # firmware/bridge/tools
BRIDGE = HERE.parent                                      # firmware/bridge
REPO = BRIDGE.parents[1]                                  # repository root
SPEC = REPO / "spec" / "mqtt.json"
OUT = BRIDGE / "src" / "net" / "ha_discovery_gen.h"
VERSION_SRC = BRIDGE / "src" / "net" / "mqtt_client.h"

DEV = "@DEV@"                       # runtime substitution, replaced by the device id
FW_TOKEN = "@FW@"                   # build substitution, replaced by kFirmwareVersion
SKIP = {"ts"}                       # bridge metadata, not an entity

# Extra presentation hints that the contract does not need to know about. Kept here rather
# than in spec/mqtt.json: they only matter to Home Assistant.
ICONS = {
    "water_flow": "mdi:water",
    "burner_power": "mdi:fire",
    "fan_speed": "mdi:fan",
    "chamber": "mdi:thermometer-high",
    "temp_box": "mdi:thermometer",
    "fw": "mdi:chip",
    "hw": "mdi:developer-board",
    "heater": "mdi:bluetooth",
    "pair_state": "mdi:bluetooth-connect",
    "uptime": "mdi:clock-outline",
    "rssi": "mdi:signal",
    "last_disconnect": "mdi:alert-circle-outline",
    "last_disconnect_at": "mdi:clock-remove",
}

# How an attributes field maps onto a Home Assistant sensor. The default is the plain
# `{{ value_json.<key> }}` template; the cases below carry device classes or need their
# template shaped (an epoch 0 must read as unknown, not as 1970).
def availability(doc: dict, keys: list[str]) -> dict:
    """Availability wiring for one entity, straight from home_assistant.availability_rule.

    Home Assistant allows either a single `availability_topic` or a list of topics with
    `availability_mode`, never both. One key therefore keeps the scalar form; two keys switch
    to the list, where the entity is available only when every signal is - which is how a
    stale reading stops looking live without muting the diagnostics.
    """
    def one(key: str) -> dict:
        return {
            "topic": doc["topics"][key]["topic"].replace("{device_id}", DEV),
            "payload_available": "online",
            "payload_not_available": "offline",
        }

    if len(keys) == 1:
        only = one(keys[0])
        return {"availability_topic": only["topic"],
                "payload_available": only["payload_available"],
                "payload_not_available": only["payload_not_available"]}
    return {"availability": [one(k) for k in keys],
            "availability_mode": doc["home_assistant"]["availability_rule"].get("mode") or "all"}


def origin(doc: dict) -> dict:
    """Discovery provenance: recommended for single-component discovery, required for device
    discovery. sw_version is deliberately absent - see spec home_assistant.origin.note."""
    spec = doc["home_assistant"]["origin"]
    out = {"name": spec["name"]}
    if spec.get("support_url"):
        out["support_url"] = spec["support_url"]
    return out


def firmware_version() -> str:
    """The version the compiler sees, read from the one place that defines it.

    The device page's sw_version must not be a second copy of a number that is bumped by hand
    in a different file: it is read from the C++ constant, and --check fails when the header
    was generated before a bump. Same rule as the stale window in test_mqtt_contract.py.
    """
    text = VERSION_SRC.read_text(encoding="utf-8")
    match = re.search(r'kFirmwareVersion\s*=\s*"([^"]+)"', text)
    if not match:
        raise SystemExit("gen_ha: kFirmwareVersion not found in %s" % VERSION_SRC)
    return match.group(1)


def device_block(doc: dict) -> dict:
    """The `device` map every entity carries, built in one place.

    Four entities used to spell this map out separately; one helper is the difference between
    "they happen to agree today" and "they cannot disagree". sw_version/hw_version live here
    rather than on diagnostic sensors because that is where Home Assistant shows them on the
    device page, and they are properties of the hardware, not measurements.
    """
    spec = doc["home_assistant"]["device"]
    out = {
        "identifiers": [DEV],
        "manufacturer": spec["manufacturer"],
        "model": spec["model"],
        "name": spec["name"],
    }
    sw = spec.get("sw_version")
    if sw:
        out["sw_version"] = firmware_version() if sw == FW_TOKEN else sw
    if spec.get("hw_version"):
        out["hw_version"] = spec["hw_version"]
    return out


def ha_diag_entity(field: dict, doc: dict) -> dict:
    key = field["key"]
    base = doc["base_topic"]
    ha = doc["home_assistant"]

    cfg: dict = {
        "name": field.get("label") or key,
        "unique_id": "%s_%s" % (DEV, key),
        "state_topic": "%s/%s/attributes" % (base, DEV),
        **availability(doc, ha["availability_rule"]["bridge_entities"]),
        "entity_category": "diagnostic",
        "value_template": "{{ value_json.%s }}" % key,
        "origin": origin(doc),
        "device": device_block(doc),
    }

    if key == "pair_state":
        cfg.update({"device_class": "enum", "options": field.get("values") or []})
    elif key == "uptime":
        # A duration the user can graph; not total_increasing because it resets on reboot.
        cfg.update({"device_class": "duration", "unit_of_measurement": "s",
                    "state_class": "measurement"})
    elif key == "rssi":
        cfg.update({"device_class": "signal_strength", "unit_of_measurement": "dBm",
                    "state_class": "measurement"})
    elif key == "last_disconnect_at":
        cfg["device_class"] = "timestamp"
        # The sensor side of device_class: timestamp parses an ISO 8601 string, not an epoch
        # integer - raw seconds produced "Invalid state message '1794460577'" in the log even
        # though the entity looked plausible. So the template formats, it does not pass through.
        # The else branch must render Home Assistant's PAYLOAD_NONE ("None") rather than an
        # empty string, which is also unparseable: 0 means "never dropped", and the entity
        # should read unknown instead of 1970-01-01 or a warning per heartbeat.
        iso = "timestamp_custom('%Y-%m-%dT%H:%M:%S%z', true)"
        cfg["value_template"] = ("{% if value_json." + key + " %}{{ value_json." + key
                                 + " | " + iso + " }}{% else %}None{% endif %}")

    if key in ICONS:
        cfg["icon"] = ICONS[key]
    return {"component": "sensor", "key": key, "config": cfg}


def ha_entity(field: dict, doc: dict) -> dict | None:
    """One entity config, or None when the field is not published as an entity."""
    key = field["key"]
    if key in SKIP:
        return None

    base = doc["base_topic"]
    kind = field["type"]
    ha = doc["home_assistant"]

    if key == "setpoint":
        component = "number"
    elif kind == "bool":
        component = "binary_sensor"
    else:
        component = "sensor"

    cfg: dict = {
        "name": field.get("label") or key,
        "unique_id": "%s_%s" % (DEV, key),
        "state_topic": "%s/%s/state" % (base, DEV),
        **availability(doc, ha["availability_rule"]["state_entities"]),
        "origin": origin(doc),
        "device": device_block(doc),
    }

    if key == "setpoint":
        cfg.update({
            "command_topic": "%s/%s/set/temperature" % (base, DEV),
            "min": field["range"][0],
            "max": field["range"][1],
            "step": 1,
            "mode": "box",
            "unit_of_measurement": field.get("unit") or "°C",
            "value_template": "{{ value_json.%s }}" % key,
            # State is echoed from the heater, not from what we asked for: a setpoint the
            # heater refused must not look applied.
            "optimistic": False,
        })
        return {"component": component, "key": key, "config": cfg}

    cfg["value_template"] = "{{ value_json.%s }}" % key

    if component == "binary_sensor":
        # value_json.flame is a Python bool after HA parses the state payload; rendering it as
        # `{{ value_json.flame }}` yields "True", which never string-matches payload_on "true".
        # Rendering the literals ourselves keeps the comparison exact.
        cfg["value_template"] = "{{ 'true' if value_json.%s else 'false' }}" % key
        cfg.update({
            "payload_on": "true",
            "payload_off": "false",
            "device_class": field.get("device_class") or "running",
        })
        return {"component": component, "key": key, "config": cfg}

    if kind == "enum":
        values = field.get("values") or []
        cfg.update({"device_class": "enum", "options": values,
                    "icon": "mdi:fire" if key == "mode" else "mdi:information"})
        return {"component": component, "key": key, "config": cfg}

    if key == "fault":
        # A fault badge needs to shout; a healthy heater needs to look calm. The template is
        # what makes one entity do both.
        cfg["icon"] = ("{% if value_json.fault == 'none' %}mdi:check-circle"
                       "{% else %}mdi:alert{% endif %}")
        return {"component": component, "key": key, "config": cfg}

    if field.get("unit"):
        cfg["unit_of_measurement"] = field["unit"]
    if field.get("device_class"):
        cfg["device_class"] = field["device_class"]
    if field.get("state_class"):
        cfg["state_class"] = field["state_class"]
    if key in ICONS:
        cfg["icon"] = ICONS[key]
    return {"component": component, "key": key, "config": cfg}


def ha_fault_binary_sensor(doc: dict) -> dict:
    """The `problem` binary_sensor over `fault`, from home_assistant.fault_binary_sensor.

    The string sensor keeps the code for a human; this answers the only question an automation
    asks. device_class problem is the community convention for it, and a sensor cannot carry
    that class, so the field needs two entities rather than one clever one.

    The template defaults a missing field to the healthy value instead of rendering None or an
    empty string: both would make Home Assistant log "Invalid state message" on every frame,
    which is the failure this file fixed once already for last_disconnect_at.
    """
    spec = doc["home_assistant"]["fault_binary_sensor"]
    field = spec["state_field"]
    healthy = spec["healthy_value"]
    cfg: dict = {
        "name": spec["name"],
        "unique_id": "%s_%s" % (DEV, spec["object_id"]),
        "state_topic": "%s/%s/state" % (doc["base_topic"], DEV),
        **availability(doc, doc["home_assistant"]["availability_rule"]["state_entities"]),
        "value_template": ("{{ 'true' if value_json.%s | default('%s') != '%s' else 'false' }}"
                           % (field, healthy, healthy)),
        "payload_on": "true",
        "payload_off": "false",
        "device_class": spec["device_class"],
        "origin": origin(doc),
        "device": device_block(doc),
    }
    return {"component": spec["component"], "key": spec["object_id"], "config": cfg}


def ha_update_entity(doc: dict) -> dict:
    """The update entity: HA's 'there is newer firmware' card over the existing OTA path.

    Everything comes from spec home_assistant.update_entity. The command topic is the
    plain ota topic (payload_install "install" triggers the cached manifest URL), so the
    update button and the bench tool drive the same flasher. state is bridge-mediated JSON:
    installed_version from the firmware, latest_version from the retained manifest topics.
    """
    ha = doc["home_assistant"]
    ue = ha["update_entity"]
    state_topic = doc["topics"]["update_state"]["topic"].replace("{device_id}", DEV)
    command_topic = doc["topics"]["ota"]["topic"].replace("{device_id}", DEV)

    cfg: dict = {
        "name": ue.get("name") or "Firmware",
        "unique_id": "%s_update" % DEV,
        "state_topic": state_topic,
        "command_topic": command_topic,
        "payload_install": ue["payload_install"],
        "title": ue["title"],
        "device_class": ue["device_class"],
        # No entity_category: an update is a first-class control, not configuration. Filing it
        # under "Configuration" hides the Install button in the section users do not open.
        **({"entity_category": ue["entity_category"]} if ue.get("entity_category") else {}),
        **availability(doc, ha["availability_rule"]["bridge_entities"]),
        "origin": origin(doc),
        "device": device_block(doc),
    }
    return {"component": "update", "key": "update", "config": cfg}


def ha_water_heater_entity(doc: dict) -> dict:
    """The water_heater domain entity: one device-level heater view over the same topics.

    Everything comes from spec home_assistant.water_heater_entity, so the mapping lives in
    the contract, not here. No mode_command_topic: a tankless heater has no switchable mode,
    so 'gas' (burner firing) / 'off' (idle) is read-only state.
    """
    base = doc["base_topic"]
    ha = doc["home_assistant"]
    wh = ha["water_heater_entity"]
    state_topic = "%s/%s/state" % (base, DEV)

    cfg: dict = {
        # null: the entity takes the device name ("Water heater"), avoiding the
        # "Water heater Water heater" the concatenation would otherwise produce.
        "name": None,
        "default_entity_id": "water_heater",
        "unique_id": "%s_water_heater" % DEV,
        "current_temperature_topic": state_topic,
        "current_temperature_template": "{{ value_json.%s }}" % wh["current_temperature"],
        "temperature_state_topic": state_topic,
        "temperature_state_template": "{{ value_json.%s }}" % wh["target_temperature"],
        "temperature_command_topic": doc["topics"]["set_temperature"]["topic"].replace(
            "{device_id}", DEV),
        "temperature_unit": wh["temperature_unit"],
        "min_temp": wh["min_temp"],
        "max_temp": wh["max_temp"],
        "precision": wh["precision"],
        "modes": wh["modes"],
        "mode_state_topic": state_topic,
        "mode_state_template": wh["mode_template"],
        # Target temperature is echoed from the heater, never assumed from the command.
        "optimistic": False,
        **availability(doc, ha["availability_rule"]["state_entities"]),
        "origin": origin(doc),
        "device": device_block(doc),
    }
    return {"component": "water_heater", "key": "water_heater", "config": cfg}


def c_str(text: str) -> str:
    """Escape for a C++ string literal."""
    return text.replace("\\", "\\\\").replace('"', '\\"')


def discovery_topic(component: str, key: str, doc: dict) -> str:
    return "%s/%s/%s/%s/config" % (doc["home_assistant"]["discovery_prefix"],
                                   component, DEV, key)


def render_payload(cfg: dict) -> str:
    return json.dumps(cfg, separators=(",", ":"), ensure_ascii=False)


def entities(doc: dict) -> list:
    """The whole table, in publish order. Shared with the tests so they check what ships."""
    fault = doc.get("home_assistant", {}).get("fault_binary_sensor")
    out = []
    for field in doc["state_fields"]:
        e = ha_entity(field, doc)
        if e:
            out.append(e)
        # A derived entity is emitted next to the field it reads, so the table keeps reading
        # as "everything about fault" instead of as two lists that can drift apart.
        if fault and field["key"] == fault["state_field"]:
            out.append(ha_fault_binary_sensor(doc))
    out += [ha_diag_entity(f, doc) for f in doc["attributes_fields"]]
    if "water_heater_entity" in doc.get("home_assistant", {}):
        out.append(ha_water_heater_entity(doc))
    if "update_entity" in doc.get("home_assistant", {}):
        out.append(ha_update_entity(doc))
    return out


def build() -> str:
    doc = json.loads(SPEC.read_text(encoding="utf-8"))
    table = entities(doc)

    out = []
    out.append("""\
// GENERATED FILE - do not edit.
// Source: spec/mqtt.json
// Regenerate: python firmware/bridge/tools/gen_ha.py
//
// Home Assistant MQTT discovery, one entry per published field. The token @DEV@ in a topic or
// payload is replaced at runtime with the device id (btwh_<last6>), which is only known once
// the heater address is configured. Layer rule (ADR-008): data only - the publisher lives in
// mqtt_client.cpp and the substitution in discovery.cpp.
""")
    out.append("""#pragma once
// Macro guard as well as #pragma once: arduino-cli compiles a copy of src/ under
// <build>/sketch/, so a header can be reached through two paths.
#ifndef BTWH_NET_HA_DISCOVERY_GEN_H
#define BTWH_NET_HA_DISCOVERY_GEN_H

#include <stddef.h>

namespace net {
namespace discovery {

struct EntityDesc {
  const char* component;   // "sensor" | "binary_sensor" | "number"
  const char* key;         // state field name, also the discovery object id
  const char* topic;       // discovery topic, carries the @DEV@ token
  const char* payload;     // entity config JSON, carries the @DEV@ token
};

static const EntityDesc kEntities[] = {
""")

    for e in table:
        topic = discovery_topic(e["component"], e["key"], doc)
        payload = render_payload(e["config"])
        out.append('  {"%s", "%s", "%s",\n   "%s"},\n'
                   % (e["component"], c_str(e["key"]), c_str(topic), c_str(payload)))
    out.append("};\nstatic const size_t kEntityCount = %d;\n\n" % len(table))

    # One fully rendered pair, so the on-board self test can check the substitution without
    # re-implementing JSON.
    sample = table[0]
    sample_dev = "btwh_eb6f5f"
    out.append("// Golden pair for selftest: the first entity with a known device id substituted.\n")
    out.append('constexpr const char* kGoldenTopic =\n    "%s";\n'
               % c_str(discovery_topic(sample["component"], sample["key"], doc).replace(DEV, sample_dev)))
    out.append('constexpr const char* kGoldenPayload =\n    "%s";\n\n'
               % c_str(render_payload(sample["config"]).replace(DEV, sample_dev)))
    out.append("}  // namespace discovery\n}  // namespace net\n\n"
               "#endif  // BTWH_NET_HA_DISCOVERY_GEN_H\n")
    return "".join(out)


def main() -> int:
    text = build()
    if "--check" in sys.argv:
        current = OUT.read_text(encoding="utf-8") if OUT.exists() else ""
        if current != text:
            print("%s is out of date; run: python firmware/bridge/tools/gen_ha.py" % OUT)
            return 1
        print("%s matches spec/mqtt.json" % OUT.name)
        return 0

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(text, encoding="utf-8", newline="\n")
    print("wrote %s (%d bytes, %d entities)" % (OUT, len(text), text.count('{"')))
    return 0


if __name__ == "__main__":
    sys.exit(main())
