# python/ — reference implementation and developer tools

The Python side exists for three reasons: it is the readable specification of the protocol, it
is how a fault is diagnosed without a soldering iron, and it is the source of the golden
vectors the firmware is tested against.

```
python/
├── bosch_api.py     ★ protocol codec, device session, resident service
├── device_fsm.py    ★ provisioning / pairing state machine (framework-agnostic)
├── bemfa.py         Bemfa cloud client (Mi Home channel)
├── boschlog.py      logging: rotating file + in-memory ring buffer
├── singlelock.py    single-instance guard for the background bridge
├── tests/           136 offline tests, no hardware, no network
└── tools/           runnable helpers
```

## Import names do not follow the directory

`import bosch_api` still works from anywhere after `pip install .`. `pyproject.toml` maps the
package root to `python/` while keeping the module names flat:

```toml
[tool.setuptools]
package-dir = { "" = "python" }
py-modules = ["bosch_api", "boschlog", "bemfa", "device_fsm", "singlelock"]
```

This is deliberate: moving files into a subdirectory is a layout change, and a layout change
must never break a downstream import or invalidate `spec/api_contract.json`.

## Running things

```bash
python run_tests.py                        # from the repository root, 136 tests
python python/tests/test_contract.py       # contract only
python python/tools/bosch_ui.py            # desktop console
python python/tools/bosch_read.py --json   # one-shot read, JSON out
```

The Windows launchers in `python/tools/` resolve their own location, so double-clicking still
works after the move.

## Tools

| File | Purpose |
|---|---|
| `bosch_ui.py` | Desktop console: four temperatures, curve, history, fault list |
| `bosch_bridge.py` | Headless bridge to the Bemfa cloud, no window |
| `bosch_read.py` | One-shot or looping read, human or JSON output |
| `collector.py` | Capture frames to CSV for protocol analysis |
| `analyze_collect.py` | Field ranges, ASCII curves, byte correlation from that CSV |

These are diagnostic aids, not the product. The product is the firmware; this directory is how
you find out why the firmware is unhappy.
