// MQTT client: the bridge's only outbound path.
//
// Layer rule (ADR-008): this is net/'s Arduino-facing edge, beside station.cpp and portal.cpp.
// It knows sockets and topics; it never sees a frame byte (app/ hands it a Snapshot) and it
// never parses JSON (homeassistant does that at the other end).
//
// Shape of the loop, and why it is a poll rather than tasks:
//   * the BLE link lives in the NimBLE task, so a 2-3 s blocking connect here cannot stall the
//     A0 stream - the callback still fills app's state while this waits
//   * one connection, one backoff schedule, one publish cadence: all of it readable in
//     mqttPoll(), which is worth more than concurrency on a single-core-ish workload
//
// Availability is a retained will: if the board dies the broker publishes "offline" for it, so
// a consumer can tell "no data" from "heater is cold". That is liveness of *this process*.
// Freshness of the readings is the separate retained `link` topic, driven by setLinkAvailable()
// - a bridge that is up but no longer hearing the heater is online and stale at the same time,
// and the contract says both.
#pragma once
// Macro guard as well as #pragma once: arduino-cli compiles a copy of src/ under
// <build>/sketch/, so a header can be reached through two paths.
#ifndef BTWH_NET_MQTT_CLIENT_H
#define BTWH_NET_MQTT_CLIENT_H

#include <stddef.h>
#include <stdint.h>

#include "net/state_json.h"

namespace net {

// Bumped when the published surface changes; the panel and HA users see it in attributes.
// 2.3.0: attributes became HA diagnostic entities (8), rssi published, last_disconnect added.
// 2.5.1: device id terminated properly; the client id no longer carries stack garbage.
// 2.6.0: inbound parsing moved to net::mqtt_rules with a host-testable suite; three parser
//       edges fixed along the way.
// 2.6.1: golden CONNECT bytes pinned (bench-verified against a real Mosquitto), the backoff
//       schedule extracted to rules::nextBackoffMs, provisioning validation under test.
// 2.7.0: OTA - the ota topic accepts a http URL, net/ota flashes the inactive slot and the
//       board reboots into it. URL grammar host-tested in rules; live-only like commands.
// 2.7.1: the same OTA image used as the OTA *test payload*: shipped over the air from the
//       bench server to prove the path end to end (serial banner + attrs both move).
// 2.8.0: the Home Assistant update entity. The retained manifest topics update/latest and
//       update/url cache on arrival (rules::parseManifestVersion for the version), update/state
//       publishes the entity's JSON, and payload_install "install" on the ota topic flashes the
//       cached URL - the same flasher, the same grammar, one path from bench and UI alike.
// 2.9.0: freshness split from liveness. The retained link topic goes offline after 90 s without
//       an A0 frame, the cached snapshot is dropped instead of republished by the heartbeat, and
//       discovery binds state-fed entities to both availability signals. Same topics, one added;
//       the contract stays version 1 because nothing old changed shape.
constexpr const char* kFirmwareVersion = "2.9.0-ota4";

// Slow-changing metadata, published retained on every connect and refreshed on the
// attributes heartbeat. Kept out of the state payload so the 4-second message stays small.
struct MqttInfo {
  const char* heater;      // BLE address as configured, e.g. 5c:f8:21:eb:6f:5f
  const char* pairState;   // "unpaired" | "paired" | "reconnecting"
  const char* hardware;    // board family, e.g. "esp32s3"
  int         rssi;        // BLE link, dBm; 0 = link down (a real reading is negative)
};

// A consumer asked for a new setpoint. The callback returns nothing: the echo that confirms it
// is the next A0 frame, not an optimistic guess.
typedef void (*SetpointFn)(void* ctx, int celsius);

// A consumer asked for an operational command: "refresh", "reboot", "unpair".
typedef void (*CommandFn)(void* ctx, const char* verb);

// The broker-address grammar and the inbound payload parsers live in mqtt_rules.h next to
// their self test: they are pure functions, and the rules layer is the part of net/ that runs
// in the host self test.

void mqttBegin(const char* broker, const char* deviceId);
// Broker account. Called right after every mqttBegin(); empty strings mean anonymous.
// mqttBegin() does not clear these - clearing is this function with ("", "").
void mqttSetCredentials(const char* user, const char* pass);
void mqttSetInfo(const MqttInfo& info);
void mqttSetHooks(SetpointFn onSetpoint, CommandFn onCommand, void* ctx);

bool mqttConfigured();     // a broker address was given
bool mqttConnected();      // CONNACK accepted and the socket is up

// Call from loop(). Connects or reconnects on the backoff schedule, services keepalive and
// inbound commands, and publishes `snap` when newFrame is set or a heartbeat is due.
// `snap` may be null before the first A0 frame; nothing is published then.
void mqttPoll(const Snapshot* snap, bool newFrame);

// Reports whether the heater data is fresh (a frame arrived inside the contract's stale_after
// window). Publishes the retained `link` topic on change, and once more after every connect so
// a consumer that restarts learns the current freshness instead of guessing. Cheap to call
// every loop iteration: it returns immediately when nothing changed.
void setLinkAvailable(bool fresh);

// Forces the next poll to publish (the `refresh` command; also the console).
void mqttRequestPublish();

// One line for the console `status` output.
void mqttStatusText(char* out, size_t cap);

}  // namespace net

#endif  // BTWH_NET_MQTT_CLIENT_H
