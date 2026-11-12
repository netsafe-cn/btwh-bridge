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
| `btwh/<device>/state` | bridge → consumer | **no — live-only, enforced by tests** | JSON, one message per A0 frame (~4 s) |
| `btwh/<device>/availability` | bridge → consumer | yes | `online` / `offline` — **liveness of the bridge**, will-set |
| `btwh/<device>/link` | bridge → consumer | yes | `online` / `offline` — **freshness of the readings**: `offline` after 90 s with no A0 frame, and until the first one arrives |
| `btwh/<device>/attributes` | bridge → consumer | **no — live-only** | JSON: firmware, board, heater address, pair state, uptime, RSSI, last disconnect (reason + epoch); refreshed every 60 s. `rssi` and `last_disconnect_at` are `null` when unknown, never `0` |
| `btwh/<device>/set/temperature` | consumer → bridge | no | integer `35`..`60`; anything else is ignored |
| `btwh/<device>/cmd` | consumer → bridge | no | JSON or text containing `refresh`, `reboot` or `unpair` |
| `btwh/<device>/ota` | consumer → bridge | **no — live-only, enforced by tests** | `http://host[:port]/path`, or the literal `install` to flash the URL last seen on `update/url` |
| `btwh/<device>/update/latest` | consumer → bridge | yes | latest available version, one printable line; published by whoever owns the manifest |
| `btwh/<device>/update/url` | consumer → bridge | yes | the image URL backing `update/latest` |
| `btwh/<device>/update/state` | bridge → consumer | yes | JSON: `{"installed_version":...,"latest_version":...}`; republished on connect, manifest change and the 60 s heartbeat |
| `homeassistant/<component>/<device>/<key>/config` | bridge → Home Assistant | yes | discovery document, one per entity |

The two update-manifest topics and the `ota` topic form a deliberate mirror pair, and
`test_mqtt_contract.py` pins it: a **retained** `ota` URL would reflash the bridge on every
reconnect (a broker-triggered boot loop), while a **non-retained** manifest would leave a
rebooted bridge believing it is up to date. Actions are live-only; the manifest is state.
`update/state` carries `latest_version` only after a manifest has been seen, so Home Assistant
shows `unknown` rather than a fake "no update available".

**Retain status, never readings.** `state` and `attributes` carry measurements, so neither is
retained: a reading that survives the bridge that took it keeps answering from the broker, and
every subscriber that does not also read `link` takes it for the truth. `availability`, `link`,
`update/state` and the discovery documents are status — `offline` is a true statement about a
bridge that stopped, and a consumer that restarts needs it without waiting for a message that
may never come. Dropping the flag costs a late consumer one interval: the bridge republishes
`state` on every A0 frame (~4 s) and `attributes` on every connect and every 60 s, so the
retained copy would have been at most four seconds ahead. Home Assistant reaches the same
conclusion for sensors (`sensor.mqtt`, `expire_after`): a broker replaying an expired value at
startup is the failure to design out.

Upgrading a board that still retained its readings is a **one-time manual step**: the broker
keeps the last retained payload until something overwrites or deletes it, and a live-only
publisher never will. After the first boot on this build, clear both topics once -
`mosquitto_pub -r -n -t btwh/<device>/state` and the same for `attributes`, or an empty retained
publish from any client. OTA cannot do this for you: the retained entry belongs to the broker,
not to the firmware. Nothing on the Home Assistant side changes either way, because `link`
already greyed the entities out.

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
- **Liveness and freshness are two claims, and a reading needs both.** `availability` says the
  bridge process is running; `link` says the heater answered inside the 90 s window. Either one
  `offline` means the numbers are not measurements of now: show `unavailable`, do not keep
  drawing the last value. This is not a theoretical distinction — on 2026-11-12 Home Assistant
  displayed a 42.3 °C outlet and a burning flame that were **two days old**, because
  `availability` was `online` the whole time the bridge was up and deaf.
- **`ts` is the bridge's clock**, not the heater's, and it is the timestamp of the frame the
  payload was built from - it does not advance on a heartbeat. If the bridge dies the will covers
  it; if it merely stops hearing the heater, `link` goes `offline` within 90 s.

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
| Publish cadence | every A0 frame, plus a 60 s heartbeat | a changed value reaches the broker within one frame; the heartbeat keeps `uptime`/RSSI and the update entity current |
| Freshness window | 90 s without an A0 frame → `link` = `offline` | 22 missed frames at the 4 s cadence: rides out a reconnect, short enough that a reading is never mistaken for the current one |
| QoS | 0 everywhere | the contract fixes it; state is a snapshot, a lost one is replaced 4 s later |
| Session | clean | nothing to persist: `state` is live-only, so a consumer that reconnects waits for the next A0 frame instead of being handed the last one |

The will is `offline` on the retained availability topic. A board that loses power therefore
turns itself grey in Home Assistant without anyone publishing anything. The retained `link`
topic is the second, independent signal: it goes `offline` when the heater stops answering and
comes back with the first frame of the new session, so a bridge that is up but deaf is
described accurately instead of looking healthy.

Commands:

- `refresh` — publish the current snapshot immediately (the client already queues it).
- `reboot` — clean BLE disconnect, then `ESP.restart()`.
- `unpair` — clear the local bond and the stored panel code. The heater keeps its own record;
  clear pairing on the panel before pairing again.

---

## 3. Home Assistant

Twenty-two entities, all grouped under one device — one water heater, eleven measurements,
a problem binary sensor, eight diagnostics, and one firmware-update entity. Measurements and the
heater card bind
**both** availability topics with `availability_mode: all`, so a stale frame greys them out;
the eight diagnostics and the update card bind `availability` alone, because pairing state,
RSSI and "install firmware" are facts about the bridge and must stay readable exactly when the
heater is unreachable. Every discovery payload carries an `origin` block naming `btwh-bridge` and
the same `device` map, whose `sw_version` is generated from `kFirmwareVersion` — the number the
compiler sees — so the device page cannot claim a version the board is not running. The entities:

| Entity | Component | Notes |
|---|---|---|
| **The heater itself** | **water_heater** | current temperature (outlet), target 35–60 via `set/temperature`, mode `gas`/`off` from flame; see below |
| Outlet / inlet / chamber / chassis temperature | sensor | `device_class: temperature`, unit `°C` |
| Setpoint | number | 35–60, step 1, bound to `set/temperature`; **not optimistic**, so a refused setpoint does not look applied |
| Water flow | sensor | `L/min` |
| Burner power | sensor | `%`, icon `mdi:fire` |
| Fan speed | sensor | `rpm` |
| Flame | binary_sensor | `device_class: running`, JSON `true`/`false` |
| Mode | sensor | `device_class: enum` with the four contract values |
| Fault | sensor | the code itself: icon template `mdi:check-circle` for `none`, `mdi:alert` otherwise |
| Fault active | binary_sensor | `device_class: problem`, `true` whenever the code is not `none`. The same field twice on purpose: a sensor cannot carry `problem`, and an automation that asks "is something wrong" should not template a string comparison |
| Firmware / board / heater address | sensor | diagnostics, from the attributes topic |
| Uptime | sensor | `device_class: duration`, refreshes once a minute |
| BLE RSSI | sensor | `device_class: signal_strength`, `dBm`; `null` (→ unknown) while the link is down, because a real reading is always negative |
| Pairing state | sensor | `device_class: enum` |
| Last disconnect reason / time | sensor | diagnostics; survives the reconnect, so a flapping link is visible while it happens to be up. Time is `unknown` while the link has never dropped, and is formatted as ISO 8601 because `device_class: timestamp` cannot parse an epoch integer |
| **Firmware update** | **update** | `device_class: firmware`, no entity category (an update is a primary control, not configuration); state from `update/state`, Install publishes `install` to the live-only `ota` topic — the same single flasher the bench tool drives. Version comparison in HA is string-based, so only plain semver builds should be offered to it |

### Surviving a Home Assistant restart

Discovery is retained, so a restarted Home Assistant rebuilds the entity list from the broker
on its own. What it cannot rebuild is the live-only half: `state` and `attributes` are never
retained, so a fresh consumer has no readings and no diagnostics until the next A0 frame and the
next heartbeat. The bridge therefore subscribes to `homeassistant/status` — Home Assistant's own
birth topic, and the only subscription it makes outside its own tree — and on `online`
republishes discovery, `availability`, `link`, `attributes` and `update/state`, and queues
`state` for the next poll. `offline` (Home Assistant's will) is ignored: the bridge keeps
publishing either way, and a retained discovery document is what gets the next start up.

### The water_heater domain entity

The measurements above make the heater *visible*; the `water_heater` entity makes it *a heater*
in Home Assistant's eyes: the dedicated card, voice control ("set the water heater to 45"),
the energy dashboard, and Matter/HomeKit bridging Home Assistant already provides.

| Home Assistant concept | Source | Topic |
|---|---|---|
| Current temperature | `temp_out` (outlet) | `state` JSON |
| Target temperature | `setpoint` | `state` JSON, confirmed by echo — never optimistic |
| Set temperature command | — | the existing `set/temperature` topic, unchanged |
| Mode | `flame` → `gas` while firing, `off` while idle | `state` JSON, read-only |
| Range / unit | 35–60 °C, step 1 | `temperature_unit: "C"` — the water_heater schema takes `C`/`F`, not the degree sign the sensor schema demands |

There is no `mode_command_topic`: a tankless heater has no switchable mode. `gas` means the
burner is firing; `off` means idle. When Home Assistant's MQTT water_heater grows an
`action_topic`, `HEATING`/`IDLE` should replace this mapping.

Everything the entity touches is a v1 contract topic — it is an adapter view, not a contract
change. A Node-RED flow or the touch panel that ignores Home Assistant sees nothing different.

The diagnostics are the remote-troubleshooting surface: a user on another continent with no
serial access reads firmware, RSSI and the last disconnect reason straight from the device page.
They are published on the attributes topic, refreshed on the same 60 s heartbeat as the state
heartbeat, so uptime and RSSI never age more than a minute.

Discovery is republished retained on every connect. That is deliberate: deleting an entity in
Home Assistant and reconnecting the bridge restores it, with no serial console involved.

`ts` has no entity. It is bridge metadata; exposing it as a sensor would invite automations on a
value that means "the bridge is alive", not "the heater is doing something".

### Contract versioning

The MQTT topics and the JSON keys inside `state`/`attributes` are the contract surface; the
Home Assistant discovery payloads are adapters over it and change freely. The contract itself
is versioned in `spec/mqtt.json` (`contract_version`):

- **minor** (1 → 2): a new topic, a new JSON field. Consumers ignore what they do not know;
  nothing breaks. No transition needed.
- **major** (1 → 2 with a breaking note): a renamed key, a moved topic, a changed meaning.
  The bridge must publish old and new in parallel for one major cycle, and `CHANGELOG.md`
  names the release that drops the old form.

**v1 freezes with the first public release.** Until then the file changes freely — that
freedom is the point of not having users yet.

### Where the numbers come from

The setpoint range (35..60 C) is stated in three places in this contract — the
`set/temperature` topic, the `setpoint` state field, and the water_heater entity — but it is
*owned* by one file: `spec/profiles.json`, one row per heater model, which also carries the
write-parameter encoding and the identity Home Assistant shows. `gen_mqtt.py` refuses to
generate when any copy in this file disagrees with the profile row, the net layer gets
`kSetpointMin/kSetpointMax` generated from it, the app layer validates and builds D3 writes
from it, and `test_profiles.py` pins the Python side to the same numbers. A new heater model
is a new row in that file, not a firmware edit.

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
mqttuser btwh <password>         # optional: broker credentials, persisted in NVS
status                           # shows "mqtt : up 192.168.110.10:1883 as btwh_eb6f5f"
mqtt                             # prints the payload for the live state
```

Point Home Assistant at the same broker and the device appears under *Settings → Devices* as
"Water heater" (Bosch / Therm 4800i). No YAML.

The OTA paths have one-line bench drivers (live-only publish, never retained):

```bash
python python/tools/mqtt_watch.py --ota http://192.168.137.1:8000/firmware.bin --device <id>
python python/tools/mqtt_watch.py --manifest 2.8.1,http://192.168.137.1:8000/firmware.bin --device <id>
```

`--ota` flashes immediately; `--manifest` publishes the retained version/URL pair that turns
the Home Assistant update entity on, whose Install button then flashes the same way.

---

## 5. Known limits

- **The setpoint write path has not been exercised on the heater yet.** The frame is
  byte-identical to the Python reference (`D3 02 55 00 <value>`) and the Python side has driven
  the heater, but this firmware path has only been compiled and reasoned about. Treat the first
  live test as a test, not as a formality.
- **No TLS anywhere, by design.** Broker credentials are supported (`mqttuser <name>
  <password>` over serial, or the provisioning portal form), and the OTA flasher takes plain
  `http://` URLs only — it carries no TLS stack. On a home LAN behind the router that is a
  defensible first version; it is not a product decision. An https image URL is refused at
  parse time, so it fails loudly rather than falling back to something unverified.
- **OTA has no rollback logic of its own.** `Update.end(true)` validates before the reboot,
  so a corrupt image never reaches the boot slot; the arduino-esp32 rollback-on-boot mechanism
  is not additionally configured yet.
- **One week attached to a real heater is the remaining v1 gate.** The bench record so far:
  a 25-minute soak with zero heartbeat drift (60 s ± 0.1 s), a forced AP outage detected in
  83 s (keepalive semantics) and recovered in 6 s, and the backoff schedule (32 → 64 → 60 s
  capped) matching the pinned sequence second for second.
