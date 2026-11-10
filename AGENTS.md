# Agent notes

Read this before changing firmware or protocol code. Project-wide conventions live in
[`CONTRIBUTING.md`](CONTRIBUTING.md); this file is the short version an agent needs first.

## Build and flash

```bash
python run_tests.py                                    # 195 offline tests, no hardware
python firmware/bridge/tools/gateway_cli.py --build    # compile only (~15 min cold)
python firmware/bridge/tools/gateway_cli.py --flash    # compile, flash COM3, open console
python firmware/bridge/tools/gateway_cli.py            # console only, no rebuild
```

- Toolchain: arduino-cli + arduino-esp32 **2.0.17** + NimBLE-Arduino **1.4.3**. Both pinned;
  NimBLE 2.x changes the security callbacks and will not compile.
- Build workspace: `~/esp` (override with `BTWH_WORK`). **Never inside the repository** — Windows
  CreateProcess has a path limit and the toolchain contains very long header names.
- `firmware/bridge/src/` is the only copy of the firmware sources. `gateway_cli.py` materialises
  an arduino-cli sketch around it at build time; do not add a second `.ino` copy.
- Every header carries **a macro guard as well as `#pragma once`**. arduino-cli compiles a copy of
  `src/` under `<build>/sketch/`, and the `-I` flag resolves a layered include back to the
  original tree. The same header then exists at two paths, `#pragma once` keys on the file rather
  than the name, and the build fails with `redefinition of 'struct app::Config'`. Measured, not
  theoretical: it cost two 15-minute builds to find.

## Before you touch protocol or payload code

```bash
python run_tests.py                                        # Python side, incl. the C++ host selftest
python firmware/bridge/tools/gen_vectors.py                # after changing spec/protocol_vectors.json
python firmware/bridge/tools/gen_profiles.py               # after changing spec/profiles.json
python firmware/bridge/tools/gen_mqtt.py                   # after changing spec/mqtt.json (cross-checks profiles.json)
python firmware/bridge/tools/gen_ha.py                     # after changing spec/mqtt.json
python firmware/bridge/tools/host_selftest.py              # the six C++ suites, on the host
# then flash and type: selftest                            # the same six suites, on the board
```

The six C++ suites (proto 64 / net 16 / mqtt 41 / rules 48 / provision 19 / discovery 73)
run in two places, and the check counts must stay in lockstep:

- **on the host** — `host_selftest.py` compiles the framework-free layers with g++/clang++
  (or `pip install ziglang`) and runs them; `run_tests.py` includes this via
  `test_cpp_selftest.py`, skipping when no compiler exists. The first hardware run of the
  discovery suite caught a check asserting a 12-byte fit as 10 - a check that until then had
  executed nowhere, which is the exact window this runner closes.
- **on the board** — the `selftest` serial command runs the same suites against real hardware.

Both stay green through bugs the other layers only find live: the client id once shipped
unterminated stack garbage (the host's stack happened to be zero) and the watchdog once
dropped a healthy link via a `millis()` underflow only reachable with a real broker answering.
When the MQTT path changes, bench-test it: configure a real broker over the serial console
and watch `[MQTT] + connected` hold past one keepalive interval before believing it works.

The suites:

- **proto** — framing, stuffing, per-fragment XOR, checksum, stream reassembly, every A0 field,
  and the model profile rows: each row's write bytes must rebuild the golden D3 vector, and its
  max setpoint must equal the `max_setpoint` the heater itself reports in the A0 frame
- **net** — the exact MQTT payload bytes for a known snapshot, `ts` omission, fault hex, mode
  words, and the rounding rule
- **mqtt** — CONNECT (with will, with credentials), PUBLISH/SUBSCRIBE bytes, a packet arriving one
  byte at a time, a two-byte remaining length, the malformed-length refusal, the device-id
  invariant: the string that becomes the client id is `[a-z0-9_]` only, terminated, and safe to
  truncate (an unterminated hex buffer once shipped stack garbage into the client id and every
  real broker refused the connection while this suite stayed green), and the golden CONNECT:
  the exact 82-byte production packet, pinned byte for byte and re-submitted to a real
  Mosquitto during the bench session (rc=5 on test credentials proves the layout parses; the
  garbage client id would have been dropped as invalid before the password was ever read)
- **rules** — the inbound parsers every consumer's bytes go through before touching hardware:
  broker-address grammar (trailing colon rejected, bare IPv6 kept whole, port bounds),
  setpoint text (the `"42.0"` Home Assistant sends, trailing newlines trimmed, fractional
  and out-of-range refused, profile bounds pinned), command verbs, the live-only rule for
  retained commands, the backoff schedule (`nextBackoffMs`, the doubling sequence with its
  one-step overshoot), and `elapsedMs`'s argument order - the arithmetic shape of the 2.5.1
  watchdog underflow (three edges in these parsers were caught by this suite's first run)
- **provision** — the pairing form's gate: MAC grammar (dash/uppercase accepted, short/
  non-hex/trailing junk refused), the SSID field bounds, the six-digit pairing code rule
  that protects the heater's three-failure lockout, and the `pair_state` words mirrored
  into the Home Assistant attribute
- **discovery** — the `@DEV@` substitution, buffer refusals, the golden topic/payload pair, the
  invariant that exactly the entities reading the attributes topic are filed as diagnostics,
  the water_heater entity's command-topic binding, and that every generated payload still fits
  the publish buffer (a grown table must fail here, not as truncated JSON at the broker)

None of them covers anything that needs the heater. `mqtt` prints the payload for the *live*
state, so the same code path can be inspected over serial before a broker exists. `broker
<host[:port]>` starts the client; `status` shows why the last attempt failed.

## Layer rules (ADR-008)

| Directory | May include | Must not know |
|---|---|---|
| `src/proto/` | `<stdint.h> <stddef.h> <string.h>` | Arduino, NimBLE, MQTT |
| `src/ble/` | NimBLE, `proto/` | pairing semantics, MQTT |
| `src/auth/` | `proto/` reply codes + a send/wait/log callback | NimBLE types, Arduino, `Serial` |
| `src/app/` | ble, auth, proto, NVS | MQTT topics |
| `src/net/` | app state, generated contract (`mqtt_contract.h`) | frame bytes, NimBLE |

`src/net/` is the mirror image of `src/proto/`: it may not include Arduino or NimBLE either, so
the payload it produces can be checked byte for byte by the on-board self test today and by a
host compiler later. Its constants are generated from `spec/mqtt.json`; never retype a topic
into firmware.

`src/proto/` must stay free of Arduino includes — that is the only reason it can be tested on a
PC later, and it is checked by review, not by CI (yet). `src/auth/` is the same story one level
up: it takes a `Transport` of function pointers, so a fake one can answer `A2/07`/`A2/08` from a
table and the whole failure matrix can be replayed without a heater.

`src/main.cpp` is the board boundary only: `setup()`, `loop()` and the serial console. If you are
adding logic there, it almost certainly belongs in `app/`.

## Never commit

Real device MACs, pairing codes, `config.json`, vendor PDFs or manuals, toolchain binaries,
serial logs, APKs or decompiled output. `.gitignore` covers these; CI fails the build if
`config.json` is tracked or a credential pattern appears in library code.

## Reporting

State plainly which level a change reached: source edited, compiles, runs on the board, or
verified by the user. Do not collapse these into "done". A fresh flash that boots is not proof
that the heater still links — say which one was actually observed.
