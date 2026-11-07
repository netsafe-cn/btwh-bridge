# btwh-bridge

**English** | [简体中文](README.zh-CN.md)

> **Unofficial project.** A BLE interoperability implementation for the Bosch
> **Therm 4800i** gas water heater — a Python reference implementation plus an
> always-on ESP32 gateway, targeting **Home Assistant** as the primary integration.
>
> Verified on real hardware: **Bosch Therm 4800i**, whose BLE module advertises
> itself as `BTWH-001-xxxx` (the identifier the code actually matches on).
>
> Not affiliated with, authorised by, or endorsed by Robert Bosch GmbH or its
> affiliates in any way.

The official app talks to one phone at a time, exposes no local API, and keeps
everything in the cloud. This project turns the heater into a **programmable local
device**: read temperatures, watch combustion state, set a target temperature —
with no cloud in the loop.

**Every protocol finding is captured as testable code.** A Python reference
implementation, cross-platform golden vectors, and an ESP32 C++ implementation are
all pinned to the same set of hex vectors, so the two implementations cannot drift
apart silently.

---

## Status

| Component | State | Notes |
|---|---|---|
| Python library `bosch_api` | ✅ Ready | **136** offline tests green; signature-freeze contract + golden vectors |
| Desktop console / Mi Home bridge | ✅ Ready | tkinter GUI + Bemfa cloud |
| ESP32-S3 gateway firmware v2 | ✅ Verified | Real-device link-up: SMP encryption → `A2/07` → `A2/08=01` → steady `A0` stream; auto-recovers after power cycle |
| MQTT / Home Assistant auto-discovery | ⏳ Planned | See [architecture](docs/ARCHITECTURE.md) |
| Wi-Fi provisioning (Improv) | 📐 Spec done | Defined by `device_fsm.py` + [API.md §9](API.md) |

---

## Two ways to run it

| | Python direct | ESP32 gateway (recommended) |
|---|---|---|
| Hardware | Laptop / Raspberry Pi / NAS | Waveshare ESP32-S3-DEV-KIT-N8R8 or similar |
| Good for | Experiments, capture, ad-hoc control | 24/7 operation, feeding HA / Mi Home |
| Pairing code | Re-enter each start | Flash once, enter at runtime, persisted in NVS |
| Reconnect | In-process auto-reconnect | Firmware auto-reconnect + power-cycle recovery |
| State | ✅ Ready | ✅ Verified |

---

## Features

| Feature | Detail |
|---|---|
| Live temperatures | Outlet / inlet / heat exchanger / chassis, 0.1 °C resolution, refreshed every 4–8 s |
| Combustion state | Burner power %, fan RPM, flame ionisation, water flow |
| Target temperature | Read and write (35–60 °C); takes effect in ~4 s and is confirmed by the device |
| History & faults | `B0` operating statistics / shower records, `D7` fault list |
| Desktop console | Single-window GUI with four temperatures, chart, and history dialogs |
| Mi Home / Xiaoai | Optional sync through Bemfa cloud for voice queries and setpoint changes |
| Reusable API | `bosch_api.py` works as a library for any Python program, with its own regression suite |

---

## Install

Requires **Python 3.9+**.

```bash
pip install -r requirements.txt
```

On Windows, `bleak` pulls in the WinRT dependencies automatically. On Linux you need
BlueZ (present by default on most distributions).

---

## Configuration

`config.json` is created on first run, or copy the template:

```bash
cp config.example.json config.json
```

| Field | Meaning | Required |
|---|---|---|
| `device_address` | Heater's BLE address, e.g. `AA:BB:CC:DD:EE:FF` | ✅ |
| `login_mac` | **This host's** BLE address (login identity — see below) | ✅ |
| `pair_code` | The 6-digit code shown on the heater's panel | ✅ |
| `bemfa_uid` | Bemfa cloud key; leave empty to skip Mi Home sync | ❌ |
| `topic_set` / `topic_up` / `topic_down` | Bemfa topic names | ❌ |

**Environment variables override the file**, which is convenient for containers or
when you would rather not write the pairing code to disk:

```bash
export BOSCH_PAIR_CODE=123456
export BOSCH_LOGIN_MAC=AABBCCDDEEFF
```

### Where the three required values come from

**① Device BLE address** — found by scanning. Usually advertised as `BTWH-001-XXXX`;
the last four characters are the final two MAC bytes.

**② Host BLE address** — the device keeps a strict MAC allow-list, and the login
frame must carry an address it recognises.

- Windows: the GUI has a "read local BLE address" button in Settings
- Linux: `cat /sys/class/bluetooth/hci0/address`

**③ Pairing code** — activate pairing on the heater panel and it displays six digits.
This is the **panel code, not the serial number on the nameplate**. It is
short-lived and different every time, and serves as both the BLE SMP passkey and the
application-layer `A2/08` password.

---

## Usage

```bash
python tools/bosch_ui.py        # desktop console (Windows: double-click tools/start_ui.bat)
python tools/bosch_bridge.py    # headless bridge (double-click tools/start_bridge.bat)
python tools/bosch_read.py      # single read
```

### As a library

```python
from bosch_api import Config, BoschService

svc = BoschService(Config.load())
svc.start()                      # pair, log in, reconnect, receive — all automatic

svc.state                        # DeviceState(temp_out=45.0, fan_speed=800, ...)
svc.authorized                   # True only when the login really succeeded
svc.pair_kind                    # 'first_pair' / 'rebind' / ''
svc.set_temperature(42)

svc.stop()
```

The interface contract lives in [`API.md`](API.md). The rule is
**additive only**: published signatures are frozen and enforced in CI by
`tests/test_contract.py` + `api_contract.json`, so any breaking change turns the build red.

---

## ESP32 gateway

Firmware, toolchain and board documentation are under [`esp32/`](esp32/); step-by-step
instructions are in [`esp32/README.md`](esp32/README.md).

Version 2 fixes three things that made the early feasibility firmware unusable:

1. **BLE SMP PIN support.** The panel code is both the link-layer passkey and the
   application-layer `A2/08` password. The early firmware used Just Works, which
   could never succeed.
2. **Pairing code entered at runtime.** Type it over serial or in the desktop panel;
   it is persisted in NVS, so one flash covers unlimited retries. The pairing window
   is only tens of seconds — recompiling never fits.
3. **Automatic recovery after power loss.** On boot, if a code is stored in NVS, the
   firmware immediately starts one connection attempt and only falls back to waiting
   for input if that fails.

When a connection attempt fails, run the log analyser — it turns the failure matrix
into executable rules:

```bash
python esp32/gateway/log_diag.py            # picks the newest logs/*.log
python esp32/gateway/log_diag.py <logfile>
```

---

## Mi Home / Xiaoai

Relies on [Bemfa cloud](https://cloud.bemfa.com) (free). Create three topics:

| Topic | Type (from last 3 chars) | Purpose |
|---|---|---|
| `boschset004` | `004` = sensor | Target temperature, publishes `#38` |
| `boschup006` | `006` = switch | +1 °C, auto-resets after triggering |
| `boschdown006` | `006` = switch | −1 °C |

Then in the Mi Home app: Me → Other platform devices → search "Bemfa" → sign in.

> **Note:** topics whose suffix is not `001`–`009` will **not** sync to Mi Home.
> `010` (thermostat) and `011` (water heater) look closest but do not sync — don't use them.

Inherent Mi Home limits for third-party devices: no room assignment, no batch control,
sensors are read-only, and custom device names are ignored by Xiaoai.

---

## Testing & quality

```bash
python run_tests.py          # everything, fully offline (no BLE, no network)
python run_tests.py -v       # per-test output
```

| Suite | Tests | Covers |
|---|---|---|
| `test_protocol` | 13 | Frame assembly, stuffing, per-chunk XOR, A0/B0/D7 parsing |
| `test_session` | 21 | Login state machine, single-flight I/O, msgId rotation on retry, bad-frame filtering |
| `test_api` | 25 | Config load/save, env overrides, state properties, Bemfa parsing, system pairing |
| `test_service` | 22 | Service queue semantics, hot config reload, idempotent start, failure surfacing |
| `test_pair_reasons` | 20 | Pairing failure reason codes, auth state, `A2/07=00` semantics |
| `test_contract` | 7 | **Signature freeze**, protocol constant freeze, **cross-platform golden vectors** |
| `test_fsm` | 28 | Provisioning/pairing orthogonality, timeouts, recovery, safety constraints |

Three rules:

- Adding a public API → update `API.md`, then run `python tests/test_contract.py --update`
- Changing the protocol layer → update `protocol_vectors.json` and make the ESP32
  vector tests pass with the same data
- Hardware probes → always prefix `probe_`; a `test_` prefix makes CI try to touch
  real hardware and hang

CI runs Linux + Windows × Python 3.11/3.12, plus a job that fails if `config.json` or
hard-coded credentials are committed. See [`.github/workflows/ci.yml`](.github/workflows/ci.yml).

---

### Releases

Versions follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html). User-visible
changes are recorded in [CHANGELOG.md](CHANGELOG.md); the rules for deciding whether a
change is a patch, minor or major bump live in
[CONTRIBUTING.md §6](CONTRIBUTING.md#6-versioning-and-releases). What counts as
"breaking" is defined by the API contract, not by the size of the diff.

## Project layout

```
.
├── bosch_api.py            ★ Core library: protocol / device session / resident service
├── boschlog.py             Unified logging (rotating file + in-memory ring buffer)
├── bemfa.py                Bemfa cloud TCP client (Mi Home channel)
├── device_fsm.py           ★ Provisioning / pairing state machine (framework-agnostic spec)
├── singlelock.py           Single-instance guard for the background bridge
├── run_tests.py            Regression entry point (offline, no hardware)
├── tests/                  ★ Unit tests, auto-discovered by the runner
├── tools/                  Runnable helpers: console, bridge, capture, batch launchers
├── api_contract.json       ★ Signature-freeze snapshot (generated by tests/test_contract.py)
├── protocol_vectors.json   ★ Golden vectors (Python ↔ ESP32, byte-for-byte)
├── API.md                  Interface contract (additive only, machine-checked in CI)
├── BLE_PROTOCOL.md         Protocol notes: frame format, field tables, unexplored areas
├── docs/                   Architecture and decision records (ADR-001 to ADR-006)
├── esp32/                  ESP32 gateway: firmware / toolchain / manual download guide
├── config.example.json     Config template (contains no real data)
├── pyproject.toml          Packaging metadata (`pip install .`)
├── CHANGELOG.md            Release history
├── CODE_OF_CONDUCT.md      Community standards
└── LICENSE                 MIT
```

The UI only renders; all BLE logic lives in `bosch_api` and all cloud sync in `bemfa`.
Swapping the UI (Qt / web / CLI) never touches protocol code.

---

## Troubleshooting

**It won't connect — the UI sits at "waiting to reconnect"**

1. **Two instances running?** They fight over the single device connection and evict each other.
2. **Is the official phone app still connected?** The device allows one client at a time.
3. **Is the device asleep?** BLE advertising stops after long idle periods — press a panel
   button or run some hot water to wake it.
4. **Did you kill the process recently?** The OS Bluetooth stack needs 10–15 s to release
   the connection; wait before restarting.

**Pairing code rejected**

- Make sure it is the **six digits shown on the panel**, not the nameplate serial number
- The code changes after every pairing session; re-enter it (the ESP32 firmware accepts
  codes at runtime, no reflash needed)
- `login_mac` must match the device allow-list — a different computer needs re-pairing

**The device stops responding after a while**

These units enforce **connection-rate protection**: repeated rapid connections degrade in
stages (first it stops pushing data, then it refuses connections), and only a power cycle
resets it. Avoid restarting the program in a tight loop.

**Windows reports "GATT not ready"**

A Bluetooth stack cache issue; the program reconnects automatically. If it persists,
close other Bluetooth software or restart the Bluetooth service.

---

## Known limitations

- Only verified on the **Bosch Therm 4800i** (BLE module `BTWH-001-xxxx`); other models
  or firmware may use different field offsets, and the parser is the first thing to re-check
- The panel code must be entered **within the pairing window** the first time. Once a code
  has paired successfully, the device keeps that authorisation long-term and the same code
  can reconnect later (verified 11 minutes after the window closed; the upper bound is
  unknown and needs longer observation)
- Three consecutive connection failures trigger device-side protection; recovery takes
  about 3 minutes. Do not retry in a tight loop
- The device may drop the link when idle (historically observed at ~45–50 s with the Python
  client; the ESP32 build ran 8 minutes without a drop)
- Windows caps MTU at 23, so long frames are split and reassembled
- Bemfa is a third-party service with no availability guarantee (its Tmall Genie channel
  shut down at the end of 2024)
- A vendor firmware update may break the private protocol; no long-term availability is promised

---

## Contributing

See [`CONTRIBUTING.md`](CONTRIBUTING.md). Most useful contributions right now: field
verification on more models, Home Assistant integration, and the ESP32 provisioning
implementation.

By participating you agree to the [Code of Conduct](CODE_OF_CONDUCT.md).

---

## Compliance & disclaimer

This project is BLE interoperability research on **hardware the author owns**, aimed at
letting people automate their own appliances.

- The repository **contains no** vendor APK, firmware, decompiled output, or extracted keys,
  and does not accept such contributions
- It circumvents no payment, licensing, or security mechanism; pairing still requires
  physical access to the panel and a code valid on that day
- `Bosch` and related marks belong to their owners and are used only to describe
  compatibility, implying no affiliation or endorsement
- The legal boundary of interoperability research varies by jurisdiction; users are
  responsible for confirming compliance where they live
- Protocol details are shared for technical discussion only and **must not** be used to
  tamper with devices you do not own or for commercial redistribution
- The software is provided "as is", without warranty of any kind; device damage or data
  loss arising from its use is the user's own responsibility

---

## License

[MIT](LICENSE)
