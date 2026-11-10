# Changelog

All notable changes to this project are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this
project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

- **flame entity never turned "on" in Home Assistant.** The state payload carries a JSON
  boolean; HA parses it into a Python bool whose template rendering is `True` (capital T),
  which never string-matches `payload_on: "true"`. The generated `value_template` now renders
  the literals itself (`{{ 'true' if value_json.flame else 'false' }}`), and the contract test
  pins the template so it cannot regress.
- **setpoint commands from HA were silently dropped.** The number entity publishes
  `str(float)` - `"42.0"` - and the parser used `strtol`, which stops at the decimal point and
  rejects the text. Now parsed with `strtod`; whole values only (`"42.5"` is still a range
  error, not something to round).
- **a retained `cmd` could reboot the board forever.** A retained `reboot` replays on every
  reconnect: reboot -> reconnect -> replay. `PublishInfo.retained` was parsed but never used;
  commands on `cmd` are now live-only. `set/temperature` deliberately still honours retained
  replay, because re-applying the last setpoint is idempotent.

### Added

- **Internationalisation positioning in both READMEs.** Each gained a "Why this
  exists" section: the official app is single-phone Bluetooth with no local API,
  and in mainland China Bosch has left the market, so the app cannot be downloaded
  at all. A compatibility note now distinguishes the verified unit (CN-market
  JSG27-CSI, an EU-built import) from untested EU units - same model, same app +
  BLE control path, EU field reports welcome. Status went honest-to-current: MQTT
  / HA discovery is verified end-to-end (11 entities, setpoint round-trip, against
  a production Mosquitto on HAOS) and the offline test count is 167. The Mi Home /
  Bemfa path is explicitly marked "China region, optional, not the core path" -
  the core is local MQTT + Home Assistant. Also repaired a comparison table that
  had lost its header row.
- **Broker credentials are configurable, end to end.** The production broker (Mosquitto on
  the HAOS host) refuses anonymous connections, so the account must travel with the config:
  the portal gained a step 4 (address, username, password; empty = keep the stored values,
  the same rule as the Wi-Fi password), `Config` persists `mqttUser`/`mqttPass` in NVS,
  `startMqtt()` hands them to the client after every `mqttBegin()`, and the serial console
  grew `mqttuser <name> <password>` beside `broker`. `mqttSetCredentials()` - written with
  the client but dead until now - is live. `mqttBegin()` no longer blanks the stored account
  (a broker address change used to silently downgrade an authenticated connection to an
  anonymous one). `test_broker_config_contract.py` (8 tests) pins the whole chain, because
  one renamed arg anywhere in it means a well-formed CONNECT the broker still refuses.
  The portal path also restarts the client after a save, which it never did - a broker set
  through the page did not take effect until a reboot.
- **Host runner for the four C++ selftest suites** (`tools/host_selftest.py` +
  `tools/host_selftest_main.cpp`): compiles the framework-free layers (proto, and
  net/state_json|mqtt_wire|discovery) with g++/clang++ - or `pip install ziglang` where neither
  exists - and runs the same suites the board's `selftest` command runs. Wired into
  `run_tests.py` via `test_cpp_selftest.py` (skips without a compiler; ubuntu CI compiles it
  with g++). Reason: the first hardware run of the discovery suite caught a check asserting a
  12-byte fit as 10 - a check that had executed nowhere until the board ran it, because no host
  runner existed. The check counts (proto 60 / net 16 / mqtt 32 / discovery 22 = 130) are pinned
  on both sides.
- **MQTT client** (`net/mqtt_wire.cpp`, `net/mqtt_client.cpp`): a 3.1.1 subset written for this
  project instead of a library, because the payload, the will and the backoff all have to match
  the contract exactly and none of the Arduino clients expose them at that level. Publishes
  `state` (retained, one per A0 frame), `attributes` (retained, on connect) and `availability`
  (retained `online`; the **will** carries `offline`). Subscribes `set/temperature` and `cmd`.
  Backoff 2 s → 60 s, keepalive 60 s with a ping at 25 s. Measured: the wire format is
  self-tested green on the board; the socket path still awaits a live broker, see "status"
  below.
- **Home Assistant discovery**, generated from `spec/mqtt.json` by `tools/gen_ha.py` into
  `ha_discovery_gen.h`: 11 entities (9 sensors, 1 number for the setpoint, 1 binary sensor for
  the flame), each naming the same state topic, the same availability topic and a stable
  `unique_id` derived from the device id. Republished retained on every connect, so deleting an
  entity in HA and reconnecting the bridge is enough to get it back. `--check` in CI.
- **BLE write path**: `app::setSetpoint()` sends `D3` with parameters `02 55 00 <value>`,
  byte-identical to `bosch_api.set_temperature`. The reply is deliberately not awaited - the next
  A0 broadcast carries the authoritative setpoint, so a consumer sees what the heater accepted.
- `broker <host[:port]>` serial command, and `status` now prints the broker state and its last
  failure reason. `broker` with no argument prints the address, `broker -` clears it.
- `python/tools/mqtt_dev_broker.py` - anonymous amqtt broker for the development loop.
- `python/tools/mqtt_watch.py` - watches `btwh/#` and prints state as a table; `--demo` publishes
  the contract's golden payload as a fake bridge, so Home Assistant and the future touch panel can
  be built while the heater is elsewhere.
- `net` self test grew two suites: `mqtt` (33 checks on the packet bytes, including a packet
  arriving one byte at a time and a two-byte remaining length) and `discovery` (12 checks,
  including the golden topic/payload pair). `python/tests/test_ha_discovery.py` (7 tests) holds
  the generated table to the contract. Total 157 offline tests.

### Fixed

- `net/state_json.h` no longer defines `SelfTestResult` / `LogFn` itself; they moved to
  `net/selftest.h` so state_json, mqtt_wire and discovery can share one type without one
  including another's header.

- `firmware/bridge/src/net/` — the layer that will speak MQTT, started with the part that does
  not need a network: `mqtt_contract.h` is **generated** from `spec/mqtt.json`
  (`tools/gen_mqtt.py`, `--check` in CI), and `state_json.cpp` renders the payload from a plain
  snapshot. No Arduino, no NimBLE, no MQTT library, so the bytes can be checked off-target.
- `net` self test, run by the same `selftest` command as `proto`: 16 checks covering the whole
  payload byte for byte against a golden string the generator rendered, `ts` omission when there
  is no clock, `fault` as `"none"`/`"E1"`, every `mode` word, and the rounding rule.
- Provisioning. SoftAP + captive DNS + one HTML form that takes the heater (picked from a
  `BTWH-*` scan), the Wi-Fi network, the six-digit panel code and the broker in a single
  submit; input is validated **before** anything reaches the radio, because a malformed attempt
  still spends one of the heater's three failures. `portal` serves the same page on the LAN
  address when the board is already online, so re-pairing does not require anything to leave its
  current network. Wi-Fi credentials added to `Config` with `saveNetwork()` / `wipe()`.
- `wifi <ssid> <password>` and boot auto-join: a board that has credentials in NVS rejoins the
  network at every boot, after the heater session is up. Without it a power cut needed a human.
- Station Wi-Fi + SNTP (`net/station.cpp`): 15 s join budget with a named failure, CN NTP pool
  with the global pool as fallback, UTC throughout. `ts` joins the payload as soon as the clock
  answers and stays absent until then, per `consumer_rules[0]`.
- `mqtt` serial command — prints the payload for the **live** state, so the exact bytes can be
  inspected before any broker exists. First run with real data:
  `{"temp_out":28.0,...,"flame":false,"mode":"idle","fault":"none"}` (no `ts`, correctly).
- Number formatting is now fixed by the contract, not by each implementation: **half away from
  zero**. Python's `"%.1f"` is half-to-even, which would have made 42.25 render as `42.2` on the
  host and `42.3` on the bridge; a self-test case pins the rule down.
- `firmware/bridge/src/proto/` — the protocol layer as its own directory, with no Arduino and no
  NimBLE include. `frame.{h,cpp}` holds framing, stuffing, per-fragment XOR, checksum and stream
  reassembly; `a0.{h,cpp}` turns a broadcast into an `A0State`. Parsing is now separate from
  printing, which is what makes it checkable at all.
- `selftest` serial command — replays `spec/protocol_vectors.json` **on the board**: rebuilds
  every frame from its parameters, stuffs it, XORs it fragment by fragment, feeds the fragments
  back through the reassembler, corrupts a checksum on purpose, and compares every A0 field.
  Verified on hardware, 60 checks.
- `tools/gen_vectors.py` — generates `src/proto/vectors_gen.h` from the JSON contract.
  `--check` fails when the header is stale, so the two cannot drift.
- `tools/watch_a0.py` — prints the A0 broadcast as a table with per-field ranges. Answers "is
  the data alive and following the heater", which `log_diag.py` (connection failures) does not.
  Optional `--csv` and `--only-active`.
- `AGENTS.md` and `MEMORY.md` — build commands, layer rules, and the facts that were expensive to
  establish (the panel code is also the SMP passkey; `A2/07=00` means the window is closed, not
  that the MAC is unauthorised; three failures trigger a three-minute rate limit).
- `firmware/bridge/src/ble/` — the only directory that includes NimBLE: connect, SMP callbacks,
  FIFO/CREDITS subscribe, fragment reassembly, chunked writes, scan. It reports "encrypted /
  bonded" and stops there; whether that means the heater accepted the code is not its business.
- `firmware/bridge/src/auth/` — `A2/07` + `A2/08` as a state machine behind a `Transport` of three
  function pointers (`send`, `wait`, `log`). No NimBLE, no Arduino, no `Serial`, so a fake
  transport can replay the whole pairing-failure matrix without a heater in the room. The outcome
  codes mirror `bosch_api.PAIR_FAIL_*` name for name, and `classify()` maps the two replies onto
  them exactly as the Python side does.
- `firmware/bridge/src/app/` — `config.{h,cpp}` (NVS, defaults are placeholders) and
  `session.{h,cpp}` (order of operations, A0 state, link-drop recovery, 30 s watchdog).
- `python/tests/test_firmware_contract.py` — five tests that fail when the firmware's reason codes
  drift from the Python API, when `reasonText()` stops returning a code the Python side has, or
  when the reply line stops printing two hex digits.

### Changed

- `src/main.cpp` went from 672 to 604 lines by moving the codec out, and now from 604 to 273 by
  moving the radio, the login and the session out. What is left is `setup()`, `loop()` and the
  serial console. The firmware still links the same heater and still streams `A0` unchanged —
  verified after flashing, not assumed.
- **No committed `.ino` any more.** `gateway_cli.py` generates a sketch shell around `src/` at
  build time, so there is one copy of the sources instead of a hand-synced duplicate.
- CI gained three guards: `proto/` and `auth/` may not include Arduino, NimBLE or ESP-IDF, and the
  generated vector header must match `spec/protocol_vectors.json`. `auth/` is guarded for the same
  reason as `proto/`: being testable off-target is the only thing that makes the layer worth its
  own directory, and one `Serial.println()` ends that.

### Fixed

- `scan` printed a literal `//n` instead of a newline (`devices://n`).
- `parseA0` now exposes `fan_power`, `mode`, `max_setpoint` and `bathroom`, including the Type0
  offsets that only existed in the Python reference. The printed `[A0]` line is unchanged.
- `A2/07` and `A2/08` replies were logged with `%d` in one of the two places that print them, so a
  rate-limit reply `3C` came out as `60`. `log_diag.py` matches two hex digits, silently ignored
  `60`, and fell back to an earlier line — the diagnosis looked healthy while the device was
  refusing the code. Every reply is now printed as `%02X` from a single place.
- Build: arduino-cli compiles a *copy* of `src/` under `<build>/sketch/`, while the layered
  include path resolves back to the original tree. The same header then exists at two paths,
  `#pragma once` keys on the file rather than the name, and the build died with
  `redefinition of 'struct app::Config'`. Every header now carries a macro guard as well, and
  `gateway_cli.py` puts `src/` on the include path so `"app/session.h"` means the same thing to
  arduino-cli and to PlatformIO.
- `log_diag.py` misdiagnosed the state measured on 2026-11-08: link up, both CCCDs subscribed,
  `A2/07` silent, then the heater hangs up ~12 s later. It blamed GATT enumeration. It now
  recognises the signature and gives the advice the measurements actually support — stop retrying
  and leave the device alone for 10-15 minutes. The first version of this advice told the user to
  clear pairing and enter a new code; that was wrong, and the same stored code authorised
  afterwards with no panel action. This is also *not* the `A2/07 = 00` case: there was no reply
  at all.
- `reboot` used to call `ESP.restart()` with the BLE link still up. The radio drops, the heater
  keeps a half-open session, and the next connection is refused for ~15 minutes — the longest
  outage in this project so far, and it looked exactly like a broken pairing. Measured twice on
  2026-11-08: a manual `reboot` at 14:58, and closing/reopening the control panel at 15:33.
  `app::shutdown()` now sends a proper LL disconnect and waits 300 ms before the CPU resets.
  A/B on the same device and the same stored code: the old path cost ~17 minutes, the new one
  reached `A2/08=01` 6.4 s and the first `A0` 9.5 s after the command.
- Both host consoles reset the board just by attaching, so every panel reopen paid that
  15-minute cost. `esp32_panel.py` and `gateway_cli.py` now open the port and send `status`
  instead of `reboot`; a reset is an explicit button, not a side effect of looking.

### Planned

- MQTT publishing from the bridge against `spec/mqtt.json`
- `bosch_proto` extracted into `firmware/components/`
- Wi-Fi provisioning (Improv) implemented in firmware
- Touch panel application in `firmware/panel/`
- Home Assistant integration in `integrations/`

## [0.2.0] - 2026-11-08

### Added

- `spec/` — cross-language contracts as machine-checked artefacts: `api_contract.json`,
  `protocol_vectors.json`, and `mqtt.json`, the bridge/consumer contract (topics, state
  fields, units, ranges, availability rules). `test_mqtt_contract.py` fails if a field named
  there stops existing on `DeviceState`, so the contract cannot drift from the code
- `firmware/panel/` — reserved directory with the design constraints for the touchscreen
  panel, and a `layout` CI job that greps for an `#include` crossing the application
  boundary so the reservation is enforced rather than suggested
- `integrations/` — reserved for Home Assistant, Node-RED and ESPHome glue
- ADR-007 records the monorepo split and why the panel is an application, not a module
- 145 offline tests (was 136)

### Changed

- **The repository is now split by role, not by file type.** `python/` holds the reference
  implementation, `firmware/<application>/` holds buildable firmware, `spec/` holds the
  contracts, `docs/` holds technical documentation. Top-level entries dropped from 23 to 14
  files, and `esp32/` — which mixed firmware, board manuals, toolchain and serial logs — is
  gone
- **Command paths moved with the sources**: `python python/tools/bosch_ui.py`,
  `python python/tests/test_contract.py --update`, `python firmware/bridge/tools/log_diag.py`.
  `python run_tests.py` is unchanged, and the Windows launchers under `python/tools/` still
  work by double-click
- **`import bosch_api` did not move.** `pyproject.toml` maps the package root to `python/`
  through `package-dir`, so the import name stays flat and the frozen contract stays valid;
  a layout change is not a breaking change here
- Every document, code comment, docstring, log line, exception message and firmware serial
  line is now English. `README.zh-CN.md` is the single Chinese file, kept as an entry point
  for Chinese-speaking users. Match on exception types and state enums, never on message text

### Fixed

- Firmware: the A0 line printed `fan 706rpm flameno faultnone`; the values now have their own
  space
- Firmware: one `Serial.printf` carried a full-width comma (`，`), invisible to a scan that
  only looked for CJK ideographs. The scan now covers full-width punctuation
- Firmware: real device addresses had only ever lived in the compiled defaults, never in NVS,
  so replacing them with placeholders made the board boot pointing at `AA:BB:CC:DD:EE:FF`.
  They are now written to NVS with the `addr` / `mac` commands, which is what the
  placeholders were for

### Verified

- Power-cycle recovery on hardware: a stored pairing code reconnects unattended
  (`A2/07=01` -> `A2/08=01` -> `A0` stream)
- Firmware rebuilt and reflashed with the English output; `log_diag` replayed against
  synthetic English logs before flashing, so the diagnostics never lost their match strings

## [0.1.0] - 2026-11-07

### Added

- Python reference implementation (`bosch_api`) with protocol codec, device session and a
  resident service featuring reconnect backoff and a command queue
- Framework-agnostic provisioning/pairing state machine (`device_fsm`), with provisioning
  and pairing deliberately kept orthogonal
- Desktop console, headless bridge, and optional Mi Home bridge over Bemfa cloud
- ESP32-S3 gateway firmware v2 with BLE SMP passkey support, runtime pairing-code entry
  and power-cycle auto-recovery
- Log analyser (`log_diag`) that turns the failure matrix into executable diagnosis
- 136 offline tests, including a signature-freeze contract and cross-platform golden
  vectors shared with the C++ implementation
- Bilingual documentation (English / Simplified Chinese) and architecture decision records

### Verified

- Real-device link-up on a `BTWH-001`: SMP encryption, `A2/07`, `A2/08=01`, steady `A0` stream
- A pairing code remains valid for reconnection after the pairing window has closed
  (verified 11 minutes later; the upper bound is still unknown)

[Unreleased]: https://github.com/netsafe-cn/btwh-bridge/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/netsafe-cn/btwh-bridge/releases/tag/v0.2.0
[0.1.0]: https://github.com/netsafe-cn/btwh-bridge/releases/tag/v0.1.0
