#include "net/mqtt_client.h"

#include <Arduino.h>
#include <WiFi.h>
#include <ctype.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "net/discovery.h"
#include "net/mqtt_contract.h"
#include "net/mqtt_wire.h"
#include "net/station.h"

namespace net {
namespace {

namespace wire = mqtt;

// Transaction buffer. The largest thing ever built is a discovery payload (~470 bytes) plus a
// topic and the fixed header, so 768 leaves room without a heap allocation per publish.
constexpr size_t kTxCap = 768;

enum Link : uint8_t {
  kOff = 0,        // no broker configured
  kIdle,           // waiting for the backoff timer
  kWaitConnack,    // TCP up, CONNECT sent, CONNACK outstanding
  kUp,             // published, subscribed, running
};

WiFiClient gSock;

char     gHost[64];
uint16_t gPort = 1883;
char     gDeviceId[20];        // btwh_<last6>
char     gUser[33];
char     gPass[65];

MqttInfo   gInfo{"-", "unpaired", "esp32s3"};
SetpointFn gOnSetpoint = nullptr;
CommandFn  gOnCommand  = nullptr;
void*      gHookCtx    = nullptr;

Link     gState       = kOff;
uint32_t gLastAttempt = 0;
uint32_t gBackoffMs   = 2000;
uint32_t gLastPing    = 0;
uint32_t gLastPublish = 0;
uint32_t gLastRx      = 0;
uint16_t gPacketId    = 1;
bool     gWantPublish = false;
// Copy, not a pointer: the reason is often a caller's stack buffer ("broker refused: ..."),
// and `status` reads this minutes later.
char     gWhy[64] = "not started";

uint8_t gTx[kTxCap];
uint8_t gRx[wire::kMaxPacket];
size_t  gRxLen = 0;

// ---------------------------------------------------------------- helpers

void makeTopic(char* out, size_t cap, const char* suffix) {
  snprintf(out, cap, "%s/%s/%s", contract::kBaseTopic, gDeviceId, suffix);
}

bool publish(const char* suffix, const void* payload, size_t len, bool retain) {
  char topic[80];
  makeTopic(topic, sizeof(topic), suffix);
  const size_t n = wire::buildPublish(gTx, sizeof(gTx), topic, payload, len, retain);
  if (!n) {
    Serial.printf("[MQTT] x payload for %s does not fit (%u bytes)\n", suffix, (unsigned)len);
    return false;
  }
  return gSock.write(gTx, n) == n;
}

void publishText(const char* suffix, const char* text, bool retain) {
  publish(suffix, text, strlen(text), retain);
}

void publishAttributes() {
  char json[220];
  snprintf(json, sizeof(json),
           "{\"fw\":\"%s\",\"hw\":\"%s\",\"heater\":\"%s\",\"pair_state\":\"%s\",\"uptime\":%lu}",
           kFirmwareVersion, gInfo.hardware ? gInfo.hardware : "esp32s3",
           gInfo.heater ? gInfo.heater : "-", gInfo.pairState ? gInfo.pairState : "unpaired",
           (unsigned long)(millis() / 1000));
  publishText("attributes", json, true);
}

// Discovery is retained and idempotent, so it is republished on every connect: that also
// repairs an entity deleted from Home Assistant without touching the bridge.
void publishDiscovery() {
  static char topic[96];
  static char payload[600];
  size_t sent = 0;
  for (size_t i = 0; i < discovery::entityCount(); i++) {
    if (!discovery::expand(discovery::entityTopic(i), gDeviceId, topic, sizeof(topic))) continue;
    if (!discovery::expand(discovery::entityPayload(i), gDeviceId, payload, sizeof(payload))) continue;
    const size_t n = wire::buildPublish(gTx, sizeof(gTx), topic, payload, strlen(payload), true);
    if (n && gSock.write(gTx, n) == n) sent++;
  }
  Serial.printf("[MQTT] discovery: %u of %u entities published\n",
                (unsigned)sent, (unsigned)discovery::entityCount());
}

bool subscribe(const char* suffix) {
  char filter[80];
  makeTopic(filter, sizeof(filter), suffix);
  const size_t n = wire::buildSubscribe(gTx, sizeof(gTx), gPacketId++, filter, 0);
  if (!n) return false;
  return gSock.write(gTx, n) == n;
}

void onConnected() {
  gState       = kUp;
  gBackoffMs   = 2000;          // a good session resets the schedule
  gLastPing    = millis();
  gLastRx      = millis();
  gWantPublish = true;

  publishText("availability", "online", true);
  publishAttributes();
  publishDiscovery();
  subscribe("set/temperature");
  subscribe("cmd");

  Serial.printf("[MQTT] + connected to %s:%u as %s\n", gHost, (unsigned)gPort, gDeviceId);
}

// Drops the socket and schedules the next attempt. `why` is printed once; the backoff doubles
// up to a minute, which is what keeps a wrong password from hammering the broker.
void drop(const char* why) {
  strncpy(gWhy, why ? why : "", sizeof(gWhy) - 1);
  gWhy[sizeof(gWhy) - 1] = 0;
  if (gState == kUp && gSock.connected()) {
    publishText("availability", "offline", true);   // explicit goodbye; the will covers crashes
    const size_t n = wire::buildDisconnect(gTx, sizeof(gTx));
    if (n) gSock.write(gTx, n);
  }
  gSock.stop();
  gRxLen       = 0;
  gState       = kIdle;
  gLastAttempt = millis();
  gBackoffMs   = gBackoffMs < 60000 ? gBackoffMs * 2 : 60000;
  Serial.printf("[MQTT] x %s (retry in %lus)\n", why, (unsigned long)(gBackoffMs / 1000));
}

void beginConnect() {
  gLastAttempt = millis();
  gSock.stop();
  gRxLen = 0;

  if (!gSock.connect(gHost, gPort, 3000)) {
    strncpy(gWhy, "tcp connect failed", sizeof(gWhy) - 1);
    gWhy[sizeof(gWhy) - 1] = 0;
    gState = kIdle;
    gBackoffMs = gBackoffMs < 60000 ? gBackoffMs * 2 : 60000;
    Serial.printf("[MQTT] x %s:%u unreachable (retry in %lus)\n",
                  gHost, (unsigned)gPort, (unsigned long)(gBackoffMs / 1000));
    return;
  }

  char willTopic[80];
  makeTopic(willTopic, sizeof(willTopic), "availability");

  wire::ConnectOpts o{};
  o.clientId     = gDeviceId;
  o.willTopic    = willTopic;
  o.willMessage  = "offline";
  o.username     = gUser[0] ? gUser : nullptr;
  o.password     = gPass[0] ? gPass : nullptr;
  o.keepAliveSec = 60;
  o.cleanSession = true;

  const size_t n = wire::buildConnect(gTx, sizeof(gTx), o);
  if (!n) { drop("connect packet does not fit"); return; }
  if (gSock.write(gTx, n) != n) { drop("connect write failed"); return; }

  gState = kWaitConnack;
  Serial.printf("[MQTT] connecting to %s:%u ...\n", gHost, (unsigned)gPort);
}

// ---------------------------------------------------------------- inbound

void handleSetpoint(const wire::PublishInfo& in) {
  char text[12];
  const size_t n = in.payloadLen < sizeof(text) - 1 ? in.payloadLen : sizeof(text) - 1;
  memcpy(text, in.payload, n);
  text[n] = 0;
  // Some clients append a newline; a trailing space should not turn 42 into "ignored".
  for (int i = (int)n - 1; i >= 0 && isspace((unsigned char)text[i]); i--) text[i] = 0;

  char* end = nullptr;
  // strtod, not strtol: Home Assistant's number entity publishes str(float) - "42.0" - and an
  // integer-only parser would silently drop every command from it. Whole values only, still:
  // "42.5" is a range error, not something to round and hope.
  const double value = strtod(text, &end);
  const bool numeric = end && *end == 0 && end != text;
  const bool whole  = numeric && value == (double)(long)value;

  // The contract says out-of-range values are ignored, not clamped: a consumer that sends 70
  // asked for something the heater cannot do, and silently turning it into 60 hides the bug.
  if (!whole || value < 35 || value > 60) {
    Serial.printf("[MQTT] setpoint ignored: [%s] (whole degrees 35..60)\n", text);
    return;
  }
  Serial.printf("[MQTT] setpoint -> %d C\n", (int)value);
  if (gOnSetpoint) gOnSetpoint(gHookCtx, (int)value);
}

void handleCommand(const wire::PublishInfo& in) {
  char text[48];
  const size_t n = in.payloadLen < sizeof(text) - 1 ? in.payloadLen : sizeof(text) - 1;
  memcpy(text, in.payload, n);
  text[n] = 0;

  // A tiny substring match instead of a JSON parser: the contract allows three words, and
  // dragging in a parser for them would cost more than it explains.
  const char* verb = nullptr;
  if (strstr(text, "refresh")) verb = "refresh";
  else if (strstr(text, "reboot")) verb = "reboot";
  else if (strstr(text, "unpair")) verb = "unpair";

  if (!verb) {
    Serial.printf("[MQTT] command ignored: [%s]\n", text);
    return;
  }
  Serial.printf("[MQTT] command: %s\n", verb);
  if (strcmp(verb, "refresh") == 0) gWantPublish = true;
  if (gOnCommand) gOnCommand(gHookCtx, verb);
}

void handle(const wire::Packet& p) {
  switch (p.type) {
    case wire::kTypeConnack: {
      const uint8_t code = p.length >= 2 ? p.body[1] : 0xFF;
      if (code == wire::kAccepted) {
        onConnected();
      } else {
        char why[64];
        snprintf(why, sizeof(why), "broker refused: %s", wire::connackText(code));
        drop(why);
      }
      break;
    }
    case wire::kTypeSuback:
      Serial.printf("[MQTT] subscription acknowledged (return code %02X)\n",
                    p.length ? p.body[p.length - 1] : 0);
      break;
    case wire::kTypePublish: {
      wire::PublishInfo in{};
      if (!wire::parsePublish(p, in)) { drop("malformed PUBLISH"); return; }
      if (in.qos == 1) {
        const size_t n = wire::buildPuback(gTx, sizeof(gTx), in.packetId);
        if (n) gSock.write(gTx, n);
      }
      char topic[80];
      makeTopic(topic, sizeof(topic), "set/temperature");
      if (wire::topicIs(in, topic)) { handleSetpoint(in); return; }
      makeTopic(topic, sizeof(topic), "cmd");
      if (wire::topicIs(in, topic)) {
        // Commands are live-only. A retained "reboot" would replay on every reconnect and
        // reboot the board forever; the retained flag on a delivery says exactly that happened.
        // set/temperature is deliberately allowed: replaying the last setpoint is idempotent.
        if (in.retained) {
          Serial.println("[MQTT] retained command ignored (commands are live-only)");
          return;
        }
        handleCommand(in);
        return;
      }
      Serial.printf("[MQTT] publish on an unknown topic ignored (%u bytes)\n",
                    (unsigned)in.payloadLen);
      break;
    }
    case wire::kTypePingresp:
      break;
    default:
      Serial.printf("[MQTT] packet type %u ignored\n", (unsigned)p.type);
      break;
  }
}

// Reads whatever the socket has, then decodes as many packets as are complete. Every packet is
// handled before the buffer is compacted, so `body` stays valid while it is being read.
void drainSocket() {
  while (gSock.available() && gRxLen < sizeof(gRx)) {
    const int c = gSock.read();
    if (c < 0) break;
    gRx[gRxLen++] = (uint8_t)c;
    gLastRx = millis();
  }

  for (;;) {
    wire::Packet p{};
    size_t used = 0;
    const int r = wire::decode(gRx, gRxLen, &p, &used);
    if (r == 0) break;
    // -1 covers both a malformed length prefix and a packet larger than kMaxPacket (legal, but
    // bigger than this client commits to decode). Either way the stream cannot continue.
    if (r < 0) { drop("malformed or oversized packet"); return; }
    handle(p);
    if (gState != kUp && gState != kWaitConnack) return;   // handle() dropped the link
    memmove(gRx, gRx + used, gRxLen - used);
    gRxLen -= used;
  }
}

}  // namespace

// ---------------------------------------------------------------- public

bool parseBroker(const char* text, char* host, size_t hostCap, uint16_t* port) {
  if (!text || !text[0] || !host || hostCap < 2) return false;

  size_t hostLen = strlen(text);
  long   p = 1883;

  const char* colon = strrchr(text, ':');
  if (colon && colon[1]) {
    char* end = nullptr;
    const long v = strtol(colon + 1, &end, 10);
    if (!end || *end != 0 || v <= 0 || v > 65535) return false;
    p = v;
    hostLen = (size_t)(colon - text);
  }
  if (hostLen == 0 || hostLen >= hostCap) return false;

  memcpy(host, text, hostLen);
  host[hostLen] = 0;
  if (port) *port = (uint16_t)p;
  return true;
}

void deviceIdFromAddress(const char* address, char* out, size_t cap) {
  if (!out || cap == 0) return;
  out[0] = 0;
  if (!address) return;

  char hex[40];
  size_t n = 0;
  for (const char* p = address; *p && n < sizeof(hex); p++) {
    if (isxdigit((unsigned char)*p)) hex[n++] = (char)tolower((unsigned char)*p);
  }
  const char* tail = n >= 6 ? hex + n - 6 : "000000";
  snprintf(out, cap, "%s_%s", contract::kBaseTopic, tail);
}

void mqttBegin(const char* broker, const char* deviceId) {
  uint16_t port = 1883;
  if (!parseBroker(broker, gHost, sizeof(gHost), &port)) {
    gState = kOff;
    strncpy(gWhy, "no broker configured", sizeof(gWhy) - 1);
    gWhy[sizeof(gWhy) - 1] = 0;
    return;
  }
  gPort = port;
  strncpy(gDeviceId, deviceId && deviceId[0] ? deviceId : "btwh_000000", sizeof(gDeviceId) - 1);
  gDeviceId[sizeof(gDeviceId) - 1] = 0;

  gUser[0] = 0;
  gPass[0] = 0;
  gBackoffMs = 2000;
  gState = kIdle;
  gLastAttempt = millis();
  Serial.printf("[MQTT] broker=%s:%u device=%s\n", gHost, (unsigned)gPort, gDeviceId);
}

void mqttSetCredentials(const char* user, const char* pass) {
  strncpy(gUser, user && user[0] ? user : "", sizeof(gUser) - 1);
  gUser[sizeof(gUser) - 1] = 0;
  strncpy(gPass, pass ? pass : "", sizeof(gPass) - 1);
  gPass[sizeof(gPass) - 1] = 0;
}

void mqttSetInfo(const MqttInfo& info) { gInfo = info; }

void mqttSetHooks(SetpointFn onSetpoint, CommandFn onCommand, void* ctx) {
  gOnSetpoint = onSetpoint;
  gOnCommand  = onCommand;
  gHookCtx    = ctx;
}

bool mqttConfigured() { return gState != kOff; }
bool mqttConnected()  { return gState == kUp && gSock.connected(); }

void mqttRequestPublish() { gWantPublish = true; }

void mqttPoll(const Snapshot* snap, bool newFrame) {
  if (gState == kOff) return;

  // Wi-Fi comes and goes on its own; a socket cannot survive that, and reconnecting before the
  // station is up only wastes attempts.
  if (!stationUp()) {
    if (gState != kIdle) drop("wifi down");
    return;
  }

  const uint32_t now = millis();

  if (gState == kIdle) {
    if (now - gLastAttempt >= gBackoffMs) beginConnect();
    return;
  }

  if (gState == kWaitConnack) {
    drainSocket();
    if (gState == kWaitConnack && now - gLastAttempt > 4000) drop("no CONNACK in 4 s");
    return;
  }

  if (!gSock.connected()) { drop("socket closed"); return; }

  drainSocket();
  if (gState != kUp) return;

  // A socket can stay "connected" for a long time after the broker stopped answering, so
  // silence is treated as failure: 90 s is three keepalive intervals and far longer than the
  // 4 s A0 cadence, which means nothing here fired for a reason unrelated to the broker.
  if (now - gLastRx > 90000) { drop("no traffic in 90 s"); return; }

  // Keepalive at 25 s against a 60 s interval: one lost PINGRESP is forgiven, a dead broker is
  // noticed well inside the interval the broker itself would use to declare us gone.
  if (now - gLastPing > 25000) {
    const size_t n = wire::buildPingreq(gTx, sizeof(gTx));
    if (!n || gSock.write(gTx, n) != n) { drop("ping write failed"); return; }
    gLastPing = now;
  }

  const bool heartbeat = now - gLastPublish > 60000;
  if (snap && (newFrame || gWantPublish || heartbeat)) {
    char payload[384];
    const size_t len = buildStateJson(payload, sizeof(payload), *snap);
    if (!len) { drop("state payload does not fit"); return; }
    if (!publish("state", payload, len, true)) { drop("state write failed"); return; }
    gLastPublish = now;
    gWantPublish = false;
  }
}

void mqttStatusText(char* out, size_t cap) {
  if (!out || !cap) return;
  switch (gState) {
    case kOff:
      snprintf(out, cap, "off (%s)", gWhy);
      break;
    case kIdle:
      snprintf(out, cap, "retry in %lus (%s:%u, last: %s)",
               (unsigned long)(gBackoffMs / 1000), gHost, (unsigned)gPort, gWhy);
      break;
    case kWaitConnack:
      snprintf(out, cap, "connecting to %s:%u", gHost, (unsigned)gPort);
      break;
    case kUp:
      snprintf(out, cap, "up %s:%u as %s", gHost, (unsigned)gPort, gDeviceId);
      break;
    default:
      snprintf(out, cap, "unknown");
      break;
  }
}

}  // namespace net
