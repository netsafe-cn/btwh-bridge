// A2/07 + A2/08: the two-step authorisation the phone app performs after the link is up.
//
// Layer rule (ADR-008): no NimBLE, no Arduino, no Serial. The caller supplies a Transport
// (send / wait / log), so the state machine can be driven by a fake one - on a PC, or on the
// board with a canned reply - without a heater in the room.
//
// The reply codes below are measured, not guessed (2026-11-07, Therm 4800i):
//   A2/07 = 02  window open, code required
//   A2/07 = 01  this MAC is registered, but the code is still required
//   A2/07 = 00  no pairing window / it expired
//   A2/07 = 3C  rate limit, 30 s      5A  rate limit, 90 s
//   A2/08 = 01  authorised            02  wrong digits     00  refused
// Three consecutive failures lock the device out for about three minutes.
#pragma once

// Classic guard as well as #pragma once: arduino-cli compiles a copy of src/ under
// <build>/sketch/, so one header can be reached through two paths. #pragma once keys
// on the file, not the name, and would let both copies through.
#ifndef BTWH_AUTH_LOGIN_H
#define BTWH_AUTH_LOGIN_H

#include <stddef.h>
#include <stdint.h>

namespace auth {

// Mirrors python/bosch_api.py PAIR_FAIL_* one for one. Keep the two lists in step; the MQTT
// layer reports these by name, and the Python and firmware sides must agree on the names.
enum Result {
  kOk = 0,
  kWindowClosed,   // A2/07 = 00
  kCooldown,       // A2/07 = 3C / 5A
  kCodeRejected,   // A2/08 = 00
  kCodeMismatch,   // A2/08 = 02
  kNoResponse,     // every send went unanswered
  kBadInput,       // nothing was sent: address or code malformed
  kLinkLost,       // the BLE link never came up, or died mid-login; set by app/, not classify()
};

// `sub` is 0x07 or 0x08. `wait` returns that sub-command's reply byte, or -1 on timeout.
// `log` may be null; it receives one line per attempt, without a prefix.
struct Transport {
  void*      ctx;
  bool     (*send)(void* ctx, uint8_t sub, const uint8_t* data, size_t len);
  int      (*wait)(void* ctx, uint8_t sub, uint32_t timeoutMs);
  void     (*log)(void* ctx, const char* line);
};

// Six ASCII digits, nothing else. Checked before anything is transmitted, because sending a
// malformed code counts as a failure and three of those lock the panel.
bool pinValid(const char* pin6);

// 6 s timeout per attempt, five attempts, a fresh msgId each time - the same budget
// bosch_api.py::_send_login_wait uses. Returns false when no reply arrived at all.
bool submitMac(const Transport& t, const uint8_t mac[6], uint8_t* reply);
bool submitCode(const Transport& t, const char* pin6, uint8_t* reply);

// Turns the pair of replies into a single outcome. Either reply may be 0xFF ("not sent").
Result classify(uint8_t reply07, uint8_t reply08);

// Short English text for the serial log; also what an MQTT consumer would receive.
const char* windowText(uint8_t reply07);
const char* codeText(uint8_t reply08);
const char* reasonText(Result r);

}  // namespace auth

#endif  // BTWH_AUTH_LOGIN_H
