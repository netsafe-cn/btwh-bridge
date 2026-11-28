#include "ble/link.h"

#include <Arduino.h>
#include <NimBLEDevice.h>

#include <string.h>

#include "proto/frame.h"

namespace ble {
namespace {

const char* kSvcUuid  = "ff4396a6-50a8-4fc0-bcf6-228e82dfedb0";
const char* kFifoUuid = "ff4396a6-50a8-4fc0-bcf6-228e82dfedb2";
const char* kCredUuid = "ff4396a6-50a8-4fc0-bcf6-228e82dfedb3";

NimBLEClient*               gClient = nullptr;
NimBLERemoteCharacteristic* gFifo   = nullptr;

proto::RxStream gRx;                 // fragment reassembly lives next to the fragments
uint8_t         gMid = 0;
uint32_t        gPasskey = 0;
bool            gEncrypted = false;
bool            gBonded = false;
Hooks           gHooks{nullptr, nullptr, nullptr};

void emitFrame(const uint8_t* frame, size_t len) {
  if (gHooks.onFrame) gHooks.onFrame(gHooks.ctx, frame, len);
}

void onFrameSink(void* /*ctx*/, const uint8_t* frame, size_t len) { emitFrame(frame, len); }

// FIFO and CREDITS share this callback, exactly as Python _on_notify does.
// 2.x changed the callback shape: instead of (data, len, isNotify) it hands over a
// NimBLEAttValue, which owns its bytes and carries the length. Same bytes, different envelope.
void onNotify(NimBLERemoteCharacteristic* /*ch*/, NimBLEAttValue value) {
  const size_t len = value.size();
  if (len == 0 || len > 64) return;
  if (proto::streamPush(&gRx, value.data(), len, onFrameSink, nullptr) < 0) {
    Serial.println("[RX ] buffer overflow, clearing to resync");
  }
}

// KEY: the panel code is also the BLE passkey. While the window is open the device asks for
// PROVIDE_PIN, and this must answer with the six digits, or SMP fails and A2/08 says 00.
//
// NimBLE-Arduino 2.x folded the security callbacks into the client callbacks and changed their
// shape: onPassKeyRequest that *returned* the pin became onPassKeyEntry that *receives* a
// NimBLEConnInfo and expects the answer to be injected. The behaviour required by the heater is
// identical; the wiring is not. When this is wrong the symptom is not an error, it is A2/08
// answering 00 - which reads like a wrong code.
class Security : public NimBLEClientCallbacks {
  void onPassKeyEntry(NimBLEConnInfo& connInfo) override {
    Serial.printf("[SMP] device asks for the PIN -> injecting %06u\n", (unsigned)gPasskey);
    NimBLEDevice::injectPassKey(connInfo, gPasskey);
  }
  void onConfirmPasskey(NimBLEConnInfo& connInfo, uint32_t pin) override {
    Serial.printf("[SMP] numeric comparison PIN=%06u -> accepting\n", (unsigned)pin);
    NimBLEDevice::injectConfirmPasskey(connInfo, true);
  }
  void onAuthenticationComplete(NimBLEConnInfo& connInfo) override {
    gEncrypted = connInfo.isEncrypted();
    gBonded    = connInfo.isBonded();
    Serial.printf("[SMP] authentication complete: %s%s\n",
                  gEncrypted ? "link encrypted" : "not encrypted / failed",
                  gBonded ? ", bonded" : "");
    if (gHooks.onSecurity) gHooks.onSecurity(gHooks.ctx, gEncrypted, gBonded);
  }
};

// setCallbacks() takes a reference and the client does not own it, so this has to outlive every
// client. One instance for the whole firmware; there is only ever one BLE link anyway.
Security gSecurity;

}  // namespace

void begin(const char* deviceName) {
  NimBLEDevice::init(deviceName);
  // KeyboardOnly: we type when the device displays a PIN; NoIO peers fall back to Just Works
  NimBLEDevice::setSecurityIOCap(BLE_HS_IO_KEYBOARD_ONLY);
  NimBLEDevice::setSecurityAuth(true, false, true);   // bonding, no forced MITM, Secure Conn.
  NimBLEDevice::setSecurityPasskey(gPasskey);
  // 2.x has no device-level security callbacks - they belong to each client now, so they are
  // attached in connect() where the client is created.
}

void setPasskey(uint32_t pin) {
  gPasskey = pin;
  NimBLEDevice::setSecurityPasskey(gPasskey);
}

void setHooks(const Hooks& hooks) { gHooks = hooks; }

void clearBonds() {
  NimBLEDevice::deleteAllBonds();
  gBonded = false;
}

bool bonded() { return gBonded; }

void disconnect() {
  if (gClient) {
    if (gClient->isConnected()) gClient->disconnect();
    NimBLEDevice::deleteClient(gClient);
    gClient = nullptr;
  }
  gFifo = nullptr;
  proto::streamReset(&gRx);
}

bool isConnected() { return gClient && gClient->isConnected(); }

int rssi() {
  // The controller answers synchronously from the connection table; no HCI round trip.
  return isConnected() ? gClient->getRssi() : 0;
}

bool connect(const char* heaterMac, uint8_t timeoutSec) {
  uint8_t probe[6];
  if (!parseMac(heaterMac, probe)) return false;

  disconnect();
  NimBLEAddress addr((std::string)heaterMac, BLE_ADDR_PUBLIC);
  gClient = NimBLEDevice::createClient();
  gClient->setConnectTimeout(timeoutSec);
  gClient->setCallbacks(gSecurity);   // reference, not ownership: see gSecurity's comment

  Serial.printf("[CONN] connecting to %s ...\n", heaterMac);
  if (!gClient->connect(addr)) {
    Serial.println("[CONN] x cannot connect (distance / panel asleep / phone app busy)");
    return false;
  }
  Serial.println("[CONN] + link established [1/5]");

  // The device may ask for encryption itself; trigger once so SMP definitely runs. This is
  // still a client method in 2.x - startSecurity() is the server-side equivalent, and switching
  // to it here would compile and then never pair.
  Serial.printf("[SMP ] starting pairing (IOCap=KeyboardOnly, PIN=%06u)... [2/5]\n",
                (unsigned)gPasskey);
  bool sec = gClient->secureConnection();
  Serial.printf("[SMP ] secureConnection() = %s, encrypted=%s\n",
                sec ? "true" : "false", gEncrypted ? "true" : "false");

  NimBLERemoteService* svc = gClient->getService(kSvcUuid);
  if (!svc) {
    Serial.println("[CONN] x service not found (common when pairing did not take)");
    return false;
  }
  gFifo = svc->getCharacteristic(kFifoUuid);
  NimBLERemoteCharacteristic* cred = svc->getCharacteristic(kCredUuid);
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

bool send(uint8_t recv, uint8_t cmd, const uint8_t* data, size_t dlen) {
  if (!gClient || !gClient->isConnected()) {
    // The heater drops an unauthorised link after ~10 s. Saying so beats "FIFO not ready",
    // which reads like a local bug when the peer is the one that hung up.
    Serial.println("[CONN] link is down (the heater disconnected us)");
    return false;
  }
  if (!gFifo) {
    Serial.println("[TX ] FIFO not ready (not subscribed)");
    return false;
  }
  uint8_t frame[160];
  size_t flen = proto::buildFrame(frame, recv, gMid++, cmd, data, dlen);
  uint8_t wire[256];
  size_t wlen = proto::stuffFrame(frame, flen, wire);

  Serial.printf("[TX ] mid=%d cmd=%02X : ", (uint8_t)(gMid - 1), cmd);
  for (size_t i = 0; i < flen; i++) Serial.printf("%02x ", frame[i]);
  Serial.println();

  for (size_t off = 0; off < wlen; off += proto::kChunk) {
    size_t n = (wlen - off < proto::kChunk) ? (wlen - off) : proto::kChunk;
    uint8_t chunk[proto::kChunk];
    memcpy(chunk, wire + off, n);
    proto::xorBuf(chunk, n);
    if (!gFifo->writeValue(chunk, n, true)) {
      Serial.println("[TX ] write failed");
      return false;
    }
    delay(20);
  }
  return true;
}

int scan(ScanEntry* out, int maxEntries, uint8_t seconds) {
  NimBLEScan* s = NimBLEDevice::getScan();
  s->setActiveScan(true);
  s->setInterval(100);
  s->setWindow(99);
  // 2.x: getResults() takes milliseconds and blocks; the old start(seconds, false) is gone.
  // getDevice() now returns a pointer into the results rather than a copy.
  NimBLEScanResults r = s->getResults((uint32_t)seconds * 1000u, false);
  Serial.printf("[SCAN] found %d devices:\n", r.getCount());

  int n = 0;
  for (int i = 0; i < r.getCount() && n < maxEntries; i++) {
    const NimBLEAdvertisedDevice* d = r.getDevice(i);
    if (!d) continue;
    strncpy(out[n].addr, d->getAddress().toString().c_str(), sizeof(out[n].addr) - 1);
    out[n].addr[sizeof(out[n].addr) - 1] = 0;
    const char* name = d->haveName() ? d->getName().c_str() : "";
    strncpy(out[n].name, name, sizeof(out[n].name) - 1);
    out[n].name[sizeof(out[n].name) - 1] = 0;
    out[n].rssi = d->getRSSI();
    n++;
  }
  s->clearResults();
  return n;
}

int scanHeaters(ScanEntry* out, int maxEntries, uint8_t seconds) {
  // The provisioning page needs "which heaters are in range", not "every phone and TV nearby".
  // The heater advertises a fixed name pattern (docs/BLE_PROTOCOL.md section 1:
  // BTWH-001-xxxx). Filter on it, then sort by signal so the nearest heater is the first
  // radio button - most of the time that is the one plugged in next to the bridge.
  ScanEntry all[24];
  int total = scan(all, (int)(sizeof(all) / sizeof(all[0])), seconds);
  if (total < 0) total = 0;

  int n = 0;
  for (int i = 0; i < total && n < maxEntries; i++) {
    if (strncmp(all[i].name, "BTWH-", 5) != 0) continue;
    out[n++] = all[i];
  }

  // Insertion sort by RSSI, strongest first. n is at most a handful, so O(n^2) is honest here.
  for (int i = 1; i < n; i++) {
    ScanEntry key = out[i];
    int j = i - 1;
    while (j >= 0 && out[j].rssi < key.rssi) { out[j + 1] = out[j]; j--; }
    out[j + 1] = key;
  }

  Serial.printf("[SCAN] %d heater(s) in range (BTWH-*)\n", n);
  for (int i = 0; i < n; i++) {
    Serial.printf("[SCAN]   %d. %s  %s  RSSI %d\n", i + 1, out[i].name, out[i].addr, out[i].rssi);
  }
  return n;
}

bool parseMac(const char* text, uint8_t out[6]) {
  int v[6] = {0}, n = 0;
  const char* p = text;
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

void formatMac(const uint8_t mac[6], char out[18]) {
  snprintf(out, 18, "%02X:%02X:%02X:%02X:%02X:%02X",
           mac[0], mac[1], mac[2], mac[3], mac[4], mac[5]);
}

}  // namespace ble
