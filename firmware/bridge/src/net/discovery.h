// Home Assistant discovery: the generated table plus the one runtime substitution it needs.
//
// Layer rule (ADR-008): the table is generated from spec/mqtt.json (ha_discovery_gen.h) and the
// expansion is plain C++, so it can be checked by `selftest` without a broker or a network.
// Publishing belongs to mqtt_client.cpp; this file only turns a template into bytes.
#pragma once
#ifndef BTWH_NET_DISCOVERY_H
#define BTWH_NET_DISCOVERY_H

#include <stddef.h>

#include "net/selftest.h"

namespace net {
namespace discovery {

// Replaces every @DEV@ in `tmpl` with `deviceId`. Returns the length written, or 0 when the
// result would not fit - a truncated discovery payload would create a broken entity, so the
// caller must skip it rather than publish half a document.
size_t expand(const char* tmpl, const char* deviceId, char* out, size_t cap);

// Number of entities in the generated table.
size_t entityCount();

// Accessors by index, so mqtt_client does not include the generated header directly (it stays
// a data file; this is the only code that knows its shape).
const char* entityTopic(size_t index);
const char* entityPayload(size_t index);

SelfTestResult runSelfTest(LogFn log, void* ctx);

}  // namespace discovery
}  // namespace net

#endif  // BTWH_NET_DISCOVERY_H
