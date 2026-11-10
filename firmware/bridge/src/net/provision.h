// Provisioning: the SoftAP + captive portal that turns a fresh board into a working bridge.
//
// Design decisions (2026-11-08, agreed with the product owner):
//   * Wi-Fi and heater pairing happen in ONE flow. The form takes all of it at once.
//     Changing only Wi-Fi later: leave the pairing code empty.
//     Changing only the code later: leave the Wi-Fi password empty.
//   * The heater is picked from a scan list (BTWH-* filter), never typed as an address.
//   * The heater holds one BLE connection at a time: installing the bridge means the vendor
//     app cannot be used at the same time. That is a product boundary, not a defect.
//   * BOOT held for 5 s wipes Wi-Fi + pairing and returns here.
//
// Layer rule (ADR-008): this header is the state machine and the storage contract. The HTTP
// server itself lives in provision_server.cpp next to it and may include Arduino; the state
// must not, so the console and later Improv can drive the same machine.
#pragma once
// Macro guard as well as #pragma once: arduino-cli compiles a copy of src/ under
// <build>/sketch/, so a header can be reached through two paths.
#ifndef BTWH_NET_PROVISION_H
#define BTWH_NET_PROVISION_H

#include <stddef.h>
#include <stdint.h>

#include "net/selftest.h"

namespace net {

// Where the board is in its life. Mirrors the `pair_state` attribute in spec/mqtt.json so
// Home Assistant shows the same picture the board has of itself.
enum class ProvState {
  kUnprovisioned,   // no Wi-Fi credentials in NVS: bring the hotspot up at boot
  kProvisioning,    // hotspot is up, the form is being filled or submitted
  kConnecting,      // credentials accepted, trying the radio path: Wi-Fi, then SMP, then A2/07
  kPaired,          // A2/08=01 seen at least once; normal operation
  kNeedsRepair,     // was paired, but the heater stopped authorising (A2/07=00 / A2/08=00);
                    // the user must read a new code - no amount of retrying will fix this
};

const char* provStateText(ProvState s);

// What the form submitted. Plain data so the console (and tests) can construct one by hand.
// Every field is optional at the UI level; the state machine decides what a given combination
// means, per the header comment above.
struct ProvisionRequest {
  char heater[18];        // BLE address from the scan list, e.g. 5c:f8:21:eb:6f:5f
  char ssid[33];          // 32 chars is the 802.11 maximum
  char wifiPass[65];      // 64 chars, WPA2 maximum
  char pairCode[8];       // six digits, or empty = keep the existing pairing
  char broker[129];       // host[:port]; empty = keep the current setting
  char mqttUser[33];      // broker account; empty = keep the current setting
  char mqttPass[65];      // broker password; empty = keep the current setting
};

// Validates a request the way the form will send it. Returns a reason string for the web UI,
// or nullptr when the combination is workable. Checked BEFORE anything is written to NVS or
// sent over the air, because a malformed pairing attempt counts toward the heater's
// three-failure lockout.
const char* validateRequest(const ProvisionRequest& r);

// Self test for the validation above (and provStateText). Runs on the host and on the board,
// like every ADR-008 suite: a rule that gates the pairing lockout must not ship unexecuted.
// Namespaced like the other suites (net::mqtt, net::rules, net::discovery) so the suite
// name in the report is the namespace you called.
namespace prov {
SelfTestResult runSelfTest(LogFn log, void* ctx);
}  // namespace prov

}  // namespace net

#endif  // BTWH_NET_PROVISION_H
