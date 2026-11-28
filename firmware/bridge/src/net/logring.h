// A bounded ring of recent log lines, so a board on a charger can still be asked what
// happened. Serial alone answers that only while a laptop is plugged in, and both long-soak
// investigations so far (the 99.9 C filler frames, the 11-21 reboots) needed history that no
// longer existed by the time anyone looked at it.
//
// The ring is bounded on purpose. An unbounded log on a device with 266 KB of free heap is a
// leak with extra steps; when this fills, the oldest bytes are overwritten, because a field
// report is about the recent past. 4 KB holds roughly 60 lines at the widths this firmware
// actually prints.
//
// Nothing here formats differently from Serial on purpose: the same line goes to both. The only
// additions are a timestamp (millis since boot, which survives no reset and pretends to nothing
// else) and a level, so a reader can skip the chatter without the writer deciding for them.
#pragma once

#include <stddef.h>
#include <stdint.h>

namespace net {
namespace logring {

// Ordered by severity: a line is kept when its level is <= the configured one.
enum class Level : uint8_t { Error = 0, Warn = 1, Info = 2, Debug = 3 };

// Default Info: boot, link, MQTT, rejections. Debug adds per-frame chatter.
void     setLevel(Level lv);
Level    level();
const char* levelName(Level lv);

// Writes one line to the ring and to Serial. `tag` should be a short literal ("MQTT", "A0").
// The message is truncated at 160 chars, which is longer than any line this firmware prints.
void write(Level lv, const char* tag, const char* fmt, ...);

// Copies the ring out, oldest first, only whole lines. Returns bytes written excluding the NUL.
// When the ring wrapped, the first (partial) line is dropped rather than served truncated.
size_t dump(char* out, size_t cap);

// Changes on every write; a reader can cheaply ask "is there anything new?" without comparing
// buffers, and the comparison cannot be fooled by two identical lines.
uint32_t sequence();

size_t   used();
uint32_t lineCount();

}  // namespace logring
}  // namespace net
