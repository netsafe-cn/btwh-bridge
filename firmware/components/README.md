# firmware/components/ — shared C, no application

**Currently empty, and that is deliberate.**

Code belongs here only when **two or more applications** need the same implementation. Today none
does: `bosch_proto` (frames, A0 fields) has exactly one consumer, the bridge — the panel never
touches BLE, because the heater accepts a single connection and would evict the bridge.

A shared library with one user is a directory, not a library. So `bosch_proto` lives in
`firmware/bridge/src/proto/`, where its single consumer can see it, and it stays free of Arduino
and NimBLE includes so it can be compiled on a PC (see ADR-008).

## The rules, for the day something does land here

1. **A component may not include anything from an application.** No `#include "../../bridge/..."`,
   ever. The `layout` CI job fails on it.
2. **A component may not depend on a framework** — no Arduino, no ESP-IDF, no NimBLE in the core
   logic. That is what lets it compile on a PC and be tested without a board.
3. **A component ships with its own test**, runnable with a host compiler. If it cannot be tested
   off-target, it is not a component, it is application code in the wrong place.

## What would legitimately move here

| Candidate | Trigger |
|---|---|
| `bosch_proto` | a second application that genuinely reads frames — e.g. a USB diagnostic dongle |
| A shared config-portal UI | a second device that also needs Wi-Fi onboarding |
| Common telemetry serialisation | more than one publisher, so the format has to be agreed |

Note what is *not* on the list: the MQTT topic strings. Those are not code — they are
`spec/mqtt.json`, and both sides generate from it rather than sharing an implementation.
