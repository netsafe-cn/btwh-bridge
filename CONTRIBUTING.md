# Contributing

Let's be clear about the boundary up front: this project is an **interoperability
implementation for hardware you own**, not a cracking tool. Before you submit,
make sure your change respects the red lines below.

---

## 1. Compliance red lines (hard, no exceptions)

**Not accepted**, in any form:

- Vendor APKs, firmware, OTA packages, or decompiled output
  (`.apk` / `.dex` / `smali` / decompiled source fragments)
- Keys, certificates, or cloud credentials extracted from a vendor app
- Code that bypasses authorisation, skips pairing, or spoofs device identity
- Real device MAC addresses, pairing codes, cloud keys, or personal logs

**Accepted**: a **restatement** of protocol format and field semantics — for example
"offset 13 is burner power percentage" — as long as no vendor artefact is attached.
Such findings belong in `BLE_PROTOCOL.md` and should cite measurement wherever possible.

> The test is simple: could someone use this to attack **someone else's** device?
> If yes, don't submit it.

---

## 2. Development setup

```bash
python -m venv .venv && . .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt
python run_tests.py                              # should be all green
```

For the ESP32 side see [`esp32/README.md`](esp32/README.md). The toolchain installs
itself into a short-path workspace — Windows `CreateProcess` has a hard path-length
limit that cannot be worked around.

---

## 3. Testing discipline (hard requirement)

`python run_tests.py` must be green before you push. CI runs Linux + Windows × Python 3.11/3.12.

| Your change | You must also |
|---|---|
| Add or modify a public API | Update `API.md`, then run `python test_contract.py --update` to refresh the signature snapshot |
| Change protocol encoding/decoding | Update `protocol_vectors.json` and make the ESP32-side vector tests pass with the same data |
| Change the state machine | Update `device_fsm.py` and `API.md §9` first, then the C++ implementation |
| Fix a bug | Add a test that reproduces it (red first, then green) |

### Naming conventions (CI depends on them)

- **Unit tests**: `test_*.py`, auto-discovered by `run_tests.py`
- **Hardware probes**: `probe_*.py`, talk to a real device, excluded from regression and
  already covered by `.gitignore`

Naming a probe with a `test_` prefix makes CI try to open a Bluetooth connection and hang.
This project has actually hit that.

### Tests must be offline

Unit tests must not open real Bluetooth connections, touch the network, or read/write
files outside the repository. Model BLE interaction with the `FakeClient` pattern in
`test_session.py`, and inject time via the `now` parameter (see `device_fsm.py`) rather
than sleeping inside tests.

---

## 4. Changing the protocol (four steps, none skippable)

```
1. Change the Python reference implementation + tests   ← this is the source of truth
2. python test_contract.py --update                     ← regenerate golden vectors
3. Change the ESP32 C++ implementation, run the same vectors
4. Both sides green → open a PR
```

The order is not negotiable. Changing C++ before Python means the two implementations
guess at each other, and afterwards nobody knows which one is right.

---

## 5. Commit conventions

Use Conventional Commits:

```
feat(esp32): accept pairing code at runtime
fix(api): raise ValueError for non-string pairing codes
test(contract): add A0 Type1 golden vector
docs: explain why provisioning and pairing are orthogonal
refactor(fsm): extract wifi_retry
```

A PR description should answer three things:

1. **What changed** (one sentence)
2. **Why** (the scenario or defect that triggered it)
3. **How you verified it** (command, log excerpt, or screenshot)

UI changes should include a screenshot. ESP32 changes should include a serial log —
`python esp32/gateway/log_diag.py` produces a structured diagnosis you can paste.

---

## 6. Versioning and releases

Three things carry a version, and they are bumped for different reasons:

| Artefact | Where the number lives | Scheme |
|---|---|---|
| Python package | `pyproject.toml` → `[project] version` | SemVer |
| Gateway firmware | `FW_VERSION`, printed in the boot banner | SemVer, `MAJOR.MINOR` |
| API surface | `api_contract.json`, checked by CI | frozen by contract, not by a number |

SemVer here is judged from the **published API contract**, not from how many lines moved:

| Bump | Trigger (any one) | Example |
|---|---|---|
| **MAJOR** | Removing or renaming a public symbol; adding a required parameter; changing a default value; changing wire encoding | renaming a `PAIR_FAIL_*` constant |
| **MINOR** | New public function or constant with a default; new optional trailing parameter; extra parsed field; new firmware command | adding `pair_summary()` |
| **PATCH** | Bug fix that keeps signature and wire format; docs; internal refactor | raising `ValueError` instead of `AttributeError` |

Rows 1 and 2 are enforced mechanically: `python test_contract.py` fails if you break the
contract without refreshing the snapshot. So "did I break the API?" is answered by the
build, not by opinion — but **deciding the bump number is still your call**, and it goes
in the PR description.

### CHANGELOG rules

[`CHANGELOG.md`](CHANGELOG.md) follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Any PR that changes behaviour updates it **in the same commit**:

1. Add the entry under `## [Unreleased]`, in the right subsection
   (`Added` / `Changed` / `Deprecated` / `Removed` / `Fixed` / `Security`)
2. Write it for a **user**, not a compiler: "pairing codes stay valid after the window
   closes (verified after 11 minutes)" beats "adjust auth state"
3. Reference the issue when there is one — `(#42)`
4. Tests-only, CI-only and comment-only changes need no entry

### Cutting a release

```bash
# 1. CHANGELOG: move [Unreleased] entries into "## [x.y.z] - YYYY-MM-DD",
#    then open a fresh empty [Unreleased] above it
# 2. CHANGELOG: add the new comparison link at the bottom
# 3. pyproject.toml: bump [project] version
python run_tests.py                    # must be green before tagging
git commit -am "chore(release): x.y.z"
git tag -a vx.y.z -m "vx.y.z"
git push origin main --tags
```

Pushing the tag makes GitHub draft the release notes from
[`.github/release.yml`](.github/release.yml) and the merged PR labels. **Edit the draft
before publishing** — the generated list is a starting point, and auto-generated notes
routinely miss the "why".

Firmware-only releases keep the same tag with a suffix, e.g. `v0.2.0-fw2.1`.

---

## 7. Code style

**Python**

- PEP 8, 100-column lines. Chinese comments are fine and common in this codebase
- Always log through `boschlog`; never use bare `print` (especially in library code)
- Public functions need docstrings that mention side effects and raised exceptions
- Don't add third-party dependencies unless there is no standard-library alternative.
  `unittest.mock` is sufficient — please don't reintroduce the standalone `mock` package

**C++ (ESP32)**

- ESP-IDF style, 4-space indent
- Protocol constants belong in the `bosch_proto` header, not scattered as magic numbers
- Every externally visible behaviour should produce one serial log line, so users can
  diagnose problems themselves

---

## 8. Reporting issues

Please include:

- Heater model and firmware version (visible on the panel or nameplate)
- Symptom and reproduction steps
- `logs/panel_*.log` or `logs/esp32_*.log` — **strip MAC addresses, pairing codes and
  cloud UIDs first**
- Output of `python esp32/gateway/log_diag.py`

Security issues (for example plaintext key leakage or unauthorised control) should **not**
be filed as public issues. Report them privately per [`SECURITY.md`](SECURITY.md).

---

## 9. License

By submitting, you agree that your contribution is released under the [MIT](LICENSE)
license, and you confirm you have the right to do so.
