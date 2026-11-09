// The bridge's MQTT payload, built from the generated contract.
//
// Layer rule (ADR-008): net/ sits above app/. It takes a plain snapshot of the state and the
// constants from spec/mqtt.json; it never sees a frame byte. It also pulls in no Arduino, no
// NimBLE and no MQTT library, which is what lets the on-board self test check the exact bytes
// that will be published without a network in the room.
#pragma once
// Macro guard as well as #pragma once: arduino-cli compiles a copy of src/ under
// <build>/sketch/, so a header can be reached through two paths.
#ifndef BTWH_NET_STATE_JSON_H
#define BTWH_NET_STATE_JSON_H

#include <stddef.h>
#include <stdint.h>

#include "net/mqtt_contract.h"
#include "net/selftest.h"

namespace net {

// A copy of the fields the bridge publishes. Plain data on purpose: app/ fills it in, this
// module only renders it, so a formatting bug cannot look like a protocol bug.
struct Snapshot {
  float   temp_out;
  float   temp_in;
  float   chamber;
  float   temp_box;
  int     setpoint;
  float   water_flow;
  int     burner_power;
  int     fan_speed;
  bool    flame;
  int     mode;    // raw field value: 0 idle, 1 burning, 2 comfort, anything else unknown
  int     fault;   // 0 -> "none", otherwise two upper-case hex digits
  int64_t ts;      // epoch seconds; 0 means "no clock yet" and the key is left out entirely
};

// Renders the state payload in contract order. Returns the length written, or 0 when the buffer
// is too small - in that case `out` is set to an empty string and nothing partial is published.
size_t buildStateJson(char* out, size_t cap, const Snapshot& s);

// Fixed-point with the rounding rule spec/mqtt.json states: half away from zero. Kept separate
// because it is the one place where C++ and Python can silently disagree (Python's "%.1f" is
// half-to-even), so it gets its own tests.
size_t formatFixed(char* out, size_t cap, float value, uint8_t precision);

const char* modeText(int mode);

// ---------------------------------------------------------------- self test
// Shape comes from net/selftest.h; the log callback replaces Serial so the test can run
// anywhere.
SelfTestResult runSelfTest(LogFn log, void* ctx);

}  // namespace net

#endif  // BTWH_NET_STATE_JSON_H
