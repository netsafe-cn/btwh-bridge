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

static const HeaterProfile kProfiles[] = {
  {"therm_4800i", "Therm 4800i", 1, 35, 60, 1, 0x02, 0x55, 0x00},
};
static const size_t kProfileCount = 1;

// The row this firmware image is built for. One image per model for now;
// a runtime probe (A0 frame_type + reported max_setpoint) can select the row
// later without changing this table.
constexpr size_t kDefaultIndex = 0;
constexpr const HeaterProfile& defaultProfile() { return kProfiles[kDefaultIndex]; }

}  // namespace profiles
}  // namespace proto

#endif  // BTWH_PROTO_PROFILES_GEN_H
