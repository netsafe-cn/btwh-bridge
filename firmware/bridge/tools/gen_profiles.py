#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""gen_profiles.py - spec/profiles.json -> src/proto/profiles_gen.h

One row per heater model: setpoint range, write-parameter encoding, identity. The firmware
cannot parse JSON at build time, so - exactly like gen_vectors.py and gen_mqtt.py - the spec
is generated into a header and never re-typed by hand.

Why the header lives in proto/: the write_param bytes are protocol facts (they must rebuild
the golden D3 vector), and proto/ is the one layer both app/ and the proto self test may
include. The net layer deliberately does NOT see this header - it gets kSetpointMin/Max from
gen_mqtt.py instead, so ADR-008's "net must not know frame bytes" survives.

The self test (proto suite) checks, for every profile row:
  * set_min < set_max
  * the write_param bytes rebuild the golden D3 setpoint vector
  * the A0 golden vector's frame_type and max_setpoint match the row

so a profile that drifts from the protocol evidence fails the build on the host and on the
board.

Usage:
    python firmware/bridge/tools/gen_profiles.py            # write the header
    python firmware/bridge/tools/gen_profiles.py --check    # fail if stale (CI)
"""
from __future__ import annotations

import json
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent            # firmware/bridge/tools
BRIDGE = HERE.parent                                      # firmware/bridge
REPO = BRIDGE.parents[1]                                  # repository root
SPEC = REPO / "spec" / "profiles.json"
OUT = BRIDGE / "src" / "proto" / "profiles_gen.h"


def c_str(text: str) -> str:
    """Escape for a C++ string literal."""
    return text.replace("\\", "\\\\").replace('"', '\\"')


def parse_hex(value: str) -> int:
    if not isinstance(value, str) or not value.startswith("0x"):
        raise ValueError("write_param values must be hex strings like \"0x55\": %r" % value)
    return int(value, 16)


def build() -> str:
    doc = json.loads(SPEC.read_text(encoding="utf-8"))
    profiles = doc["profiles"]
    default = doc["default_profile"]

    ids = [p["id"] for p in profiles]
    if len(set(ids)) != len(ids):
        raise ValueError("duplicate profile ids: %s" % ids)
    if default not in ids:
        raise ValueError("default_profile %r is not one of %s" % (default, ids))
    default_index = ids.index(default)

    out = []
    out.append("""\
// GENERATED FILE - do not edit.
// Source: spec/profiles.json
// Regenerate: python firmware/bridge/tools/gen_profiles.py
//
// The model registry: one row per supported heater. Supporting a new model is a row in the
// spec plus its A0 golden vectors - not a firmware edit. The proto self test rebuilds the
// golden D3 vector from every row, so this table cannot drift from the protocol evidence.
#pragma once
// Macro guard as well as #pragma once: arduino-cli compiles a copy of src/ under
// <build>/sketch/, so a header can be reached through two paths.
#ifndef BTWH_PROTO_PROFILES_GEN_H
#define BTWH_PROTO_PROFILES_GEN_H

#include <stddef.h>
#include <stdint.h>

namespace proto {
namespace profiles {

struct HeaterProfile {
  const char* id;          // stable identifier, e.g. "therm_4800i"
  const char* model;       // the name Home Assistant shows
  uint8_t     a0_frame_type;  // A0 payload variant: selects the parser offsets
  int         set_min;     // whole degrees C
  int         set_max;
  int         set_step;
  uint8_t     write_type;  // D3 write body: parameter type, id, index, then the value
  uint8_t     write_id;
  uint8_t     write_index;
};

""")
    out.append("static const HeaterProfile kProfiles[] = {\n")
    for p in profiles:
        sp = p["setpoint"]
        wp = p["write_param"]
        out.append('  {"%s", "%s", %d, %d, %d, %d, 0x%02X, 0x%02X, 0x%02X},\n'
                   % (p["id"], c_str(p["model"]), int(p["a0_frame_type"]),
                      int(sp["min"]), int(sp["max"]), int(sp.get("step", 1)),
                      parse_hex(wp["type"]), parse_hex(wp["id"]), parse_hex(wp["index"])))
    out.append("};\n")
    out.append("static const size_t kProfileCount = %d;\n\n" % len(profiles))
    out.append("// The row this firmware image is built for. One image per model for now;\n"
               "// a runtime probe (A0 frame_type + reported max_setpoint) can select the row\n"
               "// later without changing this table.\n")
    out.append("constexpr size_t kDefaultIndex = %d;\n" % default_index)
    out.append("constexpr const HeaterProfile& defaultProfile() { return kProfiles[kDefaultIndex]; }\n\n")
    out.append("}  // namespace profiles\n}  // namespace proto\n\n"
               "#endif  // BTWH_PROTO_PROFILES_GEN_H\n")
    return "".join(out)


def main() -> int:
    text = build()
    if "--check" in sys.argv:
        current = OUT.read_text(encoding="utf-8") if OUT.exists() else ""
        if current != text:
            print("%s is out of date; run: python firmware/bridge/tools/gen_profiles.py" % OUT)
            return 1
        print("%s matches spec/profiles.json" % OUT.name)
        return 0

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(text, encoding="utf-8", newline="\n")
    print("wrote %s (%d bytes)" % (OUT, len(text)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
