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
  // Frames whose outlet temperature was physically impossible: the 0xFF/0x63 filler seen on
  // some broadcasts decodes to 256.8 C / 99.9 C through `d[i] + d[i+1]/100`. They are dropped
  // rather than published, and counted here so the console shows the guard working instead of
  // silently hiding a sensor that really is failing.
  uint32_t outRejects;
  // millis() of the most recent A0 frame, 0 until one arrives. Reset at every session start,
  // so it answers "is there data, and how old is it" where a0Count only answers "how many
  // frames this session" - the freshness question the MQTT contract's link topic asks.
  uint32_t lastA0Ms;
  uint32_t sessionMs;
  bool     raw;
  auth::Result lastResult;
  // Auto-retry state. `failures` is consecutive; it drives the quiet period. `retryInMs` is
  // how long until the next attempt, 0 when none is scheduled.
  int      failures;
  uint32_t retryInMs;
};

// Outlet temperatures outside this window are not measurements. The setpoint is capped at 60 C
// by the contract and a gas unit overshoots by a few degrees at most, so 85 refuses the 99.9 and
// 256.8 that the 0x63 / 0xFF filler bytes decode to while still admitting a probe that reads
// high. One definition, used by the session guard that drops the reading and by the snapshot
// builder that publishes it - two independent checks on purpose, because a guard that silently
// stops working is invisible from the outside (see the 99.9 C investigation).
constexpr float kOutletMinC = -10.0f;
constexpr float kOutletMaxC =  85.0f;
inline bool outletPlausible(float t) { return t >= kOutletMinC && t <= kOutletMaxC; }

void begin(Config* cfg);

// A new panel code invalidates the old bond: the heater issues a fresh window for it.
void noteCodeChanged();

// Connect, pair, submit the MAC, submit the code. Blocking; 30 s worst case on five timeouts.
// Starts the retry ladder at zero: a human asking for a session should not inherit a quiet
// period that was scheduled for a machine.
bool start(bool forceRebond);

// Retry scheduling and the "30 s with no A0" warning. Call from loop().
//
// A failed attempt is NOT terminal any more. Measured on 2026-11-13: one hard reset left the
// bridge stuck in kFailed for the rest of the evening - no retry, no recovery, MQTT publishing
// `link: offline` until a human typed `go`. At the same time a retry loop is not the answer
// either: right after a link is cut the heater refuses sessions for minutes (2026-11-08), and
// hammering it produces the same refusal again. So the ladder waits, gives up loudly for a
// while, and then starts over - forever, because this device must survive a power cut that
// nobody is watching.
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

// Writes the setpoint over the live link (command D3, body from the model profile).
//
// Returns false when there is no authorised link or the value is outside the profile's
// setpoint range (35..60 C for the Therm 4800i). The reply is not awaited on purpose: the
// next A0 broadcast carries the authoritative setpoint, so the consumer sees what the
// heater accepted rather than what we asked for.
bool setSetpoint(int celsius);

// Hooks installed on the link, called from the NimBLE task.
void onState(const uint8_t* payload, size_t len);
void onLoginReply(uint8_t sub, uint8_t value);
void onSecurity(bool encrypted, bool bonded);

}  // namespace app

#endif  // BTWH_APP_SESSION_H
