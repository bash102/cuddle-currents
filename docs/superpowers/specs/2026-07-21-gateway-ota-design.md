# Gateway OTA (over-the-air firmware update) — Design

**Date:** 2026-07-21
**Status:** Design — pending implementation plan
**Milestone:** Roadmap #5 (Gateway provisioning / OTA)

## Goal

Update the ESP32-S3 BLE→WiFi gateway firmware **over WiFi**, one command from the
app updating the whole fleet, without USB-flashing each board. Safe by
construction: a bad image that can't get back online rolls itself back.

## Why this shape

The gateways already share an MQTT control plane (`cuddle/control/*`,
`cuddle/<gw>/cmd`, retained `cuddle/<gw>/report`). OTA rides on it: the app
publishes an OTA command, each gateway pulls the image over HTTP from the app
and flashes it. The flash is already OTA-ready — dual 3 MB app slots
(`app0`/`app1`) + `otadata` on the 16 MB partition table — so no
re-partitioning is needed.

## Decisions (locked)

1. **Delivery:** MQTT-triggered fleet pull. App hosts the `.bin` over HTTP and
   publishes a command; each gateway pulls via `esp_https_ota`, flashes, reboots,
   reports back.
2. **Rollback safety:** enabled. `CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE=y` +
   mark-valid-after-healthy-boot. A one-time full USB flash installs the
   rollback-capable bootloader; all subsequent updates are OTA and protected.
3. **Trigger surface:** `POST /api/ota` + an Ops "Update fleet" control that
   shows each gateway's running version and live update status.
4. **Version is embedded, never typed.** A single semver in
   `firmware/gateway-idf/version.txt` is the one source of truth (see below).
5. **Host LAN IP is shown in Ops** and used to build the OTA URL.

## Version plumbing — single source of truth

```
firmware/gateway-idf/version.txt   (e.g. "1.0.0")   <-- the ONE place a human edits
        |
        | ESP-IDF reads version.txt into PROJECT_VER at build time
        v
esp_app_desc_t.version  baked into build/cuddle-gateway.bin
        |                                   |
   runtime: esp_app_get_description()   app parses it out of the uploaded .bin
        ->version                            (validate magic 0xABCD5432 @0x20,
        |                                     read 32-byte version string @0x30)
        v                                    v
   report.version (retained)            /api/ota derives version from the file
```

- The firmware **reports its own running version** (`esp_app_get_description()->version`)
  in the `report` payload — no `#define` duplicated anywhere.
- The app **extracts the version from the uploaded `.bin`**. The ESP-IDF app
  image places an `esp_app_desc_t` at file offset `0x20`; its `version` field is a
  32-byte NUL-terminated string at offset `0x30`. The app validates the
  `magic_word` (`0xABCD5432`, little-endian bytes `32 54 CD AB` at `0x20`) before
  trusting the file, then reads the version. No version is ever typed by a human.
- **Convention (also added to CLAUDE.md):** bump `version.txt` whenever firmware
  changes, so the version-skip guard and rollback bookkeeping stay meaningful.

## Wire contract (additions to the existing plane)

| Topic | Direction | Retained | Payload |
|---|---|---|---|
| `cuddle/control/ota` | app → fleet | **No** | `{"url": str, "version": str, "sha256": str}` |
| `cuddle/<gw>/report` | gw → app | Yes | existing payload **plus** `"version": str` |
| `cuddle/<gw>/ota` | gw → app | **No** | `{"phase": str, "version": str, "detail": str}` |

`phase` ∈ `start` \| `downloading` \| `ok` \| `failed` \| `rejected`.

The OTA command is **non-retained** on purpose. A retained command would
re-fire on every gateway reboot/reconnect, re-flashing forever — the same
reason `cmd` is not retained. Only gateways online at publish time act on it;
an offline gateway is simply re-triggered when it returns.

## Firmware changes (`main.cpp`, `sdkconfig`, `version.txt`, `CMakeLists`)

1. **`version.txt`** with the current semver. (ESP-IDF picks it up automatically;
   no CMake change strictly required, but confirm `project()` doesn't override it.)
2. **`sdkconfig`:** `CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE=y`.
3. **Report version:** include `esp_app_get_description()->version` in
   `buildReportBody()`.
4. **OTA command handler:** subscribe `cuddle/control/ota`. On receipt:
   - If `version == running version` → publish `{"phase":"rejected","detail":"same version"}`, stop.
   - Else publish `start`, run `esp_https_ota` against `url` (streaming; the
     component verifies the image header and, when provided, `sha256`), publish
     `ok` on success then `esp_restart()`, or `failed` with the error on failure.
   - OTA runs on the app CPU without blocking the MQTT keepalive longer than the
     library requires; a failed OTA leaves the running slot untouched.
5. **Health gate / commit:** on boot, if the running partition state is
   `ESP_OTA_IMG_PENDING_VERIFY`, arm a health check. Once MQTT has been
   continuously connected for ~10 s, call
   `esp_ota_mark_app_valid_cancel_rollback()`. If the image fails to reach that
   healthy state within ~60 s, `esp_restart()` — with a pending-verify image the
   rollback-enabled bootloader boots the **previous** slot. The existing status
   LED reflects OTA/pending-verify state.

## App changes (FastAPI / Python)

1. **`POST /api/ota`** (multipart): accepts the `.bin`. Steps:
   - Parse+validate `esp_app_desc_t` → derive `version` from the file.
   - Store at `firmware_ota/<version>.bin` (gitignored runtime dir).
   - Compute `sha256`.
   - Resolve the host LAN IP; build `url = http://<lan-ip>:<port>/firmware/<version>.bin`.
   - Publish `cuddle/control/ota` `{url, version, sha256}` (non-retained).
   - Return `{version, sha256, url, targeted_gateways}`.
   - **Loopback guard:** if the resolved host is loopback / unroutable, return a
     clear `4xx` explaining the app must serve on a LAN address for OTA — never
     silently publish an unreachable URL.
2. **`GET /firmware/<name>.bin`:** serve stored images. Validate `<name>` against
   a strict pattern (no path traversal); 404 unknown.
3. **Host LAN IP detection:** determine the outbound LAN IP (the interface the
   broker/gateways can reach, e.g. via a connected-UDP-socket `getsockname`
   trick — no packets sent). Surface it on the state frame.
4. **Gateway version on the frame:** carry `report.version` through
   `GatewayState` so the API and Ops read each gateway's running version.
5. **Ops view:** show the host OTA URL base (LAN IP:port), each gateway's version
   with an out-of-date indicator (differs from the most recent uploaded image),
   an "Update fleet" control (file picker → `POST /api/ota`), and live per-gateway
   `ota` phase.

## Serving reachability

The app binds `127.0.0.1` by default; for OTA it must serve on a LAN-reachable
address (`--host 0.0.0.0`). Ops displaying the detected LAN IP makes this
visible; the loopback guard on `/api/ota` makes a misconfiguration a clear error
rather than a silent failed pull.

## Security scope (Phase-2 POC)

Plain HTTP over the trusted LAN. `sha256` gives **integrity** (not tampered in
transit / not corrupt), not **authenticity** (anyone on the LAN could serve a
`.bin`). No secure boot, no image signing. This is appropriate for a POC on a
private LAN. Production hardening — HTTPS, signed images, secure boot — is
explicitly out of scope and noted for later.

## Out of scope (deliberately)

- **Staggered/rolling rollout.** All gateways get the command at once and reboot
  near-simultaneously (brief total coverage gap). Acceptable for 2–4 boards;
  rollback + managed re-placement recover them. Sequential rollout is a future
  enhancement.
- **Per-gateway targeting.** `cuddle/<gw>/ota` command targeting can come later;
  the initial build is fleet-wide via `cuddle/control/ota`.
- Signed/encrypted images, secure boot, HTTPS.

## Testing

- **App (unit):** `.bin` version extraction (valid magic, bad magic rejected),
  `sha256`, URL construction, the loopback guard, `firmware/<name>.bin` path
  validation, and that `/api/ota` publishes the expected non-retained payload
  (MQTT publish mocked). `report.version` parsed onto `GatewayState`.
- **Firmware (hardware):** build `1.0.1`, OTA from `1.0.0`, confirm download →
  flash → healthy → commit, and `report.version` updates. Then push a
  deliberately-broken image (e.g. wrong WiFi) and confirm the health gate fails
  and the bootloader rolls back to `1.0.1`.

## CLAUDE.md addition

Under **Conventions**, add: the gateway firmware version lives in
`firmware/gateway-idf/version.txt` and **must be bumped whenever firmware
changes** — it is embedded in the image (`esp_app_desc_t`), reported by each
gateway, and drives the OTA same-version skip and rollback bookkeeping. Shipping
a firmware change without rolling the version breaks OTA's ability to tell images
apart.
