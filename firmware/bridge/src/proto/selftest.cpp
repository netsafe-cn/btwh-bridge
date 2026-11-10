#include "selftest.h"

#include <stdarg.h>
#include <stdio.h>
#include <string.h>

#include "a0.h"
#include "frame.h"
#include "profiles_gen.h"
#include "vectors_gen.h"

namespace proto {
namespace {

int g_total = 0;
int g_failed = 0;

void line(LogFn log, void* ctx, const char* fmt, ...) {
  if (!log) return;
  char buf[192];
  va_list ap;
  va_start(ap, fmt);
  vsnprintf(buf, sizeof(buf), fmt, ap);
  va_end(ap);
  log(ctx, buf);
}

bool near(float a, float b) {
  const float d = a - b;
  return d < 0.005f && d > -0.005f;
}

void dumpShort(LogFn log, void* ctx, const uint8_t* d, size_t n) {
  char buf[80];
  size_t o = 0;
  for (size_t i = 0; i < n && i < 16; i++) {
    o += (size_t)snprintf(buf + o, sizeof(buf) - o, "%02X ", d[i]);
    if (o >= sizeof(buf) - 4) break;
  }
  if (n > 16) snprintf(buf + o, sizeof(buf) - o, "...");
  log(ctx, buf);
}

void expectBytes(LogFn log, void* ctx, const char* caseName, const char* what,
                 const uint8_t* got, size_t gotLen, const uint8_t* want, size_t wantLen) {
  g_total++;
  const bool ok = (gotLen == wantLen) && (wantLen == 0 || memcmp(got, want, wantLen) == 0);
  if (ok) return;
  g_failed++;
  line(log, ctx, "  x %s / %s: length %u, expected %u", caseName, what,
       (unsigned)gotLen, (unsigned)wantLen);
  line(log, ctx, "    got:");
  dumpShort(log, ctx, got, gotLen);
  line(log, ctx, "    want:");
  dumpShort(log, ctx, want, wantLen);
}

void expectInt(LogFn log, void* ctx, const char* caseName, const char* what, int got, int want) {
  g_total++;
  if (got == want) return;
  g_failed++;
  line(log, ctx, "  x %s / %s: %d, expected %d", caseName, what, got, want);
}

void expectFloat(LogFn log, void* ctx, const char* caseName, const char* what,
                 float got, float want) {
  g_total++;
  if (near(got, want)) return;
  g_failed++;
  line(log, ctx, "  x %s / %s: %.2f, expected %.2f", caseName, what, (double)got, (double)want);
}

struct Collect {
  uint8_t buf[256];
  size_t len;
  int count;
};

void collectFrame(void* ctx, const uint8_t* frame, size_t len) {
  Collect* c = static_cast<Collect*>(ctx);
  if (len <= sizeof(c->buf)) {
    memcpy(c->buf, frame, len);
    c->len = len;
  }
  c->count++;
}

void runFrameCase(LogFn log, void* ctx, const vectors::FrameCase& c) {
  // 1) building a logical frame from its parameters reproduces the golden frame
  uint8_t frame[192];
  const size_t frameLen = buildFrame(frame, c.recv, c.mid, c.cmd, c.data, c.data_len);
  expectBytes(log, ctx, c.name, "buildFrame", frame, frameLen, c.frame, c.frame_len);

  // 2) stuffing, which only touches the data area
  uint8_t stuffed[256];
  const size_t stuffedLen = stuffFrame(c.frame, c.frame_len, stuffed);
  expectBytes(log, ctx, c.name, "stuffFrame", stuffed, stuffedLen, c.stuffed, c.stuffed_len);

  // 3) unstuffing gets the logical frame back
  uint8_t plain[256];
  const size_t plainLen = unstuffFrame(c.stuffed, c.stuffed_len, plain);
  expectBytes(log, ctx, c.name, "unstuffFrame", plain, plainLen, c.frame, c.frame_len);

  // 4) checksum accepts the frame and rejects a corrupted copy
  g_total++;
  if (!checksumOk(c.frame, c.frame_len)) {
    g_failed++;
    line(log, ctx, "  x %s / checksum: valid frame rejected", c.name);
  }
  uint8_t broken[192];
  memcpy(broken, c.frame, c.frame_len);
  broken[c.frame_len - 3] ^= 0xFF;
  g_total++;
  if (checksumOk(broken, c.frame_len)) {
    g_failed++;
    line(log, ctx, "  x %s / checksum: corrupted frame accepted", c.name);
  }

  // 5) per-fragment XOR matches, and feeding those fragments back through the stream
  //    reassembles the frame. This is the part that breaks if anyone "simplifies" the XOR
  //    into one continuous pass.
  uint8_t wire[256];
  size_t wireLen = 0;
  uint8_t off = 0;
  for (uint8_t i = 0; i < c.chunk_count; i++) {
    const uint8_t n = c.chunk_lens[i];
    memcpy(wire + wireLen, c.chunks + off, n);
    xorBuf(wire + wireLen, n);
    wireLen += n;
    off = (uint8_t)(off + n);
  }
  expectBytes(log, ctx, c.name, "xor chunks", wire, wireLen, c.stuffed, c.stuffed_len);

  RxStream stream;
  streamReset(&stream);
  Collect got;
  got.len = 0;
  got.count = 0;
  int overflow = 0;
  off = 0;
  for (uint8_t i = 0; i < c.chunk_count; i++) {
    const uint8_t n = c.chunk_lens[i];
    const int r = streamPush(&stream, c.chunks + off, n, collectFrame, &got);
    if (r < 0) overflow = 1;
    off = (uint8_t)(off + n);
  }
  expectInt(log, ctx, c.name, "stream overflow", overflow, 0);
  expectInt(log, ctx, c.name, "frames emitted", got.count, 1);
  expectBytes(log, ctx, c.name, "stream reassembly", got.buf, got.len, c.frame, c.frame_len);
}

void runA0Case(LogFn log, void* ctx, const vectors::A0Case& c) {
  const A0State s = parseA0(c.payload, c.payload_len);
  expectInt(log, ctx, c.name, "frame_type", s.frame_type, c.frame_type);
  expectFloat(log, ctx, c.name, "temp_out", s.temp_out, c.temp_out);
  expectFloat(log, ctx, c.name, "temp_in", s.temp_in, c.temp_in);
  expectFloat(log, ctx, c.name, "chamber", s.chamber, c.chamber);
  expectFloat(log, ctx, c.name, "temp_box", s.temp_box, c.temp_box);
  expectInt(log, ctx, c.name, "setpoint", s.setpoint, c.setpoint);
  expectFloat(log, ctx, c.name, "water_flow", s.water_flow, c.water_flow);
  expectInt(log, ctx, c.name, "burner_power", s.burner_power, c.burner_power);
  expectInt(log, ctx, c.name, "fan_speed", s.fan_speed, c.fan_speed);
  expectInt(log, ctx, c.name, "fan_power", s.fan_power, c.fan_power);
  expectInt(log, ctx, c.name, "mode", s.mode, c.mode);
  expectInt(log, ctx, c.name, "max_setpoint", s.max_setpoint, c.max_setpoint);
  expectInt(log, ctx, c.name, "fault", s.fault, c.fault);
  expectInt(log, ctx, c.name, "flame", s.flame ? 1 : 0, c.flame);
  expectInt(log, ctx, c.name, "bathroom", s.bathroom ? 1 : 0, c.bathroom);
}

// The model registry must agree with the protocol evidence, or the build fails here rather
// than the heater surprising a user at runtime:
//   1) the range is sane (min < max, step >= 1)
//   2) the write_param bytes rebuild the golden D3 setpoint vector's data area
//   3) the golden A0 vector's frame_type matches the row (the parser offsets must match)
//   4) the golden A0 vector's max_setpoint equals the row's max - the heater itself reports
//      60 in the state frame, so a profile that claims otherwise is wrong by definition
void runProfileChecks(LogFn log, void* ctx) {
  for (size_t i = 0; i < profiles::kProfileCount; i++) {
    const profiles::HeaterProfile& p = profiles::kProfiles[i];

    expectInt(log, ctx, p.id, "profile range sane", p.set_min < p.set_max && p.set_step >= 1, 1);

    const vectors::FrameCase* d3 = nullptr;
    for (size_t j = 0; j < vectors::kFrameCount; j++) {
      const vectors::FrameCase& c = vectors::kFrames[j];
      if (c.cmd == 0xD3 && c.data_len == 4 && c.data[0] == p.write_type
          && c.data[1] == p.write_id && c.data[2] == p.write_index) {
        d3 = &c;
        break;
      }
    }
    g_total++;
    if (!d3) {
      g_failed++;
      line(log, ctx, "  x %s / profile write_param: no golden D3 vector matches %02X %02X %02X",
           p.id, p.write_type, p.write_id, p.write_index);
    } else if (d3->data[3] < p.set_min || d3->data[3] > p.set_max) {
      g_failed++;
      line(log, ctx, "  x %s / profile range excludes golden value %d", p.id, (int)d3->data[3]);
    }

    int matched = 0;
    for (size_t j = 0; j < vectors::kA0Count; j++) {
      const vectors::A0Case& c = vectors::kA0[j];
      if (c.frame_type != p.a0_frame_type) continue;
      matched++;
      expectInt(log, ctx, p.id, "profile max_setpoint matches A0 report", c.max_setpoint, p.set_max);
    }
    expectInt(log, ctx, p.id, "profile a0_frame_type has a golden vector", matched, 1);
  }
}

}  // namespace

SelfTestResult runSelfTest(LogFn log, void* ctx) {
  g_total = 0;
  g_failed = 0;

  line(log, ctx, "--- proto self test -------------------------------");
  line(log, ctx, "vectors: %u frame cases, %u A0 cases",
       (unsigned)vectors::kFrameCount, (unsigned)vectors::kA0Count);

  for (size_t i = 0; i < vectors::kFrameCount; i++) {
    runFrameCase(log, ctx, vectors::kFrames[i]);
  }
  for (size_t i = 0; i < vectors::kA0Count; i++) {
    runA0Case(log, ctx, vectors::kA0[i]);
  }
  runProfileChecks(log, ctx);

  SelfTestResult r;
  r.total = g_total;
  r.failed = g_failed;
  if (g_failed == 0) {
    line(log, ctx, "result: PASS (%d checks)", g_total);
  } else {
    line(log, ctx, "result: FAIL (%d of %d checks)", g_failed, g_total);
  }
  line(log, ctx, "--------------------------------------------------");
  return r;
}

}  // namespace proto
