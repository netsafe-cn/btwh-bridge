# Security Policy

## What sensitive data this project handles

| Data | Stored in | Sensitivity | Notes |
|---|---|---|---|
| Heater BLE address | `config.json` / NVS | Low | Discoverable by passive scanning |
| Host BLE MAC | `config.json` / NVS | Medium | Identity in the device allow-list; leakage allows impersonated logins |
| Panel 6-digit pairing code | `config.json` / NVS | **High** | Short-lived, but usable within the window if leaked |
| Bemfa cloud key `bemfa_uid` | `config.json` / env var | **High** | Leakage hands over control of the Mi Home side |
| Wi-Fi credentials | NVS (`bosch_wifi`) | **High** | Never leaves the device |

## Protections already in place

- **No secrets in code.** Values are read only from `config.json` or `BOSCH_*` environment
  variables (see `Config`).
- **Pairing codes never reach logs or telemetry.** `device_fsm.snapshot()` emits only the
  boolean `has_pin` — never the code itself, never the Wi-Fi password. MQTT topics report
  state only.
- **Templates are fully redacted.** `config.example.json` contains no real address, code,
  or UID.
- **Two CI gates.** A dedicated job in `.github/workflows/ci.yml` fails if `config.json`
  is tracked, and scans sources for hard-coded `bemfa_uid` / `pair_code` patterns.
- **No vendor artefacts.** The repository does not distribute APKs, firmware, decompiled
  output, or extracted keys.

## What you need to do yourself

1. **Tighten file permissions** (POSIX):

   ```bash
   chmod 600 config.json
   ```

2. **Don't paste logs into public issues verbatim.** `logs/*.log` contains MAC addresses
   and pairing codes; redact them to `AA:BB:CC:DD:EE:FF` / `123456` first.

3. **Use environment variables** in containers or shared hosts so the pairing code never
   touches disk:

   ```bash
   export BOSCH_PAIR_CODE=...
   export BOSCH_BEMFA_UID=...
   ```

4. **Rotate the Bemfa key if it leaks.** Regenerate it in the Bemfa console and check that
   no unknown device got bound on the Mi Home side.

5. **Choose the ESP32 location carefully.** NVS is unencrypted, so anyone with physical
   access can read the pairing code and Wi-Fi credentials. Assess this before placing the
   device in a public area.

## Out of scope

- Someone with physical access to the heater panel (they can pair it themselves; this
  project adds no new risk)
- Someone with physical access to the ESP32 (they can dump NVS)
- Vendor-side protocol changes or cloud-side risk control
- Passive eavesdropping on the BLE link (the link is encrypted after pairing, but BLE
  makes no forward-secrecy promise)

## Reporting a vulnerability

**Please do not open a public issue.**

Use GitHub's **Private vulnerability reporting** (Security tab → Report a vulnerability),
or contact the maintainer directly.

Include the impact, reproduction steps, and your suggested fix. We aim to respond within
seven days of confirmation and will credit you in the fix announcement unless you ask to
stay anonymous.

## Supported versions

Security fixes target the latest revision of the `main` branch only. Older versions and
personal forks are not supported.
