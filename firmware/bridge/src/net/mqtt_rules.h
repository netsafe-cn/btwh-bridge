// Inbound MQTT rules: the parsing that guards the bridge's command surface.
//
// Layer rule (ADR-008): like mqtt_wire, this file compiles with nothing but the C standard
// library. It is the seam between the socket code in mqtt_client.cpp and the contract: every
// byte a consumer sends at this bridge lands in one of these functions before it can affect
// hardware. That is why they live here rather than inline in the client - the exact parsers
// a real Home Assistant (or Node-RED, or mosquitto_pub) exercises are the ones the host self
// test can pin without a broker in the room.
//
// Scope, stated so nobody adds a feature by accident:
//   broker address   "host" or "host:port" -> host + port, defaults 1883
//   setpoint text    "42", "42.0", "42\n" -> whole degrees within the profile's range
//   command verb     refresh / reboot / unpair, by substring
//   live-only rule   a retained command delivery is never executed
//   elapsed time     unsigned milliseconds arithmetic, argument order included
#pragma once
// Macro guard as well as #pragma once: arduino-cli compiles a copy of src/ under
// <build>/sketch/, so a header can be reached through two paths.
#ifndef BTWH_NET_MQTT_RULES_H
#define BTWH_NET_MQTT_RULES_H

#include <stddef.h>
#include <stdint.h>

#include "net/selftest.h"

namespace net {
namespace rules {

// ---------------------------------------------------------------- broker address

// "host" or "host:port". Empty, malformed, zero or oversized port, or a host that does not
// fit hostCap returns false and leaves the outputs untouched.
//
// Moved here from mqtt_client.cpp so the whole grammar is host-testable: the console, the
// portal and boot all feed this one parser, and a divergence between them is a config bug.
bool parseBroker(const char* text, char* host, size_t hostCap, uint16_t* port);

// ---------------------------------------------------------------- setpoint text

enum class Setpoint : uint8_t {
  kOk = 0,          // *outCelsius holds the value
  kNotANumber,      // "abc", "", "42x" - not parseable as a number at all
  kNotWhole,        // "41.5" - fractional degrees are a range problem, not a rounding one
  kOutOfRange,      // whole, but outside the profile's setpoint range
};

// Parses what a consumer published to set/temperature. Trailing whitespace is trimmed (some
// clients append a newline); leading is not, because no client does that and "  42" hiding in
// the payload is worth seeing in the log. Range bounds come from the generated contract
// header - the same profile row the app layer validates against, so the two checks cannot
// disagree. Out-of-range is reported, never clamped: a consumer that sends 70 asked for
// something the heater cannot do, and silently turning it into 60 hides the bug.
Setpoint parseSetpoint(const char* text, int* outCelsius);

// ---------------------------------------------------------------- command verb

// The three verbs the cmd topic accepts, matched by substring (case-sensitive; the contract
// documents them lower case). Returns nullptr for anything else. Kept as a parser rather than
// a table: the words are also what gOnCommand hands to the app layer, and one spelling
// authority beats two.
const char* commandVerb(const char* text);

// Commands are live-only. A retained "reboot" would replay on every reconnect and reboot the
// board forever; the retained flag on a delivery says exactly that happened. set/temperature
// is deliberately exempt in the client - replaying the last setpoint is idempotent - so this
// rule is checked at the cmd topic only. Centralised here so the safety property has a name
// a test can refer to.
bool commandDeliveryAllowed(bool retained);

// ---------------------------------------------------------------- elapsed time

// Unsigned elapsed-time arithmetic, argument order included. `newer` is the later sample.
// Returns the elapsed milliseconds; when the arguments arrive reversed (older > newer) the
// subtraction wraps to ~4e9 - deliberately huge, so "elapsed > limit" correctly reads as
// impossible rather than as zero.
//
// This pins the class of the 2.5.1 bug: the MQTT watchdog compared `now - gLastRx` with `now`
// sampled before the socket drain that stamped gLastRx, the subtraction wrapped, and a healthy
// link died 0.1 s after CONNACK. The arithmetic itself is correct; the order of the samples
// was not. Callers must sample their clock AFTER the receive path that updates the timestamp.
uint32_t elapsedMs(uint32_t newer, uint32_t older);

// ---------------------------------------------------------------- self test

SelfTestResult runSelfTest(LogFn log, void* ctx);

}  // namespace rules
}  // namespace net

#endif  // BTWH_NET_MQTT_RULES_H
