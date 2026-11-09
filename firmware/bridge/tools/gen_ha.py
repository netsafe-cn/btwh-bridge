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

Usage:
    python firmware/bridge/tools/gen_ha.py            # write the header
    python firmware/bridge/tools/gen_ha.py --check    # fail if it is stale (CI)
"""
from __future__ import annotations

import json
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent            # firmware/bridge/tools
BRIDGE = HERE.parent                                      # firmware/bridge
REPO = BRIDGE.parents[1]                                  # repository root
SPEC = REPO / "spec" / "mqtt.json"
OUT = BRIDGE / "src" / "net" / "ha_discovery_gen.h"

DEV = "@DEV@"                       # runtime substitution, replaced by the device id
SKIP = {"ts"}                       # bridge metadata, not an entity

# Extra presentation hints that the contract does not need to know about. Kept here rather
# than in spec/mqtt.json: they only matter to Home Assistant.
ICONS = {
    "water_flow": "mdi:water",
    "burner_power": "mdi:fire",
    "fan_speed": "mdi:fan",
    "chamber": "mdi:thermometer-high",
    "temp_box": "mdi:thermometer",
}


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
        "availability_topic": "%s/%s/availability" % (base, DEV),
        "payload_available": "online",
        "payload_not_available": "offline",
        "device": {
            "identifiers": [DEV],
            "manufacturer": ha["device"]["manufacturer"],
            "model": ha["device"]["model"],
            "name": ha["device"]["name"],
        },
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


def c_str(text: str) -> str:
    """Escape for a C++ string literal."""
    return text.replace("\\", "\\\\").replace('"', '\\"')


def discovery_topic(component: str, key: str, doc: dict) -> str:
    return "%s/%s/%s/%s/config" % (doc["home_assistant"]["discovery_prefix"],
                                   component, DEV, key)


def render_payload(cfg: dict) -> str:
    return json.dumps(cfg, separators=(",", ":"), ensure_ascii=False)


def build() -> str:
    doc = json.loads(SPEC.read_text(encoding="utf-8"))
    entities = [e for e in (ha_entity(f, doc) for f in doc["state_fields"]) if e]

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

    for e in entities:
        topic = discovery_topic(e["component"], e["key"], doc)
        payload = render_payload(e["config"])
        out.append('  {"%s", "%s", "%s",\n   "%s"},\n'
                   % (e["component"], c_str(e["key"]), c_str(topic), c_str(payload)))
    out.append("};\nstatic const size_t kEntityCount = %d;\n\n" % len(entities))

    # One fully rendered pair, so the on-board self test can check the substitution without
    # re-implementing JSON.
    sample = entities[0]
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
