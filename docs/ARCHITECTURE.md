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
  ziglang` when neither exists — and runs the same seven suites the board's `selftest` command
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
`spec/mqtt.json` (`tools/gen_ha.py`) rather than written by hand. Twenty-two entities across
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

### ADR-012 BLE is a connection-time dependency, never an import-time one

**Context**: on 2026-11-12 a fresh CPython 3.13 without `bleak` could not import `bosch_api`
at all. Twelve of thirteen test modules failed during collection — including the signature
snapshot and MQTT contract tests, none of which touch Bluetooth. A module-level
`from bleak import BleakClient` had quietly turned "run the contract tests" into "install a
Bluetooth runtime".

**Decision**: resolve `bleak.BleakClient` on first construction, behind a module-level lazy
class. `import bosch_api` stays Bluetooth-free; only `connect()` needs the real library.

- The name `BleakClient` stays a real module attribute. `test_session.py` replaces it with a
  fake through `mock.patch.object`; a module `__getattr__` hook would have triggered the
  import while patching, which is exactly the failure being removed.
- A missing runtime raises `RuntimeError` naming the install command, not a
  `ModuleNotFoundError` from three frames down.
- `test_import_boundaries.py` blocks `bleak` and `winrt` in a subprocess, so the rule holds
  on a machine where bleak *is* installed; a dedicated CI job runs the whole suite with
  nothing installed at all.

**Cost**: one tiny class and one attribute lookup per connection attempt.

**Benefit**: the codec, config parser, FSM and the entire offline suite run on a bare
CPython. The protocol can be worked on without a Bluetooth stack — the same property that
lets the Python implementation serve as the executable specification.

**Scope note**: the standalone tools under `python/tools/` still import bleak directly. They
are hardware tools; importing one without bleak should fail loudly.

---

### ADR-013 Flash layout and releases are one decision, TLS is another

**Context**: three records disagreed. `esptool` reports **16 MB** flash; `platformio.ini` says
8 MB; the target FQBN says 16 MB with `default_8MB`. The app partition is reported at ~88 %,
a figure only consistent with the 1.31 MB slot of the *default* scheme — i.e. a build that
lost its partition table somewhere. Separately, "release builds must use plain semver" existed
only as a sentence in `spec/mqtt.json` with nothing enforcing it, and the flasher refuses
`https://` outright.

**Decision**: split by risk, not by topic.

1. **Make the layout explicit.** A committed 16 MB CSV with two 4 MB app slots, and a
   `platformio.ini` that states `16MB`. The current 1.16 MB image sits at 29 % of a 4 MB slot,
   so TLS and the next two features cannot repartition us again.
2. **Enforce plain semver at the release boundary** — tag regex plus a CI check that
   `kFirmwareVersion` equals the tag — while `parseManifestVersion` stays lenient on the
   board, because the bench still flashes `-rc` builds.
3. **Treat `manifest.json` as a release artefact** and keep the last hop (publishing it to the
   household broker) manual: CI holds no broker credential and cannot reach the LAN.
4. **Defer https.** When it is needed it will use `WiFiClientSecure` + `setInsecure()` plus a
   SHA-256 carried in a new retained `update/sha256`. The board has no trustworthy clock, so
   certificate validity windows cannot be checked without SNTP; the hash inherits the broker's
   trust, which is already the trust root for commands.

**Cost**: the partition change is a one-way door — it cannot be delivered over the air, so the
first board needs one USB reflash. https, when it lands, costs 35–50 KB of internal heap per
install and may stretch the 120 s budget.

**Benefit**: releases become reproducible, and the two assumptions behind this decision are
recorded as assumptions — the 88 % figure and the spec's "Home Assistant compares versions as
strings" claim are both marked unverified instead of being believed.

**Full design, options and acceptance criteria**: `docs/OTA.md`.

---

### ADR-014 Liveness and freshness are two signals, not one

**Context**: on 2026-11-12 the bench board was healthy — Wi-Fi up, broker up, heartbeats on
time — while its heater was out of range. Home Assistant nevertheless showed an outlet
temperature of 42.3 °C, a flow of 11.5 L/min and a **burning flame**, with `rssi: 0` and
`pair_state: "reconnecting"` beside it. The readings were from 11-10. Three things combined:

1. `gHaveSnap` was set once and never cleared, so the 60 s heartbeat republished a cached
   snapshot indefinitely;
2. `app::start()` resets `a0Count` to 0 on every reconnect, so the loop's `frames != gLastA0`
   test read a session restart as a new frame and rebuilt a snapshot — **with a fresh `ts`** —
   out of the previous session's last A0;
3. discovery bound a single `availability` topic, which only ever meant "the bridge process is
   running".

**Decision**: the contract carries two independent retained signals.

- `availability` — this process is alive and talking to the broker. Unchanged; the will sets it.
- `link` — the heater answered inside the last 90 s. Published on change and once after every
  connect; `offline` until the first frame of a session. 90 s is 22 missed frames at the 4 s
  cadence: long enough to ride out a reconnect (5 s wait + 15 s connect budget), short enough
  that a reading is never mistaken for the current one.
- The firmware drops the cached snapshot instead of republishing it, and a frame is recognised
  by the parser's timestamp (`lastA0Ms`, reset per session) rather than by the per-session
  counter.
- Discovery wires state-fed entities (measurements, the `water_heater` view) to **both** topics
  with `availability_mode: all`, and bridge-fed entities (the attributes diagnostics, the update
  entity) to `availability` alone. `spec/mqtt.json` `home_assistant.availability_rule` is the
  single place that decides; `gen_ha.py` reads it and the discovery self test 5d enforces it.

**Cost**: one more topic that can lie if it is not driven carefully, ~110 extra bytes per
discovery payload (the largest, `water_heater`, is 984 B as generated and 1032 B once an
11-character device id replaces the token — see ADR-016 for the later growth to 1082 B and the
1280-byte transaction buffer), and a visible `unavailable` state that did not exist before. That
last one is the point: the old behaviour was not "always available", it was "available and
wrong".

**Benefit**: the failure that motivated this ADR is now self-describing in any consumer, not
only in one that reads `ts` and knows what it means. The diagnostics stay readable while the
heater is unreachable, which is exactly when a remote user needs `pair_state` and the Install
button.

**Alternatives considered** (2026-11-12, against the official MQTT discovery pages). Three
places are valid but not the recommended form. All three are kept on purpose:

- **Device-level discovery** (`homeassistant/device/<id>/config`, one message for the whole
  device) — recommended for a multi-component device; single-component discovery is not
  deprecated. Rejected on cost: the 21 payloads total 13,012 B, and collapsing them only
  removes the repeated `device`/`origin` blocks, so one message still needs ~9 KB — the
  publish buffer would go from 1280 B to ~10 KB — and the switch has to publish
  `migrate_discovery` to all 21 old topics first, or their retained entries survive as
  duplicate entities. Zero functional gain. Revisit if the entity count grows substantially.
- **`expire_after` instead of the `link` topic** — the option does not exist on
  `water_heater.mqtt` or `update.mqtt`, which are exactly the entities that were lying. The
  second availability topic is not a workaround here; it is the only mechanism the schema
  offers.
- **`suggested_display_precision`** — a `sensor` option that `water_heater` does not have. Its
  `precision` already governs the card, and the temperature sensors publish 0.5 °C steps,
  which one decimal place renders exactly.
- **Birth-message discovery instead of retained** — the documentation calls this "a better
  approach", but it depends on Home Assistant publishing its birth topic. Retained plus a
  republish on every connect survives either side restarting; the ghost-entity downside of
  retained is what the `link` topic and the dropped stale snapshot remove.

---

### ADR-015 Retain status, never readings

**Context**: ADR-014 fixed *displaying* stale readings in Home Assistant — `link` goes offline,
the entities grey out — but the values themselves still sat on the broker. Pull the bridge's
power and `btwh/<device>/state` kept answering `42.3 °C, flame on` to anything that subscribed,
which is the same lie one layer further out: a Node-RED flow, a panel, or a `mosquitto_sub`
during debugging. The official pages say two things about this. The MQTT integration's only
"best practice" sentence is `publish your device or service availability status` (done in
ADR-014). `sensor.mqtt`, under `expire_after`, goes further: a retained value is replayed by
the broker at startup and *"could cause the sensor to become available with an expired state,
[so] it is not recommended to retain the sensor's state payload at the MQTT broker"*.

**Decision**: the split is by kind, not by topic.

- **Readings are live-only** — `state` (every A0 frame, ~4 s) and `attributes` (on connect and
  every 60 s). Nothing about them is retained, so a dead bridge leaves nothing to misread.
- **Status stays retained** — `availability`, `link`, `update/state`, and the discovery
  documents. `offline` is a true statement about a bridge that stopped, and it is the input a
  restarting consumer needs before any message arrives. The manifest topics keep their ADR-014
  treatment: replaying them is idempotent, and a bridge that forgets them would claim to be up
  to date.
- `spec/mqtt.json` states the policy once (`retain`, `retain_policy`, and a `retain: false` on
  both reading topics); three tests in `test_mqtt_contract.py` pin the flags and their
  agreement with the policy block.

**Cost**: a consumer that starts while the bridge is silent gets nothing instead of a stale
number — that is the intent, but it does mean a dashboard opened during an outage shows an empty
card rather than a greyed one. `attributes` is the weaker half: after a *consumer* restart
(Home Assistant reload) the diagnostics can read `unknown` for up to one heartbeat, where the
`update` entity does not, because `update/state` is still retained. Retained state also cost
write amplification — one retained write per 4 s frame, ~900 an hour on the broker's disk — and
that goes away with the flag.

**Benefit**: "the device is offline" and "here is the last reading" can no longer be confused by
anything that speaks MQTT, whether or not it understands `link`. Pulling the plug now leaves
exactly two status messages and no measurements, which is also the cheapest possible
verification.

**Rejected**: clearing the retained value with an empty publish when `link` goes offline. For a
numeric sensor Home Assistant documents the opposite behaviour — *"an empty value `''` will be
ignored and will not update the state"* — so the screen would not change, while the empty
payload re-introduces the `Invalid state message` warning that ADR-014's follow-up removed, and
the clear would race the next frame anyway.

---

### ADR-016 The discovery payloads are audited against the platform's own schema

**Context**: with no users yet and the contract still a draft, the whole discovery surface was
re-read against the Home Assistant 2026.11 platform pages (`sensor`, `binary_sensor`, `number`,
`water_heater`, `update`) rather than against memory. Five conformance defects came out of it,
four of them invisible in the UI and one latent:

1. `device.sw_version` was empty. The version travelled in `attributes.fw` and in the update
   entity, which is enough for an automation and not enough for the device page — Home
   Assistant renders `sw_version` there from the device registry and shows nothing otherwise.
2. `device_class: timestamp` was fed an **epoch integer**. The sensor parses ISO 8601, so the
   first real disconnect would have logged `Invalid state message '1794460577'` and left the
   entity unknown — the `0` case had been fixed, the non-zero case had not.
3. `rssi: 0` and `last_disconnect_at: 0` were sentinels. A real RSSI is negative and 0 means
   "never dropped", so both were numbers nobody measured — the same class of lie ADR-014 and
   ADR-015 removed from the readings, still present in the diagnostics.
4. The update entity carried `entity_category: config`, which files the Install button under the
   section of the device page users do not open. An update is a primary control.
5. The largest payload had grown to 1082 B; with its 58-byte topic and the fixed header the
   packet needs 1143 B, and `publishDiscovery` silently skips an entity that does not fit — a
   9-byte margin against the old 1152-byte buffer. That is the same failure mode as the
   1024-byte overflow of 2026-11-12, one field away.

**Decision**: fix all five, and make the last one structural rather than a number bump.

- `device.sw_version` is generated by reading `kFirmwareVersion` out of
  `firmware/bridge/src/net/mqtt_client.h`; `hw_version` is the build target, `ESP32-S3`. Both
  live in the device map, built once by `device_block()` instead of being spelled out by four
  entity builders.
- The timestamp template formats (`timestamp_custom('%Y-%m-%dT%H:%M:%S%z', true)`) instead of
  passing the epoch through; the else branch stays `None`.
- `rssi` and `last_disconnect_at` publish JSON `null` when unknown, and the contract marks both
  `nullable`. Home Assistant renders null as unknown, so no template guard is needed and no
  sentinel survives to be misread by a non-HA consumer.
- The update entity has no `entity_category`.
- `kTxCap` and the discovery payload buffer are 1280. Self test 7 builds the **real packet**
  (topic + fixed header + payload) rather than only expanding the payload, and a Python test
  re-derives the same sum in CI against `kTxCap`, so an entity that outgrows the wire fails on
  the bench and in the pipeline instead of vanishing from the device page.

**Left alone on purpose**: single-component discovery (13 KB across 21 payloads — see ADR-014
alternatives); `setpoint` existing as a `sensor`, a `number` and the water-heater target (each is
a different affordance: history, control, card); a read-only `modes` list (a tankless heater has
no switch to command); no `expire_after` (`link` covers it for every entity, and neither
`water_heater` nor `update` has the option); `origin.support_url` pointing at a repository that
is not public yet — a dead support link is only worth removing once that stops being the plan.

**Cost**: the largest payload grows 984 → 1034 B as generated (1082 B substituted), two static
buffers grow by 128 B each, and the table carries one more string per entity. Six new tests
(215 → 221).

**Benefit**: the device page shows the firmware version it is actually running, the disconnect
time renders as a time instead of a warning, no diagnostic can show a number that was never
measured, and the "entity silently missing" failure mode now has a test on both sides of the
wire.

---

### ADR-017 Two views of a fault, a birth subscription, and a schema for the contract

**Context**: three gaps left over from ADR-016's audit, all of the same shape — a consumer can
read a *value* but has no *statement* to act on.

1. `fault` was a single string sensor. An automation that wants "tell me when something breaks"
   has to template `value_json.fault != 'none'` in every trigger, and the UI has nothing to
   highlight: `device_class: problem` is the class Home Assistant raises alerts on, and a
   `sensor` cannot carry it.
2. Retained discovery survives a Home Assistant restart; the live-only half does not. After a
   restart the diagnostics read blank for up to a minute, and no measurement exists until the
   next A0 frame — which, with the heater out of range, may never arrive. The bridge, meanwhile,
   has no way of knowing anyone restarted.
3. The contract file had no structural validator. `test_mqtt_contract.py` pins individual keys
   against the Python library and the `gen_*` checks pin the generated headers, but a topic that
   lost its `{device_id}` token, or a numeric field that lost its `unit`, was caught only if
   someone happened to write an assertion for that particular key.

**Decision**:

- `fault` gains a second entity, `binary_sensor.fault_active`, `device_class: problem`, `true`
  whenever the code is anything but `none`. The template is
  `{{ 'true' if value_json.fault | default('none') != 'none' else 'false' }}`: `default()`
  rather than a second branch, because `binary_sensor.mqtt` matches `payload_on`/`payload_off`
  only and anything else — `None`, an empty string — is logged as `Invalid state message` on
  every frame, which is the warning ADR-016 item 2 chased. Both entities bind state + link with
  `availability_mode: all`: a stale "no fault" is worse than no answer.
- The bridge subscribes to `homeassistant/status`, Home Assistant's default birth topic, and on
  `online` republishes discovery, `availability`, `link`, `attributes` and `update/state`, and
  queues `state` for the next poll. `offline` (Home Assistant's will) is ignored — the bridge
  keeps publishing either way. The topic is generated from `spec.home_assistant.birth` into
  `contract::kHaStatusTopic` instead of being retyped in the `.cpp`, and it is the only
  subscription this client makes outside its own tree.
- `spec/mqtt.schema.json` (draft 2020-12) describes the machine-read parts: every topic template
  is namespaced `btwh/{device_id}/…`, a numeric field must carry a `unit`, an enum must carry
  `values`, `retain` is a closed boolean block on a fixed key set, and the Home Assistant block
  pins its required members. `python/tests/test_spec_schema.py` validates the contract on every
  run — and, this being the half that keeps a schema honest, breaks a copy of the contract in
  three ways and asserts each one is rejected. `jsonschema` is a dev requirement; without it the
  module skips rather than fails.

**Cost**: a 22nd entity (470 B of payload, against a 1280 B packet budget that the water_heater
entry already dominates), one extra subscription together with the burst of publishes a birth
message triggers, a schema to keep in step with the contract, and one new dev-time dependency.

**Found while verifying**: `test_cpp_selftest.py` pinned the host suite at 295 checks (discovery
79) — a number that had been stale since the discovery self test grew two per-entity checks (the
link/availability wiring in ADR-014 and the real PUBLISH packet in ADR-016). The file skips when
no host compiler is present, which is exactly the machine `run_tests.py` is usually run on, so
nothing ever compared the two. With `ziglang` installed the real total is 386 (discovery 170);
the assertion, `AGENTS.md` and `firmware/bridge/README.md` now carry the measured numbers.

**Alternatives considered**:

- *One `sensor` carrying `device_class: problem`.* Not a thing: a sensor's device classes are
  measurements. The field needs two entities or the alert needs a template everywhere.
- *`binary_sensor` only, dropping the string sensor.* Loses the code, which is what a human
  troubleshooting actually needs. The two views answer different questions.
- *`expire_after` to cover the restart gap.* `water_heater`, `number` and `update` do not have
  the field, and it would not help anyway: it hides a stale value, while the gap here is a
  *missing* one. The birth message repairs instead of hiding.
- *More hand-written assertions instead of a schema.* They cover the keys someone remembered to
  write. A schema covers the next field too, which is the entire point.
- *Generating the birth subscription from a topic in the contract's `topics` block.* That block
  is namespaced by device id and validated as such; this topic is Home Assistant's, one per
  broker. It lives in its own block precisely so it cannot be mistaken for something we publish.

**Benefit**: a fault is one boolean away from a trigger and from the UI's problem highlight; a
Home Assistant restart costs no data instead of a minute of blanks; and a malformed contract
edit fails one test with a path and a message, before a generator or a firmware build ever runs.

### ADR-018 The setup page comes back by itself, and leaves by itself

**Context**: the setup AP is on-demand only, and that was a deliberate choice — a page that can
rewrite the Wi-Fi password and the broker address should not be reachable on a bridge that is
working. The consequence went unexamined until the guide was written: the only other way back to
that page is a USB cable and the `portal` console command. That is fine for whoever flashed the
board and useless for the person who owns the heater. Rename the SSID, change the password,
replace the router, and the board retries every 30 s forever, with no way to be told otherwise.

The retry loop itself had the matching hole. It ran `millis() - lastRetry > 30000` from boot, so
a board that came up before its router kept retrying indefinitely — measured 2026-11-11, a board
that booted ahead of the AP stayed offline until a manual reboot. Arduino's auto-reconnect covers
an AP that disappears and returns while associated; nothing covered "the network is gone for
good".

**Decision**:

- `net/recover.h` holds the policy as a pure function: `decide(Sample, Policy) -> {None, Retry,
  OpenPortal, ClosePortal}`. No Arduino include, so the host suite pins every boundary and the
  board runs the identical code. The four durations are parameters; `kDefaultPolicy` is 5 min /
  30 s / 10 min and the two documents that quote them quote the file.
- **5 minutes with no IP** and the AP comes up. Past the worst router reboot, short enough that
  an owner who just changed something sees it within a coffee. Below the limit the board only
  retries, at the same 30 s cadence the hand-written loop used — one 15 s join budget each.
- **10 minutes with nobody associated** and that AP closes itself, and the retries resume. The
  idle clock is refreshed every tick a client is on the AP, so a slow form fill is never cut
  off. The cycle then repeats: five minutes of retrying, ten minutes of AP, so an outage that
  lasts a weekend leaves the page reachable for two thirds of every quarter hour instead of
  holding a radio open forever. That is deliberate - the person who discovers the problem on
  Sunday evening should not have to find a USB cable - and the cost is a beaconing AP on a
  mains-powered board.
- A portal a human asked for — the console command, and the LAN-mode page a developer uses to
  re-pair a working board — is never closed by the policy. `startPortal()` takes an `automatic`
  flag for exactly that distinction and nothing else.
- Timers go through `reached(now, since, limit)`, which requires the difference to be inside the
  half range. Plain subtraction reads a reference taken before `millis()` wrapped as 49 days
  elapsed: one timer would open the AP at random after 49 days of uptime, the other would close
  it under a user. Both are refusals now.
- Two transitions the policy cannot see, handled at the call site: a close resets
  `offlineSinceMs` (or the still-satisfied five minutes reopens the AP on the next tick), and a
  `/save` whose credentials fail to join starts a fresh five-minute window instead of inheriting
  the outage that preceded it — otherwise the page reopens on the same tick and the user cannot
  read the error it printed.
- `wifi` with no argument now prints the fallback state (how long offline, how long until the
  page appears, or that a portal is up and whether it is automatic).

**Cost**: one pure module and 25 self-test checks (host total 386 → 411), two accessors on the
portal (`portalAutomatic`, `portalClients`), one extra parameter, and one more thing the guide
has to explain. Firmware 1,173,861 B / 58,900 B RAM, unchanged in shape.

**Found while verifying — two reboots, two unrelated bugs.**

*1. The setup AP turned modem sleep off, and Bluetooth will not have that.* The first bench run
opened the AP at 5 min exactly as designed, then panicked at the 10-minute idle mark:
`abort() was called at PC 0x420a0767 on core 0`, backtrace
`ppTask -> ieee80211_ioctl_process -> wifi_get_channel_process -> pm_set_sleep_type`, reset two
ticks later. Three teardown shapes reproduced it deterministically - `softAPdisconnect(true)`
(which ends in `esp_wifi_stop()`), adding `WiFi.mode(WIFI_OFF)`, and `softAPdisconnect(false)`
alone - which is what made the teardown look guilty. It was not: the precondition was
`WiFi.setSleep(false)` in `startPortal()`, a workaround from 2026-11-08 when modem sleep was
blamed for flaky association. With Bluetooth enabled the driver *requires* modem sleep and
asserts on any power-management transition without it:
`E wifi: Error! Should enable WiFi modem sleep when both WiFi and Bluetooth are enabled`. Nothing
had ever torn that AP down without a client on it, which is the only state the fallback closes
from - first-boot provisioning always has a phone attached. Restoring sleep at teardown was not
enough either: the page itself scans for heaters, and that scan aborted the same way. The fix is
to never turn it off: `setSleep(false)` is gone, the AP runs with the driver's own default, and
the 11-08 symptom was re-measured rather than assumed - the same PC associates, gets a lease,
loads the page (BLE scan included, heater list empty and honest) and submits the form. Recorded
because the 11-08 note is still in the tree and the next person to hit a flaky association will
reach for that switch again.

*2. `/save` deleted the WebServer from inside its own callback.* With the driver quiet, a save
still rebooted the board - and it always had, since `/save` was written; the reboot right after
"Saved" was easy to mistake for the board applying the new network. Measured 2026-11-12:
`Guru Meditation Error: Core 1 panic'ed (LoadProhibited)`, `EXCVADDR 0x0000003c`, backtrace
`portalPoll -> WebServer::handleClient -> WiFiClient::operator=`. `handleSave()` called
`stopPortal()`, which deletes `gServer`, and then returned into the `handleClient()` frame that
owned it. The handler now answers the POST and parks the request; `portalPoll()` performs the
teardown on the next tick, when `handleClient()` has returned and the response has been flushed.

**Known gap, recorded rather than papered over**: this covers "cannot associate". It does not
cover "associated with the wrong network". The 2026-11-12 incident was that shape — a laptop
hotspot sharing the SSID `HomeNet-2G`, board joined to `192.0.2.20`, broker unreachable —
and `stationUp` is honestly true there, so the policy stays quiet. The fix belongs on the router
(reserve the address) or in a separate broker-reachability signal, not in a second way to
invalidate a working configuration.

**Alternatives considered**:

- *"Three consecutive failures" instead of a duration.* A failed attempt and a slow one cost the
  same 15 s join budget, so a count is a duration spelled less usefully — and it cannot see the
  two cases that matter most: booting while the router is off, and an association that succeeds
  onto an AP with nothing behind it.
- *Leave the AP up until someone provisions.* A router that is off for a weekend turns a
  five-minute outage into a permanent setup page — which is the objection that produced the
  on-demand rule in the first place, arriving through the back door.
- *AP+STA, so the retries continue underneath the open page.* `WiFi.mode(WIFI_AP)` is deliberate:
  measured 2026-11-08, modem sleep plus BLE coexistence already made association fragile ("PC
  associated, three captive probes answered, zero DHCP offers"), and a soft AP on the same radio
  during a scan-and-join is exactly that interference. It would also force `stopPortal()` to tell
  tearing down an AP from tearing down a link.
- *Close on a fixed lifetime from the open, ignoring clients.* Ten minutes is a slow form fill on
  an old phone. Closing under a user is worse than not opening.
- *Fall back when the broker is unreachable rather than when Wi-Fi is down.* Wider trigger,
  much wider blast radius: any broker restart would push the bridge into AP mode with a working
  network. Rejected for now; see the known gap.

**Benefit**: a board whose network changed is recoverable by its owner with a phone, no cable
and no computer, and the recovery window is bounded in both directions — it opens on its own and
it closes on its own.

---

## 3. Repository layout

Landed in 0.2.0. The repository is a monorepo split by **role**, not by file type: an
implementation, a set of applications that run on different hardware, and the contracts they
share.

```
python/                     Reference implementation
  python/bosch_api.py              codec / session / resident service
  python/device_fsm.py             provisioning + pairing state machine
  tests/                    237 offline tests
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
| **2 · done** | MQTT publishing against `spec/mqtt.json`, HA discovery (22 entities), broker credentials, HTTP OTA + the HA update entity | Bench-verified end to end on a production HAOS Mosquitto; a panel can be built against the contract with no bridge changes |
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
| 2026-11-12 | BLE moved from import-time to connection-time (ADR-012): the offline suite runs on a bare CPython and a CI job installs nothing; 204 -> 209 tests |
| 2026-11-12 | Release and flash-layout design (ADR-013): explicit 16 MB partitions with two 4 MB app slots, plain semver enforced at the tag, a manual manifest hop, and https deferred to a hash-authenticated phase 2; `docs/OTA.md` |
| 2026-11-12 | Freshness split from liveness (ADR-014): retained `link` topic, stale snapshot no longer republished, a frame recognised by the parser's timestamp instead of the per-session counter; discovery binds state-fed entities to both signals; origin block added; 209 -> 212 tests |
| 2026-11-12 | Retain split by kind (ADR-015): `state` and `attributes` published live-only, only status retained, so a dead bridge leaves no reading on the broker; 212 -> 215 tests |
| 2026-11-12 | Discovery audited against the platform schema (ADR-016): `device.sw_version`/`hw_version` generated from the firmware constant, the timestamp sensor formats instead of passing an epoch through, `rssi`/`last_disconnect_at` publish null and are marked nullable, the update entity loses `entity_category: config`, and the wire buffer grows to 1280 with a packet-level test on both sides; 215 -> 221 tests |
| 2026-11-12 | Fault binary sensor, Home Assistant birth subscription and a JSON Schema for the contract (ADR-017): `binary_sensor.fault_active` with `device_class: problem`, `homeassistant/status` handled by republishing discovery and the live-only topics, `spec/mqtt.schema.json` validated by a test that also breaks the contract on purpose; 21 -> 22 entities, 221 -> 237 tests |
