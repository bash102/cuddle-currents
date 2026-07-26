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

Portal edits are written to NVS **when you hit Save**, not after the Wi-Fi join — a
failed join reboots the board, so saving later would discard everything you just typed.
Invalid fields (blank broker, port outside 1–65535, gateway id containing `/`, `+`, `#`)
are rejected in favour of the stored value, and an unchanged submit writes nothing. Rules
live in `main/portal_fields.h`; run them with `firmware/test/run.sh` (no board needed).
This needs WiFiManager **2.x** (`setSaveParamsCallback`), which `setup-components.sh`
vendors from the arduino-cli libraries dir.

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

## Troubleshooting: the whole fleet drops offline at random

Two independent causes, both fixed in **1.0.5** — worth knowing because the first one
survives a firmware update and needs a one-time broker cleanup.

**1. Duplicate gateway id ⇒ duplicate MQTT client id.** The gateway id becomes the MQTT
client id (`cuddle-gw-<gwid>`), and a broker evicts the existing session whenever another
client connects with the same id. A fleet sharing one id therefore knocks itself offline
in a round-robin: each gateway's reconnect kicks the previous one, and every eviction
fires the retained `cuddle/<gw>/online = 0` last-will. All five gateways look like they
"randomly" drop.

Firmware before 1.0.2 derived the id suffix from the *low* 24 bits of the efuse MAC — the
OUI, identical across a production batch — so every board defaulted to the same
`esp32-01-<oui>`. 1.0.2 fixed the derivation, but **that fix could not reach an already
provisioned board**: the colliding id had been written to NVS on first boot, and NVS wins
over the computed default. 1.0.5 detects that exact stored value and drops it, so the
device-unique default takes over on the next boot (a name you typed into the portal is
never touched). Watch for this on the serial log:

```
gwid: dropped legacy batch-collision id 'esp32-01-a172e0' -> 'esp32-01-ac8cd4'
```

Because the id changes, the **old retained topics linger on the broker as a ghost
gateway**. Clear them once (empty retained payload), per stale id:

```bash
mosquitto_pub -h <broker> -t cuddle/esp32-01-a172e0/online -r -n
mosquitto_pub -h <broker> -t cuddle/esp32-01-a172e0/report -r -n
```

Confirm the fleet is actually distinct — five gateways should mean five ids:

```bash
mosquitto_sub -h <broker> -t 'cuddle/+/online' -v -W 3
```

**2. A blocked BLE connect starved the MQTT keepalive.** `connectTo()` runs inline in
`loop()` and blocks for the NimBLE connect timeout (~30 s) when a band doesn't answer,
while PubSubClient's default keepalive is only **15 s** — so a single failed connect could
outlast it and the broker would drop the link (again firing the offline last-will). With
30 bands in a room, failed connects are routine. 1.0.5 raises the keepalive to 90 s and
processes **one** queued connect per `loop()` iteration, so `mqtt.loop()` runs between
attempts instead of after a whole queue of them.

If drops persist after this, suspect the radio rather than the code: five ESP32s each
holding 6 BLE links share the 2.4 GHz band with Wi-Fi, and BLE/Wi-Fi coexist on one
antenna. Spread the gateways out, keep them off a congested channel, and check RSSI in
the `report` payload.

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

7. **Trust model (POC — trusted LAN only)**: `/api/ota` is **unauthenticated** and images
   are served over plain HTTP, so anyone who can reach the app's LAN address can flash the
   whole fleet. The command's `sha256` is for integrity (corrupt-download detection), not
   authenticity. This is acceptable only on a trusted LAN. Before exposing to any untrusted
   network: add auth to `/api/ota`, bind to a trusted interface, and move to signed images
   over HTTPS.
