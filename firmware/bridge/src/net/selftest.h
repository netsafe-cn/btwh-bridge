// Self-test plumbing shared by every net/ module.
//
// proto/ has its own copy of this shape (proto::SelfTestResult). net/ keeps one here so
// state_json and mqtt_wire can both report into the same `selftest` run without either
// including the other's header.
#pragma once
#ifndef BTWH_NET_SELFTEST_H
#define BTWH_NET_SELFTEST_H

namespace net {

// A log callback instead of Serial: the suite runs on the board today and can run on a host
// later without dragging in Arduino.
typedef void (*LogFn)(void* ctx, const char* line);

struct SelfTestResult {
  int total;
  int failed;
};

}  // namespace net

#endif  // BTWH_NET_SELFTEST_H
