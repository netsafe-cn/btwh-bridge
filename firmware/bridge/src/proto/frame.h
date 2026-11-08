// Frame codec for the Bosch heater BLE protocol.
//
// Layer rule (ADR-008): this file must compile without Arduino, without NimBLE and without
// ESP-IDF. Only <stdint.h>, <stddef.h> and <string.h> are allowed, because that is what lets
// the same code be exercised by the on-target self test and, later, by a host compiler.
//
// The byte layout here is identical to python/bosch_api.py::BoschProtocol. Any change must be
// made in both places, and spec/protocol_vectors.json is the judge.
#pragma once

// Classic guard as well as #pragma once: arduino-cli compiles a copy of src/ under
// <build>/sketch/, so one header can be reached through two paths. #pragma once keys
// on the file, not the name, and would let both copies through.
#ifndef BTWH_PROTO_FRAME_H
#define BTWH_PROTO_FRAME_H

#include <stddef.h>
#include <stdint.h>

namespace proto {

// Logical frame: 7B 7B | sender recv mid cmd data... | chk | 7D 7D
constexpr uint8_t kDelimiter = 0x7B;
constexpr uint8_t kTerminator = 0x7D;
constexpr uint8_t kSender = 0x21;
constexpr uint8_t kRecvLogin = 0xAB;
constexpr uint8_t kCmdLogin = 0xA2;
constexpr uint8_t kCmdState = 0xA0;

// A notify fragment carries at most 20 bytes at MTU 23, and each fragment restarts the XOR.
constexpr size_t kChunk = 20;

extern const uint8_t kXorKey[9];

// XOR in place. Every notify fragment restarts at key[0] - this is the single most common
// mistake in reimplementing the protocol.
void xorBuf(uint8_t* p, size_t n);

// Builds a logical frame (checksum included, stuffing not applied). Returns the frame length.
size_t buildFrame(uint8_t* out, uint8_t recv, uint8_t mid, uint8_t cmd,
                  const uint8_t* data, size_t dlen);

// Stuffs a 00 after every 7B/7D in the data area, delimiters excluded.
size_t stuffFrame(const uint8_t* in, size_t n, uint8_t* out);
size_t unstuffFrame(const uint8_t* in, size_t n, uint8_t* out);

// Checksum = low 8 bits of the sum over [2, n-3), compared with byte n-3.
uint8_t checksum(const uint8_t* f, size_t n);
bool checksumOk(const uint8_t* f, size_t n);

// Reassembles notify fragments into complete, unstuffed frames.
struct RxStream {
  uint8_t buf[1024];
  size_t len;
};

typedef void (*FrameSink)(void* ctx, const uint8_t* frame, size_t len);

// Decrypts one fragment, appends it to the stream, and hands every complete frame to sink.
// Returns the number of frames emitted, or -1 when the stream overflowed and was reset
// (the fragment is still processed, so the caller keeps resynchronising from it).
int streamPush(RxStream* s, const uint8_t* fragment, size_t n, FrameSink sink, void* ctx);

void streamReset(RxStream* s);

}  // namespace proto

#endif  // BTWH_PROTO_FRAME_H
