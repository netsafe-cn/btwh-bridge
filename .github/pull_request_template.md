## What changed

<!-- One sentence. -->

## Why

<!-- The scenario or defect that triggered this. -->

## How it was verified

<!-- Command output, log excerpt, or screenshot. -->

## Checklist

- [ ] `python run_tests.py` is green (136 tests)
- [ ] `CHANGELOG.md` updated under `[Unreleased]` (skip only for tests/CI/comment-only)
- [ ] Version bump stated in the description: `patch` / `minor` / `major`, or `none`
- [ ] Public API changed → `API.md` updated, then `python tests/test_contract.py --update` run
- [ ] Protocol layer changed → `protocol_vectors.json` updated, and the ESP32 side passes
      with the same vectors
- [ ] No real MAC addresses, pairing codes, cloud keys, or vendor artefacts included
- [ ] New hardware probe is named `probe_*`, not `test_*`
