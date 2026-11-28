#include "net/logring.h"

#include <Arduino.h>
#include <stdarg.h>
#include <stdio.h>
#include <string.h>

namespace net {
namespace logring {
namespace {

// 4 KB: about 60 lines of the widths this firmware prints, and small enough that losing it to a
// future feature would not be the deciding cost. Internal RAM, not PSRAM: it is written from the
// BLE callback path, and a PSRAM read there can stall the radio.
constexpr size_t kRingSize    = 4096;
constexpr size_t kMaxLineLen  = 160;   // including the timestamp and NUL
constexpr size_t kTailWanted  = 3;     // past the wrap point, skip the torn first line

char     gRing[kRingSize];
size_t   gHead    = 0;   // next byte to write
size_t   gUsed    = 0;   // bytes currently holding text (<= kRingSize)
Level    gLevel   = Level::Info;
uint32_t gSeq     = 0;
uint32_t gLines   = 0;

void appendRaw(const char* p, size_t n) {
  for (size_t i = 0; i < n; i++) {
    gRing[gHead] = p[i];
    gHead = (gHead + 1) % kRingSize;
    if (gUsed < kRingSize) gUsed++;
  }
}

const char* levelTag(Level lv) {
  switch (lv) {
    case Level::Error: return "E";
    case Level::Warn:  return "W";
    case Level::Info:  return "I";
    default:           return "D";
  }
}

}  // namespace

void setLevel(Level lv) { gLevel = lv; }
Level level() { return gLevel; }

const char* levelName(Level lv) {
  switch (lv) {
    case Level::Error: return "error";
    case Level::Warn:  return "warn";
    case Level::Info:  return "info";
    default:           return "debug";
  }
}

void write(Level lv, const char* tag, const char* fmt, ...) {
  if ((uint8_t)lv > (uint8_t)gLevel) return;

  char line[kMaxLineLen];
  const uint32_t ms = millis();
  int n = snprintf(line, sizeof(line), "[%lu.%03lu] [%s] %s: ",
                   (unsigned long)(ms / 1000), (unsigned long)(ms % 1000),
                   levelTag(lv), tag);
  if (n < 0) return;
  if ((size_t)n >= sizeof(line) - 2) n = (int)sizeof(line) - 2;

  va_list ap;
  va_start(ap, fmt);
  const int m = vsnprintf(line + n, sizeof(line) - (size_t)n, fmt, ap);
  va_end(ap);
  if (m > 0) n += m;
  if ((size_t)n > sizeof(line) - 2) n = (int)sizeof(line) - 2;

  // One line, always: a caller that embeds \n would break the dump's whole-line contract.
  for (int i = 0; i < n; i++) if (line[i] == '\n' || line[i] == '\r') line[i] = ' ';
  line[n++] = '\n';

  appendRaw(line, (size_t)n);
  gSeq++;
  gLines++;

  // Serial keeps its own copy: the ring is for after the fact, Serial for whoever is watching.
  Serial.write((const uint8_t*)line, (size_t)n);
}

size_t dump(char* out, size_t cap) {
  if (cap == 0) return 0;
  const size_t n = (gUsed < cap - 1) ? gUsed : cap - 1;
  if (n == 0) { out[0] = 0; return 0; }

  // Oldest byte is wherever head will land next. When the ring has wrapped, the first line in
  // the buffer is torn; skip to the first newline so a reader never gets half a timestamp.
  size_t start = (gHead + kRingSize - gUsed) % kRingSize;
  size_t len   = n;
  if (gUsed == kRingSize) {
    size_t i = 0;
    while (i < len && gRing[(start + i) % kRingSize] != '\n') i++;
    if (i < len) { i++; start = (start + i) % kRingSize; len -= i; }
    else len = 0;   // one enormous line: nothing whole to serve
  }

  for (size_t i = 0; i < len; i++) out[i] = gRing[(start + i) % kRingSize];
  out[len] = 0;
  return len;
}

uint32_t sequence() { return gSeq; }
size_t   used()     { return gUsed; }
uint32_t lineCount() { return gLines; }

}  // namespace logring
}  // namespace net
