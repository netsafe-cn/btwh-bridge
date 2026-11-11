/*
 * ============================================================================
 * Bosch heater BLE gateway  v2.2  --  Waveshare ESP32-S3-DEV-KIT-N8R8
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
#include "net/portal.h"
#include "net/discovery.h"
#include "net/mqtt_client.h"
#include "net/mqtt_rules.h"
#include "net/mqtt_wire.h"
#include "net/station.h"
#include "net/state_json.h"
#include "proto/a0.h"
#include "proto/frame.h"
#include "proto/selftest.h"

namespace pc = proto;

static app::Config gCfg;
static String      gSerialLine;

// Last snapshot rendered for MQTT, kept so a heartbeat can be published between A0 frames
// without rebuilding it from a stale struct.
static net::Snapshot gSnap;
static bool          gHaveSnap = false;
static uint32_t      gLastA0   = 0;

static void banner(const char* s) { Serial.printf("\n===== %s =====\n", s); }

// ---------------------------------------------------------------- provisioning
// What the portal calls once the form is validated and stored. Runs after the AP is down:
// join the network the form named, then, when a code was given, run the pairing attempt.
static void startMqtt();   // defined below; the portal path can change the broker

static const char* onProvisioned(void* /*ctx*/, const net::ProvisionRequest& r) {
  strncpy(gCfg.heater, r.heater, sizeof(gCfg.heater) - 1);
  gCfg.heater[sizeof(gCfg.heater) - 1] = 0;
  if (r.pairCode[0]) {
    strncpy(gCfg.pin, r.pairCode, sizeof(gCfg.pin) - 1);
    gCfg.pin[sizeof(gCfg.pin) - 1] = 0;
  }
  strncpy(gCfg.ssid, r.ssid, sizeof(gCfg.ssid) - 1);
  gCfg.ssid[sizeof(gCfg.ssid) - 1] = 0;
  if (r.wifiPass[0]) {
    strncpy(gCfg.wifiPass, r.wifiPass, sizeof(gCfg.wifiPass) - 1);
    gCfg.wifiPass[sizeof(gCfg.wifiPass) - 1] = 0;
  }
  // Same "empty keeps the stored value" rule as the Wi-Fi password above, for the same
  // reason: re-pairing or moving networks must not silently erase the broker settings.
  if (r.broker[0]) {
    strncpy(gCfg.broker, r.broker, sizeof(gCfg.broker) - 1);
    gCfg.broker[sizeof(gCfg.broker) - 1] = 0;
  }
  if (r.mqttUser[0]) {
    strncpy(gCfg.mqttUser, r.mqttUser, sizeof(gCfg.mqttUser) - 1);
    gCfg.mqttUser[sizeof(gCfg.mqttUser) - 1] = 0;
  }
  if (r.mqttPass[0]) {
    strncpy(gCfg.mqttPass, r.mqttPass, sizeof(gCfg.mqttPass) - 1);
    gCfg.mqttPass[sizeof(gCfg.mqttPass) - 1] = 0;
  }
  gCfg.save();
  gCfg.saveNetwork();

  bool wifiOk = net::stationConnect(gCfg.ssid, gCfg.wifiPass);
  if (!wifiOk) return "saved, but the Wi-Fi would not join - reopen the portal and retype it";

  // The portal is the one place where the broker can change without a reboot, and the
  // client owns its socket: a fresh begin is how the new address takes effect at all.
  startMqtt();

  if (!gCfg.pinValid()) return "saved (no code given; Wi-Fi only)";
  return app::start(false) ? "paired" : "pairing failed - see the serial log";
}

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

static void reportTest(const char* layer, int total, int failed) {
  if (failed == 0) {
    Serial.printf("[TEST] %s self test passed: %d checks\n", layer, total);
  } else {
    Serial.printf("[TEST] %s self test FAILED: %d of %d checks\n", layer, failed, total);
  }
}

static void doSelfTest() {
  // Each layer keeps its own suite: proto owns the wire format, net owns the published bytes,
  // net::mqtt owns the broker's packet format, net::rules owns the inbound parsing (setpoint
  // text, command verbs, broker addresses, OTA URLs), net::prov owns the provisioning
  // validation, and discovery owns the entity table. None of them can check another's job.
  proto::SelfTestResult     p = proto::runSelfTest(serialLine, nullptr);
  net::SelfTestResult       n = net::runSelfTest(serialLine, nullptr);
  net::SelfTestResult       w = net::mqtt::runSelfTest(serialLine, nullptr);
  net::SelfTestResult       r = net::rules::runSelfTest(serialLine, nullptr);
  net::SelfTestResult       v = net::prov::runSelfTest(serialLine, nullptr);
  net::SelfTestResult       d = net::discovery::runSelfTest(serialLine, nullptr);
  reportTest("proto", p.total, p.failed);
  reportTest("net", n.total, n.failed);
  reportTest("mqtt", w.total, w.failed);
  reportTest("rules", r.total, r.failed);
  reportTest("provision", v.total, v.failed);
  reportTest("discovery", d.total, d.failed);
}

// Copies the last parsed broadcast into the shape net/ publishes. The only mapping between the
// protocol struct and the MQTT snapshot lives here, in the board boundary, so net/ never has to
// include proto/ and app/ never has to know a topic.
static bool makeSnapshot(net::Snapshot& s) {
  proto::A0State st;
  if (!app::lastState(st)) return false;

  s.temp_out     = st.temp_out;
  s.temp_in      = st.temp_in;
  s.chamber      = st.chamber;
  s.temp_box     = st.temp_box;
  s.setpoint     = st.setpoint;
  s.water_flow   = st.water_flow;
  s.burner_power = st.burner_power;
  s.fan_speed    = st.fan_speed;
  s.flame        = st.flame;
  s.mode         = st.mode;
  s.fault        = st.fault;
  s.ts           = net::nowEpoch();   // 0 = clock not synced; the key is then left out
  return true;
}

// Prints exactly what goes out on the state topic. This is how net/ is checked without a
// broker: the same code path MQTT uses, one `mqtt` command away.
static void printMqttPayload() {
  net::Snapshot s;
  if (!makeSnapshot(s)) {
    Serial.println("[MQTT] no state yet: waiting for an A0 frame");
    return;
  }
  char payload[384];
  if (net::buildStateJson(payload, sizeof(payload), s) == 0) {
    Serial.println("[MQTT] payload does not fit the buffer");
    return;
  }
  Serial.printf("[MQTT] %s\n", payload);
}

// ---------------------------------------------------------------- MQTT hooks
// net/ reports what a consumer asked for; deciding what that means for the heater is app/'s job
// and lives here at the boundary, exactly like the serial console.
static void onMqttSetpoint(void* /*ctx*/, int celsius) {
  if (!app::setSetpoint(celsius)) Serial.println("[MQTT] setpoint not sent");
}

static void onMqttCommand(void* /*ctx*/, const char* verb) {
  if (strcmp(verb, "refresh") == 0) return;          // mqtt_client already queued a publish

  if (strcmp(verb, "reboot") == 0) {
    Serial.println("[MQTT] reboot requested by a consumer");
    app::shutdown();                                 // clean LL disconnect, see session.h
    delay(200);
    ESP.restart();
  }

  if (strcmp(verb, "unpair") == 0) {
    Serial.println("[MQTT] unpair requested: clearing the local bond and the stored code");
    ble::clearBonds();
    gCfg.pin[0] = 0;
    gCfg.save();
    Serial.println("[MQTT] done - clear pairing on the panel before pairing again");
  }
}

// Attributes metadata is sampled live, not frozen at start: the pairing state moves while the
// bridge runs (paired -> reconnecting -> paired), and so does the BLE RSSI. Called every few
// seconds from loop(); a remote user troubleshooting reads these through the MQTT diagnostics.
static void refreshMqttInfo() {
  net::MqttInfo info;
  info.heater   = gCfg.heater;
  info.hardware = "esp32s3";
  const app::Status st = app::status();
  info.pairState = st.phase == app::kRunning ? "paired"
                 : st.phase == app::kFailed  ? "reconnecting"
                                             : "unpaired";
  info.rssi = ble::rssi();
  net::mqttSetInfo(info);
}

// Starts or restarts the client from the configured broker string. Called at boot and whenever
// `broker` changes the address; a refresh is enough because the client owns its socket.
static void startMqtt() {
  char deviceId[20];
  net::mqtt::deviceIdFromAddress(gCfg.heater, deviceId, sizeof(deviceId));

  refreshMqttInfo();
  net::mqttSetHooks(onMqttSetpoint, onMqttCommand, nullptr);

  net::mqttBegin(gCfg.broker, deviceId);
  // Credentials come after begin: begin() applies the address, this applies the account, and
  // together they are the whole connection contract from the config. Called on every restart
  // (broker command, portal save, boot) so NVS is the single source of truth.
  net::mqttSetCredentials(gCfg.mqttUser, gCfg.mqttPass);
  if (!gCfg.broker[0]) {
    Serial.println("[MQTT] no broker configured; telemetry stays on the serial port");
    return;
  }
  Serial.printf("[MQTT] starting as %s (%s)\n", deviceId,
                gCfg.mqttUser[0] ? gCfg.mqttUser : "anonymous");
}

static void doScan() {  if (ble::isConnected()) {
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
  Serial.println("  mqtt              print the payload this state would publish");
  Serial.println("  broker HOST[:P]   set the MQTT broker ( - clears it ); restarts the client");
  Serial.println("  mqttuser U PASS   set the broker account ( - clears ); restarts the client");
  Serial.println("  ota               print how to update firmware over MQTT");
  Serial.println("  portal            start the setup portal (LAN page if already online, else AP)");
  Serial.println("  wifi SSID PASS    join a network and save it (open network: wifi SSID -)");
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
  Serial.printf("  wifi       : %s  ssid=%s\n",
                net::stationUp() ? "up" : "down", gCfg.ssid[0] ? gCfg.ssid : "(none)");
  Serial.printf("  clock      : %s\n",
                net::nowEpoch() ? "synced" : "not synced (ts omitted)");
  Serial.printf("  broker     : %s  user=%s\n",
                gCfg.broker[0] ? gCfg.broker : "(none)",
                gCfg.mqttUser[0] ? gCfg.mqttUser : "(anonymous)");
  char mqtt[120];
  net::mqttStatusText(mqtt, sizeof(mqtt));
  Serial.printf("  mqtt       : %s\n", mqtt);
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
  if (low == "mqtt")                { printMqttPayload(); return; }
  if (low.startsWith("broker")) {
    String arg = line.substring(6);
    arg.trim();
    if (arg.length() == 0) {
      char line2[140];
      net::mqttStatusText(line2, sizeof(line2));
      Serial.printf("[MQTT] broker=%s  status=%s\n",
                    gCfg.broker[0] ? gCfg.broker : "(none)", line2);
      Serial.println("[MQTT] usage: broker <host[:port]>   ( - clears )");
      return;
    }
    if (arg == "-") arg = "";
    strncpy(gCfg.broker, arg.c_str(), sizeof(gCfg.broker) - 1);
    gCfg.broker[sizeof(gCfg.broker) - 1] = 0;
    gCfg.saveNetwork();
    Serial.printf("[CFG] broker = %s\n", gCfg.broker[0] ? gCfg.broker : "(none)");
    startMqtt();
    return;
  }
  if (low.startsWith("mqttuser")) {
    // `mqttuser <name> <password>`; the split is on the first space so a password may contain
    // spaces (MQTT allows them; `low` only detects the command, the case comes from `line`).
    String arg = line.substring(8);
    arg.trim();
    if (arg.length() == 0) {
      Serial.printf("[CFG] mqtt user=%s  password=%s\n",
                    gCfg.mqttUser[0] ? gCfg.mqttUser : "(anonymous)",
                    gCfg.mqttPass[0] ? "(set)" : "(none)");
      Serial.println("[CFG] usage: mqttuser <name> <password>   ( - clears both )");
      return;
    }
    if (arg == "-") {
      gCfg.mqttUser[0] = 0;
      gCfg.mqttPass[0] = 0;
    } else {
      int sp = arg.indexOf(' ');
      String userArg = sp < 0 ? arg : arg.substring(0, sp);
      String passArg = sp < 0 ? "" : arg.substring(sp + 1);
      userArg.trim();
      passArg.trim();
      strncpy(gCfg.mqttUser, userArg.c_str(), sizeof(gCfg.mqttUser) - 1);
      gCfg.mqttUser[sizeof(gCfg.mqttUser) - 1] = 0;
      strncpy(gCfg.mqttPass, passArg.c_str(), sizeof(gCfg.mqttPass) - 1);
      gCfg.mqttPass[sizeof(gCfg.mqttPass) - 1] = 0;
    }
    gCfg.saveNetwork();
    Serial.printf("[CFG] mqtt user=%s  password=%s\n",
                  gCfg.mqttUser[0] ? gCfg.mqttUser : "(anonymous)",
                  gCfg.mqttPass[0] ? "(set)" : "(none)");
    startMqtt();
    return;
  }
  if (low == "portal") {
    if (net::portalActive()) { Serial.println("[PORTAL] already running"); return; }
    net::startPortal(onProvisioned, nullptr);
    return;
  }
  if (low == "ota") {
    // The serial console can read and reboot, but the flasher deliberately listens to one
    // surface only: the ota MQTT topic. A second entry point would be a second parser to
    // keep honest, so the console points at the real path instead of duplicating it.
    Serial.println("[OTA] publish the image URL to btwh/<device>/ota (live, not retained):");
    Serial.println("      mosquitto_pub -h <broker> -t btwh/<device>/ota -m http://<host>[:port]/firmware.bin");
    return;
  }
  if (low.startsWith("wifi")) {
    // `wifi <ssid> <password>` writes NVS and joins immediately. The SSID keeps its case and
    // may contain spaces (the split is on the last space), so `low` is used only to detect the
    // command - everything after it comes from the original line.
    String arg = line.substring(4);
    arg.trim();
    if (arg.length() == 0) {
      char ip[20];
      net::stationIp(ip, sizeof(ip));
      Serial.printf("[WIFI] ssid=%s  state=%s  ip=%s\n",
                    gCfg.ssid[0] ? gCfg.ssid : "(none)",
                    net::stationUp() ? "connected" : "not connected", ip);
      Serial.printf("[WIFI] clock=%s\n", net::nowEpoch() ? "synced" : "not synced");
      Serial.println("[WIFI] usage: wifi <ssid> <password>");
      return;
    }
    int sp = arg.lastIndexOf(' ');
    if (sp < 1) {
      Serial.println("[WIFI] usage: wifi <ssid> <password>   (open network: wifi <ssid> -)");
      return;
    }
    String ssidArg = arg.substring(0, sp); ssidArg.trim();
    String passArg = arg.substring(sp + 1); passArg.trim();
    if (passArg == "-") passArg = "";        // explicit "no password" for an open network

    strncpy(gCfg.ssid, ssidArg.c_str(), sizeof(gCfg.ssid) - 1);
    gCfg.ssid[sizeof(gCfg.ssid) - 1] = 0;
    strncpy(gCfg.wifiPass, passArg.c_str(), sizeof(gCfg.wifiPass) - 1);
    gCfg.wifiPass[sizeof(gCfg.wifiPass) - 1] = 0;
    gCfg.saveNetwork();

    bool ok = net::stationConnect(gCfg.ssid, gCfg.wifiPass);
    if (ok) {
      char ip[20];
      net::stationIp(ip, sizeof(ip));
      Serial.printf("[WIFI] + ip=%s (time will follow via SNTP)\n", ip);
    } else {
      Serial.println("[WIFI] x not joined; check the name and the 2.4 GHz band");
    }
    return;
  }
  if (low == "scan")                { doScan(); return; }
  if (low == "go")                  { app::start(false); return; }
  if (low == "pair")                { app::start(true); return; }
  if (low == "reboot") {
    Serial.println("rebooting...");
    app::shutdown();      // clean LL disconnect; a hard reset leaves the heater refusing us
    delay(200);
    ESP.restart();
  }
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
  banner("Bosch heater ESP32 gateway - Waveshare ESP32-S3-DEV-KIT-N8R8");
  // The version comes from the one constant MQTT publishes, not from a second hand-typed
  // string: the banner and the attributes payload cannot disagree if they share a source.
  Serial.printf("[BOOT] firmware %s\n", net::kFirmwareVersion);

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
  } else if (net::portalNeeded(gCfg.ssid)) {
    // Fresh board (or a wipe): no Wi-Fi and no code. Offer the portal instead of asking for
    // a serial console nobody has. The console path stays for development.
    Serial.println("\n[BOOT] no Wi-Fi credentials: starting the setup portal");
    Serial.println("       connect to the BTWH-Bridge-XXXX network and follow the page");
    net::startPortal(onProvisioned, nullptr);
  } else {
    Serial.println("\nwaiting for input...");
  }

  // Network comes last: the heater link is what this device exists for, and the Wi-Fi join
  // blocks for a few seconds. A power cut must not need a human to bring Wi-Fi back either,
  // so the stored credentials are used without asking.
  if (gCfg.ssid[0] && !net::stationUp()) {
    Serial.printf("[BOOT] stored Wi-Fi %s, joining\n", gCfg.ssid);
    net::stationConnect(gCfg.ssid, gCfg.wifiPass);
  }

  // MQTT comes after Wi-Fi, for the same reason: the client needs an IP to do anything at all,
  // and starting it earlier only produces a failed attempt on the boot log.
  startMqtt();
}

void loop() {
  pollSerial();
  net::portalPoll();
  app::poll();

  // Wi-Fi rejoin. stationConnect runs once at boot, and the AP is not guaranteed to be
  // first: a router recovers from a power cut slower than this board, and a laptop hotspot
  // starts whenever its owner gets around to it. On the bench (2026-11-11) a board that
  // booted before the AP stayed offline until a manual reboot. Arduino's auto-reconnect
  // covers an AP that disappears and returns while we are associated; this retry covers
  // the never-associated-at-boot case. 30 s cadence, one 15 s join budget each: slow
  // enough not to fight the scan-and-join cycle, fast enough to come back inside a minute
  // of the AP appearing.
  static uint32_t lastWifiRetryMs = 0;
  if (gCfg.ssid[0] && !net::stationUp() && !net::portalActive()
      && millis() - lastWifiRetryMs > 30000) {
    lastWifiRetryMs = millis();
    Serial.println("[WIFI] rejoining (AP may have come up after boot)");
    net::stationConnect(gCfg.ssid, gCfg.wifiPass);
  }

  // Diagnostics metadata, resampled every 5 s: pairing state and RSSI move while the bridge
  // runs, and the MQTT attributes heartbeat (60 s) republishes whatever this sampled last.
  static uint32_t lastInfoMs = 0;
  if (millis() - lastInfoMs > 5000) {
    lastInfoMs = millis();
    refreshMqttInfo();
  }

  // One A0 frame per publish, plus a 60 s heartbeat inside the client. Publishing every frame
  // keeps `ts` fresh, which is how a consumer notices a bridge that is up but no longer hearing
  // the heater; the alternative (diffing fields) would need a second copy of the snapshot.
  const uint32_t frames  = app::status().a0Count;
  const bool     newFrame = frames != gLastA0;
  if (newFrame) {
    gLastA0   = frames;
    gHaveSnap = makeSnapshot(gSnap);
  }
  net::mqttPoll(gHaveSnap ? &gSnap : nullptr, newFrame && gHaveSnap);

  delay(10);
}
