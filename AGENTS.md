# Agent notes

Read this before changing firmware or protocol code. Project-wide conventions live in
[`CONTRIBUTING.md`](CONTRIBUTING.md); this file is the short version an agent needs first.

## Build and flash

```bash
python run_tests.py                                    # 150 offline tests, no hardware
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

## Before you touch protocol code

```bash
python run_tests.py                                        # Python side
python firmware/bridge/tools/gen_vectors.py                # after changing spec/*.json
# then flash and type: selftest                            # the same vectors, on the board
```

`selftest` is the only automated check that runs on the target. It covers framing, stuffing,
per-fragment XOR, checksum, stream reassembly and every A0 field with a vector. It does **not**
cover anything that needs the heater.

## Layer rules (ADR-008)

| Directory | May include | Must not know |
|---|---|---|
| `src/proto/` | `<stdint.h> <stddef.h> <string.h>` | Arduino, NimBLE, MQTT |
| `src/ble/` | NimBLE, `proto/` | pairing semantics, MQTT |
| `src/auth/` | `proto/` reply codes + a send/wait/log callback | NimBLE types, Arduino, `Serial` |
| `src/app/` | ble, auth, proto, NVS | MQTT topics |
| `src/net/` | app state, generated contract | frame bytes |

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
