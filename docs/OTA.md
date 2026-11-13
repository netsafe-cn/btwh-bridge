# OTA, releases and flash layout

Status: **design only** (2026-11-12). Nothing here is implemented; no firmware source was
changed for this document. Every section states what is measured, what is inferred, and what
still needs the board.

Related: ADR-011 (the OTA transport), ADR-013 (this document's decision), `spec/mqtt.json`
(`ota`, `update/latest`, `update/url`, `update/state`), `docs/MQTT.md` §4.

---

## 1. The constraints, in order of hardness

| # | Fact | Source | Confidence |
|---|---|---|---|
| 1 | The module has **16 MB flash**, despite the N8R8 part number | `esptool flash_id`: manufacturer `0x46`, device `0x4018` | **measured** |
| 2 | `platformio.ini` still says `flash_size = 8MB` | `firmware/bridge/platformio.ini:28` | fact, and it contradicts #1 |
| 3 | `firmware/bridge/README.md:221` says the target FQBN is `FlashSize=16M,PartitionScheme=default_8MB` | README table | fact, and it contradicts #2 |
| 4 | The flasher has **no TLS stack**; `parseOtaUrl` refuses `https://` | `net/mqtt_rules.cpp`, ADR-011 | fact |
| 5 | The release build must carry **plain semver** | `spec/mqtt.json` note; `CHANGELOG` | convention, **not enforced anywhere** |
| 6 | The app partition is **34.7 % full**, not 88 % | measured on the board, see below | **measured (2026-11-12)** |
| 7 | **PSRAM is 8 MB and works** once enabled | boot banner before/after adding `PSRAM=opi`: `0 bytes` → `8386279 bytes` | **measured (2026-11-12)** |

### Measured layout (2026-11-12, board `28:84:85:59:70:18`)

Read straight off the board, not inferred from a build:

```text
nvs        data nvs      0x009000  0x005000
otadata    data ota      0x00E000  0x002000
app0       app  ota_0    0x010000  0x330000   (3.19 MB)
app1       app  ota_1    0x340000  0x330000   (3.19 MB)
spiffs     data spiffs   0x670000  0x180000   (1.50 MB)
coredump   data coredump 0x7F0000  0x010000
```

`otadata` slot 1 carries `seq=2, state=VALID`, so **app1 @ 0x340000 is the running slot**. Its
image is **1,160,769 bytes (1.107 MB)** followed by 0xFF, i.e. **34.7 %** of the 3.19 MB slot,
with **2,130 KiB free**.

That settles fact 6: 88 % is what 1.107 MB becomes in the 1.31 MB app slot of the *default*
scheme — 88.6 %, to be exact. So the "88 % ceiling" was never the current state; it is the
number a build reports when `FlashSize` is passed without a matching `PartitionScheme`. The
board has been running `default_8MB` all along, and has over 2 MB of headroom.

Two consequences, and they point in opposite directions:

- **The capacity emergency is not real.** Nothing is about to fail to fit, so repartitioning
  is no longer urgent and must not be sold as one.
- **8 MB of the 16 MB is still unused**, and `PSRAM=0` is a configuration gap, not a hardware
  verdict — `platformio.ini` never enables it, and the `esp32-s3-devkitc-1` board definition
  defaults it off. Both are worth fixing on their own merits, not as rescue work.

The remaining puzzle is *why* some earlier build reported 88 %: a `pio run -t size` or an
FQBN without `PartitionScheme` would do it. It changes nothing below, because the layout is
now read from the board rather than from a build log.

---

## 2. Recommendation

Split this work. Do **not** ship TLS and the flash-layout change in one step.

| Phase | Content | Why this order |
|---|---|---|
| **1a** | Plain semver in release builds; manifest publish pipeline | These block every release regardless of transport, and neither needs TLS or a repartition. |
| **1b** | Correct flash size + explicit 16 MB partition table | **Optional, and no longer urgent** — the board is at 34.7 % of a 3.19 MB slot (§1). Worth doing to stop wasting 8 MB and to give TLS room, not to avert a capacity failure. Costs one USB reflash. |
| **2** | `https://` image URLs, with a SHA-256 in the manifest | Only needed to pull directly from a public host. On a LAN, an http mirror is one command. |

Phase 2 is designed below in full, because the contract change it needs (`update/sha256`)
should be decided before the first release publishes a manifest that lacks it.

---

## 3. Why https is not automatically the right answer here

The trust boundary today is the broker plus the LAN (ADR-011). `http://` on that LAN is
consistent with it. https adds a certificate chain, and this board has three problems with
that chain:

| Problem | Detail |
|---|---|
| **No trustworthy clock** | The S3 has no RTC battery and the firmware never runs SNTP. Certificate `notBefore`/`notAfter` validation fails on a freshly booted board. Any "verify the chain" design first has to solve time. |
| **CDN certificates rotate** | A pinned root or fingerprint for `objects.githubusercontent.com` is a maintenance item that breaks releases silently. |
| **Heap** | mbedTLS handshake plus the secure client costs roughly 35–50 KB of heap while `install()` is blocking `loop()`. The 8 MB PSRAM is **not enabled** (`ESP.getPsramSize()` reports 0), so this comes out of internal SRAM. |

Options, with the honest trade-off:

| Option | Encrypted | Authenticates the image | New moving parts | Verdict |
|---|---|---|---|---|
| **A. Keep http, publish an http mirror** | no | implicitly, by LAN access | an operator step per release | Simplest. Acceptable while the image host is a machine you own. |
| **B. https + `setInsecure()` + SHA-256 from the manifest** | yes | **yes**, via the hash | one extra retained topic, hash in the flasher | **Recommended for phase 2.** Encryption without a clock; the hash closes the substitution hole that skipping validation opens. |
| C. https + pinned root CA + SNTP | yes | yes | time sync at boot, CA rotation | Strongest on paper, worst to operate. Not worth it for one household. |
| D. Self-hosted https with a pinned self-signed cert | yes | yes | a server to keep alive | Only if the image host must be public and untrusted. |

Option B is the one that fits: the manifest already travels over the authenticated broker, so
the hash inherits the broker's trust. An attacker who can rewrite the image must also rewrite
the retained `update/sha256`, which means the broker is already compromised.

Note the interaction with ADR-011's rule that the *action* must be live-only: the hash is
**state**, so it is retained exactly like `update/latest` and `update/url`. Do not put it on
the `ota` command topic.

---

## 4. Contract change

One additive topic. Nothing existing changes shape, so a bridge that predates it keeps working.

| Topic | Retain | Payload | Direction | Notes |
|---|---|---|---|---|
| `btwh/{id}/update/sha256` | **yes** | 64 lowercase hex chars, no prefix | consumer → bridge | Optional. Absent means "no hash", and the flasher behaves as it does today. |

Rules that belong in `spec/mqtt.json` and in `test_mqtt_contract.py`:

- `update/sha256` is retained; `ota` stays live-only (already pinned).
- A manifest is only complete when `latest` **and** `url` are present. `sha256` is optional;
  if present but malformed, the flasher must refuse the install rather than ignore it —
  silently dropping a security field is the failure mode to design out.
- `update/state` does not carry the hash: Home Assistant has no field for it, and the state
  topic exists to drive an entity, not to mirror the manifest.

---

## 5. Version rules

`parseManifestVersion` today validates only "printable ASCII, non-empty, fits the buffer". It
deliberately accepts `2.8.1-rc1` because the bench uses suffixes. Keep that leniency on the
board; enforce strictness where releases are made.

| Context | Rule | Enforced by |
|---|---|---|
| Release tag | `vMAJOR.MINOR.PATCH`, nothing else | CI regex on the tag |
| `kFirmwareVersion` in a release build | plain `MAJOR.MINOR.PATCH` | CI check that the literal in `mqtt_client.h` equals the tag |
| Bench / development build | `MAJOR.MINOR.PATCH-dev.N` or `-rc.N`, never published | convention, plus the CI check above refusing a tag whose literal carries a suffix |
| Manifest published to the broker | plain semver, matching the tag | pipeline step that builds the manifest |

The one thing to verify before MINOR reaches 10: Home Assistant decides "update available" by
comparing `installed_version` and `latest_version`. `spec/mqtt.json` claims HA compares them as
**strings**, which would make `2.10.0` look older than `2.9.0`. HA's MQTT update entity
actually parses versions with its own version type, so the claim is probably stale — but it is
unverified here, and a stale sentence in a contract file is worse than no sentence. Do one
bench test with `installed=2.9.0, latest=2.10.0` before that release, then correct the note.

---

## 6. Release pipeline

The goal is one command from a tag to a bridge that shows "update available", with no hand
editing of versions.

```
tag v2.9.0
  └─ CI: build → assert version literal == tag → sha256 firmware.bin
      └─ GitHub release: firmware.bin + manifest.json
          └─ operator (or HA automation): publish the release manifest to the broker
              └─ bridge: update/state → HA update entity "on"
                  └─ Install → live 'install' on ota → flash → reboot → 2.9.0
```

`manifest.json` is a release artefact, not a new contract:

```json
{
  "version": "2.9.0",
  "url": "https://github.com/<owner>/<repo>/releases/download/v2.9.0/firmware.bin",
  "sha256": "…64 hex…",
  "size": 1216512
}
```

The last hop stays manual on purpose. CI holds no credential for the household broker, and
the broker is not reachable from a public runner. One line does it:

```bash
python python/tools/mqtt_watch.py --manifest 2.9.0,<url>,<sha256> --device <id>
```

This extends `--manifest` from `version,url` to `version,url[,sha256]`; the two-argument form
must keep working so existing scripts and `docs/MQTT.md` stay true.

---

## 7. Flash layout

16 MB, explicit CSV, committed to the repo. Sizes are chosen so that no future feature makes
us redo this.

```csv
# name,    type, subType,  offset,    size,      flags
nvs,       data, nvs,      0x9000,    0x5000,
otadata,   data, ota,      0xe000,    0x2000,
app0,      app,  ota_0,    0x10000,   0x400000,
app1,      app,  ota_1,    0x410000,  0x400000,
spiffs,    data, spiffs,   0x810000,  0x200000,
coredump,  data, coredump, 0xA10000,  0x10000,
```

| Region | Size | Note |
|---|---|---|
| app0 / app1 | 4 MB each | image is 1.107 MB → **27.7 %**. Room for TLS, a second protocol profile, and growth, twice over. |
| spiffs | 2 MB | unused today (`spiffs` at 1.5 MB is already on the board and empty); kept so a future panel or config store does not need another repartition |
| unallocated | ~5.9 MB | deliberately left free |

`platformio.ini` becomes:

```ini
board_build.flash_size = 16MB
board_build.partitions = partitions_16mb.csv
```

**The one-way door**: changing the partition table cannot be delivered by OTA. The running
image's `otadata` and app offsets no longer describe the new layout, so the first board must
be reflashed over USB once (`esptool erase_flash` then a normal upload). Plan it for a bench
session, not for a remote user. Every OTA after that is unaffected.

That door is the whole cost of §7, and §1 moved the benefit: this is now an optimisation for
a board at 34.7 %, not a fix for a full one. Do it when the board is already on the bench for
something else; do not spend a dedicated session on it before v1 freezes.

**PSRAM is no longer a phase-1 question.** It was measured on 2026-11-12: the module really
does carry the 8 MB of an "R8" part, and `PSRAM=opi` brings the banner from `PSRAM=0 bytes`
to `8386279 bytes`. The earlier `0` was the missing build flag, not missing silicon, so
`gateway_cli.py` defaults to it. Enabling it did change the boot path (`qio_opi`), which is why
it was flashed as its own run and verified in the banner before anything else depended on it.

It is a default, not a requirement. Nothing under `src/` allocates from PSRAM — the only
reference is the boot line that prints its size — so `--psram none` compiles for a board sold
without an R part. That board would otherwise rely on `CONFIG_SPIRAM_IGNORE_NOTFOUND`, which
the prebuilt `dio_opi` SDK does set (`sdkconfig.h:315`), so it boots with a warning instead of
a loop; the switch just removes the warning and the guesswork.

One related trap, worth recording because it bit this board: a bare `FlashSize=8M` selects the
4 MB `default` scheme, **not** `default_8MB`. That produces the 1.31 MB app slot behind the old
"88 %" reading, and — worse — an `arduino-cli upload` that writes app0 while `otadata` still
boots app1, so the flash succeeds and the running firmware never changes. Always name
`PartitionScheme`.

---

## 8. What phase 2 changes in the flasher

Design only; no code was written.

| File | Change | Risk |
|---|---|---|
| `net/mqtt_rules.cpp` | `parseOtaUrl` accepts `https://`, default port 443, returns the scheme | one parser, one test suite; keep the http path byte-identical |
| `net/ota.cpp` | `install(host, port, path, tls, expectedSha)`; `WiFiClientSecure` + `setInsecure()` when `tls`; streaming SHA-256 over the same chunks | heap during handshake; budget may need 120 s → 180 s |
| `net/mqtt_client.cpp` | subscribe `update/sha256`, cache it, refuse `install` when it is malformed | malformed hash must fail closed |
| `spec/mqtt.json` | the new topic above | additive, contract_version stays 1 |
| `test_mqtt_contract.py` | pin retained-ness and the "malformed hash fails closed" rule | — |

Hash verification happens **before** `Update.end(true)`: a chunk that does not match is a
failed install, and `Update.abort()` leaves the running firmware in charge — the same
guarantee ADR-011 already makes for a short image.

---

## 9. Acceptance criteria

Phase 1 is done when all of these hold:

1. `pio run -t size` reports the app slot as 4 MB and usage under 50 %.
2. `esptool.py read_flash 0x8000 0x1000 part.bin` shows two 4 MB app slots.
3. A build from tag `v2.9.0` carries `kFirmwareVersion == "2.9.0"`; CI fails if it does not.
4. The release publishes `firmware.bin` + `manifest.json`, and the recorded `sha256` matches
   the uploaded asset.
5. `mqtt_watch.py --manifest 2.9.0,<url>` still works (two-argument form), and the HA update
   entity turns **on**.
6. Install from the HA entity flashes the release image and the banner reports `2.9.0`.

Phase 2 adds:

7. An `https://` URL is accepted and flashed; an `http://` URL still works.
8. A deliberately corrupted image is refused by the hash check, and the board keeps running
   the previous version.
9. Peak heap during a TLS install is recorded and leaves headroom above the watchdog margin.

---

## 10. Open questions

| Question | Blocks | How to close |
|---|---|---|
| ~~Is the current build really at 88 % in a 1.31 MB slot?~~ **Answered 2026-11-12: no — 34.7 % of 3.19 MB.** | — | closed by reading the partition table and the app image off the board (§1) |
| ~~Does this module really carry PSRAM?~~ **Answered 2026-11-12: yes, 8 MB Octal.** | — | closed: `PSRAM=opi` → banner `8386279 bytes` |
| Does HA compare versions as strings (the spec's claim) or parse them? | the 2.10.0 release | one bench test; then fix the note either way |
| Is a public image host needed at all, or is a LAN mirror enough? | phase 2 go/no-go | answer depends on whether releases are shared with anyone outside the household |
