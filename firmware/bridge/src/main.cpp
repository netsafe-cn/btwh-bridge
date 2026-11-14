/*
 * ============================================================================
 * Bosch heater BLE gateway  v2.8  --  Waveshare ESP32-S3-DEV-KIT-N8R8
 * (published version string: net/mqtt_client.h kFirmwareVersion)
 * ----------------------------------------------------------------------------
 * Phase one (feasibility, complete before any Wi-Fi/MQTT work):
 *   1) connect to the heater and finish SMP pairing (the panel code is the BLE pin)
 *   2) A2/07 submits the MAC, A2/08 submits the pairing code, authorisation follows
 *   3) A0 broadcasts arrive and every field parses correctly
 * Since then (phase two): Wi-Fi + portal, MQTT + HA discovery (22 entities),
 * broker credentials, and OTA reflashing over this file's own MQTT link.
 * Latest: the setup page comes back by itself when the stored Wi-Fi stops working (ADR-018).
 *
 * This file is only the board boundary: setup(), loop() and the serial console. The work lives
 * in the layers below it (ADR-008), each of which compiles on its own terms:
 *   src/proto/  byte layout and parsing, no Arduino, no NimBLE  (self test covers it)
 *   src/ble/    the only place NimBLE appears: connect, subscribe, reassemble, send
 *   src/auth/   A2/07 + A2/08, driven through a send/wait callback so it can be faked
 *   src/app/    session order of operations plus the NVS config
 *   src/net/   Wi-Fi, portal, MQTT, discovery, OTA - the network edge
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
#include "net/discover.h"
#include "net/portal.h"
#include "net/discovery.h"
#include "net/mqtt_client.h"
#include "net/mqtt_rules.h"
#include "net/mqtt_wire.h"
#include "net/recover.h"
#include "net/station.h"
#include "net/state_json.h"
#include "net/wifistat.h"
#include "proto/a0.h"
#include "proto/frame.h"
#include "proto/selftest.h"

namespace pc = proto;

static app::Config gCfg;
static String      gSerialLine;

// Last snapshot rendered for MQTT, kept so a heartbeat can be published between A0 frames
// without rebuilding it from a stale struct. It is only ever published while the frame behind
// it is fresh: see kLinkStaleMs.
static net::Snapshot gSnap;
static bool          gHaveSnap     = false;
static uint32_t      gLastFrameMs  = 0;

// How long a reading stays current. A0 arrives every ~4 s, so 90 s is 22 missed frames - long
// enough to ride out a BLE reconnect (5 s wait plus a 15 s connect budget) and short enough
// that nobody reads a morning temperature as an evening one. spec/mqtt.json states the same
// number as link.stale_after_s; if one moves, the other moves with it.
constexpr uint32_t kLinkStaleMs = 90000;

// ---------------------------------------------------------------- Wi-Fi recovery clocks
// The four timestamps net/recover.h reads. All millis(); none of them is a duration, and none
// is interpreted here - the policy owns the arithmetic, including what a wrap means.
static uint32_t gOfflineSinceMs = 0;   // last tick the station was up, boot, or the last close
static uint32_t gLastAttemptMs  = 0;   // last stationConnect() attempt, from any path
static uint32_t gLastClientMs   = 0;   // last tick somebody was associated with the setup AP

static void banner(const char* s) { Serial.printf("\n===== %s =====\n", s); }

// ---------------------------------------------------------------- provisioning
// What the portal calls once the form is validated and stored. Runs after the AP is down:
// join the network the form named, then, when a code was given, run the pairing attempt.
static void startMqtt();   // defined below; the portal path can change the broker

static const char* onProvisioned(void* /*ctx*/, const net::ProvisionRequest& r) {
  // Same rule as the console `addr`: pick a different heater and the device id moves with it, so
  // whatever the old id left retained has to go. The portal is the path a buyer actually uses,
  // and it has the same "not connected yet" shape - the client restarts a few lines down.
  char oldId[20];
  char newId[20];
  net::mqtt::deviceIdFromAddress(gCfg.heater, oldId, sizeof(oldId));
  net::mqtt::deviceIdFromAddress(r.heater, newId, sizeof(newId));

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
  // The credentials are new and an attempt was just made: reset both clocks so the fallback
  // measures this failure and not the outage the form was opened for.
  gLastAttemptMs  = millis();
  gOfflineSinceMs = gLastAttemptMs;
  if (!wifiOk) return "saved, but the Wi-Fi would not join - reopen the portal and retype it";

  // The portal is the one place where the broker can change without a reboot, and the
  // client owns its socket: a fresh begin is how the new address takes effect at all.
  if (strcmp(oldId, newId) != 0) net::mqttForgetDevice(oldId);
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
  // validation, net::discovery owns the entity table, net::recover owns the Wi-Fi fallback
  // policy, and net::wifistat owns the wording for the driver's join codes. None of them can
  // check another's job.
  proto::SelfTestResult     p = proto::runSelfTest(serialLine, nullptr);
  net::SelfTestResult       n = net::runSelfTest(serialLine, nullptr);
  net::SelfTestResult       w = net::mqtt::runSelfTest(serialLine, nullptr);
  net::SelfTestResult       r = net::rules::runSelfTest(serialLine, nullptr);
  net::SelfTestResult       v = net::prov::runSelfTest(serialLine, nullptr);
  net::SelfTestResult       d = net::discovery::runSelfTest(serialLine, nullptr);
  net::SelfTestResult       c = net::recover::runSelfTest(serialLine, nullptr);
  net::SelfTestResult       f = net::wifistat::runSelfTest(serialLine, nullptr);
  reportTest("proto", p.total, p.failed);
  reportTest("net", n.total, n.failed);
  reportTest("mqtt", w.total, w.failed);
  reportTest("rules", r.total, r.failed);
  reportTest("provision", v.total, v.failed);
  reportTest("discovery", d.total, d.failed);
  reportTest("recover", c.total, c.failed);
  reportTest("wifistat", f.total, f.failed);
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
// An address found over mDNS, in effect only while the broker field is empty. Deliberately NOT
// written to NVS: it describes the network as it is today, and storing it would rebuild exactly
// the staleness this replaces (measured 2026-11-14: the same Home Assistant was carried to
// another building, got a new subnet, and every stored address went dead at once).
static char     gAutoBroker[24] = {0};
static uint16_t gAutoPort       = 1883;

// Last discovery attempt, so loop() can keep asking without hammering mDNS.
static uint32_t gLastDiscoverMs = 0;

// Resolves the broker once and caches it. Blocking for a second or two, so it runs at boot, from
// the console command, and from loop() only on the slow retry below.
static bool runDiscovery(const char* why) {
  gLastDiscoverMs = millis();
  char host[24];
  char how[72];
  uint16_t port = 1883;
  if (!net::discoverBroker(host, sizeof(host), &port, how, sizeof(how))) {
    Serial.printf("[MDNS] x %s: %s\n", why, how);
    return false;
  }
  strncpy(gAutoBroker, host, sizeof(gAutoBroker) - 1);
  gAutoBroker[sizeof(gAutoBroker) - 1] = 0;
  gAutoPort = port;
  Serial.printf("[MDNS] + %s -> %s:%u (%s)\n", why, gAutoBroker, (unsigned)gAutoPort, how);
  return true;
}

static void startMqtt() {
  char deviceId[20];
  net::mqtt::deviceIdFromAddress(gCfg.heater, deviceId, sizeof(deviceId));

  // What the user typed wins. An empty field means "find it yourself", and the discovered
  // answer is what the client is given - in the host:port form the parser already accepts.
  char target[sizeof(gCfg.broker) + 8];
  if (gCfg.broker[0]) {
    snprintf(target, sizeof(target), "%s", gCfg.broker);
  } else if (gAutoBroker[0]) {
    snprintf(target, sizeof(target), "%s:%u", gAutoBroker, (unsigned)gAutoPort);
  } else {
    target[0] = 0;
  }

  refreshMqttInfo();
  net::mqttSetHooks(onMqttSetpoint, onMqttCommand, nullptr);

  net::mqttBegin(target, deviceId);
  // Credentials come after begin: begin() applies the address, this applies the account, and
  // together they are the whole connection contract from the config. Called on every restart
  // (broker command, portal save, boot) so NVS is the single source of truth.
  net::mqttSetCredentials(gCfg.mqttUser, gCfg.mqttPass);
  if (!target[0]) {
    Serial.println("[MQTT] no broker configured and none discovered; telemetry stays on the serial");
    Serial.println("       (type `discover`, or `broker <host[:port]>` to set one by hand)");
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
  Serial.println("  discover          look for the broker over mDNS (broker field empty = auto)");
  Serial.println("  mqttuser U PASS   set the broker account ( - clears ); restarts the client");
  Serial.println("  ota               print how to update firmware over MQTT");
  Serial.println("  portal            start the setup portal by hand (LAN page if online, else AP)");
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
  if (s.lastA0Ms) {
    Serial.printf("  data age   : %lus (link goes stale at %lus)\n",
                  (unsigned long)((millis() - s.lastA0Ms) / 1000),
                  (unsigned long)(kLinkStaleMs / 1000));
  } else {
    Serial.println("  data age   : no A0 this session (link stays offline)");
  }
  Serial.printf("  last       : %s\n", auth::reasonText(s.lastResult));
  // The ladder is invisible otherwise, and "why is it not retrying" is the question that cost
  // an evening on 2026-11-13.
  if (s.retryInMs) {
    Serial.printf("  retry      : in %lus (consecutive failures: %d)\n",
                  (unsigned long)(s.retryInMs / 1000), s.failures);
  } else {
    Serial.printf("  retry      : none scheduled (consecutive failures: %d)\n", s.failures);
  }
  Serial.printf("  wifi       : %s  ssid=%s\n",
                net::stationUp() ? "up" : "down", gCfg.ssid[0] ? gCfg.ssid : "(none)");
  {
    // Two lines instead of one: "down" alone does not distinguish "the network is not there"
    // from "it is there and refuses us", which is the whole difference between a router problem
    // and a password problem (2026-11-12).
    char diag[128];
    net::stationDiag(diag, sizeof(diag));
    Serial.printf("  wifi link  : %s\n", diag);
    const char* failure = net::stationLastFailure();
    if (failure[0]) Serial.printf("  wifi last  : %s\n", failure);
  }
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
    // A typed address replaces a discovered one. Clearing the field does not silently fall back
    // to the discovered address either: "-" means "no broker", and `discover` is how you ask for
    // one, so the two commands stay predictable.
    gAutoBroker[0] = 0;
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
  if (low == "discover") {
    if (!net::stationUp()) { Serial.println("[MDNS] the station is down; join Wi-Fi first"); return; }
    if (runDiscovery("console")) startMqtt();
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
      if (net::stationUp()) {
        char diag[128];
        net::stationDiag(diag, sizeof(diag));
        Serial.printf("[WIFI] %s\n", diag);
      } else if (net::stationLastFailure()[0]) {
        // The stored attempt is the interesting one when the board is down: it names the
        // network and the driver's reason, and it is the line a bug report needs.
        Serial.printf("[WIFI] last attempt: %s\n", net::stationLastFailure());
      }
      if (net::portalActive()) {
        Serial.printf("[WIFI] portal=%s  clients=%d  fallback=%s\n",
                      net::portalAutomatic() ? "automatic" : "manual",
                      net::portalClients(),
                      net::portalAutomatic() ? "closes itself when idle" : "stays until stopped");
      } else if (!net::stationUp() && gCfg.ssid[0]) {
        Serial.printf("[WIFI] fallback: no IP for %lu s, setup page at %lu s\n",
                      (unsigned long)((millis() - gOfflineSinceMs) / 1000),
                      (unsigned long)(net::recover::kDefaultPolicy.fallbackAfterMs / 1000));
      }
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
    // Same rule as a /save that fails: the credentials are new, so the fallback window starts
    // over. Otherwise the AP would pop up seconds later and the log would claim an outage the
    // user already tried to fix. The attempt clock moves too, or the policy sees a stale one
    // and spends another 15 s join budget on the network that just refused.
    gLastAttemptMs  = millis();
    gOfflineSinceMs = gLastAttemptMs;
    if (ok) {
      // The join itself has already printed ip/gw/rssi; this line is about what was persisted.
      Serial.printf("[WIFI] saved to NVS: ssid=%s\n", gCfg.ssid);
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

    // The device id is derived from this address, so every MQTT topic moves with it. Capture
    // both ids around the write: the documents under the old one have to be deleted, or the
    // consumer keeps a second device that never updates again (measured 2026-11-13 - a bogus
    // aa:bb:cc:dd:ee:ff left 22 entities standing beside the real heater's).
    char oldId[20];
    char newId[20];
    net::mqtt::deviceIdFromAddress(gCfg.heater, oldId, sizeof(oldId));
    net::mqtt::deviceIdFromAddress(v.c_str(), newId, sizeof(newId));

    strncpy(gCfg.heater, v.c_str(), sizeof(gCfg.heater) - 1);
    gCfg.heater[sizeof(gCfg.heater) - 1] = 0;
    gCfg.save();
    Serial.printf("[CFG] heater address = %s\n", gCfg.heater);

    if (strcmp(oldId, newId) != 0) {
      // Forget first, then begin: mqttForgetDevice() writes on the live socket, and begin()
      // replaces the device id and drops that socket. Returns false when offline, where there
      // is nothing of ours retained to delete.
      net::mqttForgetDevice(oldId);
      startMqtt();
      Serial.printf("[CFG] mqtt device id %s -> %s\n", oldId, newId);
    }
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
    // Counted as an attempt: without this the policy sees "no attempt in 30 s" on its first
    // tick and immediately spends another 15 s join budget on a network that just refused.
    gLastAttemptMs  = millis();
    gOfflineSinceMs = gLastAttemptMs;
  }

  // No broker in NVS: look for one instead of leaving the bridge dumb. One or two seconds at
  // boot, and the cost of not doing it is a device that publishes nowhere until someone types an
  // address into a form.
  if (!gCfg.broker[0] && net::stationUp()) runDiscovery("no broker configured");

  // MQTT comes after Wi-Fi, for the same reason: the client needs an IP to do anything at all,
  // and starting it earlier only produces a failed attempt on the boot log.
  startMqtt();
}

void loop() {
  pollSerial();
  net::portalPoll();
  app::poll();

  // Wi-Fi recovery (ADR-018): retry the stored network, and when it stops working altogether,
  // offer the setup page again. The policy and every boundary in it live in net/recover.h; this
  // block supplies the clocks and executes the verdict.
  //
  // History worth keeping: stationConnect runs once at boot, and the AP is not guaranteed to be
  // first - a router recovers from a power cut slower than this board, and a laptop hotspot
  // starts whenever its owner gets around to it. On the bench (2026-11-11) a board that booted
  // before the AP stayed offline until a manual reboot. Arduino's auto-reconnect covers an AP
  // that disappears and returns while we are associated; this covers the
  // never-associated-at-boot case, and the case where the network is gone for good.
  {
    const uint32_t nowMs     = millis();
    const bool     stationUp = net::stationUp();
    const bool     portalUp  = net::portalActive();
    const bool     client    = net::portalClients() > 0;

    // Clock maintenance first, so the sample below is the state of the world rather than the
    // state before this tick's events. Two transitions matter:
    //   station up   both clocks ride along, so the first outage is measured from the drop and
    //                a healthy board never looks overdue for an attempt.
    //   portal gone  /save closed it. The credentials that just arrived get a fresh fallback
    //                window; without this the outage that preceded them counts against them and
    //                the AP reopens on the same tick.
    static bool portalWasUp = false;
    if (portalWasUp && !portalUp && !stationUp) gOfflineSinceMs = nowMs;
    portalWasUp = portalUp;
    if (stationUp) { gOfflineSinceMs = nowMs; gLastAttemptMs = nowMs; }
    if (client)    { gLastClientMs = nowMs; }

    net::recover::Sample s;
    s.hasCredentials = gCfg.ssid[0] != 0;
    s.stationUp      = stationUp;
    s.portalActive   = portalUp;
    s.portalAuto     = net::portalAutomatic();
    s.clientPresent  = client;
    s.nowMs          = nowMs;
    s.offlineSinceMs = gOfflineSinceMs;
    s.lastAttemptMs  = gLastAttemptMs;
    s.lastClientMs   = gLastClientMs;

    switch (net::recover::decide(s, net::recover::kDefaultPolicy)) {
      case net::recover::Action::Retry:
        gLastAttemptMs = nowMs;
        Serial.println("[WIFI] rejoining (the stored network may have come back)");
        net::stationConnect(gCfg.ssid, gCfg.wifiPass);
        break;

      case net::recover::Action::OpenPortal:
        gLastClientMs = nowMs;   // idle counts from the open, not from the boot that preceded it
        Serial.printf("[WIFI] no IP for %lu s with stored credentials: offering the setup page\n",
                      (unsigned long)(net::recover::kDefaultPolicy.fallbackAfterMs / 1000));
        net::startPortal(onProvisioned, nullptr, true);
        break;

      case net::recover::Action::ClosePortal:
        Serial.printf("[PORTAL] nobody used the setup page for %lu minutes: closing it, resuming "
                      "the Wi-Fi retries\n",
                      (unsigned long)(net::recover::kDefaultPolicy.idleCloseAfterMs / 60000));
        net::stopPortal();
        gOfflineSinceMs = nowMs;   // a fresh window, or the fallback is still satisfied and reopens
        break;

      case net::recover::Action::None:
        break;
    }
  }

  // "Wi-Fi is up and the broker is not answering" is a different fault from "no Wi-Fi", and a
  // bare "retry in 30s" log cannot tell them apart. The Windows mobile hotspot does exactly this
  // after a few hours: the AP keeps handing out leases while ICS stops forwarding, so the board
  // holds an address on a network with nothing behind it. One minute of grace covers a normal
  // connect, then it says so with the addresses that separate the two cases.
  {
    static bool     stuckSinceValid = false;
    static uint32_t stuckSinceMs    = 0;
    static uint32_t lastNoteMs      = 0;
    const bool stuck = net::stationUp() && !net::mqttConnected();
    if (!stuck) {
      stuckSinceValid = false;
      lastNoteMs      = 0;
    } else {
      if (!stuckSinceValid) {
        stuckSinceValid = true;
        stuckSinceMs    = millis();
      }
      const bool heldLongEnough = (millis() - stuckSinceMs) > 60u * 1000u;
      const bool noteDue        = lastNoteMs == 0 || (millis() - lastNoteMs) > 5u * 60u * 1000u;
      if (heldLongEnough && noteDue) {
        char diag[128];
        net::stationDiag(diag, sizeof(diag));
        Serial.printf("[NET] the Wi-Fi link is up but the broker does not answer: %s\n", diag);
        Serial.println("[NET]   that is the network behind the AP (gateway, NAT or firewall), "
                       "not the join - check from a laptop on the same AP before touching the "
                       "bridge");
        lastNoteMs = millis();
      }
    }
  }

  // Diagnostics metadata, resampled every 5 s: pairing state and RSSI move while the bridge
  // runs, and the MQTT attributes heartbeat (60 s) republishes whatever this sampled last.
  static uint32_t lastInfoMs = 0;
  if (millis() - lastInfoMs > 5000) {
    lastInfoMs = millis();
    refreshMqttInfo();
  }

  // Publish once per A0 frame, plus the client's own 60 s heartbeat. Two decisions are made
  // here and nowhere else: what counts as a new frame, and when the data stops being current.
  //
  // The frame counter cannot answer the first: app::start() resets it to 0 at every reconnect,
  // so the first loop of a new session read "0 != 40" and rebuilt a snapshot - with a brand-new
  // `ts` - out of the previous session's last A0. The parser's timestamp is monotonic within a
  // boot, so it is the one that can honestly say a frame arrived.
  const app::Status st = app::status();
  const bool newFrame = st.lastA0Ms != 0 && st.lastA0Ms != gLastFrameMs;
  if (newFrame) {
    gLastFrameMs = st.lastA0Ms;
    gHaveSnap    = makeSnapshot(gSnap);
  }

  // Freshness, not liveness. The will and the connect already tell the broker this process is
  // up, and that is exactly what it kept saying while the bridge replayed a two-day-old
  // snapshot: on 2026-11-12 Home Assistant showed a 42.3 degree outlet and a burning flame
  // from 11-10, with rssi 0 and pair_state "reconnecting" beside it. The link topic answers the
  // question availability never could, and dropping the cached snapshot is what stops the
  // heartbeat from re-ageing it.
  const bool fresh = st.linkUp && st.lastA0Ms != 0
                     && (millis() - st.lastA0Ms) < kLinkStaleMs;
  if (!fresh) gHaveSnap = false;
  net::setLinkAvailable(fresh);

  // No broker address and none discovered: keep asking. A power cut brings this board and Home
  // Assistant back at the same moment, and Home Assistant needs a minute or two longer - so the
  // boot-time discovery finds nothing and, without this, the bridge stays silent until somebody
  // cycles power. Measured need, not a precaution: the same shape as the heater ladder, where a
  // retry is the difference between a device that recovers and one that waits for a human.
  //
  // Two minutes, and only while there is no address at all: once discovery has answered, or the
  // user has typed one, this never runs again on that boot.
  if (!gCfg.broker[0] && !gAutoBroker[0] && net::stationUp()
      && (millis() - gLastDiscoverMs) > 120000u) {
    if (runDiscovery("broker still not found")) startMqtt();
  }

  net::mqttPoll(gHaveSnap ? &gSnap : nullptr, newFrame && gHaveSnap);

  delay(10);
}
