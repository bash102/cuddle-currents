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
