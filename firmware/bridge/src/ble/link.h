// BLE link to the heater: connect, subscribe, reassemble, send.
//
// Layer rule (ADR-008): this is the only directory that includes NimBLE. Everything above it
// talks in complete protocol frames and callbacks, so replacing the radio stack (or mocking it
// in a test) never reaches the protocol or the authorisation code.
//
// This layer knows nothing about pairing semantics. It reports "the link is encrypted / bonded"
// and nothing more; whether that means the heater accepted our code is auth/'s business.
#pragma once

// Classic guard as well as #pragma once: arduino-cli compiles a copy of src/ under
// <build>/sketch/, so one header can be reached through two paths. #pragma once keys
// on the file, not the name, and would let both copies through.
#ifndef BTWH_BLE_LINK_H
#define BTWH_BLE_LINK_H

#include <stddef.h>
#include <stdint.h>

namespace ble {

// Complete frame, already reassembled from the FIFO/CREDITS fragments and de-obfuscated.
typedef void (*FrameFn)(void* ctx, const uint8_t* frame, size_t len);

// SMP outcome. `bonded` separates "we paired just now" from "an old bond was resumed", which
// matters when a stale bond keeps an expired code alive.
typedef void (*SecurityFn)(void* ctx, bool encrypted, bool bonded);

struct Hooks {
  FrameFn    onFrame;
  SecurityFn onSecurity;
  void*      ctx;
};

struct ScanEntry {
  char addr[18];
  char name[28];
  int  rssi;
};

// One call, from setup(). Brings up the NimBLE stack and the KeyboardOnly security config the
// heater expects: the panel code is the BLE passkey, not a Just Works exchange.
void begin(const char* deviceName);

// The six-digit panel code, as a number. Applies to the next pairing attempt.
void setPasskey(uint32_t pin);

void setHooks(const Hooks& hooks);

// Forgets the local bond. Needed when the panel code changed or pairing went wrong; the heater
// still holds its own record, so the panel must be cleared as well.
void clearBonds();
bool bonded();

bool connect(const char* heaterMac, uint8_t timeoutSec);
bool isConnected();
void disconnect();

// RSSI of the connected heater, dBm, read straight from the controller. 0 when the link
// is down - a real reading is always negative, so 0 doubles as "not reported" upstream.
int rssi();

// Build, checksum, stuff, XOR and write one command, fragment by fragment. The msgId counter is
// internal and increments per call, exactly as the Python side does.
bool send(uint8_t recv, uint8_t cmd, const uint8_t* data, size_t dlen);

// Active scan for `seconds`. Returns the number of entries written to `out`.
int scan(ScanEntry* out, int maxEntries, uint8_t seconds);

// Same radio time, but only heaters (advertised name starts with "BTWH-"), sorted strongest
// signal first. This is what the provisioning page offers as choices. Returns the count.
int scanHeaters(ScanEntry* out, int maxEntries, uint8_t seconds);

// Address helpers shared by the console and the link. Both accept ':' or '-' separators.
bool   parseMac(const char* text, uint8_t out[6]);
void   formatMac(const uint8_t mac[6], char out[18]);

}  // namespace ble

#endif  // BTWH_BLE_LINK_H
