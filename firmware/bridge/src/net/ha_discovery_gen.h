// GENERATED FILE - do not edit.
// Source: spec/mqtt.json
// Regenerate: python firmware/bridge/tools/gen_ha.py
//
// Home Assistant MQTT discovery, one entry per published field. The token @DEV@ in a topic or
// payload is replaced at runtime with the device id (btwh_<last6>), which is only known once
// the heater address is configured. Layer rule (ADR-008): data only - the publisher lives in
// mqtt_client.cpp and the substitution in discovery.cpp.
#pragma once
// Macro guard as well as #pragma once: arduino-cli compiles a copy of src/ under
// <build>/sketch/, so a header can be reached through two paths.
#ifndef BTWH_NET_HA_DISCOVERY_GEN_H
#define BTWH_NET_HA_DISCOVERY_GEN_H

#include <stddef.h>

namespace net {
namespace discovery {

struct EntityDesc {
  const char* component;   // "sensor" | "binary_sensor" | "number"
  const char* key;         // state field name, also the discovery object id
  const char* topic;       // discovery topic, carries the @DEV@ token
  const char* payload;     // entity config JSON, carries the @DEV@ token
};

static const EntityDesc kEntities[] = {
  {"sensor", "temp_out", "homeassistant/sensor/@DEV@/temp_out/config",
   "{\"name\":\"Outlet water temperature\",\"unique_id\":\"@DEV@_temp_out\",\"state_topic\":\"btwh/@DEV@/state\",\"availability_topic\":\"btwh/@DEV@/availability\",\"payload_available\":\"online\",\"payload_not_available\":\"offline\",\"device\":{\"identifiers\":[\"@DEV@\"],\"manufacturer\":\"Bosch\",\"model\":\"Therm 4800i\",\"name\":\"Water heater\"},\"value_template\":\"{{ value_json.temp_out }}\",\"unit_of_measurement\":\"°C\",\"device_class\":\"temperature\",\"state_class\":\"measurement\"}"},
  {"sensor", "temp_in", "homeassistant/sensor/@DEV@/temp_in/config",
   "{\"name\":\"Inlet water temperature\",\"unique_id\":\"@DEV@_temp_in\",\"state_topic\":\"btwh/@DEV@/state\",\"availability_topic\":\"btwh/@DEV@/availability\",\"payload_available\":\"online\",\"payload_not_available\":\"offline\",\"device\":{\"identifiers\":[\"@DEV@\"],\"manufacturer\":\"Bosch\",\"model\":\"Therm 4800i\",\"name\":\"Water heater\"},\"value_template\":\"{{ value_json.temp_in }}\",\"unit_of_measurement\":\"°C\",\"device_class\":\"temperature\",\"state_class\":\"measurement\"}"},
  {"sensor", "chamber", "homeassistant/sensor/@DEV@/chamber/config",
   "{\"name\":\"Combustion chamber temperature\",\"unique_id\":\"@DEV@_chamber\",\"state_topic\":\"btwh/@DEV@/state\",\"availability_topic\":\"btwh/@DEV@/availability\",\"payload_available\":\"online\",\"payload_not_available\":\"offline\",\"device\":{\"identifiers\":[\"@DEV@\"],\"manufacturer\":\"Bosch\",\"model\":\"Therm 4800i\",\"name\":\"Water heater\"},\"value_template\":\"{{ value_json.chamber }}\",\"unit_of_measurement\":\"°C\",\"device_class\":\"temperature\",\"state_class\":\"measurement\",\"icon\":\"mdi:thermometer-high\"}"},
  {"sensor", "temp_box", "homeassistant/sensor/@DEV@/temp_box/config",
   "{\"name\":\"Chassis temperature\",\"unique_id\":\"@DEV@_temp_box\",\"state_topic\":\"btwh/@DEV@/state\",\"availability_topic\":\"btwh/@DEV@/availability\",\"payload_available\":\"online\",\"payload_not_available\":\"offline\",\"device\":{\"identifiers\":[\"@DEV@\"],\"manufacturer\":\"Bosch\",\"model\":\"Therm 4800i\",\"name\":\"Water heater\"},\"value_template\":\"{{ value_json.temp_box }}\",\"unit_of_measurement\":\"°C\",\"device_class\":\"temperature\",\"state_class\":\"measurement\",\"icon\":\"mdi:thermometer\"}"},
  {"number", "setpoint", "homeassistant/number/@DEV@/setpoint/config",
   "{\"name\":\"Setpoint temperature\",\"unique_id\":\"@DEV@_setpoint\",\"state_topic\":\"btwh/@DEV@/state\",\"availability_topic\":\"btwh/@DEV@/availability\",\"payload_available\":\"online\",\"payload_not_available\":\"offline\",\"device\":{\"identifiers\":[\"@DEV@\"],\"manufacturer\":\"Bosch\",\"model\":\"Therm 4800i\",\"name\":\"Water heater\"},\"command_topic\":\"btwh/@DEV@/set/temperature\",\"min\":35,\"max\":60,\"step\":1,\"mode\":\"box\",\"unit_of_measurement\":\"°C\",\"value_template\":\"{{ value_json.setpoint }}\",\"optimistic\":false}"},
  {"sensor", "water_flow", "homeassistant/sensor/@DEV@/water_flow/config",
   "{\"name\":\"Water flow\",\"unique_id\":\"@DEV@_water_flow\",\"state_topic\":\"btwh/@DEV@/state\",\"availability_topic\":\"btwh/@DEV@/availability\",\"payload_available\":\"online\",\"payload_not_available\":\"offline\",\"device\":{\"identifiers\":[\"@DEV@\"],\"manufacturer\":\"Bosch\",\"model\":\"Therm 4800i\",\"name\":\"Water heater\"},\"value_template\":\"{{ value_json.water_flow }}\",\"unit_of_measurement\":\"L/min\",\"state_class\":\"measurement\",\"icon\":\"mdi:water\"}"},
  {"sensor", "burner_power", "homeassistant/sensor/@DEV@/burner_power/config",
   "{\"name\":\"Burner power\",\"unique_id\":\"@DEV@_burner_power\",\"state_topic\":\"btwh/@DEV@/state\",\"availability_topic\":\"btwh/@DEV@/availability\",\"payload_available\":\"online\",\"payload_not_available\":\"offline\",\"device\":{\"identifiers\":[\"@DEV@\"],\"manufacturer\":\"Bosch\",\"model\":\"Therm 4800i\",\"name\":\"Water heater\"},\"value_template\":\"{{ value_json.burner_power }}\",\"unit_of_measurement\":\"%\",\"state_class\":\"measurement\",\"icon\":\"mdi:fire\"}"},
  {"sensor", "fan_speed", "homeassistant/sensor/@DEV@/fan_speed/config",
   "{\"name\":\"Fan speed\",\"unique_id\":\"@DEV@_fan_speed\",\"state_topic\":\"btwh/@DEV@/state\",\"availability_topic\":\"btwh/@DEV@/availability\",\"payload_available\":\"online\",\"payload_not_available\":\"offline\",\"device\":{\"identifiers\":[\"@DEV@\"],\"manufacturer\":\"Bosch\",\"model\":\"Therm 4800i\",\"name\":\"Water heater\"},\"value_template\":\"{{ value_json.fan_speed }}\",\"unit_of_measurement\":\"rpm\",\"state_class\":\"measurement\",\"icon\":\"mdi:fan\"}"},
  {"binary_sensor", "flame", "homeassistant/binary_sensor/@DEV@/flame/config",
   "{\"name\":\"Flame detected\",\"unique_id\":\"@DEV@_flame\",\"state_topic\":\"btwh/@DEV@/state\",\"availability_topic\":\"btwh/@DEV@/availability\",\"payload_available\":\"online\",\"payload_not_available\":\"offline\",\"device\":{\"identifiers\":[\"@DEV@\"],\"manufacturer\":\"Bosch\",\"model\":\"Therm 4800i\",\"name\":\"Water heater\"},\"value_template\":\"{{ 'true' if value_json.flame else 'false' }}\",\"payload_on\":\"true\",\"payload_off\":\"false\",\"device_class\":\"running\"}"},
  {"sensor", "mode", "homeassistant/sensor/@DEV@/mode/config",
   "{\"name\":\"Operating mode\",\"unique_id\":\"@DEV@_mode\",\"state_topic\":\"btwh/@DEV@/state\",\"availability_topic\":\"btwh/@DEV@/availability\",\"payload_available\":\"online\",\"payload_not_available\":\"offline\",\"device\":{\"identifiers\":[\"@DEV@\"],\"manufacturer\":\"Bosch\",\"model\":\"Therm 4800i\",\"name\":\"Water heater\"},\"value_template\":\"{{ value_json.mode }}\",\"device_class\":\"enum\",\"options\":[\"idle\",\"burning\",\"comfort\",\"unknown\"],\"icon\":\"mdi:fire\"}"},
  {"sensor", "fault", "homeassistant/sensor/@DEV@/fault/config",
   "{\"name\":\"Active fault code\",\"unique_id\":\"@DEV@_fault\",\"state_topic\":\"btwh/@DEV@/state\",\"availability_topic\":\"btwh/@DEV@/availability\",\"payload_available\":\"online\",\"payload_not_available\":\"offline\",\"device\":{\"identifiers\":[\"@DEV@\"],\"manufacturer\":\"Bosch\",\"model\":\"Therm 4800i\",\"name\":\"Water heater\"},\"value_template\":\"{{ value_json.fault }}\",\"icon\":\"{% if value_json.fault == 'none' %}mdi:check-circle{% else %}mdi:alert{% endif %}\"}"},
};
static const size_t kEntityCount = 11;

// Golden pair for selftest: the first entity with a known device id substituted.
constexpr const char* kGoldenTopic =
    "homeassistant/sensor/btwh_eb6f5f/temp_out/config";
constexpr const char* kGoldenPayload =
    "{\"name\":\"Outlet water temperature\",\"unique_id\":\"btwh_eb6f5f_temp_out\",\"state_topic\":\"btwh/btwh_eb6f5f/state\",\"availability_topic\":\"btwh/btwh_eb6f5f/availability\",\"payload_available\":\"online\",\"payload_not_available\":\"offline\",\"device\":{\"identifiers\":[\"btwh_eb6f5f\"],\"manufacturer\":\"Bosch\",\"model\":\"Therm 4800i\",\"name\":\"Water heater\"},\"value_template\":\"{{ value_json.temp_out }}\",\"unit_of_measurement\":\"°C\",\"device_class\":\"temperature\",\"state_class\":\"measurement\"}";

}  // namespace discovery
}  // namespace net

#endif  // BTWH_NET_HA_DISCOVERY_GEN_H
