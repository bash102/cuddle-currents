# Cuddle Currents — ESP32 BLE→WiFi gateway firmware

Bridges standard BLE Heart Rate armbands to the app over MQTT. Scans for the Heart
Rate Service (`0x180D`), connects to up to `MAX_CONNECTIONS` bands, and republishes the
**raw `0x2A37` notification bytes** so the app decodes them with its own parser.

Target board: **ESP32-S3** (dual-core, BT5 LE). Implements the contract in
`docs/superpowers/specs/2026-07-19-esp32-gateway-design.md`.

## MQTT contract

| Topic | Payload |
|---|---|
| `cuddle/<gw>/hr/<dev>` | raw `0x2A37` bytes |
| `cuddle/<gw>/status/<dev>` | `{"event":"connected"\|"disconnected","rssi":<int>}` |
| `cuddle/<gw>/online` | `"1"`/`"0"` retained; `"0"` is the Last-Will |

`<dev>` is the band's BLE address (identity is the band; gateway id is routing only).

## Toolchain (one-time)

```bash
brew install arduino-cli
arduino-cli config init
arduino-cli config add board_manager.additional_urls \
  https://espressif.github.io/arduino-esp32/package_esp32_index.json
arduino-cli core update-index
arduino-cli core install esp32:esp32
arduino-cli lib install "NimBLE-Arduino@1.4.3"   # pinned: firmware uses the 1.4 API
arduino-cli lib install "PubSubClient"
```

## Configure

```bash
cp firmware/gateway/secrets.h.example firmware/gateway/secrets.h
# edit secrets.h — Wi-Fi SSID/password, broker IP, gateway id (secrets.h is gitignored)
```

## Build & flash

```bash
# build
arduino-cli compile --fqbn esp32:esp32:esp32s3 firmware/gateway

# flash + watch boot (UART bridge port; adjust to your device)
arduino-cli upload  --fqbn esp32:esp32:esp32s3 -p /dev/cu.usbserial-A5069RR4 firmware/gateway
arduino-cli monitor -p /dev/cu.usbserial-A5069RR4 -c baudrate=115200
```

Expected serial output: Wi-Fi connect → MQTT connect → `BLE scanning for 0x180D...`,
then `BLE connected: <addr>` as bands are found.

## `MAX_CONNECTIONS` and the validation sweep

Default is **3** (NimBLE-Arduino's built-in build ceiling — no extra flags needed). To
attempt more concurrent bands you must raise BOTH the runtime cap and NimBLE's compile
ceiling:

```bash
arduino-cli compile --fqbn esp32:esp32:esp32s3 \
  --build-property "compiler.cpp.extra_flags=-DMAX_CONNECTIONS=6 -DCONFIG_BT_NIMBLE_MAX_CONNECTIONS=6" \
  firmware/gateway
```

Finding the reliable ceiling (stability + `0x2A37` throughput + dropped beats under
sustained streaming) is the roadmap's hardware-validation milestone — sweep this value
and measure, then set the shipped default from the result.

## Validate end-to-end (no app UI needed)

```bash
mosquitto -p 1883                                   # broker on the Mac (192.168.1.212)
mosquitto_sub -t 'cuddle/#' -v                      # watch the gateway's traffic
# flash the board; you should see:  cuddle/esp32-01/online 1
# with a band nearby:               cuddle/esp32-01/status/<addr> {"event":"connected",...}
#                                    cuddle/esp32-01/hr/<addr> <binary>
```

Then run the app against the same broker to see it in the UI:

```bash
cuddle --source mqtt --broker 192.168.1.212:1883
```
