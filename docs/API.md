# bosch_api interface contract

Version: v1.0 (2026-11-01) · English

Principle: **additive only**. Published signatures and semantics are frozen; new
capability arrives as new functions or new optional parameters with defaults.

---

## 1. Stability promise

| Rule | Meaning |
|---|---|
| Signatures frozen | No deletions, no renames, no new required parameters; new parameters must have defaults |
| Semantics frozen | The meaning of `True`/`False`, the exception types, and the shape of return values do not change |
| Extension | Only via new functions or new optional parameters |
| Regression gate | `python run_tests.py` must be green after any change (214 tests today) |
| Breaking change | Requires a major version bump and a migration note in this document |

---

## 2. Layers and responsibilities

```
Config          Configuration read/write (environment overrides included)
DeviceState     Device state dataclass (19 fields plus compatibility properties)
BoschProtocol   Pure codec and parsing (no I/O, fully unit-testable)
BoschDevice     One session (connect, login, send/receive, queries)
BoschService    Resident service (worker thread, auto-reconnect, command queue, result cache)
Pairing helpers Module level: ensure_paired / paired_state / unpair_device / parse_mac
```

**The API contains no UI logic.** The interface layer only displays and collects user
input; pairing, login and reconnection all happen inside the API.

---

## 3. Pairing interface (module level)

| Function | Signature | Notes |
|---|---|---|
| `paired_state` | `async (address: str) -> str` | Returns `'paired'` / `'unpaired'` / `'unknown'` (device unreachable, or not Windows) |
| `ensure_paired` | `async (address: str, log=None) -> bool` | Guarantees OS-level pairing; bonds via Just Works when needed. `True` = paired, or nothing to do |
| `unpair_device` | `async (address: str) -> bool` | Removes the host-side pairing (clears stale state). `True` = removed, or was not paired |
| `parse_mac` | `(address: str) -> int` | `'AA:BB:CC:DD:EE:FF'` → int |

### The two layers of pairing (important)

```
Link layer (BLE bonding)   <- ensure_paired, fully automatic
    Requires: the device is in its pairing window, opened by the panel action "clear pairing".

Application layer (MAC authorisation)  <- BoschDevice.login(), fully automatic
    Requires: config.pair_code holds the 6 digits currently shown on the panel.
```

**Software cannot open the pairing window remotely** — a human must use the panel. That
is a physical constraint, not an API gap. External indicators that the window is open
(the API uses the same ones):

- the BLE pairing request is `PROVIDE_PIN` (not `CONFIRM_ONLY`)
- `A2/07` answers `02` (not `00`)

---

## 4. Session interface (BoschDevice)

| Method | Signature | Notes |
|---|---|---|
| `connect` | `async () -> None` | Establish the link and subscribe to FIFO/CREDITS (reads no characteristic) |
| `close` | `async () -> None` | Disconnect and clean up |
| `login` | `async () -> bool` | `A2/07`, then `A2/08` (sent even when the device answers `01`); on success the device starts pushing `A0` |
| `send` | `async (cmd, data=b"", receiver=0x00, tag="") -> None` | Send one frame (chunking, stuffing, encryption) |
| `send_and_wait` | `async (cmd, data=b"", receiver=0x00, tag="", timeout=6.0, retries=5) -> bytes` | Single-flight: wait for a reply, resend with a fresh msgId on timeout; raises `TimeoutError` when exhausted |
| `set_temperature` | `async (value: int) -> None` | Write the setpoint (35–60) through `send_and_wait` |
| `read_history` | `async (kind=2) -> bytes` | B0 history: 2 = run statistics, 3 = bathing records |
| `read_errors` | `async (count=10) -> bytes` | D7 fault list |
| `idle_seconds` | `async () -> float` | Seconds since data last arrived |

Properties: `state: DeviceState`, `login_state: str`, `cooldown_sec: float`

### Exception contract

| Exception | Scenario |
|---|---|
| `TimeoutError` | `send_and_wait` retries exhausted; `set_temperature` unconfirmed |
| `RuntimeError("GATT not ready")` | Service enumeration failed (usually a broken pairing) |
| `RuntimeError("not connected")` | `send` before `connect` |

---

## 5. Service interface (BoschService)

Synchronous, thread-safe, meant for the GUI, scripts and automation.

| Member | Kind | Notes |
|---|---|---|
| `start()` / `stop(timeout=3)` | method | Start/stop the worker thread and connection loop |
| `running` | property | Whether the thread is alive |
| `state` | property | Most recent `DeviceState` (may be `None`) |
| `status` | property | Connection status text (fit for display) |
| `login_state` | property | Login status text (fit for display) |
| `connected` | property | Whether the link is up |
| `set_temperature(v)` | method | Enqueue (non-blocking, does not raise) |
| `query_history(kind=2)` | method | Enqueue; result lands in `results['history']` |
| `query_errors(count=10)` | method | Enqueue; result lands in `results['errors']` |
| `results` | property | `{'history': bytes, 'history_ts': float, ...}` |
| `take_logs()` | method | Drain and return the log queue |
| `update_config(cfg)` | method | Hot-swap configuration and reconnect |
| `set_pair_code_and_reconnect(code, save=True)` | method | Change the pairing code and reconnect immediately (the window is short-lived) |
| `pair_fail_reason` | property | Reason code of the last pairing failure, see §10 |
| `pair_fail_hint()` | method | One-sentence action for the user |

**Automatic behaviour** (callers need not care):

- `ensure_paired` before every connect
- three consecutive GATT failures → `unpair_device` self-heal
- `3C`/`5A` cooldown → reconnect interval of at least 30/90 s
- a stable session that gets dropped → reconnect after 30 s
- stale command queue cleared after a successful login
- configuration change → leave the current session cleanly, then reconnect

---

## 6. Protocol layer (BoschProtocol, pure functions)

| Method | Notes |
|---|---|
| `xor` / `encode_chunks` / `build` / `stuff` / `unstuff` / `decrypt_chunk` / `decode_stream` | Codec |
| `parse_state(payload) -> DeviceState` | Parse A0 (Type0/Type1 detected automatically) |
| `parse_history(payload) -> dict` | Parse B0: `{type, a (run minutes), b (ignition count), raw}` |
| `parse_errors(payload) -> List[int]` | Parse D7: skip the `0xEA` status byte, return the fault codes |
| `encode_pair_code(code) -> bytes` | Panel code → 6 bytes (one numeric byte per digit) |

---

## 7. Examples

### 7.1 Everyday use (the common case)

```python
from bosch_api import Config, BoschService

svc = BoschService(Config.load())
svc.start()                       # pairs, logs in, reconnects and collects on its own
print(svc.state.temp_out)         # outlet temperature
svc.set_temperature(42)
svc.query_errors()
```

### 7.2 First pairing / new host (two manual steps)

```python
from bosch_api import (Config, BoschDevice, ensure_paired,
                       paired_state, unpair_device)

cfg = Config.load()
addr = cfg.device_address

# Step 0 (human): panel -> "clear pairing"  (opens the window)
# Step 1 (human): panel -> "connect/pair"   (read the 6 digits)
cfg.pair_code = "123456"          # <- what the human read

# Step 2 (code): link layer
if not await ensure_paired(addr, log=print):
    raise SystemExit("pairing failed: clear pairing on the panel and retry")

# Step 3 (code): application layer
dev = BoschDevice(cfg)
await dev.connect()
if not await dev.login():
    raise SystemExit("login failed: %s" % dev.login_state)
await dev.close()
```

### 7.3 Inspecting pairing state

```python
state = await paired_state(addr)   # 'paired' / 'unpaired' / 'unknown'
if state == "unpaired":
    await ensure_paired(addr)      # re-bond (the window must be open)
elif state == "paired":            # but the device disagrees -> clear and retry
    await unpair_device(addr)
```

### 7.4 Data capture

```bash
python python/tools/collector.py --out collect.csv     # one CSV row per A0 frame
```

---

## 8. Change log

| Date | Version | Change |
|---|---|---|
| 2026-11-01 | v1.0 | First contract: pairing helpers (ensure_paired/paired_state/unpair_device), B0/D7 parsing, the nineteen A0 fields |
| 2026-11-07 | v1.0.1 | `set_pair_code_and_reconnect` used to raise `AttributeError` on non-string input; now `ValueError` (bugfix, **signature unchanged**). Added the signature-freeze contract `spec/api_contract.json`, cross-platform golden vectors `spec/protocol_vectors.json`, service and state-machine tests; regression grew 59 → 136 tests |
| 2026-11-07 | v1.0.2 | Documented structured authentication state (`authorized`, `mac_registered`, `pair_kind`, `pair_summary()`) and `BoschService.pair_fail_reason` / `pair_fail_hint()`. Corrrected the `A2/07 = 01` semantics in §10.2 — no signature changes |

---

## 9. Device-side state machine contract (ESP32 bridge)

**Reference implementation**: `python/device_fsm.py` (framework-agnostic, unit-tested), with the
suite in `python/tests/test_fsm.py`. The C++ firmware must mirror it. Change this specification
and run the regression *before* changing the firmware.

### 9.1 Provisioning and pairing are orthogonal

| Axis | Goal | States | Persistence |
|---|---|---|---|
| Provisioning | ESP32 obtains Wi-Fi SSID / password | `unprovisioned → provisioning → connecting → connected / failed` | One-off; credentials in NVS |
| Pairing | Heater accepts the ESP32's BLE authorisation | `idle → window_wait → pin_ready → bonding → login → online / failed` | Repeatable; the 6-digit code is short-lived |

**Never collapse the two into a single "pairing mode" switch**: changing router must not
require clearing pairing on the heater panel, and a heater drop must not require
re-entering the Wi-Fi password. `reset_wifi_only()` clears Wi-Fi and keeps the bond and pin.

### 9.2 Composite modes exposed to users

| Mode | Entry condition | Behaviour |
|---|---|---|
| `setup` | No Wi-Fi credentials / user request | Opens the provisioning channel; **pairing with the heater may still proceed in parallel** |
| `normal` | Credentials present, link up | Normal BLE ↔ MQTT bridging |
| `recovery` | Wi-Fi failed ≥ 3 times | Opens an AP for reconfiguration; **does not touch the pairing axis** |

### 9.3 Physical constraints

1. The pairing window can only be opened by a human on the panel; software cannot trigger it.
2. The 6-digit code is short-lived and different every time: enter it at runtime, never
   compile it into firmware, never treat it as a long-term credential.
3. The panel code is simultaneously the BLE SMP passkey and the application-layer `A2/08`
   password.
4. The window is 60 s by default; after it expires, read a new code. Once `online`, the
   window no longer applies.

### 9.4 NVS key space (C++ and Python must agree)

| namespace | keys | Owner |
|---|---|---|
| `bosch_wifi` | `ssid`, `pass` | provisioning |
| `bosch_heater` | `addr`, `login_mac`, `pin` | pairing |
| `nimble_bond` | `*` | **managed by NimBLE; the provisioning flow must never delete it** |

### 9.5 Security constraints

`snapshot()` feeds MQTT / Home Assistant and **emits only the boolean `has_pin` — never the
pin in clear text, never the Wi-Fi password**. `config.json` (real MAC, pairing codes,
cloud UID) must never be committed; the repository ships `config.example.json` only, with
secrets injected through the environment.

---

## 10. Pairing failure reasons and hints

**Background**: the pairing window is opened by a human and lasts tens of seconds; software
can neither open nor extend it. A bare "cannot connect" or "MAC not authorised" sends users
to fix the wrong thing (editing `login_mac`, changing machines) when what they need is a
fresh code from the panel. Failure reasons are therefore frozen as **reason codes**,
decoupled from display text.

### 10.1 Reason codes (module-level constants)

| Constant | Value | Trigger | One-step action for the user |
|---|---|---|---|
| `PAIR_FAIL_WINDOW_CLOSED` | `window_closed` | `A2/07` answers `00` | Panel "clear pairing" → "connect/pair" → enter the code within 30 s |
| `PAIR_FAIL_CODE_REJECTED` | `code_rejected` | `A2/08` answers `00` while the window is open | The code expired; read a new one and enter it immediately |
| `PAIR_FAIL_CODE_MISMATCH` | `code_mismatch` | `A2/08` answers `02` | Check that it is the current panel code (not the nameplate S/N) |
| `PAIR_FAIL_NO_RESPONSE` | `no_response` | 6 s × 5 retries exhausted | Stay on the pairing screen; power-cycle the heater if needed |
| `PAIR_FAIL_COOLDOWN` | `cooldown` | `A2/07` answers `3C` / `5A` | Wait 30–90 s; do not hammer the connection |
| `PAIR_FAIL_LINK` | `link_failed` | Link never came up | Move closer, wake the panel, close the phone app |

Reading it:

```python
dev.pair_fail_reason      # 'window_closed' | None | ...
dev.pair_fail_hint()      # one actionable sentence, safe to display
```

**Never infer the cause by matching `login_state` text** — wording changes, reason codes
are the contract.

### 10.2 The `A2/07` reply byte reports the window, not the MAC

Measured on an ESP32-S3 against real hardware, 2026-11-07:

| Reply | Meaning | Notes |
|---|---|---|
| `02` | Window open, code required | Normal path; send `A2/08` next |
| `01` | **MAC already registered, code still required** | Measured `01 → A2/08 = 00` failures, so `01` is **not** terminal; `A2/08` is still sent |
| `00` | **Window not open, or already timed out** | v1.0 of this document called it "MAC not authorised"; measurement shows it is window state |

Two hard rules follow:

1. On `00`, do **not** send the user off to edit `login_mac`. Still send `A2/08` (the vendor
   app behaves the same), but record the reason as `window_closed`.
2. A later `A2/08 = 00` **must not overwrite** `window_closed`; otherwise "no window" gets
   reported as "wrong code" and the user chases a code that was never the problem.

### 10.3 Window and code lifetimes

- **First pairing**: tens of seconds between the panel showing a code and the device
  validating it; software cannot extend it
- Recommended sequence: "clear pairing" → "connect/pair" → submit **within 30 s**
- Do not leave the pairing screen after reading the code — leaving closes the window
- Do not retry back-to-back on failure: the device rate-limits (`3C` = 30 s, `5A` = 90 s)

**Once a code has paired successfully, the device keeps it long-term** (measured 2026-11-07):

> paired with code `123456` at 19:21:55 → disconnected 19:24 → reconnected 19:32
> (11 minutes later, window long closed) with the same code: `A2/07 = 01`, `A2/08 = 01`,
> `A0` stream resumed.

Conclusions:

1. The code has two lifetimes — **short for the first pairing**, **bound as a long-term
   credential after success**
2. A code that never paired successfully is worthless (measured: `333223` judged `00` even
   on an already-authorised device)
3. Whether the gateway recovers after a power cycle therefore depends on **the firmware
   choosing to reconnect with the NVS code**, not on any device-side limit

**Current firmware behaviour**: on boot it reconnects automatically with the stored code,
falling back to waiting for manual entry (implemented and verified 2026-11-07).
**The upper bound of code validity is still unknown** (11 minutes confirmed; days or weeks
would need long-term observation).

### 10.4 Structured authentication state

Higher layers (UI, MQTT reporting, diagnostics) **must not parse `login_state` text**. Use
these fields instead:

| Member | Type | Meaning |
|---|---|---|
| `mac_registered` | bool | The device recognises this host's MAC (`A2/07` = `01` or `02`) |
| `authorized` | bool | Application-layer authorisation passed (`A2/08` = `01`) — **the only proof that `A0` will arrive** |
| `pair_kind` | str | `first_pair` (`A2/07 = 02`) / `rebind` (`A2/07 = 01`) / `""` |
| `pair_code_verified_at` | float \| None | When this code was last verified successfully |
| `pair_fail_reason` | str \| None | Reason code, see §10.1 |
| `pair_summary()` | dict | All of the above, **without the code**, safe to publish |

The `first_pair` / `rebind` distinction is operational:

- `first_pair`: the device has never seen this MAC, so the code must come from within the
  window
- `rebind`: the MAC is already registered; a code that once verified stays valid for a long
  time (11 minutes measured)

`BoschService` exposes the same `pair_fail_reason` and `pair_fail_hint()`, and writes them to
the log whenever a login fails.
