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

**Decision**: two independent state axes; see `python/device_fsm.py` and `API.md §9`.

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

### ADR-007 Monorepo split by role, with the panel as a separate application

**Context**: a second ESP32 is planned — a touchscreen panel on another floor. It needs the same
numbers but not the same code: the heater allows one BLE connection, so a panel that talked to it
directly would evict the bridge and need the pairing window at the heater's own front panel.

**Decision**: one repository, split by role rather than by file type.

- `firmware/<application>/` — one directory per buildable application; applications never link
  each other
- `firmware/components/` — the only place C is shared, and it may not depend on an application
- `spec/` — the only thing applications share: topics, field names, units and ranges, as JSON
- `python/` — reference implementation, with `package-dir` keeping `import bosch_api` flat

**Alternatives rejected**: two repositories (the panel would need `spec/` via submodule or a
release cadence, which is hostile to outside contributors); a single firmware with a compile-time
`#ifdef PANEL` (both applications would carry the other's dependencies, and a panel bug could only
be reproduced by flashing a bridge).

**Cost**: contributors have to learn where things belong — mitigated by a CI job that fails on an
`#include` crossing a boundary, and by a `README.md` in every top-level directory.

**Enforcement**: `test_mqtt_contract.py` keeps `mqtt.json` aligned with `DeviceState`; the `layout`
CI job keeps the firmware boundaries intact.

---

### ADR-008 Firmware is layered by dependency direction, not by feature

**Context**: the firmware is one 672-line `main.cpp`. Adding MQTT, Wi-Fi provisioning and OTA to
it would produce a file nobody can review, and a protocol change would risk the network code.
The obvious split — "bluetooth part, protocol part, config part" — is wrong, because "protocol"
means two different things (BLE frames and MQTT/HA discovery) and they sit at opposite ends of
the dependency chain.

**Decision**: split by what a module is allowed to know, so the dependency arrows only ever
point one way.

```
diag/  ─┐
net/   ─┼─>  app/  ──>  auth/  ──>  ble/  ──>  proto/
        │   (session,   (A2/07,    (NimBLE)   (frames,
        │    config)     A2/08)                A0 fields)
        └─ anyone may use config; nothing may use diag
```

| Layer | May include | Must not know about | Testable off-target |
|---|---|---|---|
| `proto/` | `<stdint.h>`, `<string.h>` | Arduino, NimBLE, MQTT | **yes** — this is the point |
| `ble/` | NimBLE | pairing semantics, MQTT | no |
| `auth/` | `proto/` frame API, a `send/wait` callback | NimBLE types | yes, with a fake transport |
| `app/` | `ble/`, `auth/`, `proto/`, NVS | MQTT topics | partly |
| `net/` | `app/` state snapshot, generated contract constants | NimBLE, frame bytes | no |
| `diag/` | everything | — | no |

Two rules carry most of the value:

- **`proto/` is the only code that can be tested without hardware.** It is also the only code that
  has a machine-readable spec (`spec/protocol_vectors.json`). Nothing else in the firmware can
  claim that.

  The day a host compiler would be available arrived: `tools/host_selftest.py` compiles the
  framework-free layers (`proto/`, `net/`'s rules and wire code, `app/`'s provisioning —
  everything that carries no Arduino include) with the host's g++/clang++ — or `pip install
  ziglang` when neither exists — and runs the same six suites the board's `selftest` command
  runs. The check counts are pinned on both sides (`test_cpp_selftest.py`), so a suite that
  only ever executed nowhere cannot exist — that window once shipped a discovery check
  asserting a 12-byte fit as 10. Compiling still is not linking: when the MQTT path changes,
  bench it against a real broker before believing it.
- **Nothing may depend on `diag/`.** Diagnostics are the outermost layer; the moment a business
  path calls into the console, the console becomes load-bearing.

**Cost, and why it is not a build-system change**: Arduino compiles every `.cpp` under `src/`
recursively, so subdirectories need no CMake, no `CMakeLists.txt`, and — with one exception — no
change to `gateway_cli.py`. The exception, found the hard way: arduino-cli does not put `src/`
itself on the include path, only each subdirectory, so `#include "app/session.h"` resolves
relative to the caller and then fails one level down. `gateway_cli.py` now passes `-I<src>` so a
layered include means the same thing here and in PlatformIO. The second half of the same trap:
arduino-cli compiles a *copy* of `src/` under `<build>/sketch/`, while that `-I` resolves back to
the original tree, so one header exists at two paths and `#pragma once` — which keys on the file,
not the name — lets both through. Every header therefore carries a macro guard as well. When the
firmware later moves to ESP-IDF, these same directories become `components/` and the include
paths do not change — the split is portable in both directions.

**Status (2026-11-11)**: `proto/`, `ble/`, `auth/`, `app/` and `net/` have landed; `main.cpp`
is the serial console and board boundary. `net/` carries Wi-Fi (`station`), the provisioning
`portal`, the hand-written MQTT client (`mqtt_wire` + `mqtt_client`), Home Assistant discovery
(`discovery` + generated `ha_discovery_gen.h`), the inbound grammar layer (`mqtt_rules`, pinned
by contract tests) and the OTA flasher (`ota`). `diag/` remains deliberately unsplit — the
console stays in `main.cpp` until it misbehaves. Each split was flashed and observed on the
board before being committed, because compiling is not the same as still linking to the heater.

**Rejected**: extracting `bosch_proto` into `firmware/components/` today. It has exactly one
consumer (the bridge); the panel never touches BLE. A shared component with one user is a
directory, not a library. It moves the day a second consumer exists — and that day is
unlikely, which is itself the argument.

---

### ADR-009 A separate repository is a release decision, not a structure decision

**Context**: the touchscreen panel may eventually deserve its own repository. Getting this
wrong in either direction is expensive: splitting too early means the MQTT contract has to be
synchronised across two repos while it is still `draft`; splitting too late means rewriting the
history and breaking every existing link.

**Decision**: keep the panel in `firmware/panel/` and revisit when **two or more** of these hold.

| Condition | Today |
|---|---|
| Its own release cadence (panel firmware version independent of the bridge) | no |
| Its own dependency tree (LVGL, a different board support package) | partly — LVGL is real |
| Its own contributor group (display/UI people rather than protocol people) | unknown |
| The contract stable enough to publish as a versioned artefact | **no** — `spec/mqtt.json` is `draft` |

**Why the structure question and the repository question are separate**: coupling is already
prevented by the `layout` CI job (no `#include` may cross an application boundary) and by
`test_mqtt_contract.py` (a field renamed in code fails unless the contract follows). Those work
inside one repository. Whether the panel ships from the same repo changes nothing about whether
it is coupled — it only changes where its version number lives.

**If it is split later**: `spec/` becomes the seam. Either it is duplicated with a CI check that
the two copies match, or it becomes a small versioned package both repos depend on. That cost is
paid once, and only if the panel earns it.

**Rejected**: splitting now, to "keep things clean". A repository boundary added before either
side has shipped is a second place to make the same mistake.

---

### ADR-010 A hand-written MQTT 3.1.1 subset, polled from `loop()`

**Context**: the bridge needs to publish a snapshot, a retained availability flag and a will, and
to receive two topics. The obvious move is a library: PubSubClient, arduino-mqtt or
async-mqtt. All three bring their own decisions about buffering, callbacks and task context.

**Decision**: implement the packets this project uses in `net/mqtt_wire.cpp` (pure C++, no
Arduino) and keep the socket glue in `net/mqtt_client.cpp`, polled from `loop()`.

**Reasons**:

- The contract fixes the will, the retain flags, the publish cadence and QoS 0. A library's
  defaults are the part that would have to be re-checked on every upgrade; ours are the only
  option.
- `mqtt_wire.cpp` has no I/O, so `selftest` checks the packet bytes on the board — including a
  packet arriving one byte at a time and a two-byte remaining length, which is where hand-rolled
  parsers usually break.
- Polling keeps all connection state in one place. A task would need a mutex around every
  `Snapshot` hand-off, and the BLE callback already runs in the NimBLE task; adding a second
  concurrent writer for a 200-byte payload is not a trade worth making.
- No new dependency means no version pin to police alongside `esp32@2.0.17` and
  `NimBLE@1.4.3`, both of which are already pinned for good reasons.

**Cost**, stated plainly: about 500 lines to maintain; no TLS, no QoS 2, no MQTT 5 properties;
broker quirks are ours to find. The self test is what makes that acceptable, and the limits are
listed in `docs/MQTT.md` §5 rather than discovered by a user.

**Related decision**: the Home Assistant discovery documents are **generated** from
`spec/mqtt.json` (`tools/gen_ha.py`) rather than written by hand. Twenty-one entities across
nine state fields and eight diagnostics is exactly the size at which one renamed key silently
breaks one entity, and a generated table cannot disagree with the contract.

**Rejected**: PubSubClient (fixed 256-byte buffer, global client state, callbacks that fight the
loop); async-mqtt (its own task and queue — see above); publishing only on change (a diff needs a
second copy of the snapshot, and a stale `ts` is a useful signal that "no change" hides).

---

### ADR-011 OTA over the bridge's own MQTT link: one flasher, live-only actions, retained state

**Context**: the bench workflow unplugs the board and reflashes over USB; a user with the board
mounted next to the heater has no serial access at all. An update mechanism that requires a laptop
is a mechanism most installs will never use.

**Decision**: the bridge reflashes itself, over the MQTT link it already maintains.

- The `ota` topic accepts `http://host[:port]/path` (or the literal `install`) and is
  **live-only** — `test_mqtt_contract.py` fails if it ever gains `retain`. A retained URL would
  reflash on every reconnect: a boot loop whose trigger is the broker replaying state.
- The version manifest (`update/latest` + `update/url`) is **retained on purpose** — the exact
  mirror of the rule above. State replays idempotently; a rebooted bridge re-learns the manifest
  from the broker on its first subscribe instead of believing it is current.
- One flasher and one grammar: the Home Assistant `update` entity's Install button, the serial
  console and the bench tool all publish to the same `ota` topic and through the same
  `rules::parseOtaUrl`. There is no second path that could disagree with the first.
- `Update.end(true)` is the gate: a short, corrupt or wrong-size image never reaches the boot
  slot, and any failure leaves the running firmware in charge.
- https image URLs are refused at parse time (the flasher carries no TLS stack); GitHub-release
  style https sources need an http mirror or a reverse proxy until TLS is a real requirement.

**Cost**: no TLS, no signed images — the trust boundary is the broker and the LAN, same as the
rest of the contract. The install path blocks `loop()` for its 120 s budget; the MQTT client is
not servicing the broker while it flashes, by design, because a half-written slot plus a live
command stream is a worse combination than a brief heartbeat gap.

**Bench-verified (2026-11-11)**: a 1.16 MB image flashed over the air in 24 s end to end from
`python -m http.server` on the bench PC; the HA update entity's `update.install` service call
(the exact action behind the UI button) flashed the same image in 4.5 s and the entity settled
back to "no update available" after the retained manifest replayed on reconnect.

---

## 3. Repository layout

Landed in 0.2.0. The repository is a monorepo split by **role**, not by file type: an
implementation, a set of applications that run on different hardware, and the contracts they
share.

```
python/                     Reference implementation
  python/bosch_api.py              codec / session / resident service
  python/device_fsm.py             provisioning + pairing state machine
  tests/                    204 offline tests
  tools/                    console, headless bridge, capture

firmware/                   One directory per application
  components/               reusable C, framework-free, host-testable
  bridge/                   ESP32-S3, no display: BLE in, MQTT out
  panel/                    reserved: ESP32-S3 + touchscreen, MQTT in only

spec/                       Cross-language contracts, checked in CI
  api_contract.json         Python signature freeze
  protocol_vectors.json     Python <-> C byte vectors
  profiles.json             one row per heater model: range, write encoding, identity
  mqtt.json                 bridge <-> consumer contract (incl. the OTA topics)

docs/                       API, protocol notes, this file, hardware guides
integrations/               reserved: Home Assistant, Node-RED, ESPHome
```

### Why the panel is an application, not a module

The heater accepts one BLE connection. If a wall panel talked to it directly it would evict the
bridge, need the pairing window at the heater's front panel, and carry a second copy of the
protocol. So the split is:

```
heater --BLE--> bridge --MQTT--> panel (any floor)
                          `---> Home Assistant / Mi Home / Node-RED
```

- `firmware/panel` may not link `firmware/bridge` or `firmware/components` — the boundary is
  checked by a CI job that greps for such an include
- `firmware/components` may not depend on any application
- the only shared artefact is `spec/mqtt.json`: the bridge publishes against it, consumers
  render from it, and `python/tests/test_mqtt_contract.py` fails if a field named there stops
  existing on `DeviceState`

### Why `import bosch_api` did not move

The library sits in `python/`, but `pyproject.toml` sets `package-dir = {"" = "python"}`, so the
import name stays flat and `spec/api_contract.json` stays valid. A layout change must never be a
breaking change.

### What the layout still anticipates

| Future | Where it lands | What it may touch |
|---|---|---|
| ESP-IDF migration | `firmware/bridge/` (same directory, new build) | nothing outside it |
| `bosch_proto` extracted from `main.cpp` | `firmware/components/bosch_proto/` | bridge links it; panel still must not |
| Home Assistant integration | `integrations/homeassistant/` | `spec/mqtt.json` only |
| A third controller (knob, e-ink) | `firmware/<name>/` | `spec/mqtt.json` only |

---

## 4. Roadmap

| Phase | Content | Exit criteria |
|---|---|---|
| **0 · done** | Protocol work, Python reference implementation, test suite, ESP32 feasibility firmware | Regression green; firmware boots |
| **1 · done** | Real-heater verification (SMP / A2/07 / A2/08 / A0), runtime pairing code, power-cycle recovery, repository restructured for multiple applications | All three log stages pass on hardware |
| **2 · done** | MQTT publishing against `spec/mqtt.json`, HA discovery (21 entities), broker credentials, HTTP OTA + the HA update entity | Bench-verified end to end on a production HAOS Mosquitto; a panel can be built against the contract with no bridge changes |
| **3 · current** | One week attached to a real heater; ESP-IDF migration; OTA rollback configuration; watchdog; touch panel in `firmware/panel/` | Seven days without a drop; rollback proven; second application shipped |
| **4 · optional** | ESPHome external component, field mapping for more models, more language bindings, https OTA sources | At least two models verified by the community |

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
| 2026-11-08 | Firmware layering (ADR-008) and the repository-split criteria (ADR-009); the panel stays in-repo until it earns its own release cadence |
| 2026-11-08 | Layout restructured by role (ADR-007): `python/`, `firmware/{components,bridge,panel}`, `spec/`, `integrations/`; `spec/mqtt.json` added as the bridge/consumer contract; 136 -> 145 tests |
| 2026-11-09 | MQTT client and Home Assistant discovery (ADR-010); `net/` grows `mqtt_wire`, `mqtt_client`, `discovery` plus two self-test suites; 145 -> 157 tests |
| 2026-11-10 | Inbound grammar layer (`mqtt_rules`) and broker-credential commands, host-tested; the six C++ suites run on the host via `host_selftest.py` (ADR-008 caveat retired); 157 -> 195 checks pinned on both sides |
| 2026-11-11 | OTA and the HA update entity (ADR-011): live-only `ota` topic, retained manifest pair, one flasher; Wi-Fi join retried at boot; 195 -> 204 tests; bench soak recorded (25 min, zero heartbeat drift, 6 s AP-outage recovery) |
