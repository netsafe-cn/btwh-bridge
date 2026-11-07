/*
 * ============================================================================
 * Bosch heater BLE gateway  v2.0  --  Waveshare ESP32-S3-DEV-KIT-N8R8
 * ----------------------------------------------------------------------------
 * Phase one (feasibility, complete before any Wi-Fi/MQTT work):
 *   1) connect to the heater and finish SMP pairing (the panel code is the BLE pin)
 *   2) A2/07 submits the MAC, A2/08 submits the pairing code, authorisation follows
 *   3) A0 broadcasts arrive and every field parses correctly
 *
 * Two defects fixed in v2.0 against v1.0:
 *   1) CRITICAL: v1 used Just Works + NoInputNoOutput and ignored the SMP pin entirely.
 *      Measured: the panel code is both the BLE passkey and the application password,
 *      and the device asks for PROVIDE_PIN while its window is open. v2 answers it.
 *   2) EFFICIENCY: the code is no longer compiled in; it is entered over serial at runtime.
 *      It lives for minutes, and v1 needed a rebuild plus reflash of 2 minutes or more per
 *      code change. v2 flashes once and then accepts any number of codes and retries.
 *
 * Hardware: Waveshare ESP32-S3-DEV-KIT-N8R8 (8 MB flash / 8 MB PSRAM)
 * Logs: COM3 (CH343 -> UART0) at 115200. Flashing also uses COM3.
 * Depends on: NimBLE-Arduino 1.4.3 (pinned in platformio.ini; do not move to 2.x)
 * Protocol reference: BLE_PROTOCOL.md / bosch_api.py (byte-identical, 136 tests green)
 * ============================================================================
 */

#include <Arduino.h>
#include <NimBLEDevice.h>
#include <Preferences.h>
#include <ctype.h>
#include <string.h>

// ---------------------------------------------------------------- defaults
// Placeholders. First run: scan, then `addr <heater MAC>` and `mac <host MAC>`.
static const char* DEF_DEVICE_MAC = "AA:BB:CC:DD:EE:FF";   // heater BLE address (placeholder)
static const char* DEF_LOGIN_MAC  = "11:22:33:44:55:66";   // authorised host MAC (placeholder)
static const char* DEF_PIN        = "";                     // panel code: entered at runtime

static const char* SVC_UUID  = "ff4396a6-50a8-4fc0-bcf6-228e82dfedb0";
static const char* FIFO_UUID = "ff4396a6-50a8-4fc0-bcf6-228e82dfedb2";
static const char* CRED_UUID = "ff4396a6-50a8-4fc0-bcf6-228e82dfedb3";

static const uint8_t XOR_KEY[9] = {'1','a','g','3','.','-','f','s','7'};
static const uint8_t SENDER     = 0x21;
static const uint8_t RECV_LOGIN = 0xAB;
static const uint8_t CMD_LOGIN  = 0xA2;
static const uint8_t CMD_STATE  = 0xA0;
static const size_t  CHUNK      = 20;

// ---------------------------------------------------------------- global state
static Preferences gPrefs;

static String  gDevMac, gLoginMac, gPin;
static uint32_t gPinNum = 0;
static bool     gNeedRebond = false;

static NimBLEClient*               gClient = nullptr;
static NimBLERemoteCharacteristic* gFifo   = nullptr;

static uint8_t gRxBuf[1024];
static size_t  gRxLen = 0;
static uint8_t gMid   = 0;

static volatile int      gAuthResp   = -1;   // A2/07 data[1]
static volatile int      gPassResp   = -1;   // A2/08 data[1]
static volatile uint32_t gA0Count    = 0;
static volatile uint32_t gLastA0Ms   = 0;
static volatile bool     gEncrypted  = false;

static bool     gRawDump   = false;
static bool     gInSession = false;
static uint32_t gSessionStart = 0;
static String   gSerialLine;

enum Phase { PH_WAIT_CODE, PH_RUNNING, PH_FAILED };
static Phase gPhase = PH_WAIT_CODE;

// ---------------------------------------------------------------- helpers
static void banner(const char* s) { Serial.printf("\n===== %s =====\n", s); }

static bool parseMac(const String& s, uint8_t out[6]) {
  int v[6] = {0}, n = 0;
  const char* p = s.c_str();
  while (*p && n < 6) {
    if (*p == ':' || *p == '-') { p++; continue; }
    if (!isxdigit((unsigned char)p[0]) || !isxdigit((unsigned char)p[1])) return false;
    char t[3] = {p[0], p[1], 0};
    v[n++] = (int)strtol(t, nullptr, 16);
    p += 2;
  }
  if (n != 6) return false;
  for (int i = 0; i < 6; i++) out[i] = (uint8_t)v[i];
  return true;
}

static String macToStr(const uint8_t m[6]) {
  char b[18];
  snprintf(b, sizeof(b), "%02X:%02X:%02X:%02X:%02X:%02X",
           m[0], m[1], m[2], m[3], m[4], m[5]);
  return String(b);
}

// ---------------------------------------------------------------- protocol codec
static void xorBuf(uint8_t* p, size_t n) {
  for (size_t i = 0; i < n; i++) p[i] ^= XOR_KEY[i % 9];
}

// Logical frame: 7B7B | sender recv mid cmd data... | chk | 7D7D
static size_t buildFrame(uint8_t* out, uint8_t recv, uint8_t mid,
                         uint8_t cmd, const uint8_t* data, size_t dlen) {
  size_t i = 0;
  out[i++] = 0x7B; out[i++] = 0x7B;
  out[i++] = SENDER; out[i++] = recv; out[i++] = mid; out[i++] = cmd;
  for (size_t k = 0; k < dlen; k++) out[i++] = data[k];
  uint8_t sum = 0;
  for (size_t k = 2; k < i; k++) sum += out[k];
  out[i++] = sum;
  out[i++] = 0x7D; out[i++] = 0x7D;
  return i;
}

// Stuffing: a 7B/7D inside frame[2 .. len-3] gets a 00 after it (chk included, as in Python)
static size_t stuffFrame(const uint8_t* in, size_t n, uint8_t* out) {
  if (n <= 4) { memcpy(out, in, n); return n; }
  size_t o = 0;
  out[o++] = in[0]; out[o++] = in[1];
  for (size_t i = 2; i < n - 2; i++) {
    out[o++] = in[i];
    if (in[i] == 0x7B || in[i] == 0x7D) out[o++] = 0x00;
  }
  out[o++] = in[n-2]; out[o++] = in[n-1];
  return o;
}

static size_t unstuffFrame(const uint8_t* in, size_t n, uint8_t* out) {
  if (n <= 2) { memcpy(out, in, n); return n; }
  size_t o = 0;
  out[o++] = in[0]; out[o++] = in[1];
  bool skip = false;
  for (size_t i = 2; i < n - 2; i++) {
    if (skip && in[i] == 0x00) { skip = false; continue; }
    out[o++] = in[i];
    skip = (in[i] == 0x7B || in[i] == 0x7D);
  }
  out[o++] = in[n-2]; out[o++] = in[n-1];
  return o;
}

// ---------------------------------------------------------------- A0 parsing
// Offsets calibrated against 148 captured frames on 2026-11-01 (BLE_PROTOCOL.md §6)
static void dumpA0(const uint8_t* d, size_t n) {
  auto b = [&](int i) -> int { return (i < (int)n) ? d[i] : 0; };
  int  ftype = b(0);
  bool t1    = (ftype == 1);
  auto temp  = [&](int i) -> float { return b(i) + b(i+1) / 100.0f; };

  float tIn   = temp(1);
  float tChm  = temp(3);
  float tBox  = temp(6);
  int   setp  = b(5);
  int   fault = b(11);
  bool  ion   = (b(10) & 0x10) != 0;

  // Offsets verified on Type1 only; Type0 keeps its own set
  int   outI  = t1 ? 28 : 21;
  float tOut  = (float)b(outI) + b(outI+1) / 100.0f;
  float flow  = t1 ? b(21) / 10.0f : b(8) / 10.0f;
  int   power = t1 ? b(13) : b(9);
  int   fan   = t1 ? (b(15) * 100 + b(16)) : b(14);

  Serial.printf("[A0] T%d out%.1f in%.1f chamber%.1f chassis%.1f set%d "
                "power%d%% flow%.1fL/min fan%drpm flame%s fault%s\n",
                ftype, tOut, tIn, tChm, tBox, setp, power, flow, fan,
                ion ? "yes" : "no", fault ? String(fault, HEX).c_str() : "none");

  if (t1) {  // suspected fields, marked so they can be confirmed later
    Serial.printf("     [guess] ambient%.1f ambient_set%.1f gas_set%.1f "
                  "pressure%dPa mode[%d]=%02X bath[%d]=%02X\n",
                  temp(16), temp(18), temp(22), b(27) * 10, 32, b(32), 33, b(33));
  }
  if (gRawDump) {
    Serial.print("     raw=");
    for (size_t i = 0; i < n; i++) Serial.printf("%02x ", d[i]);
    Serial.println();
  }
}

// ---------------------------------------------------------------- frame dispatch
static void onFrame(const uint8_t* f, size_t n) {
  if (n < 9) return;
  uint8_t sum = 0;
  for (size_t i = 2; i < n - 3; i++) sum += f[i];
  if (sum != f[n-3]) {
    Serial.printf("[RX] dropping frame chk=%02X expect=%02X len=%d\n", f[n-3], sum, (int)n);
    return;
  }
  uint8_t mid = f[4], cmd = f[5];
  const uint8_t* p = f + 6;
  size_t plen = n - 9;

  if (cmd == CMD_STATE) {
    gA0Count++;
    gLastA0Ms = millis();
    dumpA0(p, plen);
  } else if (cmd == CMD_LOGIN && plen >= 2) {
    if (p[0] == 0x07) {
      gAuthResp = p[1];
      Serial.printf("[AUTH] + A2/07 reply = %02X  (%s)\n", p[1],
        p[1] == 0x01 ? "passwordless" : p[1] == 0x02 ? "code required" :
        p[1] == 0x00 ? "window closed (code path)" :
        p[1] == 0x3C ? "cooling down (30 s)" :
        p[1] == 0x5A ? "asks for 90 s" : "other");
    } else if (p[0] == 0x08) {
      gPassResp = p[1];
      Serial.printf("[AUTH] + A2/08 reply = %02X  (%s)\n", p[1],
        p[1] == 0x01 ? "authorised" : p[1] == 0x02 ? "wrong code" :
        p[1] == 0x00 ? "rejected (expired / no window)" : "other");
    } else {
      Serial.printf("[AUTH] unknown A2 sub-command %02X: ", p[0]);
      for (size_t i = 0; i < plen; i++) Serial.printf("%02x ", p[i]);
      Serial.println();
    }
  } else {
    Serial.printf("[RX ] cmd=%02X mid=%d len=%d : ", cmd, mid, (int)plen);
    for (size_t i = 0; i < plen; i++) Serial.printf("%02x ", p[i]);
    Serial.println();
  }
}

static void drainRx() {
  while (gRxLen >= 4) {
    size_t start = 0;
    while (start + 1 < gRxLen &&
           !(gRxBuf[start] == 0x7B && gRxBuf[start+1] == 0x7B)) start++;
    if (start + 1 >= gRxLen) { gRxLen = 0; return; }
    if (start > 0) {
      memmove(gRxBuf, gRxBuf + start, gRxLen - start);
      gRxLen -= start;
    }
    size_t end = 0;
    for (size_t i = 2; i + 1 < gRxLen; i++) {
      if (gRxBuf[i] == 0x7D && gRxBuf[i+1] == 0x7D) { end = i + 2; break; }
    }
    if (!end) return;

    uint8_t plain[256];
    size_t plen = unstuffFrame(gRxBuf, end, plain);
    onFrame(plain, plen);
    memmove(gRxBuf, gRxBuf + end, gRxLen - end);
    gRxLen -= end;
  }
}

// FIFO and CREDITS share this callback, exactly as Python _on_notify does
static void onNotify(NimBLERemoteCharacteristic* ch, uint8_t* data,
                     size_t len, bool isNotify) {
  (void)ch; (void)isNotify;
  if (len == 0 || len > 64) return;
  uint8_t tmp[64];
  memcpy(tmp, data, len);
  xorBuf(tmp, len);                       // each fragment restarts at KEY[0]
  if (gRxLen + len > sizeof(gRxBuf)) {
    Serial.println("[RX ] buffer overflow, clearing to resync");
    gRxLen = 0;
  }
  memcpy(gRxBuf + gRxLen, tmp, len);
  gRxLen += len;
  drainRx();
}

// ---------------------------------------------------------------- sending
static bool sendFrame(uint8_t recv, uint8_t cmd, const uint8_t* data, size_t dlen) {
  if (!gFifo || !gClient || !gClient->isConnected()) {
    Serial.println("[TX ] FIFO not ready");
    return false;
  }
  uint8_t frame[160];
  size_t flen = buildFrame(frame, recv, gMid++, cmd, data, dlen);
  uint8_t wire[256];
  size_t wlen = stuffFrame(frame, flen, wire);

  Serial.printf("[TX ] mid=%d cmd=%02X : ", (uint8_t)(gMid - 1), cmd);
  for (size_t i = 0; i < flen; i++) Serial.printf("%02x ", frame[i]);
  Serial.println();

  for (size_t off = 0; off < wlen; off += CHUNK) {
    size_t n = (wlen - off < CHUNK) ? (wlen - off) : CHUNK;
    uint8_t chunk[CHUNK];
    memcpy(chunk, wire + off, n);
    xorBuf(chunk, n);
    if (!gFifo->writeValue(chunk, n, true)) {
      Serial.println("[TX ] write failed");
      return false;
    }
    delay(20);
  }
  return true;
}

static bool waitResp(volatile int& which, uint32_t timeoutMs) {
  uint32_t t0 = millis();
  while (millis() - t0 < timeoutMs) {
    if (which >= 0) return true;
    delay(50);
  }
  return false;
}

// Mirrors bosch_api.py::_send_login_wait: 6 s timeout, 5 resends, fresh msgId each time
static bool loginStep(const uint8_t* data, size_t len, volatile int& resp,
                      const char* tag) {
  for (int attempt = 0; attempt < 5; attempt++) {
    resp = -1;
    if (!sendFrame(RECV_LOGIN, CMD_LOGIN, data, len)) return false;
    if (waitResp(resp, 6000)) {
      Serial.printf("[AUTH] %s reply = %d\n", tag, (int)resp);
      return true;
    }
    Serial.printf("[AUTH] %s no reply, resend %d/5 (fresh msgId)\n", tag, attempt + 1);
  }
  return false;
}

// ---------------------------------------------------------------- SMP callbacks
// KEY: the panel code is also the BLE passkey. While the window is open the device asks
// for PROVIDE_PIN, and this must answer with the six digits, or SMP fails and A2/08 says 00.
class BoschSecurity : public NimBLESecurityCallbacks {
  uint32_t onPassKeyRequest() override {
    Serial.printf("[SMP] device asks for the PIN -> returning %06u\n", (unsigned)gPinNum);
    return gPinNum;
  }
  void onPassKeyNotify(uint32_t pass_key) override {
    Serial.printf("[SMP] device displays PIN %06u (should match the panel)\n", (unsigned)pass_key);
  }
  bool onSecurityRequest() override {
    Serial.println("[SMP] device requests encryption -> accepting");
    return true;
  }
  bool onConfirmPIN(uint32_t pin) override {
    Serial.printf("[SMP] numeric comparison PIN=%06u -> accepting\n", (unsigned)pin);
    return true;
  }
  void onAuthenticationComplete(ble_gap_conn_desc* desc) override {
    bool ok = desc && desc->sec_state.encrypted;
    gEncrypted = ok;
    Serial.printf("[SMP] authentication complete: %s%s\n",
                  ok ? "link encrypted" : "not encrypted / failed",
                  (desc && desc->sec_state.bonded) ? ", bonded" : "");
  }
};

// ---------------------------------------------------------------- session
static void disconnectClient() {
  if (gClient) {
    if (gClient->isConnected()) gClient->disconnect();
    NimBLEDevice::deleteClient(gClient);
    gClient = nullptr;
  }
  gFifo = nullptr;
  gRxLen = 0;
}

static bool connectAndPair() {
  uint8_t dummy[6];
  if (!parseMac(gDevMac, dummy)) {
    Serial.println("[CONN] bad heater address; fix it with addr AA:BB:CC:DD:EE:FF");
    return false;
  }

  disconnectClient();
  NimBLEAddress addr((std::string)gDevMac.c_str(), BLE_ADDR_PUBLIC);
  gClient = NimBLEDevice::createClient();
  gClient->setConnectTimeout(15);

  Serial.printf("[CONN] connecting to %s ...\n", gDevMac.c_str());
  if (!gClient->connect(addr)) {
    Serial.println("[CONN] x cannot connect (distance / panel asleep / phone app busy)");
    return false;
  }
  Serial.println("[CONN] + link established [1/5]");

  // The device may ask for encryption itself; trigger once so SMP definitely runs
  Serial.printf("[SMP ] starting pairing (IOCap=KeyboardOnly, PIN=%06u)... [2/5]\n",
                (unsigned)gPinNum);
  bool sec = gClient->secureConnection();
  Serial.printf("[SMP ] secureConnection() = %s，encrypted=%s\n",
                sec ? "true" : "false", gEncrypted ? "true" : "false");

  NimBLERemoteService* svc = gClient->getService(SVC_UUID);
  if (!svc) {
    Serial.println("[CONN] x service not found (common when pairing did not take)");
    return false;
  }
  gFifo = svc->getCharacteristic(FIFO_UUID);
  NimBLERemoteCharacteristic* cred = svc->getCharacteristic(CRED_UUID);
  if (!gFifo || !cred) {
    Serial.println("[CONN] x FIFO/CREDITS characteristics missing");
    return false;
  }
  // Same order as the app: FIFO -> 200 ms -> CREDITS, then sleep(1000)
  if (!gFifo->subscribe(true, onNotify)) {
    Serial.println("[CONN] x FIFO subscribe failed");
    return false;
  }
  delay(200);
  if (!cred->subscribe(true, onNotify)) {
    Serial.println("[CONN] x CREDITS subscribe failed");
    return false;
  }
  Serial.println("[CONN] + subscribed to FIFO + CREDITS [3/5]");
  delay(1000);
  return true;
}

static bool doLogin() {
  uint8_t mac[6];
  if (!parseMac(gLoginMac, mac)) {
    Serial.println("[AUTH] bad login MAC; fix it with mac AA:BB:CC:DD:EE:FF");
    return false;
  }
  if (gPin.length() != 6) {
    Serial.println("[AUTH] panel code empty or not six digits. Enter one first.");
    return false;
  }

  Serial.printf("[AUTH] submitting MAC = %s (device whitelist)\n", macToStr(mac).c_str());
  uint8_t b1[7] = {0x07, 0,0,0,0,0,0};
  memcpy(b1 + 1, mac, 6);
  if (!loginStep(b1, 7, gAuthResp, "A2/07")) return false;
  Serial.println("[AUTH] + MAC reply received [4/5]");

  int kind = gAuthResp;
  if (kind == 0x3C || kind == 0x5A) {
    Serial.printf("[AUTH] device asks to cool down (%02X), retry in %s\n",
                  kind, kind == 0x3C ? "30 s" : "90 s");
    return false;
  }

  delay(1000);   // app: sendLoginPass always sleeps 1000 ms first
  uint8_t b2[7];
  b2[0] = 0x08;
  for (int i = 0; i < 6; i++) b2[i+1] = gPin[i] - '0';   // one byte per digit
  Serial.printf("[AUTH] submitting code %s (nibble encoded)\n", gPin.c_str());
  if (!loginStep(b2, 7, gPassResp, "A2/08")) return false;

  if (gPassResp == 0x01) {
    Serial.println("[AUTH] + authorised [5/5], waiting for A0 broadcasts...");
    return true;
  }
  Serial.println("[AUTH] x authorisation failed. Usually no window, or an expired code:");
  Serial.println("       panel clear pairing -> read a new code -> type it (no reflash)");
  return false;
}

static void startSession(bool forceRebond) {
  if (gInSession) { Serial.println("[BUSY] session running; type reboot to interrupt"); return; }
  if (gPin.length() != 6) {
    Serial.println("Enter the six-digit panel code first (e.g. 311522) to start connecting.");
    return;
  }
  gInSession = true;
  gSessionStart = millis();
  gPhase = PH_RUNNING;
  gAuthResp = gPassResp = -1;
  gA0Count = 0;
  gLastA0Ms = millis();
  gEncrypted = false;

  NimBLEDevice::setSecurityPasskey(gPinNum);
  if (forceRebond || gNeedRebond) {
    Serial.println("[SMP ] clearing the old local bond (preparing to pair again)");
    NimBLEDevice::deleteAllBonds();
    gNeedRebond = false;
    delay(200);
  }

  banner("starting session");
  Serial.printf("[CFG ] heater=%s  loginMAC=%s  PIN=%s\n",
                gDevMac.c_str(), gLoginMac.c_str(), gPin.c_str());

  bool ok = connectAndPair();
  if (ok) ok = doLogin();
  if (!ok) {
    Serial.println("\n[result] x failed. Fix it and type the code again; no reflash needed.");
    disconnectClient();
    gPhase = PH_FAILED;
  } else {
    Serial.println("\n[result] + all three stages passed, listening for A0 (a frame every 4 s)");
  }
  gInSession = false;
}

// ---------------------------------------------------------------- commands
static void doScan() {
  if (gClient && gClient->isConnected()) {
    Serial.println("[SCAN] already connected; disconnect before scanning");
    disconnectClient();
    delay(300);
  }
  banner("BLE scan, 5 s");
  NimBLEScan* s = NimBLEDevice::getScan();
  s->setActiveScan(true);
  s->setInterval(100);
  s->setWindow(99);
  NimBLEScanResults r = s->start(5, false);
  Serial.printf("[SCAN] found %d devices://n", r.getCount());
  for (int i = 0; i < r.getCount(); i++) {
    NimBLEAdvertisedDevice d = r.getDevice(i);   // 1.4.x returns an object, not a pointer
    Serial.printf("  %-20s RSSI=%4d  %s\n",
                  d.getAddress().toString().c_str(), d.getRSSI(),
                  d.haveName() ? d.getName().c_str() : "(unnamed)");
  }
  s->clearResults();
  banner("scan finished");
}

static void setPin(const String& code) {
  bool changed = (code != gPin);
  gPin = code;
  gPinNum = (uint32_t)strtoul(code.c_str(), nullptr, 10);
  gPrefs.putString("pin", gPin);
  NimBLEDevice::setSecurityPasskey(gPinNum);
  if (changed) gNeedRebond = true;    // a new code means a new window; the old bond is void
  Serial.printf("[CFG ] panel code = %s (both the BLE PIN and the app password)%s\n",
                gPin.c_str(), changed ? " [changed -> bond cleared on next connect]" : "");
  startSession(false);
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
  Serial.println("  raw on / raw off  show / hide raw A0 bytes");
  Serial.println("  reboot            reboot");
  Serial.println("  help              this help");
}

static void printStatus() {
  banner("status");
  Serial.printf("  heater     : %s\n", gDevMac.c_str());
  Serial.printf("  login MAC  : %s\n", gLoginMac.c_str());
  Serial.printf("  panel code : %s (PIN=%06u)\n", gPin.c_str(), (unsigned)gPinNum);
  Serial.printf("  phase      : %s\n", gPhase == PH_WAIT_CODE ? "waiting for code" :
                                     gPhase == PH_RUNNING  ? "connected, listening" : "last attempt failed");
  Serial.printf("  link       : %s / encrypted=%s\n",
                (gClient && gClient->isConnected()) ? "up" : "down",
                gEncrypted ? "yes" : "no");
  Serial.printf("  A2/07=%d  A2/08=%d  A0 frames=%u  raw=%s\n",
                (int)gAuthResp, (int)gPassResp, (unsigned)gA0Count,
                gRawDump ? "on" : "off");
}

static void handleCommand(String line) {
  line.trim();
  if (!line.length()) return;
  String low = line; low.toLowerCase();

  // Six digits on their own mean the panel code
  if (line.length() == 6) {
    bool digits = true;
    for (int i = 0; i < 6; i++) if (!isdigit((unsigned char)line[i])) digits = false;
    if (digits) { setPin(line); return; }
  }
  if (low.startsWith("pin "))  { setPin(line.substring(4)); return; }
  if (low == "help" || low == "?")   { printHelp(); return; }
  if (low == "status")               { printStatus(); return; }
  if (low == "scan")                 { doScan(); return; }
  if (low == "go")                   { gNeedRebond = false; startSession(false); return; }
  if (low == "pair")                 { startSession(true); return; }
  if (low == "reboot")               { Serial.println("rebooting..."); delay(200); ESP.restart(); }
  if (low == "raw on")               { gRawDump = true;  Serial.println("[CFG] raw=on");  return; }
  if (low == "raw off")              { gRawDump = false; Serial.println("[CFG] raw=off"); return; }
  if (low.startsWith("mac ")) {
    uint8_t m[6];
    if (!parseMac(line.substring(4), m)) { Serial.println("bad MAC format"); return; }
    gLoginMac = macToStr(m);
    gPrefs.putString("loginmac", gLoginMac);
    Serial.printf("[CFG] login MAC = %s\n", gLoginMac.c_str());
    return;
  }
  if (low.startsWith("addr ")) {
    gDevMac = line.substring(5); gDevMac.trim(); gDevMac.toLowerCase();
    gPrefs.putString("devmac", gDevMac);
    Serial.printf("[CFG] heater address = %s\n", gDevMac.c_str());
    return;
  }
  Serial.printf("[?] unknown command: %s (type help for the list)\n", line.c_str());
}

static void pollSerial() {
  while (Serial.available()) {
    char c = (char)Serial.read();
    if (c == '\n' || c == '\r') {
      if (gSerialLine.length()) { handleCommand(gSerialLine); gSerialLine = ""; }
    } else if (gSerialLine.length() < 120) {
      gSerialLine += c;
    }
  }
}

// ---------------------------------------------------------------- Arduino
void setup() {
  Serial.begin(115200);
  delay(1200);
  banner("Bosch heater ESP32 gateway v2.0 - Waveshare ESP32-S3-DEV-KIT-N8R8");

  gPrefs.begin("bosch", false);
  gDevMac   = gPrefs.getString("devmac",   DEF_DEVICE_MAC);
  gLoginMac = gPrefs.getString("loginmac", DEF_LOGIN_MAC);
  gPin      = gPrefs.getString("pin",      DEF_PIN);
  gPinNum   = (uint32_t)strtoul(gPin.c_str(), nullptr, 10);

  Serial.printf("[BOOT] Chip=%s  Flash=%uMB  PSRAM=%u bytes\n",
                ESP.getChipModel(), ESP.getFlashChipSize() / (1024*1024), ESP.getPsramSize());
  Serial.printf("[BOOT] heater=%s  loginMAC=%s  stored code=%s\n",
                gDevMac.c_str(), gLoginMac.c_str(), gPin.length() ? gPin.c_str() : "(none)");

  NimBLEDevice::init("BoschGW");
  // KeyboardOnly: we type when the device displays a PIN; NoIO peers fall back to Just Works
  NimBLEDevice::setSecurityIOCap(BLE_HS_IO_KEYBOARD_ONLY);
  NimBLEDevice::setSecurityAuth(true, false, true);   // bonding, no forced MITM, Secure Conn.
  NimBLEDevice::setSecurityPasskey(gPinNum);
  NimBLEDevice::setSecurityCallbacks(new BoschSecurity());
  // Default transmit power: setPower() differs across core/NimBLE versions, not worth it

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
  if (gPin.length() == 6) {
    Serial.printf("\n[BOOT] stored code %s, reconnecting automatically (type a new one if it fails)\n",
                  gPin.c_str());
    startSession(false);
    Serial.println("[BOOT] if that fails, type a new code");
  } else {
    Serial.println("\nwaiting for input...");
  }
}

void loop() {
  pollSerial();

  if (gPhase == PH_RUNNING && gClient) {
    if (!gClient->isConnected()) {
      Serial.println("[CONN] link dropped, reconnecting in 5 s with the current code");
      gPhase = PH_WAIT_CODE;
      disconnectClient();
      delay(5000);
      startSession(false);
      return;
    }
    // 30 s without A0 means the login did not really take; warn without interrupting
    static uint32_t lastWarn = 0;
    if (gA0Count == 0 && millis() - gSessionStart > 30000 && millis() - lastWarn > 15000) {
      lastWarn = millis();
      Serial.println("[WARN] 30 s connected with no A0: A2/08 likely failed, read a new code");
    }
  }
  delay(10);
}
