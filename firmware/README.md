# firmware/ — one directory per application

Every application here is its own build. They do not import each other's sources; the only
thing they share is the contract in [`spec/mqtt.json`](../spec/mqtt.json) and, where it makes
sense, a reusable component.

```
firmware/
├── components/          reusable C, no application logic, no framework dependency
│   └── bosch_proto/     frame codec + A0 parsing, unit-testable on a PC
├── bridge/              ESP32-S3, no display: heater BLE <-> MQTT. Sits next to the heater.
└── panel/               ESP32-S3 + touchscreen, any floor: MQTT only, never touches BLE.
```

## The rule that keeps them apart

```
heater --BLE--> bridge --MQTT--> panel
                          └---> Home Assistant / Mi Home / Node-RED
```

A panel that talked to the heater directly would need the BLE stack, the pairing flow and a
copy of the protocol — and it would be a second device fighting for the one BLE connection the
heater allows. So:

| | bridge | panel |
|---|---|---|
| BLE to the heater | yes | **never** |
| Pairing code entry | yes | no |
| `components/bosch_proto` | yes | not linked |
| `spec/mqtt.json` | publishes against it | renders from it |
| Placement | next to the heater | any floor, wired to 5 V |

This is what makes a second and third panel cheap: they are all equal consumers. A panel is
not a fork of the bridge, it is an application on top of it.

## Adding a third application

Create `firmware/<name>/` with its own `platformio.ini` and `README.md`, then state in that
README which contract it consumes. If it needs code from `components/`, say which component
and why — a component may not depend on any application.

## Build

Each application owns its build. Nothing at the repository root builds firmware.

```bash
python firmware/bridge/tools/gateway_cli.py --build     # bridge
# a panel build command lands here when the application exists
```

The ESP32 toolchain (`arduino-cli`, core 2.0.17, NimBLE-Arduino 1.4.3) is a local,
uncommitted dependency. See [`bridge/README.md`](bridge/README.md) for versions and the
short-path workaround Windows needs.
