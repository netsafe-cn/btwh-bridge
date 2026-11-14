// MQTT 3.1.1 wire format - only the packets this bridge actually sends and receives.
//
// Layer rule (ADR-008): like proto/, this file must compile with nothing but the C standard
// library. It never touches a socket and never prints. That is what lets `selftest` check the
// exact bytes that will go on the wire without a broker in the room, and it is why the client
// in mqtt_client.cpp is short: framing is here, sockets are there.
//
// Scope, stated so nobody adds a feature by accident:
//   out  CONNECT (with will), PUBLISH (QoS 0), SUBSCRIBE, PINGREQ, DISCONNECT, PUBACK
//   in   CONNACK, PUBLISH (QoS 0/1), SUBACK, PINGRESP
// QoS 2, TLS, sessions with persistence and the newer MQTT 5 properties are all out. The
// contract in spec/mqtt.json fixes qos = 0, so QoS 1 is decoded only far enough to acknowledge
// a broker that upgrades us, not used for publishing.
#pragma once
// Macro guard as well as #pragma once: arduino-cli compiles a copy of src/ under
// <build>/sketch/, so a header can be reached through two paths.
#ifndef BTWH_NET_MQTT_WIRE_H
#define BTWH_NET_MQTT_WIRE_H

#include <stddef.h>
#include <stdint.h>

#include "net/selftest.h"

namespace net {
namespace mqtt {

// Largest packet decoded in one piece. A Home Assistant discovery payload is far smaller; the
// bound exists so the client's read buffer has a fixed size on an 8 KB task stack.
constexpr size_t kMaxPacket = 512;

enum PacketType : uint8_t {
  kTypeConnect    = 1,
  kTypeConnack    = 2,
  kTypePublish    = 3,
  kTypePuback     = 4,
  kTypeSubscribe  = 8,
  kTypeSuback     = 9,
  kTypePingreq    = 12,
  kTypePingresp   = 13,
  kTypeDisconnect = 14,
};

// CONNACK return codes, named after the spec so the log line needs no translation table.
enum ConnackCode : uint8_t {
  kAccepted          = 0x00,
  kBadProtocol       = 0x01,
  kIdRejected        = 0x02,
  kServerUnavailable = 0x03,
  kBadCredentials    = 0x04,
  kNotAuthorised     = 0x05,
};

struct ConnectOpts {
  const char* clientId;       // required; the broker must see a stable id
  const char* willTopic;      // nullptr = no will
  const char* willMessage;    // "offline"; retained, so a dead bridge reads as offline
  const char* username;       // nullptr or "" = anonymous
  const char* password;       // ignored when username is empty
  uint16_t    keepAliveSec;
  bool        cleanSession;
};

// A decoded fixed header plus a view of its body. `body` points into the caller's buffer, so
// the packet must be consumed before that buffer is refilled.
struct Packet {
  uint8_t        type;
  uint8_t        flags;
  const uint8_t* body;
  size_t         length;
};

// An inbound PUBLISH, unpacked. `topic` is not NUL-terminated - compare with topicIs().
struct PublishInfo {
  const char*    topic;
  size_t         topicLen;
  const uint8_t* payload;
  size_t         payloadLen;
  uint8_t        qos;
  uint16_t       packetId;   // 0 unless qos > 0
  bool           retained;
  bool           duplicate;
};

// ---------------------------------------------------------------- outbound
// Every builder returns the number of bytes written, or 0 when the buffer is too small. A 0
// means "do not send", never "sent a partial packet".
size_t buildConnect(uint8_t* out, size_t cap, const ConnectOpts& o);
size_t buildPublish(uint8_t* out, size_t cap, const char* topic,
                    const void* payload, size_t len, bool retain);
size_t buildSubscribe(uint8_t* out, size_t cap, uint16_t packetId,
                      const char* filter, uint8_t qos);
size_t buildPuback(uint8_t* out, size_t cap, uint16_t packetId);
size_t buildPingreq(uint8_t* out, size_t cap);
size_t buildDisconnect(uint8_t* out, size_t cap);

// ---------------------------------------------------------------- inbound
// Decodes one packet from the front of `buf`.
//   > 0  bytes consumed, `out` filled
//     0  incomplete, call again after reading more
//    -1  malformed (bad remaining length, truncated header); the connection should be dropped
int decode(const uint8_t* buf, size_t len, Packet* out, size_t* consumed);

// Size of the packet starting at `buf`, read from the fixed header alone. 0 = the header is still
// arriving, -1 = the length prefix is not a legal four-byte varint. Unlike decode() this does not
// care how large the packet is, which is the whole point: it lets the client throw away a packet
// it cannot hold and keep the stream in sync instead of dropping the connection. MQTT puts no
// upper bound on packet size, so a broker - or another integration publishing a kilobyte of JSON
// on a subscribed topic - can legitimately send one.
int packetLength(const uint8_t* buf, size_t len, size_t* total);

// Unpacks a PUBLISH body. False when the body is too short to hold the topic it claims.
bool parsePublish(const Packet& p, PublishInfo& out);

// Topic comparison without allocating: filters are compiled in, topics arrive unterminated.
bool topicIs(const PublishInfo& info, const char* expected);

// Human-readable CONNACK code, for the serial log only.
const char* connackText(uint8_t code);

// "btwh_" + the last six hex digits of a BLE address, lower case. Stable across reflashes,
// which is what keeps Home Assistant entities alive after an update. Lives here (not in
// mqtt_client.cpp) so the self test can pin it: the output is a client id and a topic
// fragment, and MQTT forbids control characters in both - an unterminated hex buffer once
// shipped stack garbage into a client id and every broker connection was refused.
void deviceIdFromAddress(const char* address, char* out, size_t cap);

// ---------------------------------------------------------------- self test
SelfTestResult runSelfTest(LogFn log, void* ctx);

}  // namespace mqtt
}  // namespace net

#endif  // BTWH_NET_MQTT_WIRE_H
