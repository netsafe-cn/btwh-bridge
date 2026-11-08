// A0 broadcast parsing.
//
// Same layer rule as frame.h: no Arduino, no NimBLE. Parsing and printing are deliberately
// separate functions - the printer lives in main.cpp - so this can be compared field by field
// against the golden vectors on the target board.
//
// Offsets were calibrated against 148 captured frames on 2026-11-01 and are documented in
// BLE_PROTOCOL.md section 6. They must match python/bosch_api.py::parse_state exactly; where
// the two disagree, the Python side and the vectors win.
#pragma once

// Classic guard as well as #pragma once: arduino-cli compiles a copy of src/ under
// <build>/sketch/, so one header can be reached through two paths. #pragma once keys
// on the file, not the name, and would let both copies through.
#ifndef BTWH_PROTO_A0_H
#define BTWH_PROTO_A0_H

#include <stddef.h>
#include <stdint.h>

namespace proto {

struct A0State {
  uint8_t frame_type;   // data[0]: 0 = Type0, 1 = Type1
  bool type1;

  // Shared offsets, verified on hardware
  float temp_in;        // data[1] + data[2]/100
  float chamber;        // data[3] + data[4]/100
  int   setpoint;       // data[5]
  float temp_box;       // data[6] + data[7]/100
  bool  flame;          // data[10] & 0x10
  int   fault;          // data[11], 0 = none

  // Type-dependent offsets. Type1 is covered by a golden vector; Type0 follows the Python
  // reference implementation and has no vector yet.
  float temp_out;       // Type1: [28]+[29]/100, Type0: [21]+[22]/100
  float water_flow;     // Type1: [21]/10, Type0: [8]/10  (L/min)
  int   burner_power;   // Type1: [13], Type0: [9]  (%)
  int   fan_speed;      // Type1: [15]*100+[16], Type0: [14]  (rpm)
  int   fan_power;      // Type1: [12], Type0: [13]
  int   mode;           // Type1: [32], Type0: [16]
  int   max_setpoint;   // ([33] or [17]) & 0x7F
  bool  bathroom;       // ([33] or [17]) & 0x80

  // Type1 only, still marked as guesses in the protocol notes
  float ambient;        // [16]+[17]/100
  float ambient_set;    // [18]+[19]/100
  float gas_set;        // [22]+[23]/100, unit not confirmed
  int   pressure;       // [27] * 10, unit Pa (unconfirmed)
};

// Out-of-range reads return 0 rather than reading past the buffer, so a truncated frame
// produces zeros instead of garbage. Callers that need "absent" must check the frame length.
A0State parseA0(const uint8_t* d, size_t n);

}  // namespace proto

#endif  // BTWH_PROTO_A0_H
