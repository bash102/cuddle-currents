# Cuddle Currents — ESP-IDF gateway firmware (raises the BLE ceiling past 3)

Same gateway as [`../gateway`](../gateway) (BLE Heart Rate armbands → MQTT), but built
with **ESP-IDF** instead of arduino-cli. The only reason this port exists: the arduino
toolchain ships a **precompiled BT controller capped at ~3 concurrent ACL links**, and no
`-D` flag can move it. Under ESP-IDF the controller and NimBLE host both compile from
source, so `sdkconfig` finally governs the ceiling — this build targets **6** concurrent
bands (hard max 9; ≤6 recommended for RAM headroom on the S3).

Firmware logic (`main/main.cpp`) is identical to the sketch except the BLE calls use the
**esp-nimble-cpp 2.x** API (the arduino build's NimBLE-Arduino 1.4.x bundles its own host
and can't be used under IDF). MQTT contract, captive-portal provisioning, and the
FreeRTOS-queue concurrency model are unchanged — see [`../gateway/README.md`](../gateway/README.md).

## What sets the ceiling

Two `sdkconfig` keys, kept in step (in `sdkconfig.defaults`):

| Key | Value | Layer |
|---|---|---|
| `CONFIG_BT_NIMBLE_MAX_CONNECTIONS` | 6 | NimBLE host — max central links |
| `CONFIG_BT_CTRL_BLE_MAX_ACT` | 7 | S3 controller — max BLE *activities* (6 links + 1 scan) |

`MAX_ACT` counts activities, not connections: each link + scanning + advertising is one.
A central scanning while holding 6 links needs 7; the firmware stops scanning before the
last connect, but 7 gives headroom for a drop that restarts the scanner mid-connect.
(On the ESP32-S3 the controller lives in the classic `BT_CTRL_*` namespace, **not** the
`BT_LE_*` one used by the C-series NPL controller.)

## Components

- `espressif/arduino-esp32` **3.3.10** and `h2zero/esp-nimble-cpp` **2.5.0** — registry
  (managed) deps, declared in `main/idf_component.yml`, fetched on `idf.py reconfigure`.
- `PubSubClient` + `WiFiManager` — not in the registry; `setup-components.sh` vendors them
  from the arduino-cli libraries dir into `components/` with CMake shims.

## Toolchain (one-time)

```bash
# ESP-IDF v5.5 (arduino-esp32 3.3.x needs idf >=5.3,<6.1)
git clone -b v5.5 --recursive https://github.com/espressif/esp-idf.git ~/esp/esp-idf
~/esp/esp-idf/install.sh esp32s3     # if it errors inside a venv, run from a clean shell
```

`activate.sh` sources the IDF environment for a shell whose default `python3` is a project
virtualenv (IDF refuses to run nested in another venv): it drops `.venv` off `PATH`, pins
the system `python3`, then sources `export.sh`.

## Configure

Wi-Fi is set at **runtime** via the captive portal (same as `../gateway`). `secrets.h`
only seeds compile-time defaults for the MQTT broker / port / gateway id:

```bash
cp main/secrets.h.example main/secrets.h   # edit broker/port/gwid; gitignored
```

## Build & flash

```bash
cd firmware/gateway-idf
. ./activate.sh                 # ESP-IDF env
bash setup-components.sh        # vendor PubSubClient + WiFiManager (once)
idf.py set-target esp32s3       # first time only (pulls managed components)
idf.py build
idf.py -p /dev/cu.usbserial-A5069RR4 flash monitor   # adjust port
```

Expected serial: Wi-Fi join → MQTT connect → `Cuddle Currents gateway (max 6 bands)` →
`BLE scanning for 0x180D...`, then `BLE: subscribed to <addr>` for **more than 3** bands.

## Provisioning & MQTT contract

Identical to the arduino build — captive portal `Cuddle-Gateway-Setup` at `192.168.4.1`,
hold BOOT (GPIO0) at reset to reopen it (or build with `-DFORCE_PORTAL`). Topics:
`cuddle/<gw>/hr/<dev>` (raw 0x2A37), `cuddle/<gw>/status/<dev>`, `cuddle/<gw>/online`
(retained LWT). See [`../gateway/README.md`](../gateway/README.md) for details.

## Managed mode (Level B — app-orchestrated assignment)

This build also implements the **managed mode** of the app's Level B orchestration (see the
top-level [`docs/superpowers/roadmap.md`](../../docs/superpowers/roadmap.md)): the app, not
the gateway, decides which bands each gateway holds. Boots **opportunistic** by default
(today's auto-connect-up-to-capacity behavior, unchanged) and only switches to managed on an
explicit command — a fresh-out-of-the-box or misconfigured gateway never gets stuck waiting
for an orchestrator that isn't there.

- `cuddle/<gw>/report` — retained, published on change + a ~2s heartbeat: `capacity`,
  effective `mode`, `connected[]` (held addrs + RSSI), `seen[]` (scanned-but-unconnected
  bands), `ts`.
- `cuddle/<gw>/cmd` — subscribed: `{"action":"connect","dev":"..."}` looks the address up in
  the scan cache and connects; `{"action":"release","dev":"..."}` disconnects it.
- `cuddle/control/mode` (`managed`/`opportunistic`) and `cuddle/control/online` (the
  orchestrator's retained liveness flag) select managed mode and gate it.
- **Transition-based auto-revert**: managed mode holds as long as the last-known online state
  is `true`; on a `true→online-goes-false` transition it starts a ~15s grace timer and falls
  back to opportunistic if the orchestrator doesn't come back before it expires, then snaps
  back to managed the instant `control/online` says `"1"` again. In managed mode the gateway
  keeps scanning (to keep `seen[]` fresh) but never auto-connects — only `cmd` does.

Implemented and build-verified (`idf.py build` clean, no new warnings); **on-hardware
validation of managed mode is still pending** — no gateway has been flashed and run against a
live orchestrator yet, so treat this as build-verified, not field-verified.

## OTA updates

Gateways receive firmware updates via MQTT-triggered pull:

1. **Bump the version**: Edit `version.txt`, increment the version string (e.g., `1.2.3` →
   `1.2.4`). This is embedded in the image and reported in each gateway's `report.version`;
   OTA skips gateways already running the new version.

2. **Build the image**: `idf.py build` produces `build/cuddle-gateway.bin`.

3. **Push to the fleet**: Use the Ops UI "Update fleet" button, or curl:
   ```bash
   curl -F bin=@build/cuddle-gateway.bin http://<lan-ip>:8770/api/ota
   ```
   The app broadcasts an MQTT command on `cuddle/control/ota` (NON-retained) with the image
   URL; all gateways fetch and self-update.

4. **App accessibility**: The app must run with `--host 0.0.0.0` so gateways on the same LAN
   can reach the image URL. Localhost-only bindings will cause OTA to fail for remote devices.

5. **First rollback-enabled build**: The dual-slot OTA partition table (`ota_0`/`ota_1` +
   `otadata`) has existed since the first IDF build; the first rollback-enabled build only adds
   the bootloader rollback config (`CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE`), which the
   bootloader must be reflashed once over USB (`idf.py flash`) to pick up. Once a
   rollback-capable image is on the device, all later OTA updates are protected.

6. **Auto-rollback health gate**: The gateway enters a health-check window after OTA: if it
   cannot reach MQTT within ~60s, it auto-reverts to the previous slot and reboots. This
   prevents a broken image from bricking a gateway fleet. After connection, the new version
   is confirmed and the previous image is discarded.
