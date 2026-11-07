# Changelog

All notable changes to this project are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this
project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed

- Runnable helpers moved into `tools/` and the test suite into `tests/`. Invoke them as
  `python tools/bosch_ui.py`, `python tools/bosch_bridge.py`, `python tools/bosch_read.py`,
  and refresh the contract with `python tests/test_contract.py --update`. `python
  run_tests.py` is unchanged. Library modules stay at the repository root, so
  `import bosch_api` is unaffected.

- Exception messages are now English (`GATT not ready`, `not connected`). Match on the
  exception type, not on the message text.

### Planned

- MQTT Discovery integration for Home Assistant
- Wi-Fi provisioning (Improv) implemented in firmware
- Migration of the firmware to ESP-IDF
- Field verification on additional heater models

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

[Unreleased]: https://github.com/netsafe-cn/btwh-bridge/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/netsafe-cn/btwh-bridge/releases/tag/v0.1.0
