# MQTT and Home Assistant

The bridge is a one-way radio-to-bus adapter with a small command path. This document says what
goes over the bus, what a consumer may rely on, and how to test it without a heater.

The topics and the field list are **not** documented here by hand: they live in
[`spec/mqtt.json`](../spec/mqtt.json) and are generated into `net/mqtt_contract.h` and
`net/ha_discovery_gen.h`. Byte-level checks are in `net/mqtt_wire.cpp` and run on the board.

---

## 1. Topics

| Topic | Direction | Retained | Payload |
|---|---|---|---|
| `btwh/<device>/state` | bridge → consumer | yes | JSON, one message per A0 frame (~4 s) |
| `btwh/<device>/availability` | bridge → consumer | yes | `online` / `offline` (will) |
| `btwh/<device>/attributes` | bridge → consumer | yes | JSON: firmware, board, heater address, pair state, uptime |
| `btwh/<device>/set/temperature` | consumer → bridge | no | integer `35`..`60`; anything else is ignored |
| `btwh/<device>/cmd` | consumer → bridge | no | JSON or text containing `refresh`, `reboot` or `unpair` |
| `homeassistant/<component>/<device>/<key>/config` | bridge → Home Assistant | yes | discovery document, one per entity |

`<device>` is `btwh_` plus the last six hex digits of the heater's BLE address, lower case
(`5c:f8:21:eb:6f:5f` → `btwh_eb6f5f`). It is derived, never configured, and it survives a
reflash — which is what keeps the Home Assistant entities alive across an update.

### State

```json
{"temp_out":42.3,"temp_in":15.0,"chamber":42.3,"temp_box":24.0,"setpoint":42,
 "water_flow":11.5,"burner_power":47,"fan_speed":1612,"flame":true,
 "mode":"burning","fault":"none","ts":1762584000}
```

Rules a consumer must honour (they are in `consumer_rules` in the contract):

- **A missing field means "not reported yet".** Printing it as 0 makes an unreachable bridge look
  like a cold heater.
- **`fault` is `"none"` or two upper-case hex digits.** Never an empty string.
- **`availability == "offline"` overrides state.** Show `unavailable`; do not keep drawing the
  last value as if it were current.
- **`ts` is the bridge's clock**, not the heater's. A `ts` that stops advancing means the bridge
  is alive but no longer hearing the heater — a different failure from an offline bridge.

`ts` is omitted entirely until SNTP answers, which is why it is absent on a freshly booted board.

---

## 2. How the client behaves

`net/mqtt_client.cpp` is polled from `loop()`. The behaviour that is worth knowing before
debugging it:

| Behaviour | Value | Why |
|---|---|---|
| Reconnect backoff | 2 s, doubling to 60 s | a wrong password must not hammer the broker; the schedule resets after any successful CONNACK |
| Keepalive | 60 s, PINGREQ at 25 s | a lost PINGRESP is forgiven once; a dead broker is noticed well inside the broker's own timeout |
| CONNACK timeout | 4 s | a TCP connection that never answers is worse than a retry |
| Publish cadence | every A0 frame, plus a 60 s heartbeat | `ts` stays fresh, so "bridge up, heater silent" is visible |
| QoS | 0 everywhere | the contract fixes it; state is a snapshot, a lost one is replaced 4 s later |
| Session | clean | nothing to persist: the retained state is rewritten on connect |

The will is `offline` on the retained availability topic. A board that loses power therefore
turns itself grey in Home Assistant without anyone publishing anything.

Commands:

- `refresh` — publish the current snapshot immediately (the client already queues it).
- `reboot` — clean BLE disconnect, then `ESP.restart()`.
- `unpair` — clear the local bond and the stored panel code. The heater keeps its own record;
  clear pairing on the panel before pairing again.

---

## 3. Home Assistant

Eleven entities, all grouped under one device:

| Entity | Component | Notes |
|---|---|---|
| Outlet / inlet / chamber / chassis temperature | sensor | `device_class: temperature`, unit `C` |
| Setpoint | number | 35–60, step 1, bound to `set/temperature`; **not optimistic**, so a refused setpoint does not look applied |
| Water flow | sensor | `L/min` |
| Burner power | sensor | `%`, icon `mdi:fire` |
| Fan speed | sensor | `rpm` |
| Flame | binary_sensor | `device_class: running`, JSON `true`/`false` |
| Mode | sensor | `device_class: enum` with the four contract values |
| Fault | sensor | icon template: `mdi:check-circle` for `none`, `mdi:alert` otherwise |

Discovery is republished retained on every connect. That is deliberate: deleting an entity in
Home Assistant and reconnecting the bridge restores it, with no serial console involved.

`ts` has no entity. It is bridge metadata; exposing it as a sensor would invite automations on a
value that means "the bridge is alive", not "the heater is doing something".

---

## 4. Testing without the heater

Three terminals, no hardware:

```bash
python python/tools/mqtt_dev_broker.py                 # 1. broker on :1883, anonymous
python python/tools/mqtt_watch.py --demo --device btwh_demo01   # 2. fake bridge
python python/tools/mqtt_watch.py --device btwh_demo01          # 3. watch it
```

`--demo` publishes the contract's golden payload through the same renderer the firmware uses, so
what Home Assistant sees is byte-identical to a real frame for those values. Use it to build the
dashboard, then swap in the board by changing only the device id.

With the board in the loop:

```
broker 192.168.110.10            # serial console; restarts the client
status                           # shows "mqtt : up 192.168.110.10:1883 as btwh_eb6f5f"
mqtt                             # prints the payload for the live state
```

Point Home Assistant at the same broker and the device appears under *Settings → Devices* as
"Water heater" (Bosch / Therm 4800i). No YAML.

---

## 5. Known limits

- **The setpoint write path has not been exercised on the heater yet.** The frame is
  byte-identical to the Python reference (`D3 02 55 00 <value>`) and the Python side has driven
  the heater, but this firmware path has only been compiled and reasoned about. Treat the first
  live test as a test, not as a formality.
- **No broker authentication, no TLS.** `broker` accepts `host[:port]` only. On a home LAN behind
  the router that is a defensible first version; it is not a product decision.
- **Attributes do not carry `rssi`.** The contract lists it, but nothing in `ble/` exposes the
  connection's signal strength yet, and publishing a made-up number is worse than omitting it.
- **The portal does not ask for the broker.** It is set with the `broker` command (or NVS). The
  form is one submit for Wi-Fi + heater + code; adding a fourth field is a change to
  `portal_html.h` and the request struct, and it has not been done.
