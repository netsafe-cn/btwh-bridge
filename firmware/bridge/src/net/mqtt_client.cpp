#include "net/mqtt_client.h"

#include <Arduino.h>
#include <WiFi.h>
#include <stdio.h>
#include <string.h>

#include "net/discovery.h"
#include "net/mqtt_contract.h"
#include "net/mqtt_rules.h"
#include "net/mqtt_wire.h"
#include "net/ota.h"
#include "net/station.h"

namespace net {
namespace {

namespace wire = mqtt;

// Transaction buffer. The largest thing ever built is a discovery payload (~470 bytes) plus a
// topic and the fixed header. The water_heater discovery payload is the largest publish at
// ~940 bytes, so 1152 leaves room without a heap allocation per publish.
constexpr size_t kTxCap = 1152;

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

MqttInfo   gInfo{"-", "unpaired", "esp32s3", 0};
SetpointFn gOnSetpoint = nullptr;
CommandFn  gOnCommand  = nullptr;
void*      gHookCtx    = nullptr;

Link     gState       = kOff;
uint32_t gLastAttempt = 0;
uint32_t gBackoffMs   = 2000;
uint32_t gLastPing    = 0;
uint32_t gLastPublish = 0;
uint32_t gLastAttrs   = 0;
uint32_t gLastRx      = 0;
uint16_t gPacketId    = 1;
bool     gWantPublish = false;
// Copy, not a pointer: the reason is often a caller's stack buffer ("broker refused: ..."),
// and `status` reads this minutes later.
char     gWhy[64] = "not started";
// When the broker link last dropped, epoch seconds; 0 = never since boot. Published in the
// attributes payload so a remote user can see a flapping link while it happens to be up.
int64_t  gLastDropAt = 0;
// The retained manifest (update/latest + update/url), re-delivered by the broker on every
// subscribe. RAM only: losing it across a reboot costs one retained replay, which is the
// whole point of publishing the manifest retained. Empty latest means no manifest seen and
// the update entity honestly reports 'unknown' for latest_version.
char     gLatestVersion[32] = "";
char     gManifestUrl[128]  = "";

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
  // The reasons are this file's own literals (no quotes or backslashes), so the JSON needs
  // no escaping; if a reason ever grows punctuation, quote it at the drop() call site.
  char json[340];
  snprintf(json, sizeof(json),
           "{\"fw\":\"%s\",\"hw\":\"%s\",\"heater\":\"%s\",\"pair_state\":\"%s\","
           "\"uptime\":%lu,\"rssi\":%d,\"last_disconnect\":\"%s\",\"last_disconnect_at\":%lld}",
           kFirmwareVersion, gInfo.hardware ? gInfo.hardware : "esp32s3",
           gInfo.heater ? gInfo.heater : "-", gInfo.pairState ? gInfo.pairState : "unpaired",
           (unsigned long)(millis() / 1000), gInfo.rssi, gWhy,
           (long long)gLastDropAt);
  publishText("attributes", json, true);
}

void publishUpdateState() {
  // The update entity's state: installed comes from this binary, latest from the manifest.
  // latest_version is omitted until a manifest has been seen - Home Assistant then shows
  // 'unknown' instead of a fake 'up to date'. Version strings are this file's literals and
  // rules-validated manifest bytes (printable, no quotes), so no JSON escaping is needed.
  if (gLatestVersion[0]) {
    char json[112];
    snprintf(json, sizeof(json),
             "{\"installed_version\":\"%s\",\"latest_version\":\"%s\"}",
             kFirmwareVersion, gLatestVersion);
    publishText("update/state", json, true);
  } else {
    char json[48];
    snprintf(json, sizeof(json), "{\"installed_version\":\"%s\"}", kFirmwareVersion);
    publishText("update/state", json, true);
  }
}

// Discovery is retained and idempotent, so it is republished on every connect: that also
// repairs an entity deleted from Home Assistant without touching the bridge.
void publishDiscovery() {
  static char topic[96];
  static char payload[1024];
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
  gLastAttrs   = millis();      // the connect just published them
  gWantPublish = true;

  publishText("availability", "online", true);
  publishAttributes();
  publishUpdateState();
  publishDiscovery();
  subscribe("set/temperature");
  subscribe("cmd");
  subscribe("ota");
  subscribe("update/latest");
  subscribe("update/url");

  Serial.printf("[MQTT] + connected to %s:%u as %s\n", gHost, (unsigned)gPort, gDeviceId);
}

// Drops the socket and schedules the next attempt. `why` is printed once; the backoff doubles
// up to a minute, which is what keeps a wrong password from hammering the broker.
void drop(const char* why) {
  strncpy(gWhy, why ? why : "", sizeof(gWhy) - 1);
  gWhy[sizeof(gWhy) - 1] = 0;
  gLastDropAt = nowEpoch();
  if (gState == kUp && gSock.connected()) {
    publishText("availability", "offline", true);   // explicit goodbye; the will covers crashes
    const size_t n = wire::buildDisconnect(gTx, sizeof(gTx));
    if (n) gSock.write(gTx, n);
  }
  gSock.stop();
  gRxLen       = 0;
  gState       = kIdle;
  gLastAttempt = millis();
  gBackoffMs   = rules::nextBackoffMs(gBackoffMs);
  Serial.printf("[MQTT] x %s (retry in %lus)\n", why, (unsigned long)(gBackoffMs / 1000));
}

void beginConnect() {
  gLastAttempt = millis();
  gSock.stop();
  gRxLen = 0;

  if (!gSock.connect(gHost, gPort, 3000)) {
    strncpy(gWhy, "tcp connect failed", sizeof(gWhy) - 1);
    gWhy[sizeof(gWhy) - 1] = 0;
    gLastDropAt = nowEpoch();
    gState = kIdle;
    gBackoffMs = rules::nextBackoffMs(gBackoffMs);
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

  int celsius = 0;
  const rules::Setpoint r = rules::parseSetpoint(text, &celsius);
  if (r != rules::Setpoint::kOk) {
    // The reason is printed with the raw text on purpose: the categories are for the code,
    // the log is for the human who typed the command (or the automation that built it).
    const char* why = r == rules::Setpoint::kNotANumber ? "not a number"
                    : r == rules::Setpoint::kNotWhole   ? "fractional degrees"
                                                        : "out of range";
    Serial.printf("[MQTT] setpoint ignored: [%s] (%s; whole degrees %d..%d)\n",
                  text, why, contract::kSetpointMin, contract::kSetpointMax);
    return;
  }
  Serial.printf("[MQTT] setpoint -> %d C\n", celsius);
  if (gOnSetpoint) gOnSetpoint(gHookCtx, celsius);
}

void handleCommand(const wire::PublishInfo& in) {
  char text[48];
  const size_t n = in.payloadLen < sizeof(text) - 1 ? in.payloadLen : sizeof(text) - 1;
  memcpy(text, in.payload, n);
  text[n] = 0;

  const char* verb = rules::commandVerb(text);
  if (!verb) {
    Serial.printf("[MQTT] command ignored: [%s]\n", text);
    return;
  }
  Serial.printf("[MQTT] command: %s\n", verb);
  if (strcmp(verb, "refresh") == 0) gWantPublish = true;
  if (gOnCommand) gOnCommand(gHookCtx, verb);
}

void handleOta(const wire::PublishInfo& in) {
  char text[128];
  const size_t n = in.payloadLen < sizeof(text) - 1 ? in.payloadLen : sizeof(text) - 1;
  memcpy(text, in.payload, n);
  text[n] = 0;

  // Home Assistant's update entity sends the payload_install word ("install"); the bench
  // tool sends a full URL. One topic, two verbs, one flasher.
  char     host[32];
  uint16_t port = 0;
  char     path[64];
  if (strcmp(text, "install") == 0) {
    if (!gManifestUrl[0]) {
      Serial.println("[MQTT] ota install refused: no manifest URL seen on update/url");
      return;
    }
    Serial.println("[MQTT] ota: install (URL from update/url)");
    if (!rules::parseOtaUrl(gManifestUrl, host, sizeof(host), &port, path, sizeof(path))) {
      // The manifest URL was validated when it arrived; if this fires, the cache was
      // corrupted - say so rather than flash garbage.
      Serial.println("[MQTT] x cached manifest URL no longer parses; firmware unchanged");
      return;
    }
  } else if (!rules::parseOtaUrl(text, host, sizeof(host), &port, path, sizeof(path))) {
    Serial.printf("[MQTT] ota ignored: [%s] (http://host[:port]/path or 'install' only)\n", text);
    return;
  } else {
    Serial.printf("[MQTT] ota: %s:%u%s\n", host, (unsigned)port, path);
  }

  // install() blocks for the length of the download (budgeted inside); the MQTT session
  // is idle-but-open during that and the broker may drop it - that is fine, because the
  // only two exits are "validated, reboot now" and "failed, the session recovers on the
  // next poll like any drop".
  if (ota::install(host, port, path)) {
    // Flush before the restart so the last line - the one a debugging human needs - is
    // not still in the UART FIFO when the chip resets.
    Serial.flush();
    delay(100);
    ESP.restart();
  }
  Serial.println("[MQTT] ota failed; firmware unchanged");
}

// The manifest topics are deliberately NOT live-only: they are retained, the broker
// replays them on every (re)subscribe, and replaying state is idempotent - the exact
// opposite of the ota topic one level up, where a retained replay would reflash the chip.
void handleManifestLatest(const wire::PublishInfo& in) {
  char text[48];
  const size_t n = in.payloadLen < sizeof(text) - 1 ? in.payloadLen : sizeof(text) - 1;
  memcpy(text, in.payload, n);
  text[n] = 0;

  char version[32];
  if (!rules::parseManifestVersion(text, version, sizeof(version))) {
    Serial.println("[MQTT] update/latest ignored (empty or not printable text)");
    return;
  }
  if (strcmp(version, gLatestVersion) != 0) {
    snprintf(gLatestVersion, sizeof(gLatestVersion), "%s", version);
    Serial.printf("[MQTT] manifest: latest %s\n", gLatestVersion);
  }
  publishUpdateState();
}

void handleManifestUrl(const wire::PublishInfo& in) {
  // Validate on arrival with the same parser the flash path uses; store only the raw
  // bytes. Revalidate at install time - the cache is two-way consistent then.
  char text[128];
  const size_t n = in.payloadLen < sizeof(text) - 1 ? in.payloadLen : sizeof(text) - 1;
  memcpy(text, in.payload, n);
  text[n] = 0;

  char     host[32];
  uint16_t port = 0;
  char     path[64];
  if (text[0] && !rules::parseOtaUrl(text, host, sizeof(host), &port, path, sizeof(path))) {
    Serial.println("[MQTT] update/url ignored (http://host[:port]/path only)");
    return;
  }
  // An empty payload is a publish-clear (mqtt_watch publishes retained '' to reset);
  // accept it as "no manifest".
  snprintf(gManifestUrl, sizeof(gManifestUrl), "%s", text);
  Serial.printf("[MQTT] manifest: url %s\n", text[0] ? text : "(cleared)");
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
        // Commands are live-only (rules::commandDeliveryAllowed): a retained "reboot" would
        // replay on every reconnect and reboot the board forever; the retained flag on a
        // delivery says exactly that happened. set/temperature is deliberately exempt -
        // replaying the last setpoint is idempotent.
        if (!rules::commandDeliveryAllowed(in.retained)) {
          Serial.println("[MQTT] retained command ignored (commands are live-only)");
          return;
        }
        handleCommand(in);
        return;
      }
      makeTopic(topic, sizeof(topic), "ota");
      if (wire::topicIs(in, topic)) {
        // OTA is live-only for a sharper reason than reboot: a retained URL would reflash
        // the board on every reconnect - a boot loop that overwrites the firmware each
        // time. The same one-line guard, the same one-line cost for forgetting it.
        if (!rules::commandDeliveryAllowed(in.retained)) {
          Serial.println("[MQTT] retained ota ignored (ota is live-only)");
          return;
        }
        handleOta(in);
        return;
      }
      // The manifest topics are the ota topic's mirror image: retained is the point.
      // Everything above refuses a retained delivery; these two cache whatever they see,
      // because state replayed is state restored, not an action repeated.
      makeTopic(topic, sizeof(topic), "update/latest");
      if (wire::topicIs(in, topic)) { handleManifestLatest(in); return; }
      makeTopic(topic, sizeof(topic), "update/url");
      if (wire::topicIs(in, topic)) { handleManifestUrl(in); return; }
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

void mqttBegin(const char* broker, const char* deviceId) {
  uint16_t port = 1883;
  // The broker-address grammar (host, host:port, validation) lives in rules::parseBroker -
  // host-testable, and shared by the console, the portal and boot by construction.
  if (!rules::parseBroker(broker, gHost, sizeof(gHost), &port)) {
    gState = kOff;
    strncpy(gWhy, "no broker configured", sizeof(gWhy) - 1);
    gWhy[sizeof(gWhy) - 1] = 0;
    return;
  }
  gPort = port;
  strncpy(gDeviceId, deviceId && deviceId[0] ? deviceId : "btwh_000000", sizeof(gDeviceId) - 1);
  gDeviceId[sizeof(gDeviceId) - 1] = 0;

  // Credentials are deliberately NOT cleared here. begin() is called whenever the broker
  // address changes (console, portal, boot), and the caller follows it with
  // mqttSetCredentials() in the same breath - resetting here would silently turn an
  // authenticated broker into a refused anonymous connection. Clearing is explicit:
  // mqttSetCredentials("", "").
  gBackoffMs = 2000;
  gState = kIdle;
  gLastAttempt = millis();
  Serial.printf("[MQTT] broker=%s:%u device=%s auth=%s\n", gHost, (unsigned)gPort, gDeviceId,
                gUser[0] ? gUser : "anonymous");
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

  uint32_t now = millis();

  if (gState == kIdle) {
    if (rules::elapsedMs(now, gLastAttempt) >= gBackoffMs) beginConnect();
    return;
  }

  if (gState == kWaitConnack) {
    drainSocket();
    if (gState == kWaitConnack && rules::elapsedMs(now, gLastAttempt) > 4000) drop("no CONNACK in 4 s");
    return;
  }

  if (!gSock.connected()) { drop("socket closed"); return; }

  drainSocket();
  if (gState != kUp) return;

  // Re-sample: drainSocket() may have just stamped gLastRx with a millis() newer than the
  // `now` sampled at the top of this call (a SUBACK landing inside this iteration). Every
  // elapsed below goes through rules::elapsedMs(later, earlier) - the argument order the
  // rules self test pins - but the order only helps if the later sample really is later.
  now = millis();

  // A socket can stay "connected" for a long time after the broker stopped answering, so
  // silence is treated as failure: 90 s is three keepalive intervals and far longer than the
  // 4 s A0 cadence, which means nothing here fired for a reason unrelated to the broker.
  if (rules::elapsedMs(now, gLastRx) > 90000) { drop("no traffic in 90 s"); return; }

  // Keepalive at 25 s against a 60 s interval: one lost PINGRESP is forgiven, a dead broker is
  // noticed well inside the interval the broker itself would use to declare us gone.
  if (rules::elapsedMs(now, gLastPing) > 25000) {
    const size_t n = wire::buildPingreq(gTx, sizeof(gTx));
    if (!n || gSock.write(gTx, n) != n) { drop("ping write failed"); return; }
    gLastPing = now;
  }

  const bool heartbeat = rules::elapsedMs(now, gLastPublish) > 60000;
  // The attributes heartbeat rides the same 60 s cadence as the state heartbeat: uptime and
  // RSSI must age at most a minute, or the diagnostics tell a remote user lies about a bridge
  // that is up but deaf. It does not wait for a snapshot - attributes exist before the first
  // A0 frame, which is exactly when the pairing state is worth reading.
  if (rules::elapsedMs(now, gLastAttrs) > 60000) {
    publishAttributes();
    // The update entity rides the same cadence: after an OTA reboot the installed_version
    // must move within a minute, or the entity lies about which firmware is running.
    publishUpdateState();
    gLastAttrs = now;
  }
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
