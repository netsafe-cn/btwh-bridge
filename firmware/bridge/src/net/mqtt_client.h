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
// a consumer can tell "no data" from "heater is cold".
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
constexpr const char* kFirmwareVersion = "2.2.0-mqtt";

// Slow-changing metadata, published retained on every connect. Kept out of the state payload
// so the 4-second message stays small.
struct MqttInfo {
  const char* heater;      // BLE address as configured, e.g. 5c:f8:21:eb:6f:5f
  const char* pairState;   // "unpaired" | "paired" | "reconnecting"
  const char* hardware;    // board family, e.g. "esp32s3"
};

// A consumer asked for a new setpoint. The callback returns nothing: the echo that confirms it
// is the next A0 frame, not an optimistic guess.
typedef void (*SetpointFn)(void* ctx, int celsius);

// A consumer asked for an operational command: "refresh", "reboot", "unpair".
typedef void (*CommandFn)(void* ctx, const char* verb);

// "host" or "host:port". Empty or malformed returns false and the client stays off.
bool parseBroker(const char* text, char* host, size_t hostCap, uint16_t* port);

// "btwh_" + the last six hex digits of the BLE address, lower case. Stable across reflashes,
// which is what keeps Home Assistant entities alive after an update.
void deviceIdFromAddress(const char* address, char* out, size_t cap);

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

// Forces the next poll to publish (the `refresh` command; also the console).
void mqttRequestPublish();

// One line for the console `status` output.
void mqttStatusText(char* out, size_t cap);

}  // namespace net

#endif  // BTWH_NET_MQTT_CLIENT_H
