# Architecture and decision records

This document explains **why the project is shaped the way it is**. Read it before changing
the architecture, and append a new ADR at the end when you do.

---

## 1. Layers

```
Home Assistant / Mi Home / Node-RED / any MQTT client
        ^
        | MQTT Discovery (target)
ESP32 firmware
  |-- provisioning   Wi-Fi setup
  |-- pairing        heater BLE authorisation
  |-- telemetry      state reporting / setpoint
  `-- bosch_proto    pure C, no framework, unit-testable on a PC
        ^
        | BLE GATT (FIFO / CREDITS)
Bosch Therm 4800i gas water heater (BLE name BTWH-001-xxxx)
```

The Python side mirrors the same shape:

```
BoschProtocol (pure codec) -> BoschDevice (one session) -> BoschService (resident)
```

The Python reference implementation **is the executable specification** of the protocol;
the C++ side must match it byte for byte.

| Layer | Responsibility | Not responsible for |
|---|---|---|
| `bosch_proto` / `BoschProtocol` | Framing, checksum, stuffing, per-fragment XOR, field parsing | No I/O, no knowledge of Bluetooth |
| Session | Connect, subscribe, single-flight request/response, timeout and resend, login state machine | Reconnection policy |
| Service / firmware main loop | Reconnect backoff, command queue, state cache, reporting | Parsing protocol bytes |
| Integration | MQTT Discovery, HA entity mapping | Any protocol knowledge |

---

## 2. Architecture decision records

### ADR-001 Decouple the protocol from any framework

**Context**: the reverse-engineered protocol is the most valuable asset in the project. Written
inside Arduino `setup()/loop()` it could not be reused under another framework, nor unit-tested
on a PC.

**Decision**: extract it as a pure C library (`bosch_proto`) plus a Python reference
implementation, aligned by the hex vectors in `protocol_vectors.json`.

**Cost**: an extra indirection, and two maintained implementations (C and Python).

**Benefit**: the protocol is unit-tested in CI; swapping firmware frameworks no longer touches
it; ESPHome, Arduino and IDF can all reuse the same code.

---

### ADR-002 ESP-IDF is the firmware trunk, not Arduino or ESPHome

**Context**: Arduino-ESP32 core plus NimBLE-Arduino got a feasibility build running quickly, but
it does not hold up long term.

**Decision**: move the production firmware to ESP-IDF + NimBLE.

**Reasons**:

- NimBLE versions diverge between arduino-esp32 2.x and 3.x; the project is pinned to 1.4.3 and
  has no upgrade path
- 8 MB Octal PSRAM only becomes usable with IDF's memory configuration
- OTA, dual-bank rollback, watchdogs, task priorities and deep sleep are all controllable in IDF
- Reconnection and BLE event handling are more predictable there

**Cost**: a steeper toolchain; contributors must install IDF; the first build is slow.

**Mitigation**: provide a Docker image and one-shot `idf.py` scripts; document the shortest path.

**Note**: ESPHome will not be the trunk, but it can ship in phase 2 as an external component for
users who only want YAML.

---

### ADR-003 Prefer MQTT Discovery over the ESPHome native API

**Context**: both approaches let Home Assistant discover entities automatically.

**Decision**: MQTT Discovery first.

**Reasons**:

- Nothing is bound to a build system: HA, Mi Home, Node-RED and self-hosted services all work
- Users do not have to switch to ESPHome just to integrate
- The state and command model is simple and testable (plain JSON, assertable offline)

**Cost**: the user needs an MQTT broker (HA users usually have one already).

**Alternative**: an ESPHome external component, offered later as the zero-configuration path.

---

### ADR-004 Provisioning and pairing are orthogonal

**Context**: it is tempting to treat "get the ESP32 online" and "make the heater trust it" as one
"pairing mode".

**Decision**: two independent state axes; see `device_fsm.py` and `API.md §9`.

| Axis | States | Persistence |
|---|---|---|
| Provisioning | `unprovisioned → provisioning → connecting → connected / failed` | One-off; credentials in NVS |
| Pairing | `idle → window_wait → pin_ready → bonding → login → online / failed` | Repeatable; the 6-digit code is short-lived |

**Reason**: the consequences of coupling are concrete — changing router would mean walking to the
heater panel to clear pairing, and a heater drop would mean re-entering the Wi-Fi password.
`reset_wifi_only()` explicitly clears Wi-Fi while keeping the bond and the pin.

**Cost**: one more layer of state, and the UI has to show both.

---

### ADR-005 Lock interface stability with a frozen signature snapshot plus golden vectors

**Context**: the project is open source and the community will depend on `bosch_api`. A verbal
promise of "additive only" carries no weight.

**Decision**:

- `api_contract.json` stores the signature snapshot and `tests/test_contract.py` compares against
  it. Removing a symbol, renaming a parameter, changing a default, or turning an optional
  parameter into a required one turns the build red; appending an optional parameter with a
  default is allowed
- `protocol_vectors.json` stores key frames as hex, verified in both Python and C++

**Cost**: every intentional extension needs `--update` and a diff review.

**Benefit**: a breaking change cannot slip into a pull request unnoticed.

---

### ADR-006 The pairing code is entered at runtime, never compiled in

**Context**: the panel code is short-lived (a window of about 60 s) and different every time. Early
firmware hard-coded it, so each change meant a rebuild — and on Windows a full rebuild takes far
longer than the window.

**Decision**: enter the code over serial or the desktop panel at runtime, persist it in NVS, and
use the same digits for both the SMP passkey and `A2/08`.

**Cost**: the first deployment needs one human interaction (unavoidable anyway — a human has to
press the panel to open the window).

**Constraint**: the code must never appear in logs, MQTT reports or `snapshot()`; only the boolean
`has_pin` is published.

---

## 3. Target repository layout

```
├── bosch_proto/            C protocol library (no framework) + vector tests
├── firmware/               ESP-IDF project
│   ├── main/
│   │   ├── provisioning/   Improv Wi-Fi + captive portal fallback
│   │   ├── pairing/        SMP + A2/07 + A2/08 state machine
│   │   ├── telemetry/      state cache / MQTT Discovery
│   │   └── proto_port/     IDF adapter for bosch_proto
│   └── test/               host-side firmware tests
├── python/                 Python reference implementation and tools
├── tests/                  vector and contract tests
├── docs/                   this directory: architecture, protocol, roadmap
└── tools/                  log_diag, capture analysis, vector generation
```

The library modules currently sit at the repository root, with `tests/` and `tools/` alongside
them. Moving them into the layout above would break every published import, so it lands in phase 2
as a single change — no gradual migration.

---

## 4. Roadmap

| Phase | Content | Exit criteria |
|---|---|---|
| **0 · done** | Protocol work, Python reference implementation, test suite (136 tests), ESP32 feasibility firmware | Regression green; firmware boots |
| **1 · current** | Real-heater verification → provisioning/pairing module → MQTT Discovery | All three log stages pass (SMP / A2 / A0); HA discovers the entities |
| **2** | Move to ESP-IDF, OTA, watchdog, hardened reconnection, repository restructure | Seven days without a drop; OTA rollback works |
| **3** | Optional ESPHome external component, field mapping for more models, more language bindings | At least two models verified by the community |

---

## 5. Risks and mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| Vendor firmware changes the protocol | Project stops working | Field parsing is centralised in the protocol layer; raw frames can be reported for fast diagnosis; firmware versions are recorded in the docs |
| Legal pressure from the vendor | Project taken down | No vendor artefacts, no cracking, neutral project name, explicit non-affiliation in the README |
| Device rate-limits connections | Refused after rapid retries | Backoff in the service layer; the firmware also caps its reconnect rate |
| BLE allows one client | Phone app and gateway are mutually exclusive | Documented; provisioning tells the user to close the app |
| Secrets committed by accident | A user's device exposed | `.gitignore`, a CI secrets job, and a sanitised `config.example.json` |
| The two implementations drift | Silent, hard-to-find faults | Golden vectors verified in both directions, enforced by CI |
| Windows path limits / slow builds | Contributors give up | Short-path workspace script; documented; Docker in phase 2 |

---

## 6. Change log

| Date | Change |
|---|---|
| 2026-11-07 | First version: layers, six ADRs, target layout, roadmap, risk table |
