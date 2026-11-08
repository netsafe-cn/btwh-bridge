#include "app/session.h"

#include <Arduino.h>
#include <string.h>

#include "ble/link.h"
#include "proto/a0.h"
#include "proto/frame.h"

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

uint32_t gSessionStart = 0;

proto::A0State gLastState{};
bool          gHaveState = false;

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
  gSessionStart = millis();
  gReply07      = -1;
  gReply08      = -1;
  gA0Count      = 0;
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
    Serial.printf("\n[result] x failed (%s). Fix it and type the code again; no reflash needed.\n",
                  auth::reasonText(gLast));
    ble::disconnect();
    gPhase = kFailed;
  } else {
    Serial.println("\n[result] + all three stages passed, listening for A0 (a frame every 4 s)");
  }
  gRunning = false;
  return ok;
}

void poll() {
  if (gPhase != kRunning) return;

  if (!ble::isConnected()) {
    Serial.println("[CONN] link dropped, reconnecting in 5 s with the current code");
    gPhase = kWaitCode;
    ble::disconnect();
    delay(5000);
    start(false);
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
  s.sessionMs = gSessionStart ? (millis() - gSessionStart) : 0;
  s.raw       = gRaw;
  s.lastResult = gLast;
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

void onState(const uint8_t* payload, size_t len) {
  gA0Count++;
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
