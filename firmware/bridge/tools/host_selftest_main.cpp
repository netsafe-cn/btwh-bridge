// Host runner for the four C++ selftest suites.
//
// The same suites the `selftest` serial command runs on the board, compiled for the host.
// The board still checks them against real hardware; this runner catches a broken check
// before the first flash. That is exactly the gap that let the wrong "expand fits exactly"
// expectation (a 12-byte need asserted as 10) survive a full day of green CI: the C++
// suites ran nowhere but the target.
//
// Compiled by firmware/bridge/tools/host_selftest.py; do not add to the Arduino build.
#include <stdio.h>

#include "net/discovery.h"
#include "net/mqtt_rules.h"
#include "net/mqtt_wire.h"
#include "net/selftest.h"
#include "net/state_json.h"
#include "proto/selftest.h"

namespace {

void print(void* /*ctx*/, const char* line) { puts(line); }

void report(const char* name, int total, int failed, int* bad) {
  // Same shape as the board's reportTest, so a log from either reads the same way.
  if (failed) {
    printf("[TEST] %s self test FAILED: %d of %d checks\n", name, failed, total);
    *bad += failed;
  } else {
    printf("[TEST] %s self test passed: %d checks\n", name, total);
  }
}

}  // namespace

int main() {
  int bad = 0;

  const proto::SelfTestResult p = proto::runSelfTest(print, nullptr);
  report("proto", p.total, p.failed, &bad);

  const net::SelfTestResult n = net::runSelfTest(print, nullptr);
  report("net", n.total, n.failed, &bad);

  const net::SelfTestResult w = net::mqtt::runSelfTest(print, nullptr);
  report("mqtt", w.total, w.failed, &bad);

  const net::SelfTestResult r = net::rules::runSelfTest(print, nullptr);
  report("rules", r.total, r.failed, &bad);

  const net::SelfTestResult d = net::discovery::runSelfTest(print, nullptr);
  report("discovery", d.total, d.failed, &bad);

  const int total = p.total + n.total + w.total + r.total + d.total;
  if (bad) {
    printf("host selftest FAILED: %d check(s)\n", bad);
    return 1;
  }
  printf("host selftest passed: %d checks\n", total);
  return 0;
}
