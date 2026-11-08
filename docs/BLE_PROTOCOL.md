# BLE protocol notes — Bosch gas water heaters

> For the Bosch **Therm 4800i** gas water heater. Its Bluetooth module advertises as
> `BTWH-001-xxxx` (the identifier the code matches on). Measured on **Bosch Therm 4800i**,
> advertised name `BTWH-001-6F5F`; the vendor app package is
> `com.bosch.de.tt.prowaterheater`.
>
> **Disclaimer**: everything below comes from packet capture and bench comparison against
> a device I own. This is **not an official protocol**. It is for research on, and use
> with, your own hardware — not for commercial cracking or for tampering with anyone
> else's device. Firmware updates may invalidate any of it.

---

## 1. Device and connection

| Item | Value |
|---|---|
| Advertised name | `BTWH-001-6F5F` (pattern: `BTWH-001-` + last two MAC bytes) |
| Service base | `ff4396a6-50a8-4fc0-bcf6-228e82dfed` + two-digit suffix |
| Characteristics | `..b0` service / `..b1` MODE / `..b2` FIFO / `..b3` CREDITS / `..d0` service / `..d1` COUNTER |

### Characteristic roles

| Characteristic | Properties | Purpose |
|---|---|---|
| `b1` MODE | read / write | Factory value `ff`. The vendor app **never writes it, only reads and prints** — leave it alone |
| `b2` FIFO | write + notify | **All traffic goes through here** |
| `b3` CREDITS | write + notify | Subscribe to it (same CCCD write), but **writing to it is not required** |
| `d1` COUNTER | read only | Device uptime in seconds (22 s over a 22 s interval) — use it to detect a reboot |

### Connection notes

- CCCD value is **`01 00` (notify)** on both characteristics, subscribed ~200 ms apart
- Use **write with response** for the write characteristic
- MTU: Android negotiates 517, **Windows is pinned at 23**. A frame is up to 43 bytes, so it
  arrives as 20 + 20 + 3 and has to be reassembled
- The device **drops the link after ~45–50 s** (sooner — about 18 s — if you have not logged
  in), so automatic reconnect is mandatory
- The advertisement carries **no AD data at all** (no manufacturer data, no service data).
  Temperature is not broadcast

---

## 2. Frame format

```
7B 7B | sender | receiver | msgId | cmd | data... | chk | 7D 7D
```

| Field | Notes |
|---|---|
| Delimiters | leading `7B 7B`, trailing `7D 7D` |
| `sender` | Always `0x21` for the central |
| `receiver` | `0x00` for business frames; `0xAB` for login / configuration frames |
| `msgId` | Incrementing counter starting at 0; **the device echoes it in its reply** |
| `cmd` | Command byte, see §5 |
| `chk` | `sum(sender..data) & 0xFF` (the two leading bytes are excluded) |
| Base length | 9 bytes when there is no payload |

### Byte stuffing

Inside the data area (`sender` through `chk`), every `0x7B` or `0x7D` is followed by a
`0x00` pad byte.

> ⚠️ **No stuffing after the leading `7B 7B`.** Otherwise a `sender` of `0x00` looks like a
> pad byte and gets dropped, and every field after it shifts. This is the single easiest
> mistake to make.

---

## 3. Encryption

- XOR key: `1ag3.-fs7` (9 ASCII bytes)
- **Each notify fragment (≤ 20 bytes) restarts from `KEY[0]`** — the XOR is per fragment,
  not continuous across the frame

```
encrypt: split plaintext into 20-byte groups; within a group byte[i] ^= KEY[i % 9]
decrypt: XOR each fragment first, concatenate into a plaintext stream, then split on 7D 7D
```

This one cost a lot of time: a continuous whole-frame XOR produces data that looks plausible
but never validates.

---

## 4. Login and pairing (the section that matters)

### 4.1 Sequence

```
1. A2/07 + 6-byte MAC      -> decide identity from reply data[1]
2. A2/08 + encoded code    -> only data[1] == 1 actually enables the session
3. device starts pushing an A0 broadcast every 4-8 s
```

### 4.2 Reply semantics for step one (`data[1]`)

| Value | Meaning | Action |
|---|---|---|
| `01` | Already authorised | **Still run step two** |
| `02` | Password required | Run step two |
| `00` | Rejected | This MAC is not in the device whitelist |
| `3C` (60) | Cooling down | Retry later |

> ⚠️ **The big trap: `data[1] == 01` does not mean data will flow.**
> Measured: send only `A2/07` and skip `A2/08`, and the login "succeeds" while the device
> pushes **not a single byte**, then drops you after 45 s. You must send the pairing-code
> frame **unconditionally** before `A0` starts.

### 4.3 MAC whitelist

The device keeps an **exact MAC whitelist**; the 6 bytes in the login frame must match what
the device remembers.

Measured on the same unit:

| MAC submitted | Reply |
|---|---|
| all `00` | `00` rejected |
| random `1A2B3C4D5E6F` | `00` rejected |
| Huawei OUI `5CEA1D112233` | `00` rejected |
| the device's own MAC | `00` rejected |
| this host's Bluetooth address | `01` authorised |

The MAC is an **application-layer field** — no need to change the adapter address. To read
the host address:

- Windows: WinRT `BluetoothAdapter.GetDefaultAsync().BluetoothAddress`
- Linux: `/sys/class/bluetooth/hci0/address`

### 4.4 Pairing-code encoding

The six digits shown on the panel (not the nameplate S/N) are encoded trivially: **each
digit becomes one byte**.

```
"831152"  ->  08 03 01 01 05 02
```

Full frame (including the `A2 08` command bytes):

```
7b 7b 21 ab 01 a2 08 08 03 01 01 05 02 8b 7d 7d
              ^^ ^^ ^^^^^^^^^^^^^^^^^^
             cmd data (6 bytes = one digit each)
```

Note this is the **temporary on-panel code**, not the nameplate S/N. Bosch uses them
differently across models; this unit accepts the panel code.

### 4.5 The code is dynamic and short-lived (measured 2026-11-01)

The code is **not a fixed value**. It is generated when the heater enters its
Settings → Connect/Pair menu and **expires when that menu times out**.

- A code saved from a previous session is almost certainly dead. When the link goes silent,
  **the first thing to do is generate a fresh code**
- Move fast after reading it: leave the menu open, connect immediately
- If nothing arrives with a fresh code, suspect the host Bluetooth stack first: repeated
  aggressive connect/kill cycles leave it wedged. Restart the adapter (or the machine) and
  retry before blaming the heater

Order of investigation when the device goes silent:

```
1. Can the phone app connect?  yes = device is fine, look at the host;  no = device side
2. Reset the Bluetooth radio on the host
3. Generate a new panel code and connect immediately (do not run a scan first)
4. Still nothing -> restart the host Bluetooth service / the machine
5. Only then power-cycle the heater itself (panel off for 30 s)
```

---

## 5. Command table

| cmd | Name | Status |
|---|---|---|
| `A0` | Broadcast state (device → central) | ✅ decoded |
| `A1` | Set broadcast interval `[type, sec]` | ✅ works but **not required** |
| `A2` | Login (`07` MAC / `08` password) | ✅ decoded |
| `B0` | **History** `[type]`: 2 = run statistics, 3 = bathing records | ✅ see §16 |
| `C0` `C1` | Unknown | ❓ |
| `C2` | Serial test | ❓ |
| `D0` `D1` | Unknown | ❓ |
| `D2` | Read parameter | ✅ decoded |
| `D3` | Write parameter | ✅ decoded (temperature) |
| `D4` `D5` | Unknown | ❓ |
| `D7` | **Fault list** `[count]` | ✅ see §16 |
| `D8` | Read version | ✅ parameters 0–2, see §10 |
| `D9`–`DF` | Unknown | ❓ |
| `E0` | Unknown | ❓ |
| `E1` | Establish association | ❓ the vendor app never calls it either |
| `F0` | Unknown | ❓ |

The table was assembled from live captures cross-checked against app behaviour.

---

## 6. A0 broadcast payload

Pushed by the device on its own; the app **never requests it** (it reads a cache). `data` is
**34 bytes** and `type` is always 1 (asking for `type=0` still yields type 1).

### 6.1 Field table (calibrated against measurements, Type 1 frame)

> ⚠️ Offsets published in early third-party write-ups **do not match this firmware** —
> power, flow and fan speed especially. The table below is the one verified by bench tests.

| Index | Meaning | Conversion | Confidence |
|---|---|---|---|
| `[0]` | Broadcast type (0 = Type0, 1 = Type1) | — | ✅ |
| `[1][2]` | **Inlet temperature** | `[1] + [2]/100` | ✅ |
| `[3][4]` | **Combustion chamber temperature** | `[3] + [4]/100` | ✅ |
| `[5]` | **Setpoint temperature** | integer °C | ✅✅ verified three ways |
| `[6][7]` | **Chassis temperature** (ambient reference) | `[6] + [7]/100` | ✅ |
| `[8]` | (flow/10 in Type0; always 0 in Type1) | — | 🔶 |
| `[9]` | (power in Type0; always 0 in Type1) | — | 🔶 |
| `[10]` | Ionisation / flame detection bit | `& 0x10` | 🔶 |
| `[11]` | **Active fault code** | 0 = no fault | 🔶 |
| `[12]` | Gas ratio (0–7) | — | 🔶 |
| **`[13]`** | **Burner power %** (`[14]` is the fraction) | 0–100 | ✅ measured |
| `[14]` | Power fraction | `/100` | ✅ |
| **`[15][16]`** | **Fan speed, rpm** | `[15] * 100 + [16]` | ✅ measured |
| `[17]` | High-frequency jitter (0–90) | — | ❓ |
| `[18]` | Setpoint constant (always 6) | — | ❓ |
| `[19]` `[22]` `[23]` `[24]` | Combustion-related, zero when water stops | — | 🔶 |
| **`[21]`** | **Water flow, L/min × 10** | `[21] / 10` | ✅ measured |
| `[28][29]` | **Outlet temperature** (`[21][22]` in Type0) | `[28] + [29]/100` | ✅ |
| `[32]` | Operating mode (always 0 here, unusable) | — | ⚠️ |
| `[33]` | bit7 = bathroom controller / low 7 bits = max setpoint | — | 🔶 |

### 6.2 Temperature semantics (verified)

All four temperature fields were confirmed by running the unit under load:

| Field | Idle | Running hot water | Notes |
|---|---|---|---|
| `[1][2]` inlet | 26.1 | 18.5 | Cold water entering; lowest while drawing |
| `[3][4]` chamber | 26.7 | 39–45 | Combustion chamber / exchanger, heats first |
| `[6][7]` chassis | 26.0 | 26.0 | Enclosure temperature, nearly independent of draw — closest thing to "ambient" |
| `[28][29]` outlet | 25.4 | 40–45 | Outlet; what the user actually feels |

> An earlier version of these notes had `[3][4]` and `[28][29]` swapped. The test that
> settled it: `[28][29]` matches the panel display, and it falls back slowest after the
> water stops (residual heat in the pipework).

### 6.3 Determining operating state

`data[32]` is **always 0 on this unit** and unusable. Reliable rule:

```
flow  [21]/10 > 0.5   -> drawing hot water (burning)
else power [13] > 10  -> standby burn (a 10-17% flame is held after the tap closes)
else                  -> idle
```

Measured: fully open 10–11 L/min at 44–47%; the moment the tap closes power drops to 15–17%
and the fan goes from 1410 to 705 rpm.

---

## 7. D2 — read parameter

### 7.1 Frame

```
D2 | paramType | paramId
```

Reply payload: `[paramId, status, value]` (`status = 0` means OK).

### 7.2 Valid parameter whitelist (measured)

| paramType | Allowed paramId |
|---|---|
| `2` | 2, 14, 27, 43, 97, 29, 30, 39, 40 |
| `6` | 0–17, 107, 109 |

### 7.3 Observed values (this unit, idle)

| Request | Value | Guess |
|---|---|---|
| `2/2` `2/14` `2/27` `2/97` `2/29` `2/30` `2/39` | `0` | unused, or needs a trigger |
| `6/1` | `0` | — |
| `6/2` | `4` | model / configuration |
| `6/3` | `1` | model / configuration |
| `6/8` | `17` | **probably a temperature** |
| `6/9` | `20` | **probably a temperature** |
| `6/10` | `25` | **probably a temperature** |
| `6/13`–`6/16` | `0` | — |

> `6/8 = 17`, `6/9 = 20`, `6/10 = 25` all look temperature-shaped (outdoor, target, limit —
> unknown). Worth sampling while drawing water and while changing the setpoint.

Some requests simply get no reply (the device is slow). Retry, or space requests further
apart.

---

## 8. D3 — write parameter

### 8.1 Setpoint temperature

```
D3 | 02 | 55 | 00 | (byte) temperature
```

Reply payload `55 01` (`paramId` echoed, status 01 = success).

Measured: sending `7b 7b 21 00 02 d3 02 55 00 26 73 7d 7d` (0x26 = 38) makes `[5]` of the
next A0 read 38 within four seconds.

### 8.2 Writable whitelist (measured)

`paramType` accepts `2 / 6 / 30`; temperature goes through `85`, so the effective whitelist
for the third parameter is `2 / 6 / 30 / 85`.

Other writable parameters are **unexplored**. The vendor app has several `setParam`
overloads (byte / int / array), which suggests the same frame shape can be tried elsewhere.

---

## 9. A1 — set broadcast interval

```
A1 | type (0 or 1) | seconds (0-60)
```

- The app only sends it when the user changes the interval in the UI
- **Not required**: without any A1 the device still pushes A0 every 4–8 s
- Asking for `type = 0` still yields `type = 1`

---

## 10. D8 — read version

```
D8 | parameter (0-2)
```

Reply payload `[parameter, status, valueHi, valueLo]`, `status = 01` means success.

| Request | Reply | Notes |
|---|---|---|
| `D8 00` | `00 00 00` | parameter 0 invalid |
| `D8 01` | `01 01 41 13` | has data |
| `D8 02` | `02 01 01 09` | has data |
| `D8 03` | no reply | highest parameter is 2 |

> ⚠️ **Timing matters.** Send `D8` **within about three seconds of a successful login**. If
> you send a dozen `D2` requests first, the device stops answering entirely — which once
> made us conclude, wrongly, that `D8` was unsupported.

The meaning of the low two bytes is unresolved: possibly a packed firmware version
(`0x4113` / `0x0109`), possibly BCD (`4.1.13` / `1.09`). More samples needed.

---

## 11. Known to be silent / unexplored

| Command | Situation |
|---|---|
| `D2` with `paramType = 30` | all eight paramIds **silent** ⇒ only whitelist classes 2 and 6 exist |
| `E1` (establish association) | never observed in use, format unknown |
| `C2` (serial test) | not tried |
| `B1/C0/C1/D0/D1/D4/D5/E0/F0` | entirely unknown; likely side effects, not probed |

---

## 12. What is still worth digging (by value)

### 12.1 High value: fault codes and error history

The vendor's own Bluetooth gateway documentation states the series supports:

- **Error History** — historical fault codes
- **Diagnostic Mode** — a built-in diagnostic channel
- **Backup / Restore** — full configuration backup, used when replacing a mainboard

This is the most valuable next step: **fault codes matter far more than temperatures**,
especially when the unit throws an E-series error.

Ideas for probing:

1. Work through `D2` with `paramType = 2 / 6 / 30` (30 not yet tried)
2. Try the untouched `D`-range commands (`D4`, `D5`, `D9`–`DF`) with parameters `01` / `00`
3. **Provoke a fault on purpose** (close the gas valve to force an ignition failure) while
   scanning A0 to see which bytes move
4. Diagnostic mode probably maps to some `Dx` command plus a specific parameter

### 12.2 Medium value: consumption and run statistics

- Gas volume, running hours, ignition count
- Possibly behind `D2` with `paramType = 30`, or `D4` / `D5`
- `d1 COUNTER` is confirmed to be uptime in seconds, usable as a reboot detector

### 12.3 Medium value: fill in the unknown A0 fields

For `[6] [8] [9] [10] [12] [13–18] [20] [21] [24] [30] [31]` we know *when* they carry a
value but not what they mean.

**Method**: single-variable experiments. Change exactly one physical condition per run
(adjust the setpoint, open a tap, change the flow, switch mode) and record which bytes move.
This is the approach the community converges on for good reason.

### 12.4 Low value: reading the version

`D8` is silent, so the format may be wrong. It could also be inferred from the advertised
name or the `d1` characteristic, but it does not buy much.

---

## 13. Pitfalls (all of these happened)

1. **No stuffing after the leading bytes** — otherwise a `sender` of `0x00` is dropped and
   the whole frame desynchronises
2. **XOR is per fragment** — not continuous across the frame, or checksums never match
3. **`data[1] == 01` still requires the pairing code** — skip it and you receive zero bytes
4. **Login frames use `receiver = 0xAB`, business frames `0x00`** — get it backwards and it
   looks like the device never answers business frames
5. **The device hangs up** — 45–50 s, so reconnect automatically; always `try/finally` a
   forced disconnect, or a leaked link leaves every later attempt at `GATT not ready`
6. **The device cools down** — rapid reconnects get refused; back off 8 / 16 / 32 / 60 s
7. **One client at a time** — while the phone app is connected the PC cannot stay connected
   (it connects, then drops)
8. **Windows GATT cache** — occasional `BleakDeviceNotFoundError` or missing
   characteristics; one reconnect usually clears it
9. **Do not write MODE (`b1`)** — factory `ff`, the app never writes it, and undoing a stray
   write is a nuisance
10. **★★ Unpairing on Windows leaves a stale key on the device — everything goes silent
    (2026-11-01, the worst one).** After you delete the pairing in Windows, the device still
    holds the old link key for this address. Connecting in plaintext then makes it
    **silently discard every business frame**: the link establishes, GATT enumerates, `b1`
    and `d1` read fine, ATT writes return success — but `A2/07` gets no reply and `A0` never
    arrives. The phone is unaffected because it never bonded that way.
    **Fix**: keep system-level pairing. The device uses Just Works (`CONFIRM_ONLY`, no PIN);
    `bosch_api.ensure_paired()` bonds automatically and `BoschService` calls it before
    connecting. Diagnosing this cost over two hours — checking `paired_state()` first would
    have saved all of it.
11. **Only one client per device** — two program instances evict each other: the UI sits on
    "connecting" forever, the device stops advertising (it is occupied), and the cloud
    bridge starts echo-looping messages. The symptoms point convincingly at "device asleep"
    or "connection guard". Always take a single-instance lock.
12. **Send `D8` soon after login** — a dozen `D2` frames first and the device ignores it
    (easy to misread as "unsupported")
13. **Power has more than four steps** — observed `0 / 30 / 50 / 70 / 80`. Use `!= 0` to
    decide "is it burning"; do not assume a fixed set of levels.

---

## 14. Bundled tooling

| File | Purpose |
|---|---|
| `python/tools/bosch_ui.py` | Desktop console (BLE + Bemfa cloud + config + pairing-code login) |
| `python/tools/bosch_bridge.py` | Headless Bemfa bridge |
| `python/tools/bosch_read.py` | One-shot read; protocol constants and codec live here |
| `python/tools/collector.py` | Continuously capture A0 into a per-frame CSV |
| `python/tools/analyze_collect.py` | Analyse a collected CSV |

---

## 15. Disclaimer

These notes record observations and experiments on **hardware I own**. The protocol was
derived from interoperability testing, is not a public interface, and may break with any
firmware update. Do not use it against devices you are not authorised to access.

## 16. B0 history / D7 fault list (measured 2026-11-01)

### 16.1 B0 history

Request: `B0` + `[type]` (2 = run statistics, 3 = bathing records).

Measured reply for type 2: `02 00 00 12 67 00 00 05 b6` (9 bytes).

```
[0]      = echoed type (02)
[1..4]   = big-endian 32-bit A = 0x00001267 = 4711
[5..8]   = big-endian 32-bit B = 0x000005B6 = 1462
```

Per the vendor app's internal note ("big-endian count ÷ 60 = hours"):

- **A = run minutes** → 4711 min ≈ **78.5 hours**
- **B = ignition count** → 1462 (3.2 minutes per burn, plausible for domestic use)

> ✅ Cross-checked against the phone app (2026-11-01): the order and units are correct.

### 16.2 D7 fault list

Request: `D7` + `[count]` (1–10).

Measured on the same unit while the app reported "no fault history":

| Request | Reply |
|---|---|
| `D7 01` | `ea` |
| `D7 05` | `ea 00 00 00 00` |
| `D7 0A` | `ea 00 00 00 00 00 00 00 00 00` |

Conclusions:

1. The reply length **exactly equals the requested count**
2. The first byte is **always `0xEA`** — a status/start marker, **not a fault code**
   (originally misread as fault code EA until the app cross-check corrected it)
3. Remaining bytes are fault codes; `0x00` is an empty slot, so all-zero means no history

Parsing:

```python
codes = [b for b in payload[1:] if b]   # skip the status byte, keep non-zero codes
```
