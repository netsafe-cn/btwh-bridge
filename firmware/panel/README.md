# firmware/panel/ — reserved: touch panel application

**Status: not implemented. This directory holds the design constraints so the structure does
not have to be renegotiated later.**

## What it is

An ESP32-S3 with a touchscreen, mounted wherever a wall socket is convenient — a different
floor from the heater, which is the whole point. It shows the same numbers the app shows and
lets you set the temperature.

```
heater --BLE--> bridge (next to the heater) --MQTT--> panel (any floor)
```

## The one rule

**This directory must never include anything from `firmware/bridge/`, and must never link
`components/bosch_proto`.**

The panel is a consumer of the bridge, not a variant of it. Concretely:

| | why not |
|---|---|
| BLE stack | The heater accepts one connection. Two devices would evict each other, and the panel would need the pairing window at the heater's front panel — defeating the purpose of putting it upstairs. |
| `bosch_proto` | Frame codec is the bridge's job. A panel parsing `A0` frames would mean the protocol lives in two firmwares and drifts. |
| pairing code entry | Nothing to pair. The panel joins Wi-Fi, not the heater. |

## What it may depend on

- [`spec/mqtt.json`](../../spec/mqtt.json) — topics and state fields. Ideally the field list is
  generated into a header at build time; failing that, a compile-time assertion keeps them in
  step.
- An MQTT client and an LVGL (or equivalent) UI toolkit. Nothing from this repository.

## Open decisions (deliberately not made yet)

| Question | Options | Notes |
|---|---|---|
| Board | ESP32-S3 with 480×320 or 800×480 panel | PSRAM matters for LVGL buffers; the bridge's N8R8 already proves the Octal PSRAM path |
| Wi-Fi onboarding | Improv over BLE, or SoftAP captive portal | Improv shares code with the bridge's provisioning work; SoftAP needs no phone app |
| Offline behaviour | last value greyed out, or "unavailable" | Contract rule: `availability: offline` overrides state. Pick the visual, not the semantics. |
| Discovery | subscribe to `btwh/+/state`, or configured device id | One panel per heater is the common case; multi-heater should not need a rebuild |

## Exit criteria for moving from reserved to real

1. `firmware/bridge` publishes `state` and `availability` per the contract
2. The panel renders every field in `state_fields` without special-casing any of them
3. Killing the bridge makes the panel show "unavailable" within one keepalive interval
4. A second panel on the same broker works with no configuration change on the first
