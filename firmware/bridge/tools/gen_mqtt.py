#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""gen_mqtt.py - spec/mqtt.json -> src/net/mqtt_contract.h

The MQTT contract already exists as a JSON file and is checked against the Python API by
test_mqtt_contract.py. The firmware needs the same information, but it cannot parse JSON at
build time, so it is generated - exactly like spec/protocol_vectors.json -> vectors_gen.h.

What the header carries:

  * topic and discovery constants (never hand-copied into firmware)
  * the field table: key, type, precision, unit, device_class, state_class, label
  * one golden snapshot and the exact JSON string the bridge must publish for it

The golden string is the whole point. The on-board self test builds the same snapshot and
compares bytes, so the C++ formatter and the contract cannot drift apart - and neither can the
Python side, because this script renders with the rule written into the contract.

Number formatting is fixed here, once:

    rounding      half away from zero
    decimals      `precision` from the field, always fixed-point (42.3, not 42.30 or 42.299999)
    separators    ':' and ',' with no spaces, same as json.dumps(separators=(',', ':'))

Half away from zero is chosen because it is the one rule both languages implement without
surprises: Python's "%.1f" is half-to-even, so 42.25 renders as 42.2 there and 42.3 here. The
contract states the rule; both implementations follow it.

Usage:
    python firmware/bridge/tools/gen_mqtt.py            # write the header
    python firmware/bridge/tools/gen_mqtt.py --check    # fail if it is stale (CI)
"""
from __future__ import annotations

import json
import math
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent            # firmware/bridge/tools
BRIDGE = HERE.parent                                      # firmware/bridge
REPO = BRIDGE.parents[1]                                  # repository root
SPEC = REPO / "spec" / "mqtt.json"
OUT = BRIDGE / "src" / "net" / "mqtt_contract.h"

# The sample the self test compares against. Deliberately ordinary values: a heater at 47 %
# with water flowing, which is the state a user is most likely to be looking at.
SAMPLE = {
    "temp_out": 42.3,
    "temp_in": 15.0,
    "chamber": 42.3,
    "temp_box": 24.0,
    "setpoint": 42,
    "water_flow": 11.5,
    "burner_power": 47,
    "fan_speed": 1612,
    "flame": True,
    "mode": 1,                 # 1 = burning, the raw field value
    "fault": 0,                # 0 -> "none"
    "ts": 1762584000,
}

MODE_TEXT = {0: "idle", 1: "burning", 2: "comfort"}      # anything else -> "unknown"


def c_str(text: str) -> str:
    """Escape for a C++ string literal."""
    return text.replace("\\", "\\\\").replace('"', '\\"')


def fmt_fixed(value: float, precision: int) -> str:
    """Fixed-point with half-away-from-zero, byte-identical to net::formatFixed."""
    if precision <= 0:
        return str(int(round_half_away(value)))
    scale = 10 ** precision
    scaled = round_half_away(value * scale)
    sign = "-" if scaled < 0 else ""
    digits = str(abs(int(scaled))).rjust(precision + 1, "0")
    return "%s%s.%s" % (sign, digits[:-precision], digits[-precision:])


def round_half_away(x: float) -> int:
    return int(math.floor(abs(x) + 0.5)) * (-1 if x < 0 else 1)


def render_state(fields: list[dict], sample: dict) -> str:
    """The exact payload, built in contract order."""
    parts = []
    for f in fields:
        key = f["key"]
        kind = f["type"]
        value = sample[key]
        if kind == "float":
            text = fmt_fixed(value, int(f.get("precision") or 1))
        elif kind == "int":
            text = str(int(value))
        elif kind == "bool":
            text = "true" if value else "false"
        elif kind == "enum":
            text = '"%s"' % MODE_TEXT.get(int(value), "unknown")
        else:                                   # string
            text = '"none"' if value in (0, None) else '"%02X"' % int(value)
        parts.append('"%s":%s' % (key, text))
    return "{" + ",".join(parts) + "}"


def build() -> str:
    doc = json.loads(SPEC.read_text(encoding="utf-8"))
    fields = doc["state_fields"]
    ha = doc["home_assistant"]

    out = []
    out.append("""\
// GENERATED FILE - do not edit.
// Source: spec/mqtt.json
// Regenerate: python firmware/bridge/tools/gen_mqtt.py
//
// The bridge's MQTT surface, generated from the contract so it cannot be re-typed by hand.
// Layer rule (ADR-008): net/ may use these constants; it must never see a frame byte, and
// proto/ must never see these.
#pragma once
// Macro guard as well as #pragma once: arduino-cli compiles a copy of src/ under
// <build>/sketch/, so a header can be reached through two paths.
#ifndef BTWH_NET_MQTT_CONTRACT_H
#define BTWH_NET_MQTT_CONTRACT_H

#include <stddef.h>
#include <stdint.h>

namespace net {
namespace contract {

constexpr const char* kBaseTopic        = "%s";
constexpr const char* kDiscoveryPrefix  = "%s";
constexpr const char* kDeviceModel      = "%s";
constexpr const char* kDeviceManufacturer = "%s";
constexpr const char* kDeviceName       = "%s";
constexpr uint8_t     kQos              = %d;

// One row per published field, in the order the payload must use.
struct FieldDesc {
  const char* key;
  const char* type;          // "float" | "int" | "bool" | "enum" | "string"
  uint8_t     precision;     // decimals for floats, 0 otherwise
  const char* unit;          // "" when the field has none
  const char* device_class;  // "" when unknown to Home Assistant
  const char* state_class;   // "" when not a measurement
  const char* label;
};
""" % (doc["base_topic"], ha["discovery_prefix"], ha["device"]["model"],
       ha["device"]["manufacturer"], ha["device"]["name"], doc["qos"]))

    out.append("static const FieldDesc kFields[] = {\n")
    for f in fields:
        out.append('  {"%s", "%s", %d, "%s", "%s", "%s", "%s"},\n'
                   % (f["key"], f["type"], int(f.get("precision") or 0),
                      c_str(f.get("unit") or ""), f.get("device_class") or "",
                      f.get("state_class") or "", c_str(f.get("label") or "")))
    out.append("};\nstatic const size_t kFieldCount = %d;\n\n" % len(fields))

    out.append("""\
// The self test renders this snapshot and compares against kGoldenState below.
struct GoldenSample {
  float    temp_out;
  float    temp_in;
  float    chamber;
  float    temp_box;
  int      setpoint;
  float    water_flow;
  int      burner_power;
  int      fan_speed;
  bool     flame;
  int      mode;      // 0 idle, 1 burning, 2 comfort, anything else -> "unknown"
  int      fault;     // 0 -> "none", otherwise two upper-case hex digits
  int64_t  ts;        // epoch seconds; 0 means "not known yet", and the key is then omitted
};

""")
    out.append("constexpr GoldenSample kGolden = {%s, %s, %s, %s, %d, %s, %d, %d, %s, %d, %d, %dLL};\n"
               % (fmt_fixed(SAMPLE["temp_out"], 1), fmt_fixed(SAMPLE["temp_in"], 1),
                  fmt_fixed(SAMPLE["chamber"], 1), fmt_fixed(SAMPLE["temp_box"], 1),
                  SAMPLE["setpoint"], fmt_fixed(SAMPLE["water_flow"], 1),
                  SAMPLE["burner_power"], SAMPLE["fan_speed"],
                  "true" if SAMPLE["flame"] else "false",
                  SAMPLE["mode"], SAMPLE["fault"], SAMPLE["ts"]))
    # The payload as a C++ literal: the JSON quotes need escaping, the bytes must not change.
    golden = render_state(fields, SAMPLE).replace('"', '\\"')
    out.append('\nconstexpr const char* kGoldenState =\n    "%s";\n\n' % golden)
    out.append("}  // namespace contract\n}  // namespace net\n\n"
               "#endif  // BTWH_NET_MQTT_CONTRACT_H\n")
    return "".join(out)


def main() -> int:
    text = build()
    if "--check" in sys.argv:
        current = OUT.read_text(encoding="utf-8") if OUT.exists() else ""
        if current != text:
            print("%s is out of date; run: python firmware/bridge/tools/gen_mqtt.py" % OUT)
            return 1
        print("%s matches spec/mqtt.json" % OUT.name)
        return 0

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(text, encoding="utf-8", newline="\n")
    print("wrote %s (%d bytes)" % (OUT, len(text)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
