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
//       ota5: readings stop being retained. state and attributes are published live-only, so a
//       bridge that loses power leaves no reading behind on the broker; only status is retained
//       (availability, link, the update entities, discovery). Wire-compatible: same topics, same
//       payloads, one fewer flag.
//       ota6: the discovery payloads conform to the platform schema (ADR-016). device.sw_version
//       is generated from this very constant, rssi/last_disconnect_at publish null when unknown
//       instead of 0, and the transaction buffer grew to 1280 for the larger payload.
//       ota7: a 22nd entity (binary_sensor fault_active, device_class problem) and a subscription
//       to Home Assistant's birth topic, which republishes discovery and the live-only topics
//       when HA restarts. No topic, payload key or command changed shape; contract stays 1.
//       ota8: the last_disconnect_at label stops saying "epoch". Since ota6 the entity renders
//       ISO 8601 through timestamp_custom, so the old label described the wire value and lied
//       about the display. Label only: no topic, key, template or entity id moves.
//       ota9: the Wi-Fi fallback (ADR-018). After 5 min with no IP the setup AP comes back on
//       its own and closes after 10 idle minutes. No topic, payload key or command changed
//       shape; the contract stays 1. The setup AP also stopped turning modem sleep off, which
//       was aborting the driver whenever Bluetooth was also running.
//       ota10: /save stops rebooting the board. The form handler used to delete the WebServer
//       from inside its own callback and then return into it; the teardown is deferred to the
//       next poll tick. Behaviour and payloads unchanged.
//       ota11: the join failure gets a diagnosis. net/wifistat turns WiFi.status() and
//       WiFi.disconnectReason() into a sentence ("no network with that name is broadcasting
//       here", "the password was rejected"), a failed attempt prints both codes and the
//       sentence, a success prints ip/gw/mask/dns/rssi/channel/bssid, and the setup page shows
//       the same sentence. Diagnostics only: no topic, payload key or command changed shape.
constexpr const char* kFirmwareVersion = "2.9.0-ota11";

// Slow-changing metadata, published on every connect and refreshed on the attributes heartbeat.
// Live-only like state: uptime and RSSI are readings, not configuration.
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
// Deletes every retained document `deviceId` left at the broker: its discovery configs and the
// retained availability / link / OTA-state topics. Call it BEFORE mqttBegin() with a new id,
// which is what the `addr` console command does. Without it the old id stays retained and HA
// grows a second device that never updates again (measured 2026-11-13: aa:bb:cc:dd:ee:ff left
// 22 entities behind beside the real heater's). Returns false when the client is not connected
// - nothing was published in that state, so there is nothing to delete.
bool mqttForgetDevice(const char* deviceId);
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
