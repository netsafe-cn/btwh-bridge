# Changelog

All notable changes to this project are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this
project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **A getting-started guide for the person who owns the heater, not the repository
  (`docs/GETTING_STARTED.zh-CN.md`).** Every existing document assumes the reader can read a
  build log; the person who ends up holding the board cannot. The guide covers what to buy and
  what not to buy (ESP32-S3, 8 MB flash minimum, the traps: C3, WROOM-32, charge-only USB
  cables), two ways to flash one `btwh-bridge-full.bin` at `0x0` - the Espressif GUI tool and a
  single `esptool` command - then the phone-only provisioning flow (join `BTWH-Bridge-XXXX`,
  password `bridge123`, `192.168.4.1`, an English-form-to-Chinese table), and the pairing
  sequence that people get wrong: the six-digit code has to go into the *same* form submission
  as the Wi-Fi password, because saving tears the setup AP down. It also states the limitation
  plainly instead of hiding it: a board whose Wi-Fi changed has no way back in without a USB
  cable and the `portal` console command. The whole-image build (bootloader + partitions +
  boot_app0 + app merged, 1,236,944 B) was produced and its magic bytes checked at `0x0`,
  `0x8000` and `0x10000`; flashing it wipes stored configuration, which the guide says out loud.

- **A `problem` binary sensor over `fault`, so a fault is one boolean away from a trigger
  (ADR-017).** `binary_sensor.fault_active` is `true` whenever the code is not `none`. The string
  sensor stays beside it: the code is what a human troubleshooting needs, and a `sensor` cannot
  carry `device_class: problem`, which is the class Home Assistant highlights and automations
  trigger on without a template. Both read the state payload and bind state + link with
  `availability_mode: all`, because a stale "no fault" is worse than no answer. The template
  defaults a missing field to `none` rather than branching: `binary_sensor.mqtt` matches
  `payload_on`/`payload_off` only, and anything else is logged as `Invalid state message` on
  every frame - the warning ADR-016 chased. 21 -> 22 entities.

- **Home Assistant's birth message, so an HA restart costs no data (ADR-017).** Retained
  discovery rebuilds the entity list by itself; `state` and `attributes` are live-only (ADR-015)
  and do not, so a restarted Home Assistant showed blank diagnostics for up to a minute and no
  measurement until the next A0 frame. The bridge now subscribes to `homeassistant/status` - the
  topic comes from `spec.home_assistant.birth` into `contract::kHaStatusTopic`, not retyped in
  the .cpp - and on `online` republishes discovery, `availability`, `link`, `attributes` and
  `update/state`, and queues `state` for the next poll. `offline` (Home Assistant's will) is
  ignored: the bridge keeps publishing either way. It is the only subscription this client makes
  outside its own device tree.

- **`spec/mqtt.schema.json`, and a test that makes it bite (ADR-017).** The contract's *shape*
  had no validator: a topic that lost its `{device_id}` token, or a numeric field that lost its
  `unit`, was caught only if someone had written an assertion for that key. The draft 2020-12
  schema pins the machine-read parts - topic templates namespaced `btwh/{device_id}/…`, unit
  required for numeric fields, `values` required for enums, `retain` as a closed boolean block,
  the Home Assistant block's required members - and `python/tests/test_spec_schema.py` validates
  the contract on every run. Five of its six tests break a copy of the contract on purpose (no
  `{device_id}`, no unit, a misspelled `retain` key, several at once, and the schema's own
  validity), because a schema that only ever passes proves nothing. `jsonschema` joins
  requirements-dev.txt; without it the module skips instead of failing.

- **A `link` topic, so "the bridge is up" and "the reading is current" stop being one message
  (ADR-014).** `btwh/<device>/link` is retained, published on change and once per connect, and
  says `offline` after 90 s without an A0 frame (22 missed frames at the 4 s cadence) and until
  the first frame of a session. Home Assistant binds it beside `availability` with
  `availability_mode: all` for the measurements and the water-heater card, and leaves the
  diagnostics and the update card on `availability` alone - pairing state, RSSI and Install must
  stay readable while the heater is unreachable. Discovery payloads also carry an `origin`
  block now: recommended for single-component discovery, `name` only, no `sw_version`, because
  the version already travels in `attributes.fw` and a second copy is one more thing that drifts.

- **A release design, and the flash layout it depends on (ADR-013).** `docs/OTA.md` separates
  two problems that were tangled together: the build's flash size disagrees with the hardware
  (`esptool` reports 16 MB; `platformio.ini` still says 8 MB), and the flasher has no TLS. It
  proposes a committed 16 MB partition table with two 4 MB app slots (the current image would
  sit at 29 %, not the reported ~88 %), plain semver enforced at the tag rather than on the
  board, a `manifest.json` release artefact, and https as a second phase using `setInsecure()`
  plus a retained `update/sha256` — because the board has no clock and cannot check a
  certificate's validity window. Design only: no firmware source changed, and every unverified
  number is marked as such.

- **The README now says who wrote this.** Most of the code was written by an AI assistant under
  human direction, and both READMEs say so above the fold, with a section covering the split
  between human and AI work, the failure mode that arrangement creates (generated code reads as
  if it was thought through), the three bugs that were caught by running it rather than reading
  it, and what a reader should therefore do differently. The reason is not modesty: this firmware
  drives a gas appliance, and a reader who assumes months of human authorship is being misled
  about how much review stands behind the code. `AGENTS.md` now says the disclosure must survive
  future edits, and `CONTRIBUTING.md` asks AI-assisted patches to say so in the pull request.

- **An architecture diagram at the top of both READMEs.** Hand-drawn SVG goes stale the moment a
  layer or a topic changes, so the picture is generated: `docs/diagrams/gen_architecture.py`
  emits `architecture-{light,dark}.svg` from one description, and the READMEs pick the right one
  with `<picture>` + `prefers-color-scheme` so it stays readable in both GitHub themes. CI runs
  the generator with `--check`, so a hand-edit or a stale render fails the build instead of
  quietly describing a system that no longer exists.

- **The OTA pair is now pinned by contract tests (195 → 204 tests).** `test_mqtt_contract.py`
  fails if the `ota` topic ever gains `retain` (a retained URL reflashes on every reconnect -
  a broker-triggered boot loop) or if the two manifest topics ever lose it (a rebooted bridge
  must re-learn the manifest, not assume it is current). `test_ha_discovery.py` gains a
  `TestUpdateEntity` suite pinning the one-flasher rule - the entity's command topic must be
  the plain `ota` topic, never the manifest URL topic - plus a cross-table check that no two
  discovery entities share a `unique_id` (two colliding ids silently overwrite each other in
  Home Assistant; the device page shows one and no error anywhere).

- **Docs refreshed to the 2.8 reality.** The README status tables now name 21 entities and the
  OTA path; the suite list carries all 13 test modules; MQTT.md's topic table includes the four
  update topics and its known-limits section drops three claims the firmware has since
  overturned (broker auth, RSSI in attributes, portal broker field); ARCHITECTURE.md gains
  ADR-011 (OTA: one flasher, live-only actions, retained state), retires ADR-008's
  "no host compiler" caveat, and updates the roadmap; firmware/bridge/README.md's serial
  command table now lists all commands incl. `mqttuser` / `broker` / `portal` / `wifi`, and
  its status table reflects the 2026-11-11 bench record (25-min soak, 6 s AP-outage recovery).

- **The Home Assistant `update` entity: firmware updates from the HA UI.** A 21st discovery
  entity (component `update`, device_class firmware) gives the bridge an "update available"
  card with an Install button. The design keeps one flasher and one grammar: the retained
  manifest topics `btwh/<id>/update/latest` + `update/url` (version, image URL) are cached
  by the bridge on arrival (`rules::parseManifestVersion` trims the shell echo, refuses
  non-printable or oversized), the bridge publishes the entity's state as
  `{"installed_version":...,"latest_version":...}` on `btwh/<id>/update/state` (latest
  omitted until a manifest is seen, so HA shows 'unknown' rather than a fake up-to-date),
  and Install publishes the `payload_install` word "install" to the existing live-only
  `ota` topic, which flashes the cached URL. Manifest topics are retained on purpose -
  replaying state is idempotent, the exact opposite of the live-only ota topic - so a
  rebooted bridge re-learns the manifest from the broker on its first subscribe.
  `mqtt_watch.py --manifest LATEST,URL --device <id>` is the bench driver. Note: HA
  string-compares versions, so dev suffixes are bench-only; release builds must use plain
  semver. Non-goal still: https images (no TLS stack in the flasher path).
  Bench-verified end to end through the real HA path (2026-11-11): manifest published
  retained, the entity appeared as `update.<...>_water_heater_firmware` state **on**
  (installed 2.8.0-update, latest 2.8.1-ota3), the `update.install` service call - the
  exact action behind the UI button - flashed 1.16 MB in 4.5 s, the board rebooted into
  2.8.1-ota3, and the entity settled at **off** (installed = latest) after the retained
  manifest replayed on the post-flash reconnect.

- **OTA: the bridge can now be reflashed over its own MQTT link.** The new `btwh/<id>/ota`
  topic accepts a `http://host[:port]/path` firmware URL; `net/ota.cpp` downloads it with a
  120 s budget, streams it to the inactive slot in 2 KB chunks, validates (Update
  checksum) and only then reboots - any failure leaves the running firmware untouched.
  The URL grammar lives in `rules::parseOtaUrl` (host-tested, 19 new checks: https
  refused, missing path refused, port bounds, truncation-free buffers) and delivery is
  live-only like commands, because a retained URL would reflash on every reconnect.
  `mqtt_watch.py --ota <url> --device <id>` is the one-line bench driver. https remains a
  deliberate non-goal (the flasher carries no TLS stack); the Home Assistant `update`
  entity has since landed - see the entry above.
  Bench-verified end to end (2026-11-11): 2.7.0-ota flashed over USB, then 2.7.1-ota2
  shipped over the air from a plain `python -m http.server` on the bench PC - MQTT command
  to reboot into the new image in 24 s wall clock (1.16 MB image, 5.0 s download+flash,
  banner and published attributes both moved to 2.7.1-ota2).

### Changed

- **The last-disconnect entity stopped advertising "epoch" in its name (2.9.0-ota8).** Since the
  ADR-016 fix the entity renders ISO 8601 through `timestamp_custom` and carries
  `device_class: timestamp`, but the label still read `Last MQTT disconnect (epoch)` - true of the
  wire value, false of everything the device page shows. Renamed to `Last MQTT disconnect time`
  and the contract note now says plainly which of the two is epoch and which is ISO 8601. Label
  only: no topic, key, template or unique id moves, so the existing entity keeps its history.

- **Readings are no longer retained on the broker, only status is (ADR-015).** `state` and
  `attributes` were published with the retain flag, so pulling the bridge's power left
  `42.3 °C, flame on` on the broker for anything that subscribed - the same lie ADR-014 removed
  from Home Assistant, one layer further out. Both topics are now live-only; `availability`,
  `link`, `update/state` and the discovery documents stay retained, because "offline" is a true
  statement about a bridge that stopped and a restarted consumer needs it before any message
  arrives. Nothing is lost in freshness: `state` is republished on every A0 frame (~4 s) and
  `attributes` on every connect and every 60 s, so the retained copy was at most four seconds
  ahead - while costing one retained write per frame, roughly 900 an hour on the broker's disk.
  This is also what `sensor.mqtt` recommends: a retained value is replayed at startup and can
  bring an entity back "available with an expired state". `spec/mqtt.json` now states the policy
  once (`retain`, `retain_policy`) and three contract tests pin it. Clearing the retained value
  with an empty publish was rejected on the documentation's own terms: for a numeric sensor an
  empty payload "will be ignored and will not update the state", so the screen would not change
  and the `Invalid state message` warning would come back.

### Fixed

- **The host selftest total was stale in the one test that pins it.** `test_cpp_selftest.py`
  asserted 295 checks (discovery 79) while the discovery suite had grown to 170 - two extra
  per-entity checks had been added by ADR-014 and ADR-016 without updating the number, and the
  test skips when no host C++ compiler is present, which is why it never noticed. Measured with
  `ziglang` on 2026-11-12: proto 64 / net 16 / mqtt 41 / rules 76 / provision 19 / discovery 170
  = **386**. The assertion, `AGENTS.md` and `firmware/bridge/README.md` now carry the measured
  values; on a runner with g++ the assertion had been failing since the discovery suite grew.

- **Discovery audited against the platform schema, and five conformance defects fixed (ADR-016).**
  The largest was latent: `device_class: timestamp` was fed a raw epoch integer, which the sensor
  cannot parse - the first real disconnect would have logged `Invalid state message
  '1794460577'` and left the entity unknown (the `0` case had been fixed the day before, the
  non-zero one had not). The template now formats with `timestamp_custom`, and the else branch
  still says `None`. Second, `rssi: 0` and `last_disconnect_at: 0` were sentinels standing in for
  "unknown": a real RSSI is negative and 0 only means "never dropped", so both were numbers
  nobody measured. They now publish JSON `null` and are marked `nullable` in the contract, which
  Home Assistant renders as unknown and a non-HA consumer cannot mistake for a reading. Third,
  `device.sw_version` was empty, so the device page showed no firmware version at all; it is now
  generated by reading `kFirmwareVersion` out of the C++ header (with `hw_version: ESP32-S3`),
  and the four entity builders share one `device_block()` instead of spelling the map out
  separately. Fourth, the update entity carried `entity_category: config`, which files the
  Install button under the section of the device page nobody opens. Fifth, and the reason for
  the buffer bump below: the largest payload had grown to 1082 B and its packet needs 1143 B in
  a 1152-byte buffer - nine bytes of margin before `publishDiscovery` would silently skip the
  entity, exactly the failure mode of 2026-11-12.

### Changed

- **The MQTT transaction buffer and the discovery payload buffer grow from 1152 to 1280 bytes,
  and the guard is now a packet-level test instead of a comment.** Self test 7 builds the real
  PUBLISH (topic + fixed header + payload) rather than only expanding the payload, and a new
  Python test re-derives the same sum in CI against `kTxCap`, asserts headroom, and fails before
  an entity can outgrow the wire. `publishDiscovery` also logs `x discovery: only N of M` when a
  payload does not fit, instead of reporting the short count as a plain success line.

- **A bridge that was up but deaf reported two-day-old readings as live.** Found on the bench
  2026-11-12: Home Assistant showed 42.3 °C outlet, 11.5 L/min and a burning flame that had been
  true on 11-10, with `rssi: 0` and `pair_state: "reconnecting"` printed beside them. Two causes.
  `gHaveSnap` was set once and never cleared, so the 60 s heartbeat republished the cached
  snapshot indefinitely. And because `app::start()` resets `a0Count` on every reconnect, the
  loop's `frames != gLastA0` test read a session restart as a new frame and rebuilt a snapshot -
  with a fresh `ts` - out of the previous session's last A0, which is worse than being stale
  because it looked current. The loop now recognises a frame by the parser's `lastA0Ms` stamp,
  drops the cached snapshot as soon as the link goes stale, and publishes `link` so the state is
  described rather than inferred. Discovery buffers moved 1024 -> 1152 bytes for the larger
  payloads; the old size would have made the board skip the water-heater entity silently.

- **Wi-Fi join is retried, not only attempted.** `stationConnect` ran once at boot; a board
  that boots before its AP (router recovering from a power cut, laptop hotspot started by
  hand) stayed offline forever - reproduced on the bench 2026-11-11, when the bridge came up
  before the hotspot and needed a manual `reboot` to ever join. `loop()` now retries the
  join every 30 s while credentials exist and the station is down (portal excluded);
  Arduino's auto-reconnect already covered the associated-then-dropped case.

- **The golden CONNECT: bench-verified bytes, pinned.** The exact 82-byte CONNECT packet the
  firmware builds (client id, retained will, credentials, 60 s keepalive) is now a byte-for-byte
  check in the mqtt suite, calibrated from the live bench session and re-submitted to the real
  Mosquitto for confirmation (CONNACK rc=5 on test credentials - the packet parses, the client
  id is valid, only the password is not). Any future edit to the flag order, length prefixes
  or field order fails at the bytes, not as a silent refusal at some user's broker.

- **`rules::nextBackoffMs` - the reconnect schedule as a function.** The doubling-with-a-
  minute-cap expression existed twice (drop path and TCP-unreachable path) as identical
  inline copies; it is one host-tested function now, with the sequence pinned including its
  one-step overshoot (32000 -> 64000 before settling at 60000 - the cap applies to the
  output). During the 2.5.1 bench session that schedule looked broken; with the sequence
  pinned, "flapping at a constant 4 s" can only mean the link is being killed, not that the
  doubling failed.

- **`net::prov` self test: the provisioning validation, 19 checks.** `validateRequest` gates
  the pairing flow, and a malformed attempt still counts toward the heater's three-failure
  lockout - yet the MAC grammar, the six-digit code rule and the `pair_state` words that
  mirror the Home Assistant attribute had never executed in a test. They now run on the host
  and on the board: five state words, the baseline request, dash/uppercase MAC forms, short/
  non-hex/trailing-junk/empty addresses, the empty and maximum SSID, and the pairing code
  boundary cases (empty keeps the pairing; five, seven, lettered and spaced codes are
  refused before anything is transmitted).

- **`net::mqtt_rules` - the inbound contract parsers, out of the socket code and into a
  host-testable layer.** Broker addresses, setpoint text, command verbs, the live-only
  command rule and the elapsed-time arithmetic now live in one pure-C file with a 48-check
  self test. The client calls these functions; the host self test pins them without a broker
  in the room. First contact with the tests found three parser edges that had never been
  exercised (see Fixed).

### Fixed

- **Three `parseBroker` edges, caught by the new rules self test on its first run.**
  `broker host:` (a colon announcing a port that never arrives) was stored as a literal
  "host:" address instead of being rejected; a bare IPv6 literal like `fe80::1` was torn
  apart by the last-colon split into host "fe80:" port 1, which would have connected to the
  wrong port; and a setpoint payload with leading whitespace was silently accepted by
  `strtod` where the contract says the text is a plain number. The demo bridge clamped
  out-of-range setpoints where the firmware refuses them - it rehearses the firmware now.

- **The MQTT link dropped itself 0.1 s after connecting.** The 90-second watchdog compared
  `now - gLastRx`, with `now` sampled at the top of the poll - but `drainSocket()` can stamp
  `gLastRx` with a *newer* `millis()` inside the same call (a SUBACK landing right after
  CONNACK), so the unsigned subtraction underflowed to ~4e9 and killed the healthy link.
  Symptoms: connect, subscribe, "no traffic in 90 s", retry, forever - once the client id
  bug above was fixed and a real broker started accepting the connection. `now` is
  re-sampled after the drain; the watchdog now measures what it claims.

- **The MQTT client id carried stack garbage, and every broker refused the connection.**
  `deviceIdFromAddress` collected the hex digits of the BLE address into a buffer it never
  terminated, then `snprintf`'s `%s` read past it into whatever the stack held - on the board
  that was `\x10`, a control character MQTT forbids in client ids and topics, so Mosquitto
  dropped every CONNECT as "client identifier not valid" while the firmware reported "no
  CONNACK in 4 s". The host self test never saw it: its stack happened to be zero. Found by
  pointing the board at a fake broker on the bench PC and hexdumping the first packet; fixed
  by terminating the buffer, and the function moved into the wire layer with 7 new self-test
  checks pinning the exact invariant (charset `[a-z0-9_]`, fallback, truncation safety).
  This was the first real-broker contact for the client - the WiFi/MQTT path had never been
  exercised before this session.

### Added

- **The model profile registry (`spec/profiles.json`): one row per heater, generated everywhere.**
  Setpoint range, write-parameter encoding and identity now have a single source. `gen_profiles.py`
  emits `src/proto/profiles_gen.h` (the table the app layer validates and builds D3 writes from),
  `gen_mqtt.py` cross-checks every range copy in `spec/mqtt.json` and emits the net layer's
  `kSetpointMin/Max`, and `test_profiles.py` pins `bosch_api.py` and `spec/api_contract.json` to
  the same row. Supporting a new heater becomes a spec row plus its A0 golden vectors - not a
  firmware edit. The 35..60 hardcode that lived in the net and app layers independently is gone.
- **A wrong golden vector, found by the new profile check.** `d3_set_temperature_42` carried three
  data bytes (`02 55 2A`, no index) while the Python reference, the firmware, the protocol notes
  and the measured hardware frame all use four (`02 55 00 2A`). Nothing ever compared the write
  bytes against the vector, so it drifted. The proto self test now rebuilds the golden D3 frame
  from every profile row, and checks the row's max against the `max_setpoint` the heater itself
  reports in the A0 frame.
- **The water_heater domain entity: the heater stops being a pile of sensors.** One entity,
  every Home Assistant convention included: dedicated card, voice control semantics, energy
  dashboard, and the Matter/HomeKit bridging Home Assistant already provides. Current
  temperature is the outlet, target is the setpoint bound to the existing `set/temperature`
  topic (not optimistic - a refused setpoint must not look applied), and mode is read-only
  `gas` (burner firing) / `off` (idle); a tankless heater has no switchable mode. All topics
  are v1 contract topics - an adapter view, not a contract change.
- **Contract versioning.** `spec/mqtt.json` declares `contract_version: 1` with the rules:
  adding topics/fields is minor, renaming/moving/reinterpreting is major and requires
  parallel publishing for one major cycle. v1 freezes with the first public release.

### Changed

- The MQTT publish buffers grew (`kTxCap` 768 -> 1152): the water_heater discovery payload is
  ~940 bytes. The discovery selftest now verifies every generated payload still fits the
  publish path, so a table that outgrows the buffers fails on the board, not as truncated
  JSON at a broker.
- `host_selftest.py` tracks headers as well as sources: a regenerated `vectors_gen.h` now
  rebuilds the host exe instead of silently testing the stale one (found when the fixed
  golden vector kept "failing").
- The demo bridge's setpoint clamp reads `spec/profiles.json` instead of its own 35/60, so
  the demo cannot disagree with the real bridge.
- Firmware version 2.5.0-profiles. On-board selftest 181 -> 185 checks (proto 60 -> 64); the
  offline suite 186 -> 195 (new `test_profiles`).
- Firmware version 2.5.1-device-id (bug fix on top). On-board selftest 185 -> 192 checks
  (mqtt 32 -> 39, the device-id invariant); the offline suite stays at 195.
- Firmware version 2.6.0-rules. On-board selftest 192 -> 235 checks (new `rules` suite, 43);
  the offline suite stays at 195. All suites run on the host as well.
- Firmware version 2.6.1-golden. On-board selftest 235 -> 261: the golden CONNECT (mqtt 39 ->
  41), the backoff schedule and five more parser checks (rules 43 -> 48), and the new
  `provision` suite (19). Six suites: proto 64 / net 16 / mqtt 41 / rules 48 / provision 19 /
  discovery 73; the offline suite stays at 195.
- Firmware version 2.7.1-ota2 (the 2.7.0-ota feature, shipped over the air as its own
  proof). On-board selftest 261 -> 280 (rules 48 -> 67, the OTA URL grammar). Six suites:
  proto 64 / net 16 / mqtt 41 / rules 67 / provision 19 / discovery 73; the offline suite
  stays at 195. Bench note: the 2.6.1 binary previously on the board was a mixed build
  (banner 2.6.1, attributes 2.6.0 - arduino-cli did not rebuild mqtt_client.cpp.o after
  its header changed); `gateway_cli.py --clean` exists because of it, and this version
  was built clean, then updated over the air.

- Firmware version 2.8.0-update. On-board selftest 280 -> 295: the manifest version
  grammar (rules 67 -> 76) and the update-entity bindings (discovery 73 -> 79: installs
  through the ota topic, says the install word, reads the bridge-mediated state). Six
  suites: proto 64 / net 16 / mqtt 41 / rules 76 / provision 19 / discovery 79. The same
  bench day also measured the broker-unreachable backoff live (Mosquitto addon stopped
  via the HA websocket API): retry in 32 s -> 64 s -> 60 s -> 60 s, matching the pinned
  `nextBackoffMs` sequence to the second, and full recovery on the first retry after the
  broker returned.

### Added

- **Diagnostic entities: the remote-troubleshooting surface.** Every attributes field is now a
  Home Assistant entity filed under `entity_category: diagnostic` (19 entities total: 11
  measurements + 8 diagnostics). A user on another continent with no serial access reads the
  firmware version, BLE RSSI, pairing state and the last disconnect straight from the device
  page. This is the third piece of the internationalisation trio (license, story, and this).
- **`rssi` is actually published now.** It was contracted in spec/mqtt.json's attributes_fields
  all along, but `publishAttributes()` never wrote it - a spec field with no firmware writer is
  a diagnostic that lies. Sampled live from the controller's connection table every 5 s.
- **`last_disconnect` / `last_disconnect_at`**: the reason and epoch time of the last broker-link
  drop, stamped in `drop()` and in the TCP-connect failure path. Retained through the reconnect,
  so a flapping link is visible while it happens to be up; `0`/never reads as `unknown` in HA
  rather than 1970-01-01.
- **Attributes heartbeat.** The attributes topic is now republished every 60 s, not only on
  connect: without it, uptime and RSSI freeze and a stuck bridge looks healthy for as long as
  the socket lives.

### Fixed

- **flame entity never turned "on" in Home Assistant.** The state payload carries a JSON
  boolean; HA parses it into a Python bool whose template rendering is `True` (capital T),
  which never string-matches `payload_on: "true"`. The generated `value_template` now renders
  the literals itself (`{{ 'true' if value_json.flame else 'false' }}`), and the contract test
  pins the template so it cannot regress.
- **setpoint commands from HA were silently dropped.** The number entity publishes
  `str(float)` - `"42.0"` - and the parser used `strtol`, which stops at the decimal point and
  rejects the text. Now parsed with `strtod`; whole values only (`"42.5"` is still a range
  error, not something to round).
- **a retained `cmd` could reboot the board forever.** A retained `reboot` replays on every
  reconnect: reboot -> reconnect -> replay. `PublishInfo.retained` was parsed but never used;
  commands on `cmd` are now live-only. `set/temperature` deliberately still honours retained
  replay, because re-applying the last setpoint is idempotent.

### Added

- **Internationalisation positioning in both READMEs.** Each gained a "Why this
  exists" section: the official app is single-phone Bluetooth with no local API,
  and in mainland China Bosch has left the market, so the app cannot be downloaded
  at all. A compatibility note now distinguishes the verified unit (CN-market
  JSG27-CSI, an EU-built import) from untested EU units - same model, same app +
  BLE control path, EU field reports welcome. Status went honest-to-current: MQTT
  / HA discovery is verified end-to-end (11 entities, setpoint round-trip, against
  a production Mosquitto on HAOS) and the offline test count is 167. The Mi Home /
  Bemfa path is explicitly marked "China region, optional, not the core path" -
  the core is local MQTT + Home Assistant. Also repaired a comparison table that
  had lost its header row.
- **Broker credentials are configurable, end to end.** The production broker (Mosquitto on
  the HAOS host) refuses anonymous connections, so the account must travel with the config:
  the portal gained a step 4 (address, username, password; empty = keep the stored values,
  the same rule as the Wi-Fi password), `Config` persists `mqttUser`/`mqttPass` in NVS,
  `startMqtt()` hands them to the client after every `mqttBegin()`, and the serial console
  grew `mqttuser <name> <password>` beside `broker`. `mqttSetCredentials()` - written with
  the client but dead until now - is live. `mqttBegin()` no longer blanks the stored account
  (a broker address change used to silently downgrade an authenticated connection to an
  anonymous one). `test_broker_config_contract.py` (8 tests) pins the whole chain, because
  one renamed arg anywhere in it means a well-formed CONNECT the broker still refuses.
  The portal path also restarts the client after a save, which it never did - a broker set
  through the page did not take effect until a reboot.
- **Host runner for the four C++ selftest suites** (`tools/host_selftest.py` +
  `tools/host_selftest_main.cpp`): compiles the framework-free layers (proto, and
  net/state_json|mqtt_wire|discovery) with g++/clang++ - or `pip install ziglang` where neither
  exists - and runs the same suites the board's `selftest` command runs. Wired into
  `run_tests.py` via `test_cpp_selftest.py` (skips without a compiler; ubuntu CI compiles it
  with g++). Reason: the first hardware run of the discovery suite caught a check asserting a
  12-byte fit as 10 - a check that had executed nowhere until the board ran it, because no host
  runner existed. The check counts (proto 60 / net 16 / mqtt 32 / discovery 22 = 130) are pinned
  on both sides.
- **MQTT client** (`net/mqtt_wire.cpp`, `net/mqtt_client.cpp`): a 3.1.1 subset written for this
  project instead of a library, because the payload, the will and the backoff all have to match
  the contract exactly and none of the Arduino clients expose them at that level. Publishes
  `state` (retained, one per A0 frame), `attributes` (retained, on connect) and `availability`
  (retained `online`; the **will** carries `offline`). Subscribes `set/temperature` and `cmd`.
  Backoff 2 s → 60 s, keepalive 60 s with a ping at 25 s. Measured: the wire format is
  self-tested green on the board; the socket path still awaits a live broker, see "status"
  below.
- **Home Assistant discovery**, generated from `spec/mqtt.json` by `tools/gen_ha.py` into
  `ha_discovery_gen.h`: 11 entities (9 sensors, 1 number for the setpoint, 1 binary sensor for
  the flame), each naming the same state topic, the same availability topic and a stable
  `unique_id` derived from the device id. Republished retained on every connect, so deleting an
  entity in HA and reconnecting the bridge is enough to get it back. `--check` in CI.
- **BLE write path**: `app::setSetpoint()` sends `D3` with parameters `02 55 00 <value>`,
  byte-identical to `bosch_api.set_temperature`. The reply is deliberately not awaited - the next
  A0 broadcast carries the authoritative setpoint, so a consumer sees what the heater accepted.
- `broker <host[:port]>` serial command, and `status` now prints the broker state and its last
  failure reason. `broker` with no argument prints the address, `broker -` clears it.
- `python/tools/mqtt_dev_broker.py` - anonymous amqtt broker for the development loop.
- `python/tools/mqtt_watch.py` - watches `btwh/#` and prints state as a table; `--demo` publishes
  the contract's golden payload as a fake bridge, so Home Assistant and the future touch panel can
  be built while the heater is elsewhere.
- `net` self test grew two suites: `mqtt` (33 checks on the packet bytes, including a packet
  arriving one byte at a time and a two-byte remaining length) and `discovery` (12 checks,
  including the golden topic/payload pair). `python/tests/test_ha_discovery.py` (7 tests) holds
  the generated table to the contract. Total 157 offline tests.

### Fixed

- `net/state_json.h` no longer defines `SelfTestResult` / `LogFn` itself; they moved to
  `net/selftest.h` so state_json, mqtt_wire and discovery can share one type without one
  including another's header.

- `firmware/bridge/src/net/` — the layer that will speak MQTT, started with the part that does
  not need a network: `mqtt_contract.h` is **generated** from `spec/mqtt.json`
  (`tools/gen_mqtt.py`, `--check` in CI), and `state_json.cpp` renders the payload from a plain
  snapshot. No Arduino, no NimBLE, no MQTT library, so the bytes can be checked off-target.
- `net` self test, run by the same `selftest` command as `proto`: 16 checks covering the whole
  payload byte for byte against a golden string the generator rendered, `ts` omission when there
  is no clock, `fault` as `"none"`/`"E1"`, every `mode` word, and the rounding rule.
- Provisioning. SoftAP + captive DNS + one HTML form that takes the heater (picked from a
  `BTWH-*` scan), the Wi-Fi network, the six-digit panel code and the broker in a single
  submit; input is validated **before** anything reaches the radio, because a malformed attempt
  still spends one of the heater's three failures. `portal` serves the same page on the LAN
  address when the board is already online, so re-pairing does not require anything to leave its
  current network. Wi-Fi credentials added to `Config` with `saveNetwork()` / `wipe()`.
- `wifi <ssid> <password>` and boot auto-join: a board that has credentials in NVS rejoins the
  network at every boot, after the heater session is up. Without it a power cut needed a human.
- Station Wi-Fi + SNTP (`net/station.cpp`): 15 s join budget with a named failure, CN NTP pool
  with the global pool as fallback, UTC throughout. `ts` joins the payload as soon as the clock
  answers and stays absent until then, per `consumer_rules[0]`.
- `mqtt` serial command — prints the payload for the **live** state, so the exact bytes can be
  inspected before any broker exists. First run with real data:
  `{"temp_out":28.0,...,"flame":false,"mode":"idle","fault":"none"}` (no `ts`, correctly).
- Number formatting is now fixed by the contract, not by each implementation: **half away from
  zero**. Python's `"%.1f"` is half-to-even, which would have made 42.25 render as `42.2` on the
  host and `42.3` on the bridge; a self-test case pins the rule down.
- `firmware/bridge/src/proto/` — the protocol layer as its own directory, with no Arduino and no
  NimBLE include. `frame.{h,cpp}` holds framing, stuffing, per-fragment XOR, checksum and stream
  reassembly; `a0.{h,cpp}` turns a broadcast into an `A0State`. Parsing is now separate from
  printing, which is what makes it checkable at all.
- `selftest` serial command — replays `spec/protocol_vectors.json` **on the board**: rebuilds
  every frame from its parameters, stuffs it, XORs it fragment by fragment, feeds the fragments
  back through the reassembler, corrupts a checksum on purpose, and compares every A0 field.
  Verified on hardware, 60 checks.
- `tools/gen_vectors.py` — generates `src/proto/vectors_gen.h` from the JSON contract.
  `--check` fails when the header is stale, so the two cannot drift.
- `tools/watch_a0.py` — prints the A0 broadcast as a table with per-field ranges. Answers "is
  the data alive and following the heater", which `log_diag.py` (connection failures) does not.
  Optional `--csv` and `--only-active`.
- `AGENTS.md` and `MEMORY.md` — build commands, layer rules, and the facts that were expensive to
  establish (the panel code is also the SMP passkey; `A2/07=00` means the window is closed, not
  that the MAC is unauthorised; three failures trigger a three-minute rate limit).
- `firmware/bridge/src/ble/` — the only directory that includes NimBLE: connect, SMP callbacks,
  FIFO/CREDITS subscribe, fragment reassembly, chunked writes, scan. It reports "encrypted /
  bonded" and stops there; whether that means the heater accepted the code is not its business.
- `firmware/bridge/src/auth/` — `A2/07` + `A2/08` as a state machine behind a `Transport` of three
  function pointers (`send`, `wait`, `log`). No NimBLE, no Arduino, no `Serial`, so a fake
  transport can replay the whole pairing-failure matrix without a heater in the room. The outcome
  codes mirror `bosch_api.PAIR_FAIL_*` name for name, and `classify()` maps the two replies onto
  them exactly as the Python side does.
- `firmware/bridge/src/app/` — `config.{h,cpp}` (NVS, defaults are placeholders) and
  `session.{h,cpp}` (order of operations, A0 state, link-drop recovery, 30 s watchdog).
- `python/tests/test_firmware_contract.py` — five tests that fail when the firmware's reason codes
  drift from the Python API, when `reasonText()` stops returning a code the Python side has, or
  when the reply line stops printing two hex digits.

### Changed

- `src/main.cpp` went from 672 to 604 lines by moving the codec out, and now from 604 to 273 by
  moving the radio, the login and the session out. What is left is `setup()`, `loop()` and the
  serial console. The firmware still links the same heater and still streams `A0` unchanged —
  verified after flashing, not assumed.
- **No committed `.ino` any more.** `gateway_cli.py` generates a sketch shell around `src/` at
  build time, so there is one copy of the sources instead of a hand-synced duplicate.
- CI gained three guards: `proto/` and `auth/` may not include Arduino, NimBLE or ESP-IDF, and the
  generated vector header must match `spec/protocol_vectors.json`. `auth/` is guarded for the same
  reason as `proto/`: being testable off-target is the only thing that makes the layer worth its
  own directory, and one `Serial.println()` ends that.

### Fixed

- `scan` printed a literal `//n` instead of a newline (`devices://n`).
- `parseA0` now exposes `fan_power`, `mode`, `max_setpoint` and `bathroom`, including the Type0
  offsets that only existed in the Python reference. The printed `[A0]` line is unchanged.
- `A2/07` and `A2/08` replies were logged with `%d` in one of the two places that print them, so a
  rate-limit reply `3C` came out as `60`. `log_diag.py` matches two hex digits, silently ignored
  `60`, and fell back to an earlier line — the diagnosis looked healthy while the device was
  refusing the code. Every reply is now printed as `%02X` from a single place.
- Build: arduino-cli compiles a *copy* of `src/` under `<build>/sketch/`, while the layered
  include path resolves back to the original tree. The same header then exists at two paths,
  `#pragma once` keys on the file rather than the name, and the build died with
  `redefinition of 'struct app::Config'`. Every header now carries a macro guard as well, and
  `gateway_cli.py` puts `src/` on the include path so `"app/session.h"` means the same thing to
  arduino-cli and to PlatformIO.
- `log_diag.py` misdiagnosed the state measured on 2026-11-08: link up, both CCCDs subscribed,
  `A2/07` silent, then the heater hangs up ~12 s later. It blamed GATT enumeration. It now
  recognises the signature and gives the advice the measurements actually support — stop retrying
  and leave the device alone for 10-15 minutes. The first version of this advice told the user to
  clear pairing and enter a new code; that was wrong, and the same stored code authorised
  afterwards with no panel action. This is also *not* the `A2/07 = 00` case: there was no reply
  at all.
- `reboot` used to call `ESP.restart()` with the BLE link still up. The radio drops, the heater
  keeps a half-open session, and the next connection is refused for ~15 minutes — the longest
  outage in this project so far, and it looked exactly like a broken pairing. Measured twice on
  2026-11-08: a manual `reboot` at 14:58, and closing/reopening the control panel at 15:33.
  `app::shutdown()` now sends a proper LL disconnect and waits 300 ms before the CPU resets.
  A/B on the same device and the same stored code: the old path cost ~17 minutes, the new one
  reached `A2/08=01` 6.4 s and the first `A0` 9.5 s after the command.
- Both host consoles reset the board just by attaching, so every panel reopen paid that
  15-minute cost. `esp32_panel.py` and `gateway_cli.py` now open the port and send `status`
  instead of `reboot`; a reset is an explicit button, not a side effect of looking.

### Planned

- MQTT publishing from the bridge against `spec/mqtt.json`
- `bosch_proto` extracted into `firmware/components/`
- Wi-Fi provisioning (Improv) implemented in firmware
- Touch panel application in `firmware/panel/`
- Home Assistant integration in `integrations/`

## [0.2.0] - 2026-11-08

### Added

- `spec/` — cross-language contracts as machine-checked artefacts: `api_contract.json`,
  `protocol_vectors.json`, and `mqtt.json`, the bridge/consumer contract (topics, state
  fields, units, ranges, availability rules). `test_mqtt_contract.py` fails if a field named
  there stops existing on `DeviceState`, so the contract cannot drift from the code
- `firmware/panel/` — reserved directory with the design constraints for the touchscreen
  panel, and a `layout` CI job that greps for an `#include` crossing the application
  boundary so the reservation is enforced rather than suggested
- `integrations/` — reserved for Home Assistant, Node-RED and ESPHome glue
- ADR-007 records the monorepo split and why the panel is an application, not a module
- 145 offline tests (was 136)

### Changed

- **The repository is now split by role, not by file type.** `python/` holds the reference
  implementation, `firmware/<application>/` holds buildable firmware, `spec/` holds the
  contracts, `docs/` holds technical documentation. Top-level entries dropped from 23 to 14
  files, and `esp32/` — which mixed firmware, board manuals, toolchain and serial logs — is
  gone
- **Command paths moved with the sources**: `python python/tools/bosch_ui.py`,
  `python python/tests/test_contract.py --update`, `python firmware/bridge/tools/log_diag.py`.
  `python run_tests.py` is unchanged, and the Windows launchers under `python/tools/` still
  work by double-click
- **`import bosch_api` did not move.** `pyproject.toml` maps the package root to `python/`
  through `package-dir`, so the import name stays flat and the frozen contract stays valid;
  a layout change is not a breaking change here
- Every document, code comment, docstring, log line, exception message and firmware serial
  line is now English. `README.zh-CN.md` is the single Chinese file, kept as an entry point
  for Chinese-speaking users. Match on exception types and state enums, never on message text

### Fixed

- Firmware: the A0 line printed `fan 706rpm flameno faultnone`; the values now have their own
  space
- Firmware: one `Serial.printf` carried a full-width comma (`，`), invisible to a scan that
  only looked for CJK ideographs. The scan now covers full-width punctuation
- Firmware: real device addresses had only ever lived in the compiled defaults, never in NVS,
  so replacing them with placeholders made the board boot pointing at `AA:BB:CC:DD:EE:FF`.
  They are now written to NVS with the `addr` / `mac` commands, which is what the
  placeholders were for

### Verified

- Power-cycle recovery on hardware: a stored pairing code reconnects unattended
  (`A2/07=01` -> `A2/08=01` -> `A0` stream)
- Firmware rebuilt and reflashed with the English output; `log_diag` replayed against
  synthetic English logs before flashing, so the diagnostics never lost their match strings

## [0.1.0] - 2026-11-07

### Added

- Python reference implementation (`bosch_api`) with protocol codec, device session and a
  resident service featuring reconnect backoff and a command queue
- Framework-agnostic provisioning/pairing state machine (`device_fsm`), with provisioning
  and pairing deliberately kept orthogonal
- Desktop console, headless bridge, and optional Mi Home bridge over Bemfa cloud
- ESP32-S3 gateway firmware v2 with BLE SMP passkey support, runtime pairing-code entry
  and power-cycle auto-recovery
- Log analyser (`log_diag`) that turns the failure matrix into executable diagnosis
- 136 offline tests, including a signature-freeze contract and cross-platform golden
  vectors shared with the C++ implementation
- Bilingual documentation (English / Simplified Chinese) and architecture decision records

### Verified

- Real-device link-up on a `BTWH-001`: SMP encryption, `A2/07`, `A2/08=01`, steady `A0` stream
- A pairing code remains valid for reconnection after the pairing window has closed
  (verified 11 minutes later; the upper bound is still unknown)

[Unreleased]: https://github.com/netsafe-cn/btwh-bridge/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/netsafe-cn/btwh-bridge/releases/tag/v0.2.0
[0.1.0]: https://github.com/netsafe-cn/btwh-bridge/releases/tag/v0.1.0
