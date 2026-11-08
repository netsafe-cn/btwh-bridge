// On-target self test for the protocol layer.
//
// Why not a host test: the toolchain here only produces Xtensa binaries, so "run the golden
// vectors" has to happen on the board. That is not a downgrade - it exercises the exact binary
// that ships, against the same bytes python/tests/test_contract.py checks.
//
// What it covers: framing, stuffing, per-fragment XOR, checksum, stream reassembly, and every
// A0 field that has a vector. What it cannot cover: anything that needs the heater.
#pragma once

#include <stddef.h>

namespace proto {

// One line of output per event. The caller decides where it goes (Serial, a buffer, nothing).
typedef void (*LogFn)(void* ctx, const char* line);

struct SelfTestResult {
  int total;
  int failed;
};

SelfTestResult runSelfTest(LogFn log, void* ctx);

}  // namespace proto
