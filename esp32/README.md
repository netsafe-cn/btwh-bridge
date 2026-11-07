# ESP32 gateway v2 — Waveshare ESP32-S3-DEV-KIT-N8R8

> Built 2026-11-07. Board identified as COM3 = CH343 (flash + logs), COM4 = native USB.
> Vendor manuals, schematics and datasheets: see [`manual/`](manual/).

## 1. Two defects fixed in v2

| # | Problem | Consequence | v2 fix |
|---|---|---|---|
| 1 | **v1 ignored the BLE SMP pin entirely** | Measured on hardware: the panel code is **both the BLE passkey and the application-layer password**, and the device requests `PROVIDE_PIN` while the pairing window is open. v1 used `NoInputNoOutput` + Just Works, so SMP failed and `A2/08` could only answer `00` | Implement `onPassKeyRequest()` to return the six-digit panel code; set IO capability to `KEYBOARD_ONLY` (falls back to Just Works when the peer has no IO) |
| 2 | **The pairing code was compiled in** | A panel code expires within minutes; changing it meant a rebuild plus reflash of two minutes or more — far too slow in practice | **Enter the code at runtime** over serial; configuration is persisted in NVS, so one flash covers unlimited retries |

The protocol was also re-checked byte for byte against `bosch_api.py`: the stuffed range
includes the checksum, XOR restarts per fragment, FIFO and CREDITS share one callback, `A2`
resends five times at six-second intervals with a fresh msgId, and the A0 field offsets are the
ones measured on 2026-11-01.

## 2. Layout

```
esp32/
├── gateway/                       main project (PlatformIO / arduino-cli)
│   ├── src/main.cpp               firmware source, v2.0
│   ├── arduino_sketch/            the .ino arduino-cli needs (copied from main.cpp)
│   ├── platformio.ini             PlatformIO configuration (NimBLE pinned to 1.4.3)
│   ├── gateway_cli.py             build / flash / serial console / logging in one command
│   ├── esp32_panel.py             always-on-top control panel (enter the six-digit code)
│   ├── pair_once.py               pair in one command and report a verdict
│   └── log_diag.py                serial log diagnosis
├── manual/                        board documentation download guide (PDFs not shipped)
└── README.md                      this file
```

## 3. First run (one command)

```bash
cd esp32/gateway
python gateway_cli.py --flash
```

It finds the CH343 port, builds, flashes and opens the serial console.

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
| `go` | Reconnect with the current code (no new pairing) |
| `pair` | Clear the local bond and force a fresh SMP pairing |
| `scan` | Scan for 5 s and list nearby BLE devices (address / RSSI) |
| `mac AA:BB:CC:DD:EE:FF` | Set the login MAC (the host on the device's whitelist) |
| `addr AA:BB:CC:DD:EE:FF` | Set the heater's BLE address |
| `status` | Print configuration and runtime state |
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
| No serial output | Wrong port | **Use COM3 (CH343), not COM4**; do not enable USB CDC On Boot |

## 6. Expansion order once verification passes

```
1. Wi-Fi connection (first provisioning without touching code)
2. MQTT reporting + Home Assistant discovery
3. Bemfa reporting (Mi Home channel, reusing the existing topic convention)
4. Setpoint commands downstream (D3 write, sendFrame is already usable)
5. OTA updates (no more unplugging to reach USB)
6. Reconnect hardening + watchdog (soak for two days)
```

## 7. Hardware facts

| Item | Value |
|---|---|
| Board | Waveshare ESP32-S3-DEV-KIT-N8R8; **esptool reports 16 MB flash** (manufacturer 0x46 / device 0x4018), which contradicts the "N8" in the part number — trust the measurement |
| PSRAM | 8 MB **Octal** (the R8 suffix). Only enabled with `PSRAM=opi`; left at the default (Disabled) `ESP.getPsramSize()` reports 0 |
| FQBN, currently on the board | `esp32:esp32:esp32s3:FlashSize=8M` — built, flashed and boot-verified |
| FQBN, target | `esp32:esp32:esp32s3:FlashSize=16M,PartitionScheme=default_8MB,PSRAM=opi` — matches the 16 MB flash and unlocks the 8 MB PSRAM |
| Partitions | `FlashSize=8M` implies `default_8MB` (3 MB app × OTA + 1.5 MB SPIFFS). Switching to 16M **requires the explicit `PartitionScheme`**, otherwise it falls back to `default` and the app partition shrinks to 1.2 MB |
| BLE stack | **NimBLE-Arduino 1.4.3** (2.x changed the callback signatures — pinned) |
| Log / flash port | COM3 (CH343 → UART0), 115200; upload at 460800 |
| Heater address | Found with `scan`, then persisted with `addr <MAC>` (placeholder `AA:BB:CC:DD:EE:FF`) |
| Login MAC | This host's Bluetooth address, set with `mac <MAC>` (placeholder `11:22:33:44:55:66`) |

Protocol references: `BLE_PROTOCOL.md` in the repository root (field calibration in §6, B0/D7 in
§16), `API.md`, and `bosch_api.py` (136 tests). The SMP pin behaviour is corroborated by captures
on real hardware — see `API.md §10`.

## 8. Build environment (new machine / reinstall)

No Arduino IDE: **arduino-cli on the command line**, so `python gateway_cli.py --flash` covers
build + flash + log.

| Component | Version / requirement |
|---|---|
| arduino-cli | ≥ 1.0, on `PATH` or at `esp32/toolchain/arduino-cli.exe` (the local toolchain is not committed) |
| arduino-esp32 core | **2.0.17** (3.x ships no bundled NimBLE and changed the callbacks) |
| NimBLE-Arduino | **1.4.3** (2.x is incompatible — pinned) |
| Board FQBN | `esp32:esp32:esp32s3:FlashSize=8M` |

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

## 9. Status (2026-11-07)

| Stage | State |
|---|---|
| Vendor manuals / schematic / datasheet / driver | ✅ `manual/` |
| Board identification (COM3 = CH343, COM4 = native USB) | ✅ |
| arduino-cli toolchain (short path, local packages, NimBLE 1.4.3) | ✅ |
| v2 firmware (runtime code entry, SMP pin, `A2/07`/`A2/08`, A0 parsing) | ✅ builds at 536 569 B, flash hash verified, boot banner clean |
| Real-heater BLE session | ✅ **passed**: encrypted link, `A2/07` = `01`, `A2/08` = `01`, steady A0 |
| Power-cycle recovery | ✅ reconnects unattended using the code stored in NVS |
| Hardware facts (16 MB flash, 8 MB Octal PSRAM idle) | ✅ identified; the configuration upgrade is the next step |
| Wi-Fi / MQTT / Home Assistant | ⏳ next |

> ⚠️ Do not run `gateway_cli.py --flash` casually: changing the FQBN triggers a full rebuild that
> takes over 20 minutes on Windows, and it flashes a firmware whose boot has not been verified
> yet. To just watch the console use `python gateway_cli.py` with no arguments, or the control
> panel.

### Next cut once the PSRAM upgrade is done

Switch the FQBN in `gateway_cli.py` to the target value in §7 → `--flash` → confirm the boot
banner reports `Flash=16MB  PSRAM=8388608 bytes` → then move on to Wi-Fi / MQTT / discovery.
Keep a copy of the verified binaries in the build workspace (default `~/esp/backup_v2/`) so
esptool can restore them.
