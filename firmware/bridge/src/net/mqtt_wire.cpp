#include "net/mqtt_wire.h"

#include <ctype.h>
#include <stdio.h>
#include <string.h>

#include "net/mqtt_contract.h"

namespace net {
namespace mqtt {
namespace {

// ---------------------------------------------------------------- primitives

// Big-endian 16-bit, the only integer encoding MQTT uses outside the length prefix.
void putU16(uint8_t* p, uint16_t v) {
  p[0] = (uint8_t)(v >> 8);
  p[1] = (uint8_t)(v & 0xFF);
}

uint16_t getU16(const uint8_t* p) { return (uint16_t)((p[0] << 8) | p[1]); }

// Length-prefixed UTF-8 string. Refuses rather than truncates: a truncated topic would
// publish to the wrong place, which is worse than not publishing.
bool putStr(uint8_t* out, size_t cap, size_t& len, const char* s) {
  size_t n = s ? strlen(s) : 0;
  if (n > 0xFFFF || len + 2 + n > cap) return false;
  putU16(out + len, (uint16_t)n);
  len += 2;
  if (n) memcpy(out + len, s, n);
  len += n;
  return true;
}

// Remaining-length varint: 7 bits per byte, high bit = "another byte follows".
size_t remainingLenBytes(size_t value) {
  size_t n = 1;
  while (value >= 128) { value /= 128; n++; }
  return n;
}

size_t putRemaining(uint8_t* out, size_t value) {
  size_t i = 0;
  do {
    uint8_t d = (uint8_t)(value % 128);
    value /= 128;
    if (value) d |= 0x80;
    out[i++] = d;
  } while (value);
  return i;
}

// Writes the fixed header and returns its size, or 0 when there is no room.
size_t putHeader(uint8_t* out, size_t cap, uint8_t first, size_t bodyLen) {
  size_t need = 1 + remainingLenBytes(bodyLen) + bodyLen;
  if (need > cap) return 0;
  out[0] = first;
  return 1 + putRemaining(out + 1, bodyLen);
}

// ---------------------------------------------------------------- self test

struct Ctx {
  LogFn log;
  void* user;
  int   total;
  int   failed;
  char  line[200];
};

void check(Ctx& c, bool ok, const char* what, const char* detail) {
  c.total++;
  if (ok) return;
  c.failed++;
  if (!c.log) return;
  snprintf(c.line, sizeof(c.line), "FAIL %s%s%s", what, detail ? ": " : "",
           detail ? detail : "");
  c.log(c.user, c.line);
}

// Renders a buffer as hex so a failure says what was actually built.
void hexLine(char* out, size_t cap, const uint8_t* p, size_t n) {
  size_t at = 0;
  out[0] = 0;
  for (size_t i = 0; i < n && at + 4 < cap; i++) {
    int w = snprintf(out + at, cap - at, "%02X ", p[i]);
    if (w <= 0) break;
    at += (size_t)w;
  }
}

void checkBytes(Ctx& c, const char* what, const uint8_t* got, size_t gotLen,
                const uint8_t* want, size_t wantLen) {
  bool ok = gotLen == wantLen && (gotLen == 0 || memcmp(got, want, gotLen) == 0);
  if (!ok) {
    char detail[120];
    hexLine(detail, sizeof(detail), got, gotLen);
    check(c, false, what, detail);
  } else {
    check(c, true, what, nullptr);
  }
}

}  // namespace

// ---------------------------------------------------------------- outbound

size_t buildConnect(uint8_t* out, size_t cap, const ConnectOpts& o) {
  if (!out || !o.clientId) return 0;

  const bool hasWill = o.willTopic && o.willTopic[0];
  const bool hasUser = o.username && o.username[0];
  const size_t cidLen = strlen(o.clientId);
  const size_t willLen = hasWill ? 2 + strlen(o.willTopic) + 2 + strlen(o.willMessage ? o.willMessage : "") : 0;
  const size_t userLen = hasUser ? 2 + strlen(o.username) : 0;
  const size_t passLen = hasUser ? 2 + (o.password ? strlen(o.password) : 0) : 0;

  // Variable header: "MQTT" (6) + level (1) + flags (1) + keepalive (2).
  const size_t bodyLen = 6 + 1 + 1 + 2 + 2 + cidLen + willLen + userLen + passLen;
  const size_t header = putHeader(out, cap, 0x10, bodyLen);
  if (!header) return 0;

  size_t len = header;

  // Variable header, written at the start of the body.
  putU16(out + len, 4);            // UTF-8 strings in CONNECT start with a 2-byte length
  out[len + 2] = 'M'; out[len + 3] = 'Q'; out[len + 4] = 'T'; out[len + 5] = 'T';
  len += 6;
  out[len++] = 4;                 // protocol level: 3.1.1

  uint8_t flags = 0;
  if (o.cleanSession) flags |= 0x02;
  if (hasWill) {
    flags |= 0x04;                // will flag
    flags |= 0x20;                // will retain: a dead bridge must read as offline
    // will QoS stays 0 (bits 3-4 clear) - the contract fixes qos = 0
  }
  if (hasUser) { flags |= 0x80; flags |= 0x40; }   // username implies the password field
  out[len++] = flags;
  putU16(out + len, o.keepAliveSec);
  len += 2;

  size_t at = len;
  if (!putStr(out, cap, at, o.clientId)) return 0;
  if (hasWill) {
    if (!putStr(out, cap, at, o.willTopic)) return 0;
    if (!putStr(out, cap, at, o.willMessage ? o.willMessage : "")) return 0;
  }
  if (hasUser) {
    if (!putStr(out, cap, at, o.username)) return 0;
    if (!putStr(out, cap, at, o.password ? o.password : "")) return 0;
  }
  return at;
}

size_t buildPublish(uint8_t* out, size_t cap, const char* topic,
                    const void* payload, size_t len, bool retain) {
  if (!out || !topic || (len && !payload)) return 0;
  const size_t topicLen = strlen(topic);
  const size_t bodyLen = 2 + topicLen + len;
  const size_t header = putHeader(out, cap, (uint8_t)(0x30 | (retain ? 0x01 : 0x00)), bodyLen);
  if (!header) return 0;

  size_t at = header;
  putU16(out + at, (uint16_t)topicLen);
  at += 2;
  memcpy(out + at, topic, topicLen);
  at += topicLen;
  if (len) memcpy(out + at, payload, len);
  return at + len;
}

size_t buildSubscribe(uint8_t* out, size_t cap, uint16_t packetId,
                      const char* filter, uint8_t qos) {
  if (!out || !filter) return 0;
  const size_t filterLen = strlen(filter);
  const size_t bodyLen = 2 + 2 + filterLen + 1;
  const size_t header = putHeader(out, cap, 0x82, bodyLen);   // 0x82: SUBSCRIBE needs bit 1
  if (!header) return 0;

  size_t at = header;
  putU16(out + at, packetId);
  at += 2;
  putU16(out + at, (uint16_t)filterLen);
  at += 2;
  memcpy(out + at, filter, filterLen);
  at += filterLen;
  out[at++] = (uint8_t)(qos & 0x03);
  return at;
}

size_t buildPuback(uint8_t* out, size_t cap, uint16_t packetId) {
  if (!out || cap < 4) return 0;
  out[0] = 0x40;
  out[1] = 0x02;
  putU16(out + 2, packetId);
  return 4;
}

size_t buildPingreq(uint8_t* out, size_t cap) {
  if (!out || cap < 2) return 0;
  out[0] = 0xC0;
  out[1] = 0x00;
  return 2;
}

size_t buildDisconnect(uint8_t* out, size_t cap) {
  if (!out || cap < 2) return 0;
  out[0] = 0xE0;
  out[1] = 0x00;
  return 2;
}

void deviceIdFromAddress(const char* address, char* out, size_t cap) {
  if (!out || cap == 0) return;
  out[0] = 0;
  if (!address) return;

  // Only hex digits are collected, and the buffer is terminated: this string becomes an MQTT
  // client id and every topic the bridge publishes, and MQTT rejects control characters in
  // both. An unterminated hex[] once leaked stack garbage (\x10) into the client id - the
  // host self test stayed green (its stack happened to be zero) while every real broker
  // refused the connection as "client identifier not valid".
  char hex[13];
  size_t n = 0;
  for (const char* p = address; *p && n < sizeof(hex) - 1; p++) {
    if (isxdigit((unsigned char)*p)) hex[n++] = (char)tolower((unsigned char)*p);
  }
  hex[n] = 0;
  const char* tail = n >= 6 ? hex + n - 6 : "000000";
  snprintf(out, cap, "%s_%s", contract::kBaseTopic, tail);
}

// ---------------------------------------------------------------- inbound

int decode(const uint8_t* buf, size_t len, Packet* out, size_t* consumed) {
  if (!buf || !out || len < 2) return 0;

  size_t rem = 0;
  size_t mul = 1;
  size_t i = 1;
  uint8_t d = 0;
  do {
    if (i >= len) return 0;                 // the length prefix itself is incomplete
    d = buf[i++];
    rem += (size_t)(d & 0x7F) * mul;
    mul *= 128;
    // Four bytes is the protocol maximum. Anything longer is a desynchronised stream, not a
    // big packet, and the only safe action is to drop the connection.
    if (mul > 128UL * 128 * 128) return -1;
  } while (d & 0x80);

  if (rem > kMaxPacket) return -1;
  if (len < i + rem) return 0;              // body still arriving

  out->type = (uint8_t)(buf[0] >> 4);
  out->flags = (uint8_t)(buf[0] & 0x0F);
  out->body = buf + i;
  out->length = rem;
  if (consumed) *consumed = i + rem;
  return (int)(i + rem);
}

bool parsePublish(const Packet& p, PublishInfo& out) {
  if (p.type != kTypePublish || p.length < 2) return false;
  const size_t topicLen = getU16(p.body);
  size_t off = 2 + topicLen;
  if (off > p.length) return false;

  out.topic      = (const char*)(p.body + 2);
  out.topicLen   = topicLen;
  out.qos        = (uint8_t)((p.flags >> 1) & 0x03);
  out.retained   = (p.flags & 0x01) != 0;
  out.duplicate  = (p.flags & 0x08) != 0;
  out.packetId   = 0;
  if (out.qos > 0) {
    if (off + 2 > p.length) return false;
    out.packetId = getU16(p.body + off);
    off += 2;
  }
  out.payload    = p.body + off;
  out.payloadLen = p.length - off;
  return true;
}

bool topicIs(const PublishInfo& info, const char* expected) {
  if (!expected) return false;
  const size_t n = strlen(expected);
  return n == info.topicLen && memcmp(info.topic, expected, n) == 0;
}

const char* connackText(uint8_t code) {
  switch (code) {
    case kAccepted:          return "accepted";
    case kBadProtocol:       return "protocol version refused";
    case kIdRejected:        return "client id rejected";
    case kServerUnavailable: return "broker unavailable";
    case kBadCredentials:    return "bad username or password";
    case kNotAuthorised:     return "not authorised";
    default:                 return "unknown return code";
  }
}

// ---------------------------------------------------------------- self test
//
// These are the checks that would otherwise need a broker: the bytes of a CONNECT, the exact
// splitting of a packet that arrives one byte at a time, and the refusal paths. All of it runs
// on the board in `selftest` with no network.
SelfTestResult runSelfTest(LogFn log, void* ctx) {
  Ctx c{log, ctx, 0, 0, {0}};
  uint8_t buf[kMaxPacket];
  Packet  pkt{};
  size_t  used = 0;

  // 1. CONNECT without a will: header 0x10, flags 0x02 (clean session), keepalive 60.
  ConnectOpts basic{};
  basic.clientId      = "btwh_eb6f5f";
  basic.keepAliveSec  = 60;
  basic.cleanSession  = true;
  size_t n = buildConnect(buf, sizeof(buf), basic);
  check(c, n > 0, "connect is built", nullptr);
  check(c, n > 12 && buf[0] == 0x10, "connect header byte", nullptr);
  check(c, n > 12 && buf[9] == 0x02, "connect flags: clean session only", nullptr);
  check(c, n > 12 && buf[10] == 0x00 && buf[11] == 0x3C, "connect keepalive 60", nullptr);

  // 2. CONNECT with a retained will: flags gain will (0x04) and will-retain (0x20) -> 0x26.
  ConnectOpts withWill = basic;
  withWill.willTopic   = "btwh/btwh_eb6f5f/availability";
  withWill.willMessage = "offline";
  n = buildConnect(buf, sizeof(buf), withWill);
  check(c, n > 0, "connect with will is built", nullptr);
  check(c, n > 12 && buf[9] == 0x26, "connect flags: clean + will + retain", nullptr);

  // 3. CONNECT with credentials: username sets both bits (0x02 | 0x80 | 0x40) = 0xC2.
  ConnectOpts withAuth = basic;
  withAuth.username = "bridge";
  withAuth.password = "secret";
  n = buildConnect(buf, sizeof(buf), withAuth);
  check(c, n > 12 && buf[9] == (uint8_t)0xC2, "connect flags: credentials", nullptr);

  // 4. A buffer one byte short must refuse, not truncate.
  check(c, buildConnect(buf, 4, basic) == 0, "connect refuses a small buffer", nullptr);

  // 5. PUBLISH with retain: header 0x31, topic length big-endian, payload verbatim.
  const char* topic = "btwh/btwh_eb6f5f/state";
  const char* payload = "{\"setpoint\":42}";
  n = buildPublish(buf, sizeof(buf), topic, payload, strlen(payload), true);
  check(c, n > 0 && buf[0] == 0x31, "publish header: retain bit", nullptr);
  check(c, n == 1 + remainingLenBytes(strlen(payload) + 2 + strlen(topic)) + 2 + strlen(topic) + strlen(payload),
        "publish length is exact", nullptr);

  // 6. The same bytes decode back, field by field.
  int got = decode(buf, n, &pkt, &used);
  check(c, got > 0 && used == n, "decode consumes the whole publish", nullptr);
  check(c, pkt.type == kTypePublish, "decode type is publish", nullptr);
  PublishInfo info{};
  check(c, parsePublish(pkt, info), "parsePublish accepts the body", nullptr);
  check(c, topicIs(info, topic), "topic round-trips", nullptr);
  check(c, info.payloadLen == strlen(payload) &&
           memcmp(info.payload, payload, info.payloadLen) == 0, "payload round-trips", nullptr);
  check(c, info.retained && info.qos == 0, "retain set, qos 0", nullptr);

  // 7. Fragmented arrival: one byte at a time must return 0 until the last byte.
  bool premature = false;
  for (size_t i = 0; i + 1 < n; i++) {
    if (decode(buf, i + 1, &pkt, &used) != 0) premature = true;
  }
  check(c, !premature, "a partial packet never decodes early", nullptr);
  check(c, decode(buf, n, &pkt, &used) == (int)n, "the full packet decodes", nullptr);

  // 8. A two-byte remaining length (payload > 127 bytes) is the case a naive one-byte parser
  //    gets wrong, and discovery payloads are in that range.
  static uint8_t big[300];
  memset(big, 'x', sizeof(big));
  n = buildPublish(buf, sizeof(buf), "homeassistant/sensor/x/config", big, 200, true);
  check(c, n > 0 && (buf[1] & 0x80) != 0, "long publish uses a multi-byte length", nullptr);
  check(c, decode(buf, n, &pkt, &used) == (int)n, "long publish decodes", nullptr);
  check(c, parsePublish(pkt, info) && info.payloadLen == 200, "long payload length survives",
        nullptr);

  // 9. Malformed: five continuation bytes can never be a valid length.
  const uint8_t broken[] = {0x30, 0xFF, 0xFF, 0xFF, 0xFF, 0x7F};
  check(c, decode(broken, sizeof(broken), &pkt, &used) == -1, "bad length prefix is rejected",
        nullptr);

  // 10. A topic longer than the body claims must be refused, not read past the buffer.
  const uint8_t lie[] = {0x30, 0x04, 0x00, 0x20, 'a', 'b'};
  check(c, decode(lie, sizeof(lie), &pkt, &used) == (int)sizeof(lie), "short body decodes",
        nullptr);
  check(c, !parsePublish(pkt, info), "a lying topic length is refused", nullptr);

  // 11. Control packets, byte for byte.
  const uint8_t ping[] = {0xC0, 0x00};
  n = buildPingreq(buf, sizeof(buf));
  checkBytes(c, "PINGREQ", buf, n, ping, sizeof(ping));
  const uint8_t disc[] = {0xE0, 0x00};
  n = buildDisconnect(buf, sizeof(buf));
  checkBytes(c, "DISCONNECT", buf, n, disc, sizeof(disc));
  n = buildPuback(buf, sizeof(buf), 0x1234);
  const uint8_t ack[] = {0x40, 0x02, 0x12, 0x34};
  checkBytes(c, "PUBACK", buf, n, ack, sizeof(ack));

  // 12. SUBSCRIBE: 0x82, packet id, filter, requested qos.
  n = buildSubscribe(buf, sizeof(buf), 1, "btwh/btwh_eb6f5f/set/temperature", 0);
  check(c, n > 0 && buf[0] == 0x82, "subscribe header", nullptr);
  check(c, n > 0 && buf[2] == 0x00 && buf[3] == 0x01, "subscribe packet id", nullptr);
  check(c, n > 0 && buf[n - 1] == 0x00, "subscribe qos", nullptr);

  // 13. CONNACK text: the return code is what the log needs, not a number.
  check(c, strcmp(connackText(kAccepted), "accepted") == 0, "connack 0 text", nullptr);
  check(c, strcmp(connackText(kBadCredentials), "bad username or password") == 0,
        "connack 4 text", nullptr);

  // 14. Device id: the string that becomes the client id and every topic. MQTT rejects
  //     control characters in both, so the charset invariant is pinned here - it is exactly
  //     the invariant an unterminated buffer violated on the board while this suite stayed
  //     green (the host's stack happened to be zero).
  {
    char id[24];
    deviceIdFromAddress("5c:f8:21:eb:6f:5f", id, sizeof(id));
    check(c, strcmp(id, "btwh_eb6f5f") == 0, "device id from a full address", nullptr);
    check(c, strlen(id) == 11, "device id length is exact", nullptr);
    bool clean = true;
    for (const char* p = id; *p; p++) {
      if (!((*p >= 'a' && *p <= 'z') || (*p >= '0' && *p <= '9') || *p == '_')) clean = false;
    }
    check(c, clean, "device id charset is [a-z0-9_] only", nullptr);

    deviceIdFromAddress("aa:bb", id, sizeof(id));
    check(c, strcmp(id, "btwh_000000") == 0, "short address falls back to 000000", nullptr);

    deviceIdFromAddress("ZZ:aa:bb:cc:dd:ee", id, sizeof(id));
    check(c, strcmp(id, "btwh_ccddee") == 0, "non-hex characters are filtered", nullptr);

    deviceIdFromAddress(nullptr, id, sizeof(id));
    check(c, id[0] == 0, "null address yields an empty string", nullptr);

    char tiny[6];
    deviceIdFromAddress("5c:f8:21:eb:6f:5f", tiny, sizeof(tiny));
    check(c, tiny[sizeof(tiny) - 1] == 0, "a small buffer is still terminated", nullptr);
  }

  // 15. Golden CONNECT, byte for byte: the full production shape - client id from the BLE
  // address, retained will on the availability topic, credentials, 60 s keepalive. These are
  // the exact bytes a real Mosquitto accepted during the first bench session (2026-11-10,
  // board -> PC hotspot -> ICS -> LAN -> broker on HAOS); the same session caught the client
  // id carrying an unterminated buffer, which no broker-free check had seen. Pin the whole
  // packet so any future edit to buildConnect - flag order, length prefixes, field order -
  // fails here, at the bytes, instead of as a silent 'client identifier not valid' at some
  // user's broker. Test credentials, not real ones.
  {
    ConnectOpts golden{};
    golden.clientId     = "btwh_eb6f5f";
    golden.willTopic    = "btwh/btwh_eb6f5f/availability";
    golden.willMessage  = "offline";
    golden.username     = "btwh";
    golden.password     = "benchpass";
    golden.keepAliveSec = 60;
    golden.cleanSession = true;

    const uint8_t want[] = {
        0x10, 0x50,                                   // CONNECT, 80-byte body
        0x00, 0x04, 'M', 'Q', 'T', 'T', 0x04,         // protocol name, level 3.1.1
        0xE6,                                         // clean + will + will-retain + user + pass
        0x00, 0x3C,                                   // keepalive 60 s
        0x00, 0x0B, 'b', 't', 'w', 'h', '_', 'e', 'b', '6', 'f', '5', 'f',
        0x00, 0x1D, 'b', 't', 'w', 'h', '/', 'b', 't', 'w', 'h', '_', 'e', 'b', '6', 'f',
        '5', 'f', '/', 'a', 'v', 'a', 'i', 'l', 'a', 'b', 'i', 'l', 'i', 't', 'y',
        0x00, 0x07, 'o', 'f', 'f', 'l', 'i', 'n', 'e',
        0x00, 0x04, 'b', 't', 'w', 'h',
        0x00, 0x09, 'b', 'e', 'n', 'c', 'h', 'p', 'a', 's', 's',
    };

    const size_t got = buildConnect(buf, sizeof(buf), golden);
    check(c, got == sizeof(want), "golden connect: exact length", nullptr);
    check(c, got == sizeof(want) && memcmp(buf, want, sizeof(want)) == 0,
          "golden connect: byte for byte", nullptr);
  }

  return SelfTestResult{c.total, c.failed};
}

}  // namespace mqtt
}  // namespace net
