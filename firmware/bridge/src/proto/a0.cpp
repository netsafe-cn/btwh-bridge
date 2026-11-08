#include "a0.h"

namespace proto {

namespace {

// Index with a zero fallback: a truncated frame must not read beyond the buffer.
inline int at(const uint8_t* d, size_t n, int i) {
  return (i >= 0 && (size_t)i < n) ? d[i] : 0;
}

// Temperatures are [integer][hundredths]: data[i] + data[i+1]/100.
inline float tempAt(const uint8_t* d, size_t n, int i) {
  return at(d, n, i) + at(d, n, i + 1) / 100.0f;
}

}  // namespace

A0State parseA0(const uint8_t* d, size_t n) {
  A0State s;
  s.frame_type = (uint8_t)at(d, n, 0);
  s.type1 = (s.frame_type == 1);

  s.temp_in = tempAt(d, n, 1);
  s.chamber = tempAt(d, n, 3);
  s.setpoint = at(d, n, 5);
  s.temp_box = tempAt(d, n, 6);
  s.flame = (at(d, n, 10) & 0x10) != 0;
  s.fault = at(d, n, 11);

  // Only the outlet differs in offset; the rest differ in whether the field exists at all.
  const int outIdx = s.type1 ? 28 : 21;
  s.temp_out = tempAt(d, n, outIdx);

  if (s.type1) {
    s.water_flow = at(d, n, 21) / 10.0f;
    s.burner_power = at(d, n, 13);
    s.fan_speed = at(d, n, 15) * 100 + at(d, n, 16);
    s.fan_power = at(d, n, 12);
    s.mode = at(d, n, 32);
    s.max_setpoint = at(d, n, 33) & 0x7F;
    s.bathroom = (at(d, n, 33) & 0x80) != 0;
  } else {
    s.water_flow = at(d, n, 8) / 10.0f;
    s.burner_power = at(d, n, 9);
    s.fan_speed = at(d, n, 14);
    s.fan_power = at(d, n, 13);
    s.mode = at(d, n, 16);
    s.max_setpoint = at(d, n, 17) & 0x7F;
    s.bathroom = (at(d, n, 17) & 0x80) != 0;
  }

  s.ambient = tempAt(d, n, 16);
  s.ambient_set = tempAt(d, n, 18);
  s.gas_set = tempAt(d, n, 22);
  s.pressure = at(d, n, 27) * 10;

  return s;
}

}  // namespace proto
