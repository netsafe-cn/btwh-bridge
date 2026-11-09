// Session orchestration: bring the link up, authorise, then keep it alive.
//
// Layer rule (ADR-008): app/ sits on top of ble/, auth/ and proto/. It owns the order of
// operations and the state a future MQTT layer will publish; it does not touch frame bytes.
#pragma once

// Classic guard as well as #pragma once: arduino-cli compiles a copy of src/ under
// <build>/sketch/, so one header can be reached through two paths. #pragma once keys
// on the file, not the name, and would let both copies through.
#ifndef BTWH_APP_SESSION_H
#define BTWH_APP_SESSION_H

#include <stddef.h>
#include <stdint.h>

#include "app/config.h"
#include "auth/login.h"
#include "proto/a0.h"

namespace app {

enum Phase {
  kWaitCode = 0,   // nothing running, waiting for input
  kRunning,        // link up and authorised
  kFailed,         // the last attempt failed; the reason is in lastResult()
};

struct Status {
  Phase    phase;
  bool     linkUp;
  bool     encrypted;
  int      reply07;
  int      reply08;
  uint32_t a0Count;
  uint32_t sessionMs;
  bool     raw;
  auth::Result lastResult;
};

void begin(Config* cfg);

// A new panel code invalidates the old bond: the heater issues a fresh window for it.
void noteCodeChanged();

// Connect, pair, submit the MAC, submit the code. Blocking; 30 s worst case on five timeouts.
bool start(bool forceRebond);

// Link-drop recovery and the "30 s with no A0" warning. Call from loop().
void poll();

// Orderly teardown before the CPU restarts. ESP.restart() drops the BLE link at the radio
// level; the heater then keeps a half-open session and refuses the next connection for minutes
// (measured twice on 2026-11-08: `reboot` under a live link cost ~15 minutes of no service).
// Calling this first sends a proper LL disconnect instead.
void shutdown();

Status status();

void setRawDump(bool on);
bool rawDump();

// The last parsed broadcast, for anything above app/ that wants to render it (the `mqtt`
// console command does exactly that). False until the first frame arrives. Deliberately
// returns the protocol structure, not an MQTT snapshot: app/ must not depend on net/.
bool lastState(proto::A0State& out);

// The heater address in use. The provisioning page needs it when a link is already up and a
// scan cannot run - net/ may read app state, never the other way round.
const char* configuredHeater();

// Writes the setpoint over the live link (command D3, parameters 02/55).
//
// Returns false when there is no authorised link or the value is outside 35..60 C. The reply
// is not awaited on purpose: the next A0 broadcast carries the authoritative setpoint, so the
// consumer sees what the heater accepted rather than what we asked for.
bool setSetpoint(int celsius);

// Hooks installed on the link, called from the NimBLE task.
void onState(const uint8_t* payload, size_t len);
void onLoginReply(uint8_t sub, uint8_t value);
void onSecurity(bool encrypted, bool bonded);

}  // namespace app

#endif  // BTWH_APP_SESSION_H
