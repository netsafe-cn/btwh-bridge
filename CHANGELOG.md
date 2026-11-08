# Changelog

All notable changes to this project are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this
project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

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
- `AGENTS.md` and `MEMORY.md` — build commands, layer rules, and the facts that were expensive to
  establish (the panel code is also the SMP passkey; `A2/07=00` means the window is closed, not
  that the MAC is unauthorised; three failures trigger a three-minute rate limit).

### Changed

- `src/main.cpp` went from 672 to 604 lines by moving the codec out. The firmware still links the
  same heater and still streams `A0` unchanged — verified after flashing, not assumed.
- **No committed `.ino` any more.** `gateway_cli.py` generates a sketch shell around `src/` at
  build time, so there is one copy of the sources instead of a hand-synced duplicate.
- CI gained two guards: `proto/` may not include Arduino, NimBLE or ESP-IDF, and the generated
  vector header must match `spec/protocol_vectors.json`.

### Fixed

- `scan` printed a literal `//n` instead of a newline (`devices://n`).
- `parseA0` now exposes `fan_power`, `mode`, `max_setpoint` and `bathroom`, including the Type0
  offsets that only existed in the Python reference. The printed `[A0]` line is unchanged.

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
