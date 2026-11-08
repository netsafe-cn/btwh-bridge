# integrations/ — reserved: third-party ecosystem glue

Nothing here yet. The directory exists so that ecosystem code has a home that is neither the
library nor the firmware, and does not end up in `python/tools/` by default.

## What is expected to land here

| Directory | What it would do | Depends on |
|---|---|---|
| `homeassistant/` | HACS-installable integration, or a Lovelace card for the entities MQTT Discovery already creates | `spec/mqtt.json` |
| `node-red/` | Example flow: a dashboard, or an automation that warns on a fault code | `spec/mqtt.json` |
| `esphome/` | External component, for people who would rather not flash our binary | `spec/mqtt.json` |

## The rule

An integration consumes the contract and never links the implementation. Concretely it may not
import `bosch_api`, and may not talk BLE to the heater. If an integration needs a value the
contract does not expose, the fix is to extend `spec/mqtt.json` — not to reach around it.

That constraint is what keeps the bridge replaceable: an ESPHome component, a rewritten bridge
or a future ESP-IDF port can all sit under the same integrations without touching them.
