# Project memory

Facts established by measurement, kept here so they are not re-derived. Everything below was
observed on real hardware unless marked otherwise. Protocol detail lives in
[`docs/BLE_PROTOCOL.md`](docs/BLE_PROTOCOL.md); this is the index of things that bit us.

## Hardware in use

| Item | Value |
|---|---|
| Board | Waveshare ESP32-S3-DEV-KIT-N8R8 (`ESP32-S3-WROOM-1-N8R8`) |
| Serial | `COM3` = CH343 (UART0: flash + logs). `COM4` = native USB/JTAG, unused |
| Flash | esptool reports **16 MB** (0x46/0x4018), not the 8 MB the model name suggests |
| PSRAM | 8 MB Octal, **not enabled** in the current build (`PSRAM=0 bytes` in the boot banner) |
| Heater | Bosch Therm 4800i, BLE name `BTWH-001-6F5F` |

## Established by measurement

- **The panel code is both the BLE SMP passkey and the `A2/08` application password.** Early
  firmware used Just Works and could never succeed.
- **`A2/07` reports the pairing window, not the MAC state.** `02` = open, `00` = closed or
  expired, `01` = MAC already registered. Treating `00` as "MAC unauthorised" sends users to edit
  `login_mac`, which never helps.
- **A code that paired once stays valid after the window closes** (verified 11 minutes later;
  the upper bound is unknown). This is what makes power-cycle recovery possible.
- **`01` is not a pass.** A `01` → `A2/08=00` failure was observed; only `A2/08=01` means
  authorised.
- **Three consecutive connection failures trigger device-side rate limiting**, recovering after
  about 3 minutes. Do not retry in a tight loop.
- **An abrupt reset of the bridge can leave the heater refusing sessions for ~15 minutes, and
  retrying makes it feel worse.** Measured 2026-11-08 14:58 → 15:15: after the board restarted
  under a live link, the heater accepted the connection, completed SMP, accepted both CCCDs, then
  never answered `A2/07` and hung up 12 s later; by 15:11 it would not even connect (`cannot
  connect` in 1 s). Retries at 15:03, 15:06, 15:08 and 15:11 all failed. At 15:15 — with **the
  same stored code and no panel action** — `A2/07=01` and `A2/08=01` came straight back and the
  stream resumed. Corollary: never read this signature as "the code is stale". The fix is to stop
  touching it and wait, which is why the bridge needs a backoff supervisor rather than a retry loop.
- **The trigger is an abrupt reset, not the reboot itself — and a clean disconnect fixes it.**
  Reproduced a second time the same day: closing the control panel at 15:32:54 (which held a
  17-minute live session) and reopening it at 15:33:27 gave `cannot connect` within 2 s, because
  both host consoles used to send `reboot` on attach, i.e. `ESP.restart()` with the BLE link still
  up. **Verified by A/B on the same device, same code, same link**: old firmware `reboot` at 14:58
  cost ~17 minutes of no service; new firmware (`app::shutdown()` sends LL_TERMINATE_IND and waits
  300 ms first) at 15:47 went `link established` 2.2 s, `A2/08=01` 6.4 s, first `A0` 9.5 s after
  the reboot command. Both consoles now attach with `status` instead of resetting the board.
- **A0 offsets** were calibrated against 148 frames captured 2026-11-01. Type0 and Type1 differ
  except for the temperatures. See `docs/BLE_PROTOCOL.md` §6.
- **`D7` first byte `0xEA` is a status byte, not a fault code.** The app showed no fault history
  while the parser reported one.
- **`[10] & 0x10` is the flame bit and it tracks combustion at any power** — it was never wrong,
  just never sampled while burning. 2026-11-08 tap cycle (4 s frames): 0 % → `no`, first burning
  frame at **19 %** → `yes`, last burning frame at 47 %, and the very next frame went `no` while
  power was still 40 % (flame out, load not yet released). Full trace in `docs/BLE_PROTOCOL.md` §6.4.

## Current firmware state

- v2.1: layered into `proto/` / `ble/` / `auth/` / `app/`, runtime pairing code over serial,
  persisted in NVS, automatic reconnect after power loss.
- Verified on hardware: SMP encryption → `A2/07` → `A2/08=01` → steady `A0` stream, and
  unattended recovery after a reboot.
- Steady-state baseline (2026-11-08 15:15-15:24, panel log): **101 `A0` frames in 8.6 minutes**,
  interval min 3 s / median 4 s / max 9 s, no gap over 10 s, no dropped checksums, no link drop.
- Opening the host console no longer disturbs the bridge: verified 15:50:43, the panel attached to
  a session that already had 59 frames, printed `board left running`, and the stream continued.
- `selftest` runs the golden vectors on the board (framing, stuffing, XOR, checksum, reassembly,
  A0 fields).

## Open questions

- How long a paired code stays valid — 11 minutes is verified, days are not.
- Whether the heater keeps its authorisation across its own power loss (untested; the user
  considers this a non-issue).
- `PSRAM=0`: the R8 module has 8 MB Octal PSRAM that no build has enabled yet. Enabling it needs
  `FlashSize=16M,PartitionScheme=default_8MB,PSRAM=opi` and a full rebuild.

## Working environment quirks

- `github.com` is intermittently unreachable here (TLS handshake failures, 502 from the local
  proxy). `api.github.com` is reliable. Push retries are normal, not a sign of a bad commit.
- **Two Pythons.** The one first on `PATH` inside a bash tool call is a bundled 3.13.12 **without
  tkinter**, so every GUI tool (`python/tools/bosch_ui.py`, `firmware/bridge/tools/esp32_panel.py`)
  dies silently under it — the `.bat` falls back to terminal mode, which looks like "the panel is
  broken". Use `C:\Users\LZY\AppData\Local\Programs\Python\Python313\python.exe` (3.13.16,
  tkinter 8.6, pyserial 3.5). Both interpreters have pyserial.
- **A GUI started from an agent tool call does not survive that call.** `Start-Process` children
  are reaped when the invocation returns; the window appears, writes a line or two, then vanishes.
  Verified on 2026-11-08. The user must launch the panel themselves.
