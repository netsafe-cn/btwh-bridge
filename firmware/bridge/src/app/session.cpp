#include "app/session.h"

#include <Arduino.h>
#include <string.h>

#include "ble/link.h"
#include "proto/a0.h"
#include "proto/frame.h"
#include "proto/profiles_gen.h"

namespace app {
namespace {

Config* gCfg = nullptr;

Phase        gPhase      = kWaitCode;
bool         gRunning    = false;
bool         gBondStale  = false;
bool         gRaw        = false;
bool         gEncrypted  = false;
auth::Result gLast       = auth::kOk;

// Written from the NimBLE task (notify callback) and read from loop(); volatile is enough here
// because both are 32-bit aligned ints and only the loop side ever clears them.
volatile int      gReply07 = -1;
volatile int      gReply08 = -1;
volatile uint32_t gA0Count = 0;
// When the last A0 arrived, on the millis() clock. The counter above is per session and
// restarts at 0, so it cannot answer "is this data current?" - a session that reconnects
// would look like it just delivered a frame when it has delivered none. This stamp can.
volatile uint32_t gLastA0Ms = 0;

uint32_t gSessionStart = 0;

proto::A0State gLastState{};
bool          gHaveState = false;

// ---------------------------------------------------------------- retry ladder
// Every number here comes from a measurement, not from taste.
//
//   kRetryAfterDropMs   A link that dropped is usually a heater-side event (the heater hung
//                       up). Reconnecting within seconds runs straight into the refusal window
//                       measured on 2026-11-08, so wait a minute first. The old code used 5 s
//                       and a blocking delay().
//   kRetryAfterFailMs   After a failed authorisation there is nothing to gain from haste: the
//                       heater's window is closed or the code is wrong. Two minutes keeps the
//                       device self-healing without burning attempts.
//   kQuietAfter         Three consecutive failures is no longer a transient; stop knocking.
//   kQuietMs            Fifteen minutes of silence, then the ladder restarts. Measured
//                       2026-11-08: ~4 minutes was enough after a 3-minute quiet period;
//                       2026-11-13: 27 minutes of silence was enough after a hard reset. The
//                       real minimum is unknown, so this errs on the long side - a bridge that
//                       comes back after 15 minutes beats one that needs a human.
constexpr uint32_t kRetryAfterDropMs = 60UL * 1000;
constexpr uint32_t kRetryAfterFailMs = 120UL * 1000;
constexpr int      kQuietAfter       = 3;
constexpr uint32_t kQuietMs          = 15UL * 60 * 1000;

int      gFailures   = 0;      // consecutive failures, reset by a success or a human request
bool     gRetryArmed = false;
uint32_t gRetryAtMs  = 0;

void armRetry(uint32_t delayMs) {
  gRetryArmed = true;
  gRetryAtMs  = millis() + delayMs;
}

void disarmRetry() { gRetryArmed = false; }

void banner(const char* s) { Serial.printf("\n===== %s =====\n", s); }

// ---------------------------------------------------------------- auth transport
// auth/ knows nothing about NimBLE. These three functions are the whole bridge: a send that
// wraps the framed write, a wait that polls the reply slot the notify path fills, and a logger.
bool txSend(void* /*ctx*/, uint8_t /*sub*/, const uint8_t* data, size_t len) {
  return ble::send(proto::kRecvLogin, proto::kCmdLogin, data, len);
}

int txWait(void* /*ctx*/, uint8_t sub, uint32_t timeoutMs) {
  volatile int* slot = (sub == 0x07) ? &gReply07 : &gReply08;
  uint32_t t0 = millis();
  while (millis() - t0 < timeoutMs) {
    if (*slot >= 0) return *slot;
    delay(50);
  }
  return -1;
}

void txLog(void* /*ctx*/, const char* line) { Serial.printf("[AUTH] %s\n", line); }

auth::Transport transport() {
  auth::Transport t;
  t.ctx  = nullptr;
  t.send = txSend;
  t.wait = txWait;
  t.log  = txLog;
  return t;
}

// ---------------------------------------------------------------- A0 printing
// Parsing lives in proto/a0.cpp; this only formats the result, which is what lets the self test
// compare fields without a serial port.
void printA0(const proto::A0State& s) {
  Serial.printf("[A0] T%u out%.1f in%.1f chamber%.1f chassis%.1f set%d "
                "power%d%% flow%.1fL/min fan %drpm flame %s fault %s\n",
                s.frame_type, s.temp_out, s.temp_in, s.chamber, s.temp_box, s.setpoint,
                s.burner_power, s.water_flow, s.fan_speed,
                s.flame ? "yes" : "no",
                s.fault ? String(s.fault, HEX).c_str() : "none");

  if (s.type1) {  // suspected fields, marked so they can be confirmed later
    Serial.printf("     [guess] ambient%.1f ambient_set%.1f gas_set%.1f "
                  "pressure%dPa mode[32]=%02X bath[33]=%02X\n",
                  s.ambient, s.ambient_set, s.gas_set, s.pressure,
                  s.mode, (unsigned)(s.max_setpoint | (s.bathroom ? 0x80 : 0)));
  }
}

// ---------------------------------------------------------------- login
bool doLogin() {
  uint8_t mac[6];
  if (!ble::parseMac(gCfg->login, mac)) {
    Serial.println("[AUTH] bad login MAC; fix it with mac AA:BB:CC:DD:EE:FF");
    gLast = auth::kBadInput;
    return false;
  }
  if (!auth::pinValid(gCfg->pin)) {
    Serial.println("[AUTH] panel code empty or not six digits. Enter one first.");
    gLast = auth::kBadInput;
    return false;
  }

  char macText[18];
  ble::formatMac(mac, macText);
  Serial.printf("[AUTH] submitting MAC = %s (device whitelist)\n", macText);

  const auth::Transport t = transport();
  uint8_t r07 = 0, r08 = 0;

  if (!auth::submitMac(t, mac, &r07)) {
    gLast = ble::isConnected() ? auth::kNoResponse : auth::kLinkLost;
    Serial.println("[AUTH] x A2/07 gave no usable reply (see the attempts above)");
    return false;
  }
  Serial.println("[AUTH] + MAC reply received [4/5]");

  if (r07 == 0x3C || r07 == 0x5A) {
    Serial.printf("[AUTH] x device asks to cool down (%02X), retry in %s\n",
                  r07, r07 == 0x3C ? "30 s" : "90 s");
    gLast = auth::kCooldown;
    return false;
  }

  delay(1000);   // app: sendLoginPass always sleeps 1000 ms first
  Serial.println("[AUTH] submitting the code (six digits, one byte per digit)");
  if (!auth::submitCode(t, gCfg->pin, &r08)) {
    gLast = ble::isConnected() ? auth::kNoResponse : auth::kLinkLost;
    Serial.println("[AUTH] x A2/08 gave no usable reply (see the attempts above)");
    return false;
  }

  gLast = auth::classify(r07, r08);
  if (gLast == auth::kOk) {
    Serial.println("[AUTH] + authorised [5/5], waiting for A0 broadcasts...");
    return true;
  }
  Serial.printf("[AUTH] x authorisation failed: %s (A2/07=%02X A2/08=%02X)\n",
                auth::reasonText(gLast), r07, r08);
  Serial.println("       panel clear pairing -> read a new code -> type it (no reflash)");
  return false;
}

}  // namespace

void begin(Config* cfg) { gCfg = cfg; }

void noteCodeChanged() { gBondStale = true; }

bool start(bool forceRebond) {
  if (!gCfg) return false;
  if (gRunning) {
    Serial.println("[BUSY] session running; type reboot to interrupt");
    return false;
  }
  if (!gCfg->pinValid()) {
    Serial.println("Enter the six-digit panel code first (e.g. 311522) to start connecting.");
    return false;
  }

  gRunning      = true;
  gPhase        = kRunning;
  disarmRetry();          // we are about to try; a queued attempt would be a duplicate
  gSessionStart = millis();
  gReply07      = -1;
  gReply08      = -1;
  gA0Count      = 0;
  // A new session has delivered no frames yet. Keeping the previous session's snapshot here
  // is what let a reconnect republish old heater readings as if they had just been measured.
  gLastA0Ms     = 0;
  gHaveState    = false;
  gEncrypted    = false;
  gLast         = auth::kOk;

  ble::setPasskey(gCfg->pinValue());
  if (forceRebond || gBondStale) {
    Serial.println("[SMP ] clearing the old local bond (preparing to pair again)");
    ble::clearBonds();
    gBondStale = false;
    delay(200);
  }

  banner("starting session");
  Serial.printf("[CFG ] heater=%s  loginMAC=%s  code=%s\n",
                gCfg->heater, gCfg->login, gCfg->pin);

  bool ok = ble::connect(gCfg->heater, 15);
  if (ok) {
    ok = doLogin();
  } else {
    // No link at all. Without this the result line would say "failed (ok)".
    gLast = auth::kLinkLost;
  }

  if (!ok) {
    Serial.printf("\n[result] x failed (%s).\n", auth::reasonText(gLast));
    ble::disconnect();
    gPhase = kFailed;

    // The ladder. A failure is no longer the end of the story: schedule the next attempt,
    // going quiet for a long stretch once it is clearly not a transient.
    //
    // Order matters here and cost a build: computing the delay from the counter AFTER resetting
    // it made the "15 min quiet" branch schedule 2 minutes - the log said one thing and the
    // timer did another, which the board test caught at 22:53 on 2026-11-13.
    gFailures++;
    uint32_t delayMs;
    if (gFailures >= kQuietAfter) {
      Serial.printf("[RETRY] %d failures in a row: going quiet for 15 min, then starting over\n",
                    gFailures);
      delayMs  = kQuietMs;
      gFailures = 0;              // the ladder restarts once the silence is over
    } else {
      Serial.printf("[RETRY] attempt %d failed; next try in 2 min\n", gFailures);
      delayMs = kRetryAfterFailMs;
    }
    armRetry(delayMs);
  } else {
    Serial.println("\n[result] + all three stages passed, listening for A0 (a frame every 4 s)");
    gFailures = 0;
    disarmRetry();
  }
  gRunning = false;
  return ok;
}

void poll() {
  // A scheduled attempt fires whatever the phase: kFailed used to be terminal, and that is
  // exactly the state a power cut leaves behind.
  if (gRetryArmed && (int32_t)(millis() - gRetryAtMs) >= 0) {
    disarmRetry();
    if (!gRunning && gCfg && gCfg->pinValid()) {
      Serial.println("[RETRY] trying again with the stored code");
      start(false);
    }
    return;
  }

  if (gPhase != kRunning) return;

  if (!ble::isConnected()) {
    // Was a blocking delay(5000) + immediate start(): it froze loop() (and with it MQTT and
    // the console) and reconnected inside the heater's refusal window.
    Serial.println("[CONN] link dropped; scheduling a fresh session in 1 min");
    gPhase = kWaitCode;
    ble::disconnect();
    armRetry(kRetryAfterDropMs);
    return;
  }

  // 30 s without A0 means the login did not really take; warn without interrupting
  static uint32_t lastWarn = 0;
  if (gA0Count == 0 && millis() - gSessionStart > 30000 && millis() - lastWarn > 15000) {
    lastWarn = millis();
    Serial.println("[WARN] 30 s connected with no A0: A2/08 likely failed, read a new code");
  }
}

void shutdown() {
  gPhase = kWaitCode;          // stop poll() from reconnecting while we tear down
  disarmRetry();
  if (ble::isConnected()) {
    Serial.println("[CONN] closing the link cleanly before restart");
    ble::disconnect();
    delay(300);                // let LL_TERMINATE_IND reach the heater before the CPU resets
  }
  gRunning = false;
}

Status status() {
  Status s;
  s.phase     = gPhase;
  s.linkUp    = ble::isConnected();
  s.encrypted = gEncrypted;
  s.reply07   = gReply07;
  s.reply08   = gReply08;
  s.a0Count   = gA0Count;
  s.lastA0Ms  = gLastA0Ms;
  s.sessionMs = gSessionStart ? (millis() - gSessionStart) : 0;
  s.raw       = gRaw;
  s.lastResult = gLast;
  s.failures  = gFailures;
  s.retryInMs = gRetryArmed ? (uint32_t)((int32_t)(gRetryAtMs - millis()) > 0
                                         ? (int32_t)(gRetryAtMs - millis()) : 0)
                            : 0;
  return s;
}

void setRawDump(bool on) { gRaw = on; }
bool rawDump() { return gRaw; }

bool lastState(proto::A0State& out) {
  if (!gHaveState) return false;
  out = gLastState;
  return true;
}

const char* configuredHeater() { return gCfg ? gCfg->heater : ""; }

bool setSetpoint(int celsius) {
  if (gPhase != kRunning || !ble::isConnected()) {
    Serial.println("[CMD] setpoint refused: no authorised link");
    return false;
  }
  const proto::profiles::HeaterProfile& p = proto::profiles::defaultProfile();
  if (celsius < p.set_min || celsius > p.set_max) {
    Serial.printf("[CMD] setpoint %d refused: outside %d..%d C\n",
                  celsius, p.set_min, p.set_max);
    return false;
  }

  // The write body comes from the profile row: parameter type, parameter id, index, then the
  // value in whole degrees. Byte-identical to the Python reference (bosch_api.set_temperature)
  // and to the d3_set_temperature_42 golden vector, which the proto self test rebuilds from
  // the very same row.
  const uint8_t data[4] = {p.write_type, p.write_id, p.write_index, (uint8_t)celsius};
  const bool ok = ble::send(proto::kRecvBiz, proto::kCmdWriteParam, data, sizeof(data));
  Serial.printf("[CMD] set %d C -> %s\n", celsius, ok ? "frame sent" : "send FAILED");
  return ok;
}

void onState(const uint8_t* payload, size_t len) {
  gA0Count++;
  gLastA0Ms = millis();
  gLastState = proto::parseA0(payload, len);
  gHaveState = true;
  printA0(gLastState);
  if (gRaw) {
    Serial.print("     raw=");
    for (size_t i = 0; i < len; i++) Serial.printf("%02x ", payload[i]);
    Serial.println();
  }
}

void onLoginReply(uint8_t sub, uint8_t value) {
  if (sub == 0x07) {
    gReply07 = value;
    Serial.printf("[AUTH] + A2/07 reply = %02X  (%s)\n", value, auth::windowText(value));
  } else {
    gReply08 = value;
    Serial.printf("[AUTH] + A2/08 reply = %02X  (%s)\n", value, auth::codeText(value));
  }
}

void onSecurity(bool encrypted, bool /*bonded*/) { gEncrypted = encrypted; }

}  // namespace app
