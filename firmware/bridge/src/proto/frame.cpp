#include "frame.h"

#include <string.h>

namespace proto {

const uint8_t kXorKey[9] = {'1', 'a', 'g', '3', '.', '-', 'f', 's', '7'};

void xorBuf(uint8_t* p, size_t n) {
  for (size_t i = 0; i < n; i++) p[i] ^= kXorKey[i % 9];
}

size_t buildFrame(uint8_t* out, uint8_t recv, uint8_t mid, uint8_t cmd,
                  const uint8_t* data, size_t dlen) {
  size_t i = 0;
  out[i++] = kDelimiter;
  out[i++] = kDelimiter;
  out[i++] = kSender;
  out[i++] = recv;
  out[i++] = mid;
  out[i++] = cmd;
  for (size_t k = 0; k < dlen; k++) out[i++] = data[k];
  uint8_t sum = 0;
  for (size_t k = 2; k < i; k++) sum += out[k];
  out[i++] = sum;
  out[i++] = kTerminator;
  out[i++] = kTerminator;
  return i;
}

size_t stuffFrame(const uint8_t* in, size_t n, uint8_t* out) {
  if (n <= 4) {
    memcpy(out, in, n);
    return n;
  }
  size_t o = 0;
  out[o++] = in[0];
  out[o++] = in[1];
  for (size_t i = 2; i < n - 2; i++) {
    out[o++] = in[i];
    if (in[i] == kDelimiter || in[i] == kTerminator) out[o++] = 0x00;
  }
  out[o++] = in[n - 2];
  out[o++] = in[n - 1];
  return o;
}

size_t unstuffFrame(const uint8_t* in, size_t n, uint8_t* out) {
  if (n <= 2) {
    memcpy(out, in, n);
    return n;
  }
  size_t o = 0;
  out[o++] = in[0];
  out[o++] = in[1];
  bool skip = false;
  for (size_t i = 2; i < n - 2; i++) {
    if (skip && in[i] == 0x00) {
      skip = false;
      continue;
    }
    out[o++] = in[i];
    skip = (in[i] == kDelimiter || in[i] == kTerminator);
  }
  out[o++] = in[n - 2];
  out[o++] = in[n - 1];
  return o;
}

uint8_t checksum(const uint8_t* f, size_t n) {
  uint8_t sum = 0;
  for (size_t i = 2; i < n - 3; i++) sum += f[i];
  return sum;
}

bool checksumOk(const uint8_t* f, size_t n) {
  if (n < 9) return false;
  return checksum(f, n) == f[n - 3];
}

void streamReset(RxStream* s) { s->len = 0; }

int streamPush(RxStream* s, const uint8_t* fragment, size_t n, FrameSink sink, void* ctx) {
  if (n == 0 || n > 64) return 0;

  uint8_t tmp[64];
  memcpy(tmp, fragment, n);
  xorBuf(tmp, n);  // each fragment restarts at key[0]

  bool overflowed = false;
  if (s->len + n > sizeof(s->buf)) {
    s->len = 0;  // drop what we have and resynchronise from this fragment
    overflowed = true;
  }
  memcpy(s->buf + s->len, tmp, n);
  s->len += n;

  int emitted = 0;
  while (s->len >= 4) {
    size_t start = 0;
    while (start + 1 < s->len &&
           !(s->buf[start] == kDelimiter && s->buf[start + 1] == kDelimiter)) {
      start++;
    }
    if (start + 1 >= s->len) {
      s->len = 0;  // nothing that looks like a frame start; keep the buffer clean
      break;
    }
    if (start > 0) {
      memmove(s->buf, s->buf + start, s->len - start);
      s->len -= start;
    }

    size_t end = 0;
    for (size_t i = 2; i + 1 < s->len; i++) {
      if (s->buf[i] == kTerminator && s->buf[i + 1] == kTerminator) {
        end = i + 2;
        break;
      }
    }
    if (!end) break;  // incomplete frame stays buffered

    uint8_t plain[256];
    size_t plen = unstuffFrame(s->buf, end, plain);
    if (sink) sink(ctx, plain, plen);
    emitted++;

    memmove(s->buf, s->buf + end, s->len - end);
    s->len -= end;
  }

  return overflowed ? -1 : emitted;
}

}  // namespace proto
