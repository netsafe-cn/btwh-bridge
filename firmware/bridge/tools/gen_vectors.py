#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""gen_vectors.py - turn spec/protocol_vectors.json into a C header for the on-target self test

The firmware has no host-side test harness: the toolchain here only produces Xtensa binaries, so
"run the golden vectors" means running them on the board. This script emits the vectors as C
arrays so `selftest` can compare its own output against the same bytes the Python suite checks.

    python firmware/bridge/tools/gen_vectors.py            # write src/proto/vectors_gen.h
    python firmware/bridge/tools/gen_vectors.py --check    # fail if the header is out of date

The generated file is committed, so a build never depends on having run this first. The --check
mode is what keeps it from drifting.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent            # firmware/bridge/tools
BRIDGE = HERE.parent                                      # firmware/bridge
REPO = BRIDGE.parents[1]                                  # repository root
SPEC = REPO / "spec" / "protocol_vectors.json"
OUT = BRIDGE / "src" / "proto" / "vectors_gen.h"

HEADER = """\
// GENERATED FILE - do not edit.
// Source: spec/protocol_vectors.json
// Regenerate: python firmware/bridge/tools/gen_vectors.py
//
// These are the same bytes python/tests/test_contract.py checks, so a mismatch here means the
// C++ codec and the Python codec have drifted apart.
#pragma once

#include <stddef.h>
#include <stdint.h>

namespace proto {
namespace vectors {

struct FrameCase {
  const char* name;
  uint8_t recv;
  uint8_t mid;
  uint8_t cmd;
  const uint8_t* data;
  size_t data_len;
  const uint8_t* frame;
  size_t frame_len;
  const uint8_t* stuffed;
  size_t stuffed_len;
  const uint8_t* chunks;          // every fragment, concatenated
  uint8_t chunk_lens[8];          // per-fragment length, in order
  uint8_t chunk_count;
};

struct A0Case {
  const char* name;
  const uint8_t* payload;
  size_t payload_len;
  uint8_t frame_type;
  float temp_out;
  float temp_in;
  float chamber;
  float temp_box;
  int setpoint;
  float water_flow;
  int burner_power;
  int fan_speed;
  int fan_power;
  int mode;
  int max_setpoint;
  int fault;
  int flame;
  int bathroom;
};

"""


def c_bytes(name: str, data: bytes, per_line: int = 12) -> str:
    if not data:
        return "static const uint8_t %s[1] = {0x00};\n" % name
    rows = []
    for i in range(0, len(data), per_line):
        rows.append("  " + ", ".join("0x%02X" % b for b in data[i:i + per_line]) + ",")
    return "static const uint8_t %s[] = {\n%s\n};\n" % (name, "\n".join(rows))


def parse_frame(case: dict) -> tuple[int, int, int, bytes, bytes, bytes, list[bytes]]:
    """frame_hex is 7B7B | sender recv mid cmd data.. | chk | 7D7D."""
    raw = bytes.fromhex(case["frame_hex"])
    if raw[0] != 0x7B or raw[1] != 0x7B or raw[-2] != 0x7D or raw[-1] != 0x7D:
        raise SystemExit("%s: frame_hex is not delimited" % case["name"])
    recv, mid, cmd = raw[3], raw[4], raw[5]
    data = raw[6:-3]
    stuffed = bytes.fromhex(case["stuffed_hex"])
    chunks = [bytes.fromhex(c) for c in case["chunks_hex"]]
    return recv, mid, cmd, data, raw, stuffed, chunks


def build() -> str:
    doc = json.loads(SPEC.read_text(encoding="utf-8"))
    cases = doc["cases"]

    frames = []
    a0s = []
    body = []

    for case in cases:
        name = case["name"]
        if "payload_hex" in case:
            a0s.append(case)
        elif "frame_hex" in case:
            frames.append(case)
        else:
            raise SystemExit("%s: neither payload_hex nor frame_hex" % name)

    for case in frames:
        recv, mid, cmd, data, frame, stuffed, chunks = parse_frame(case)
        if len(chunks) > 8:
            raise SystemExit("%s: more than 8 fragments, raise chunk_lens[]" % case["name"])
        ident = case["name"]
        body.append(c_bytes(ident + "_data", data))
        body.append(c_bytes(ident + "_frame", frame))
        body.append(c_bytes(ident + "_stuffed", stuffed))
        body.append(c_bytes(ident + "_chunks", b"".join(chunks)))

    for case in a0s:
        body.append(c_bytes(case["name"] + "_payload", bytes.fromhex(case["payload_hex"])))

    out = [HEADER, "\n".join(body), "\n"]

    # ---- FrameCase table
    out.append("static const FrameCase kFrames[] = {\n")
    for case in frames:
        recv, mid, cmd, data, frame, stuffed, chunks = parse_frame(case)
        lens = ", ".join(str(len(c)) for c in chunks)
        out.append(
            '  {"%s", 0x%02X, 0x%02X, 0x%02X, %s_data, %d, %s_frame, %d, %s_stuffed, %d, '
            "%s_chunks, {%s}, %d},\n"
            % (case["name"], recv, mid, cmd, case["name"], len(data),
               case["name"], len(frame), case["name"], len(stuffed),
               case["name"], lens, len(chunks))
        )
    out.append("};\nstatic const size_t kFrameCount = %d;\n\n" % len(frames))

    # ---- A0Case table
    out.append("static const A0Case kA0[] = {\n")
    for case in a0s:
        st = case["state"]
        out.append(
            '  {"%s", %s_payload, %d, %d, %.2ff, %.2ff, %.2ff, %.2ff, %d, %.2ff, %d, %d, %d, '
            "%d, %d, %d, %d, %d},\n"
            % (case["name"], case["name"], len(bytes.fromhex(case["payload_hex"])),
               st["frame_type"], st["temp_out"], st["temp_in"], st["chamber"], st["temp_box"],
               st["setpoint"], st["water_flow"], st["burner_power"], st["fan_speed"],
               st.get("fan_power") or 0, st.get("mode") or 0, st.get("max_setpoint") or 0,
               st["failure"], 1 if st["ionized"] else 0, 1 if st.get("bathroom") else 0)
        )
    out.append("};\nstatic const size_t kA0Count = %d;\n\n" % len(a0s))

    out.append("}  // namespace vectors\n}  // namespace proto\n")
    return "".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--check", action="store_true",
                    help="verify the generated header matches the spec, write nothing")
    a = ap.parse_args()

    text = build()
    if a.check:
        current = OUT.read_text(encoding="utf-8") if OUT.exists() else ""
        if current != text:
            print("%s is out of date; run: python firmware/bridge/tools/gen_vectors.py" % OUT)
            return 1
        print("%s matches spec/protocol_vectors.json" % OUT.name)
        return 0

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(text, encoding="utf-8", newline="\n")
    frames = text.count('{"') - text.count('static const A0Case')
    print("wrote %s (%d bytes)" % (OUT, len(text)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
