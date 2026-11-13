# ESP32 gateway v2 — Waveshare ESP32-S3-DEV-KIT-N8R8

> Built 2026-11-07. Board identified as COM3 = CH343 (flash + logs), COM4 = native USB.
> Vendor manuals, schematics and datasheets: see [`docs/hardware/`](../../docs/hardware/).

## 1. Two defects fixed in v2

| # | Problem | Consequence | v2 fix |
|---|---|---|---|
| 1 | **v1 ignored the BLE SMP pin entirely** | Measured on hardware: the panel code is **both the BLE passkey and the application-layer password**, and the device requests `PROVIDE_PIN` while the pairing window is open. v1 used `NoInputNoOutput` + Just Works, so SMP failed and `A2/08` could only answer `00` | Implement `onPassKeyRequest()` to return the six-digit panel code; set IO capability to `KEYBOARD_ONLY` (falls back to Just Works when the peer has no IO) |
| 2 | **The pairing code was compiled in** | A panel code expires within minutes; changing it meant a rebuild plus reflash of two minutes or more — far too slow in practice | **Enter the code at runtime** over serial; configuration is persisted in NVS, so one flash covers unlimited retries |

The protocol was also re-checked byte for byte against `python/bosch_api.py`: the stuffed range
includes the checksum, XOR restarts per fragment, FIFO and CREDITS share one callback, `A2`
resends five times at six-second intervals with a fresh msgId, and the A0 field offsets are the
ones measured on 2026-11-01.

## 2. Layout

```
firmware/bridge/
├── src/
│   ├── main.cpp               setup / loop / serial console
│   ├── proto/                 frames and A0 parsing - no Arduino, no NimBLE
│   │   ├── frame.h  frame.cpp     codec: build, stuff, XOR, checksum, reassembly
│   │   ├── a0.h     a0.cpp        A0 broadcast -> A0State
│   │   ├── selftest.h  selftest.cpp   runs the golden vectors on the board
│   │   ├── vectors_gen.h      GENERATED from spec/protocol_vectors.json
│   │   └── profiles_gen.h     GENERATED from spec/profiles.json (one row per model)
│   ├── ble/                   link.h  link.cpp    the only place NimBLE appears
│   ├── auth/                  login.h login.cpp   A2/07 + A2/08 behind a Transport
│   ├── app/                   config.{h,cpp}  NVS config (broker + credentials)
│   │                          session.{h,cpp} connect order, A0 state, link watch
│   └── net/                   the network stack (ADR-008's top layer, now landed)
│       ├── station.cpp/h      Wi-Fi join + SNTP clock (the retry cadence is recover's)
│       ├── recover.cpp/h      when to offer the setup AP again (pure policy, ADR-018)
│       ├── portal.cpp/h + portal_html.h   one-submit provisioning form
│       ├── provision.cpp/h    Improv-style provisioning gates
│       ├── mqtt_wire.cpp/h    hand-written MQTT 3.1.1 packets (pure C++)
│       ├── mqtt_rules.cpp/h   inbound grammar: broker, setpoint, commands, OTA URL
│       ├── mqtt_client.cpp/h  the session: connect, backoff, publish, subscribe
│       ├── discovery.cpp/h + ha_discovery_gen.h  22 entities, GENERATED from spec
│       ├── state_json.cpp/h   snapshot -> contract JSON bytes
│       ├── ota.cpp/h          HTTP flasher: 2 KB chunks, 120 s budget, checksum-gated
│       └── selftest.h         shared selftest plumbing
├── platformio.ini             PlatformIO configuration (NimBLE pinned to 1.4.3)
├── tools/
│   ├── gateway_cli.py         build / flash / serial console / logging in one command
│   ├── gen_vectors.py         spec/protocol_vectors.json -> src/proto/vectors_gen.h
│   ├── gen_profiles.py        spec/profiles.json -> src/proto/profiles_gen.h
│   ├── gen_mqtt.py            spec/mqtt.json -> src/net/mqtt_contract.h
│   ├── gen_ha.py              spec/mqtt.json -> src/net/ha_discovery_gen.h (22 entities)
│   ├── host_selftest.py       the seven C++ suites on the host (g++/clang++/zig)
│   ├── esp32_panel.py         always-on-top control panel (enter the six-digit code)
│   ├── pair_once.py           pair in one command and report a verdict
│   ├── watch_a0.py            A0 broadcast as a table with per-field ranges
│   └── log_diag.py            serial log diagnosis
└── README.md                  this file
```

There is no committed `.ino`. `gateway_cli.py` generates a sketch shell around `src/` at build
time — an `.ino` containing only a comment, plus a copy of `src/`. Two build systems read one copy
of the code, so nothing can drift.

Not committed, created on first build: `toolchain/` (arduino-cli and packages), `logs/`
(serial captures), `legacy_v1/` (the superseded feasibility firmware).

This is one application in a directory of applications. It publishes against
[`spec/mqtt.json`](../../spec/mqtt.json); a touch panel that only consumes that contract lives
in [`firmware/panel/`](../panel/), and it must never link anything here.

### 2.1 Internal structure

Split by **what a module is allowed to know**, so dependency arrows only point one way (ADR-008).

```
src/
├── main.cpp        setup / loop / serial console          (board boundary only)
├── proto/          frames, A0 fields                      done: no Arduino, no NimBLE
├── ble/            NimBLE: connect, subscribe, reassemble, send   done
├── auth/           A2/07 + A2/08 behind a Transport       done: no NimBLE, no Serial
├── app/            session order, NVS config, A0 state    done
├── net/            wifi, portal, mqtt, discovery, ota     done (see the layout above)
└── diag/           (serial console stays in main.cpp)     deliberate: not split yet
```

Why this order and not "bluetooth part / protocol part / config part":

- `proto/` is the only layer that can be tested on a PC, and the only one with a machine-readable
  spec (`spec/protocol_vectors.json`). Keeping it free of Arduino includes is what makes a native
  test possible — that single property is worth the directory.
- `auth/` sits above `ble/`, not inside it, because the pairing handshake is protocol logic that
  happens to use a BLE send/wait callback. It should be testable with a fake transport.
- `net/` (MQTT, HA discovery) sits at the top: it consumes a state snapshot and the constants
  generated from `spec/mqtt.json`, and must never see a frame byte.

**No build system change is needed.** Arduino compiles every `.cpp` under `src/` recursively, so
these subdirectories work with the existing `gateway_cli.py`; the one thing it does add is
`src/` itself to the C++ include path, because arduino-cli otherwise only exposes each
subdirectory and an include written as `app/session.h` would fail one level down. When the
firmware later moves to ESP-IDF, the same directories become `components/` and include paths
stay put.

Splitting is not free: it changes every call site and needs a rebuild plus a reflash to prove the
board still links. Do it as one commit, with the golden vectors as the judge.

### 2.2 Proving the layers still correct

The eight C++ suites (proto 64 / net 16 / mqtt 41 / rules 76 / provision 19 / discovery 170 /
recover 25 / wifistat 42 — 453 checks) run in **two places**, and the counts are pinned in lockstep by
`python/tests/test_cpp_selftest.py`:

1. **On the host**, no board needed: `tools/host_selftest.py` compiles the framework-free
   layers with the host's g++/clang++ (or `pip install ziglang` when neither exists) and runs
   the same suites. A broken codec is caught before a commit, not after a flash — the
   hardware once shipped a discovery check asserting a 12-byte fit as 10 that had never
   executed anywhere until the board ran it.
2. **On the board**: after flashing, type:

```
selftest
```

It rebuilds every frame from its parameters, stuffs it, XORs it fragment by fragment, feeds the
fragments back through the reassembler, corrupts a checksum on purpose, and compares every A0
field against `spec/protocol_vectors.json`. One line per failure, a count at the end.

Both stay green through bugs the other only finds live: the client id once shipped unterminated
stack garbage while the host's stack happened to be zero, and the watchdog once dropped a healthy
link via a `millis()` underflow only reachable with a real broker answering. When the MQTT path
changes, bench it against a real broker before believing it.

```bash
python firmware/bridge/tools/gen_vectors.py          # spec -> src/proto/vectors_gen.h
python firmware/bridge/tools/gen_vectors.py --check  # CI: fail if the header is stale
```

`vectors_gen.h` is committed so a build never depends on running Python first; `--check` is what
stops it drifting from the JSON.

## 3. First run (one command)

```bash
cd firmware/bridge
python tools/gateway_cli.py --flash
```

It finds the CH343 port, reads the chip's real flash size, builds for exactly that, flashes and
opens the serial console. Boards with 4 MB are refused (two 3.19 MB OTA slots do not fit) and
boards without PSRAM work, because nothing under `src/` allocates from it.

`--build --flash-size 8` builds for an 8 MB part when no board is attached; `--psram none` drops
the Octal PSRAM flag entirely.

### Then follow this order

```
1. Make sure the Python console / bridge on the PC is closed (two BLE clients fight over the heater)
2. Heater panel -> "clear pairing"            <- opens the pairing window; do not skip
3. Panel -> "connect / pair" -> note the six digits
4. Type those six digits into the console     <- takes milliseconds, no reflash
5. Watch the [SMP] / [AUTH] / [A0] stages
```

Code expired or rejected? **Read a new one and type it again** — no build involved.

### Serial commands

| Input | Effect |
|---|---|
| `311522` | Six-digit panel code → connect, pair and log in immediately |
| `pin <code>` | Same, explicit form |
| `go` | Reconnect with the current code (no new pairing) |
| `pair` | Clear the local bond and force a fresh SMP pairing |
| `scan` | Scan for 5 s and list nearby BLE devices (address / RSSI) |
| `mac AA:BB:CC:DD:EE:FF` | Set the login MAC (the host on the device's whitelist) |
| `addr AA:BB:CC:DD:EE:FF` | Set the heater's BLE address |
| `wifi <ssid> <password>` | Join a network now, persisted to NVS (open network: `wifi <ssid> -`; `wifi` alone prints the state) |
| `broker <host[:port]>` | Set the MQTT broker; `broker` alone prints the current one (`-` clears) |
| `mqttuser <name> <password>` | Broker credentials, persisted in NVS (`-` clears both; a password may contain spaces) |
| `portal` | Start the Wi-Fi provisioning portal by hand (one form: Wi-Fi + heater + code + broker). Not needed for a board that cannot join: that path is automatic after 5 min, see ADR-018 |
| `ota` | Prints how to flash: the URL goes to the MQTT `ota` topic — the console deliberately refuses to become a second flasher |
| `status` | Print configuration and runtime state |
| `mqtt` | Print the payload for the live MQTT state |
| `selftest` | Run the eight C++ suites on the board (453 checks) |
| `raw on` / `raw off` | Show / hide raw A0 bytes |
| `reboot` / `help` | Reboot / help |

## 4. Three criteria that must all hold

1. The `[SMP]` stage reports the link is encrypted
2. The `[AUTH]` stage shows an `A2/07` reply followed by **`A2/08` = `01`**
3. `[A0]` frames arrive with sane values: flow 0–11 L/min, power 0–50%, fan 700–1500 rpm

## 5. Failure matrix

| Symptom | Cause | Action |
|---|---|---|
| Link never comes up | Panel asleep / phone app holds the device / too far | `scan` to confirm the device is advertising; turn off phone Bluetooth |
| `A2/08` replies `00` | **Pairing window not open**, or the code expired | Panel "clear pairing" → new code → type it again |
| `[SMP]` fails after the PIN request | Wrong code, or the window closed | As above; check the PIN with `status` |
| Service not found | Pairing did not take effect | Look at the `[SMP]` stage |
| A2 answers but no A0 | The login did not really pass | Confirm `A2/08` = `01` |
| A0 fields look wrong | Incomplete frame | `raw on` and check the payload is 34 bytes |
| No serial output | Wrong port | **Use the CH343 port, not the native-USB one** (identified as COM3 at first bring-up; it can re-enumerate, e.g. to COM8 — `gateway_cli.py` finds it, or pass `--port`); do not enable USB CDC On Boot |

## 6. Expansion order (as executed)

```
1. Wi-Fi connection (first provisioning without touching code)          done
2. MQTT reporting + Home Assistant discovery (22 entities)              done, bench-verified
3. Setpoint commands downstream (D3 write, sendFrame is already usable) bench round-trip done
4. OTA updates (no more unplugging to reach USB)                        done, incl. HA update entity
5. Reconnect hardening + watchdog (soak for two days)                   partially: 25-min soak green,
                                                                         AP-outage recovery 6 s; one-week
                                                                         live soak with the heater pending
6. Bemfa reporting (Mi Home channel, reusing the existing topics)       not started
```

## 7. Hardware facts

| Item | Value |
|---|---|
| Board | Waveshare ESP32-S3-DEV-KIT-N8R8; **esptool reports 16 MB flash** (manufacturer 0x46 / device 0x4018), which contradicts the "N8" in the part number — trust the measurement |
| PSRAM | 8 MB **Octal**, and now **measured**: with `PSRAM=opi` the banner reads `PSRAM=8386279 bytes`. With the option left at its default the same banner reads `0`, which looks like missing hardware but is only a missing build flag |
| FQBN, on the board | `esp32:esp32:esp32s3:FlashSize=16M,PartitionScheme=default_8MB,PSRAM=opi` — built, flashed and boot-verified 2026-11-12: 16 MB flash, 8 MB PSRAM, app slot 3.19 MB at 34 % |
| FQBN, do **not** use | `esp32:esp32:esp32s3:FlashSize=8M` on its own. It selects the 4 MB `default` scheme: a 1.31 MB app slot (which is where the old "88 % full" came from) and an upload that lands in app0 while `otadata` still boots app1 — a flash that succeeds and changes nothing |
| Partitions | `FlashSize=8M` does **not** imply `default_8MB`; it falls back to `default`. The scheme must always be named explicitly. `default_8MB` = 3 MB app × OTA + 1.5 MB SPIFFS, which is what the board actually runs |
| BLE stack | **NimBLE-Arduino 1.4.3** (2.x changed the callback signatures — pinned) |
| Log / flash port | **COM8** today (CH343 → UART0), 115200; upload at 460800. The number is not stable across machines or replugs — `gateway_cli.py` finds the port by VID/PID (`1A86:55D3`), so trust that over this line |
| Heater address | Found with `scan`, then persisted with `addr <MAC>` (placeholder `AA:BB:CC:DD:EE:FF`) |
| Login MAC | This host's Bluetooth address, set with `mac <MAC>` (placeholder `11:22:33:44:55:66`) |

Protocol references: `docs/BLE_PROTOCOL.md` in the repository root (field calibration in §6, B0/D7 in
§16), `docs/API.md`, and `python/bosch_api.py` (237 tests). The SMP pin behaviour is corroborated by captures
on real hardware — see `docs/API.md §10`.

## 8. Build environment (new machine / reinstall)

No Arduino IDE: **arduino-cli on the command line**, so `python tools/gateway_cli.py --flash` covers
build + flash + log.

| Component | Version / requirement |
|---|---|
| arduino-cli | ≥ 1.0, on `PATH` or at `firmware/bridge/toolchain/arduino-cli.exe` (the local toolchain is not committed) |
| arduino-esp32 core | **2.0.17** (3.x ships no bundled NimBLE and changed the callbacks) |
| NimBLE-Arduino | **1.4.3** (2.x is incompatible — pinned) |
| Board FQBN | `esp32:esp32:esp32s3:FlashSize=16M,PartitionScheme=default_8MB,PSRAM=opi` |

Standard installation, given a working network:

```bash
arduino-cli core install esp32:esp32@2.0.17
arduino-cli lib install NimBLE-Arduino@1.4.3
```

The build workspace defaults to `~/esp` (on Windows `C:\Users\<you>\esp`) and can be overridden
with the `BTWH_WORK` environment variable. **Do not put it inside the repository**: Windows'
`CreateProcess` has a path-length limit, the toolchain contains very long C++ header names, and a
deeper directory makes `g++` fail with `The filename or extension is too long`. The source of
truth stays in the repository; the short-path copy is only a build artefact.

### Slow networks (measured 2026-11-07)

| Channel | Throughput |
|---|---|
| GitHub direct / PlatformIO CDN | timeout / 18 KB/s |
| arduino-cli's own downloader | 78 KB/s |
| GitHub mirror prefix + parallel curl | 500–665 KB/s |

When downloads stall, prefix the release URL with any GitHub mirror and unpack the archives into
`<workspace>/data/packages/esp32/` following the Arduino layout. Two traps: extraction must use
the `\\?\` long-path prefix or it aborts partway; and completeness must be judged by the presence
of `libexec/gcc/xtensa-esp32s3-elf/8.4.0/cc1plus.exe` — a leftover `gcc.exe` makes it look
finished when it is not.

## 9. Status (2026-11-12)

| Stage | State |
|---|---|
| Vendor manuals / schematic / datasheet / driver | ✅ `manual/` |
| Board identification (CH343 port; can re-enumerate, e.g. COM3 → COM8 after a re-plug — `gateway_cli.py` auto-detects, `--port` overrides) | ✅ |
| arduino-cli toolchain (short path, local packages, NimBLE 1.4.3) | ✅ |
| v2 firmware (runtime code entry, SMP pin, `A2/07`/`A2/08`, A0 parsing) | ✅ |
| Real-heater BLE session | ✅ **passed**: encrypted link, `A2/07` = `01`, `A2/08` = `01`, steady A0 |
| Power-cycle recovery | ✅ reconnects unattended using the code stored in NVS, including booting before the AP (30 s join retry) |
| Wi-Fi fallback | ✅ bench-verified end to end on the real board with the stored SSID removed: 5 min without an IP → AP up, 10 idle minutes → AP closed, retries resumed, no reset, and the cycle repeats while the network stays broken (ADR-018). The page also works over that AP — form, BLE scan, save. Two reboots surfaced on the way (modem sleep, and `/save` deleting its own server); both fixed |
| Wi-Fi / provisioning portal / broker credentials | ✅ |
| MQTT + Home Assistant discovery (22 entities) | ✅ bench-verified against a production HAOS Mosquitto |
| OTA + HA update entity | ✅ end to end: MQTT URL flash, manifest, Install button, 4.5 s reflash, entity settles off |
| Host-side C++ selftest (`host_selftest.py`, 453 checks) | ✅ in CI |
| Stability soak | ✅ 25 min: zero heartbeat drift, forced AP outage recovered in 6 s; one-week live soak pending |
| Hardware facts (16 MB flash, 8 MB Octal PSRAM idle) | ✅ identified; the configuration upgrade is the next step |

> ⚠️ Do not run `gateway_cli.py --flash` casually: changing the FQBN triggers a full rebuild that
> takes over 20 minutes on Windows, and it flashes a firmware whose boot has not been verified
> yet. To just watch the console use `python tools/gateway_cli.py` with no arguments, or the control
> panel.

### Next cut once the PSRAM upgrade is done

Switch the FQBN in `gateway_cli.py` to the target value in §7 → `--flash` → confirm the boot
banner reports `Flash=16MB  PSRAM=8388608 bytes` → then move on to Wi-Fi / MQTT / discovery.
Keep a copy of the verified binaries in the build workspace (default `~/esp/backup_v2/`) so
esptool can restore them.
