/*
 * ============================================================================
 * Bosch heater BLE gateway  v2.1  --  Waveshare ESP32-S3-DEV-KIT-N8R8
 * ----------------------------------------------------------------------------
 * Phase one (feasibility, complete before any Wi-Fi/MQTT work):
 *   1) connect to the heater and finish SMP pairing (the panel code is the BLE pin)
 *   2) A2/07 submits the MAC, A2/08 submits the pairing code, authorisation follows
 *   3) A0 broadcasts arrive and every field parses correctly
 *
 * This file is only the board boundary: setup(), loop() and the serial console. The work lives
 * in the layers below it (ADR-008), each of which compiles on its own terms:
 *   src/proto/  byte layout and parsing, no Arduino, no NimBLE  (self test covers it)
 *   src/ble/    the only place NimBLE appears: connect, subscribe, reassemble, send
 *   src/auth/   A2/07 + A2/08, driven through a send/wait callback so it can be faked
 *   src/app/    session order of operations plus the NVS config
 *
 * Two defects fixed back in v2.0 against v1.0, kept here as history:
 *   1) CRITICAL: v1 used Just Works + NoInputNoOutput and ignored the SMP pin entirely.
 *      Measured: the panel code is both the BLE passkey and the application password,
 *      and the device asks for PROVIDE_PIN while its window is open. v2 answers it.
 *   2) EFFICIENCY: the code is no longer compiled in; it is entered over serial at runtime.
 *      It lives for minutes, and v1 needed a rebuild plus reflash for every code change.
 *      v2 flashes once and then accepts any number of codes and retries.
 *
 * Hardware: Waveshare ESP32-S3-DEV-KIT-N8R8 (8 MB flash / 8 MB PSRAM)
 * Logs: COM3 (CH343 -> UART0) at 115200. Flashing also uses COM3.
 * Depends on: NimBLE-Arduino 1.4.3 (pinned; do not move to 2.x)
 * Protocol reference: docs/BLE_PROTOCOL.md / python/bosch_api.py (byte-identical)
 * ============================================================================
 */

#include <Arduino.h>
#include <ctype.h>
#include <string.h>

#include "app/config.h"
#include "app/session.h"
#include "auth/login.h"
#include "ble/link.h"
#include "proto/a0.h"
#include "proto/frame.h"
#include "proto/selftest.h"

namespace pc = proto;

static app::Config gCfg;
static String      gSerialLine;

static void banner(const char* s) { Serial.printf("\n===== %s =====\n", s); }

// ---------------------------------------------------------------- frame dispatch
// ble/ hands over complete frames; routing them is all this does. Anything that needs a heater
// to interpret belongs in app/, and anything that needs bytes belongs in proto/.
static void onFrame(void* /*ctx*/, const uint8_t* f, size_t n) {
  if (n < 9) return;
  if (!proto::checksumOk(f, n)) {
    Serial.printf("[RX] dropping frame chk=%02X expect=%02X len=%d\n",
                  f[n - 3], proto::checksum(f, n), (int)n);
    return;
  }
  uint8_t mid = f[4], cmd = f[5];
  const uint8_t* p = f + 6;
  size_t plen = n - 9;

  if (cmd == pc::kCmdState) {
    app::onState(p, plen);
  } else if (cmd == pc::kCmdLogin && plen >= 2 && (p[0] == 0x07 || p[0] == 0x08)) {
    app::onLoginReply(p[0], p[1]);
  } else {
    Serial.printf("[RX ] cmd=%02X mid=%d len=%d : ", cmd, mid, (int)plen);
    for (size_t i = 0; i < plen; i++) Serial.printf("%02x ", p[i]);
    Serial.println();
  }
}

static void onSecurity(void* /*ctx*/, bool encrypted, bool bonded) {
  app::onSecurity(encrypted, bonded);
}

// ---------------------------------------------------------------- commands
// Where proto::runSelfTest sends its lines. Keeping the printer outside proto/ is what lets the
// same test run on a host later without dragging in Arduino.
static void serialLine(void* /*ctx*/, const char* line) { Serial.println(line); }

static void doSelfTest() {
  proto::SelfTestResult r = proto::runSelfTest(serialLine, nullptr);
  if (r.failed == 0) {
    Serial.printf("[TEST] proto self test passed: %d checks\n", r.total);
  } else {
    Serial.printf("[TEST] proto self test FAILED: %d of %d checks\n", r.failed, r.total);
  }
}

static void doScan() {
  if (ble::isConnected()) {
    Serial.println("[SCAN] already connected; disconnecting first");
    ble::disconnect();
    delay(300);
  }
  banner("BLE scan, 5 s");
  ble::ScanEntry entries[16];
  int n = ble::scan(entries, (int)(sizeof(entries) / sizeof(entries[0])), 5);
  for (int i = 0; i < n; i++) {
    Serial.printf("  %-20s RSSI=%4d  %s\n", entries[i].addr, entries[i].rssi,
                  entries[i].name[0] ? entries[i].name : "(unnamed)");
  }
  banner("scan finished");
}

static void setPin(const String& code) {
  bool changed = (code != gCfg.pin);
  strncpy(gCfg.pin, code.c_str(), sizeof(gCfg.pin) - 1);
  gCfg.pin[sizeof(gCfg.pin) - 1] = 0;
  gCfg.save();
  ble::setPasskey(gCfg.pinValue());
  if (changed) app::noteCodeChanged();   // a new code means a new window; the old bond is void
  Serial.printf("[CFG ] panel code = %s (both the BLE PIN and the app password)%s\n",
                gCfg.pin, changed ? " [changed -> bond cleared on next connect]" : "");
  app::start(false);
}

static void printHelp() {
  banner("serial commands (type and press enter; no reflash needed)");
  Serial.println("  311522            six-digit panel code -> connect, pair, log in");
  Serial.println("  pin 311522        same as above");
  Serial.println("  go                reconnect with the current code (no new pairing)");
  Serial.println("  pair              clear the local bond and force a new SMP pairing");
  Serial.println("  scan              scan for 5 s and list nearby BLE devices");
  Serial.println("  mac AA:BB:...     set the login MAC (the host on the whitelist)");
  Serial.println("  addr AA:BB:...    set the heater BLE address");
  Serial.println("  status            print configuration and runtime state");
  Serial.println("  selftest          run the golden protocol vectors on the board");
  Serial.println("  raw on / raw off  show / hide raw A0 bytes");
  Serial.println("  reboot            reboot");
  Serial.println("  help              this help");
}

static const char* phaseText(app::Phase p) {
  switch (p) {
    case app::kWaitCode: return "waiting for code";
    case app::kRunning:  return "connected, listening";
    case app::kFailed:   return "last attempt failed";
  }
  return "unknown";
}

static void printStatus() {
  const app::Status s = app::status();
  banner("status");
  Serial.printf("  heater     : %s\n", gCfg.heater);
  Serial.printf("  login MAC  : %s\n", gCfg.login);
  Serial.printf("  panel code : %s (PIN=%06u)\n", gCfg.pin, (unsigned)gCfg.pinValue());
  Serial.printf("  phase      : %s\n", phaseText(s.phase));
  Serial.printf("  link       : %s / encrypted=%s\n",
                s.linkUp ? "up" : "down", s.encrypted ? "yes" : "no");
  Serial.printf("  A2/07=%d  A2/08=%d  A0 frames=%u  raw=%s\n",
                s.reply07, s.reply08, (unsigned)s.a0Count, s.raw ? "on" : "off");
  Serial.printf("  last       : %s\n", auth::reasonText(s.lastResult));
}

static void handleCommand(String line) {
  line.trim();
  if (!line.length()) return;
  String low = line;
  low.toLowerCase();

  // Six digits on their own mean the panel code
  if (line.length() == 6) {
    bool digits = true;
    for (int i = 0; i < 6; i++) if (!isdigit((unsigned char)line[i])) digits = false;
    if (digits) { setPin(line); return; }
  }
  if (low.startsWith("pin "))   { setPin(line.substring(4)); return; }
  if (low == "help" || low == "?")  { printHelp(); return; }
  if (low == "status")              { printStatus(); return; }
  if (low == "selftest")            { doSelfTest(); return; }
  if (low == "scan")                { doScan(); return; }
  if (low == "go")                  { app::start(false); return; }
  if (low == "pair")                { app::start(true); return; }
  if (low == "reboot")              { Serial.println("rebooting..."); delay(200); ESP.restart(); }
  if (low == "raw on")              { app::setRawDump(true);  Serial.println("[CFG] raw=on");  return; }
  if (low == "raw off")             { app::setRawDump(false); Serial.println("[CFG] raw=off"); return; }
  if (low.startsWith("mac ")) {
    uint8_t m[6];
    char text[18];
    if (!ble::parseMac(line.substring(4).c_str(), m)) {
      Serial.println("bad MAC format");
      return;
    }
    ble::formatMac(m, text);
    strncpy(gCfg.login, text, sizeof(gCfg.login) - 1);
    gCfg.login[sizeof(gCfg.login) - 1] = 0;
    gCfg.save();
    Serial.printf("[CFG] login MAC = %s\n", gCfg.login);
    return;
  }
  if (low.startsWith("addr ")) {
    String v = line.substring(5);
    v.trim();
    v.toLowerCase();
    strncpy(gCfg.heater, v.c_str(), sizeof(gCfg.heater) - 1);
    gCfg.heater[sizeof(gCfg.heater) - 1] = 0;
    gCfg.save();
    Serial.printf("[CFG] heater address = %s\n", gCfg.heater);
    return;
  }
  Serial.printf("[?] unknown command: %s (type help for the list)\n", line.c_str());
}

static void pollSerial() {
  while (Serial.available()) {
    char c = (char)Serial.read();
    if (c == '\n' || c == '\r') {
      if (gSerialLine.length()) {
        String line = gSerialLine;
        gSerialLine = "";
        handleCommand(line);
      }
    } else if (gSerialLine.length() < 120) {
      gSerialLine += c;
    }
  }
}

// ---------------------------------------------------------------- Arduino
void setup() {
  Serial.begin(115200);
  delay(1200);
  banner("Bosch heater ESP32 gateway v2.1 - Waveshare ESP32-S3-DEV-KIT-N8R8");

  gCfg.load();

  ble::begin("BoschGW");
  ble::setPasskey(gCfg.pinValue());
  ble::Hooks hooks;
  hooks.onFrame    = onFrame;
  hooks.onSecurity = onSecurity;
  hooks.ctx        = nullptr;
  ble::setHooks(hooks);
  app::begin(&gCfg);

  Serial.printf("[BOOT] Chip=%s  Flash=%uMB  PSRAM=%u bytes\n",
                ESP.getChipModel(), ESP.getFlashChipSize() / (1024 * 1024), ESP.getPsramSize());
  Serial.printf("[BOOT] heater=%s  loginMAC=%s  stored code=%s\n",
                gCfg.heater, gCfg.login, gCfg.pin[0] ? gCfg.pin : "(none)");

  banner("ready");
  Serial.println("Order of operations:");
  Serial.println("  1) close the Python console / bridge on the PC (they fight for the device)");
  Serial.println("  2) heater panel -> clear pairing -> read the new six-digit code");
  Serial.println("  3) type that code here and press enter (seconds, no reflash)");
  Serial.println("  4) watch [SMP]/[AUTH]/[A0]; on failure read a new code and retry");
  Serial.println();
  printHelp();

  // Power-cycle recovery: reconnect once when NVS holds a six-digit code.
  // Basis (measured 2026-11-07): a code that paired successfully is kept by the device
  // and still works after the window closes (A2/07=01 -> A2/08=01). On failure, fall back.
  if (gCfg.pinValid()) {
    Serial.printf("\n[BOOT] stored code %s, reconnecting automatically (type a new one if it fails)\n",
                  gCfg.pin);
    app::start(false);
    Serial.println("[BOOT] if that fails, type a new code");
  } else {
    Serial.println("\nwaiting for input...");
  }
}

void loop() {
  pollSerial();
  app::poll();
  delay(10);
}
