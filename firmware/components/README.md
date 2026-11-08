# firmware/components/ — shared C, no application

Code here is reusable by any application in `firmware/`, and by nothing outside it. Two rules
keep this directory from turning into a junk drawer:

1. **A component may not depend on an application.** No `#include "../bridge/..."`, ever.
2. **A component may not depend on a framework.** No Arduino, no ESP-IDF headers in the core
   logic — so it compiles on a PC and can be unit-tested without a board.

## bosch_proto (planned)

The protocol layer that currently lives inline in `firmware/bridge/src/main.cpp`:

| Function | Role |
|---|---|
| `buildFrame` / `stuffFrame` / `xorBuf` | logical frame -> wire bytes, stuffing, per-fragment XOR |
| `decodeStream` | plaintext stream -> complete frames |
| `parseA0` | 34-byte broadcast -> field struct |

Moving it here is the next firmware step, and it buys three things:

- the golden vectors in [`spec/protocol_vectors.json`](../../spec/protocol_vectors.json) can be
  run as a native test, so a codec change fails on the PC before it reaches a board
- the bridge's `main.cpp` drops to session and command handling
- if a future application genuinely needs the bytes (a diagnostic dongle, say), it links the
  same code instead of copying it

Until then the codec lives in `firmware/bridge/src/main.cpp` and is correct there — it has been
verified against real hardware. This move is a refactor with a test harness as the exit
criterion, not a rewrite.
