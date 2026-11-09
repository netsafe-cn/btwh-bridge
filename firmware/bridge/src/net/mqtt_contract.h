// GENERATED FILE - do not edit.
// Source: spec/mqtt.json
// Regenerate: python firmware/bridge/tools/gen_mqtt.py
//
// The bridge's MQTT surface, generated from the contract so it cannot be re-typed by hand.
// Layer rule (ADR-008): net/ may use these constants; it must never see a frame byte, and
// proto/ must never see these.
#pragma once
// Macro guard as well as #pragma once: arduino-cli compiles a copy of src/ under
// <build>/sketch/, so a header can be reached through two paths.
#ifndef BTWH_NET_MQTT_CONTRACT_H
#define BTWH_NET_MQTT_CONTRACT_H

#include <stddef.h>
#include <stdint.h>

namespace net {
namespace contract {

constexpr const char* kBaseTopic        = "btwh";
constexpr const char* kDiscoveryPrefix  = "homeassistant";
constexpr const char* kDeviceModel      = "Therm 4800i";
constexpr const char* kDeviceManufacturer = "Bosch";
constexpr const char* kDeviceName       = "Water heater";
constexpr uint8_t     kQos              = 0;

// One row per published field, in the order the payload must use.
struct FieldDesc {
  const char* key;
  const char* type;          // "float" | "int" | "bool" | "enum" | "string"
  uint8_t     precision;     // decimals for floats, 0 otherwise
  const char* unit;          // "" when the field has none
  const char* device_class;  // "" when unknown to Home Assistant
  const char* state_class;   // "" when not a measurement
  const char* label;
};
static const FieldDesc kFields[] = {
  {"temp_out", "float", 1, "°C", "temperature", "measurement", "Outlet water temperature"},
  {"temp_in", "float", 1, "°C", "temperature", "measurement", "Inlet water temperature"},
  {"chamber", "float", 1, "°C", "temperature", "measurement", "Combustion chamber temperature"},
  {"temp_box", "float", 1, "°C", "temperature", "measurement", "Chassis temperature"},
  {"setpoint", "int", 0, "°C", "temperature", "setpoint", "Setpoint temperature"},
  {"water_flow", "float", 1, "L/min", "", "measurement", "Water flow"},
  {"burner_power", "int", 0, "%", "", "measurement", "Burner power"},
  {"fan_speed", "int", 0, "rpm", "", "measurement", "Fan speed"},
  {"flame", "bool", 0, "", "running", "", "Flame detected"},
  {"mode", "enum", 0, "", "", "", "Operating mode"},
  {"fault", "string", 0, "", "", "", "Active fault code"},
  {"ts", "int", 0, "s", "", "", "Bridge timestamp (epoch)"},
};
static const size_t kFieldCount = 12;

// The self test renders this snapshot and compares against kGoldenState below.
struct GoldenSample {
  float    temp_out;
  float    temp_in;
  float    chamber;
  float    temp_box;
  int      setpoint;
  float    water_flow;
  int      burner_power;
  int      fan_speed;
  bool     flame;
  int      mode;      // 0 idle, 1 burning, 2 comfort, anything else -> "unknown"
  int      fault;     // 0 -> "none", otherwise two upper-case hex digits
  int64_t  ts;        // epoch seconds; 0 means "not known yet", and the key is then omitted
};

constexpr GoldenSample kGolden = {42.3, 15.0, 42.3, 24.0, 42, 11.5, 47, 1612, true, 1, 0, 1762584000LL};

constexpr const char* kGoldenState =
    "{\"temp_out\":42.3,\"temp_in\":15.0,\"chamber\":42.3,\"temp_box\":24.0,\"setpoint\":42,\"water_flow\":11.5,\"burner_power\":47,\"fan_speed\":1612,\"flame\":true,\"mode\":\"burning\",\"fault\":\"none\",\"ts\":1762584000}";

}  // namespace contract
}  // namespace net

#endif  // BTWH_NET_MQTT_CONTRACT_H
