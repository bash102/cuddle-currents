# Gateway OTA Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Update ESP32-S3 gateway firmware over WiFi — one app command updates the whole fleet — with self-healing rollback on a bad image.

**Architecture:** The app hosts the `.bin` over HTTP and publishes a non-retained `cuddle/control/ota` command; each gateway pulls via `esp_https_ota`, flashes, health-gates (MQTT up ~10s → commit, else bootloader rollback), and reports progress on `cuddle/<gw>/ota`. Firmware version is a single source of truth in `version.txt`, embedded in the image (`esp_app_desc_t`) and extracted from the uploaded `.bin` — never typed.

**Tech Stack:** ESP-IDF v5.5 (`esp_https_ota`, `esp_app_desc`, `app_update`/rollback), FastAPI (multipart upload + static serve), existing MQTT control plane, vanilla-JS Ops frontend.

**Reference spec:** `docs/superpowers/specs/2026-07-21-gateway-ota-design.md`

## Global Constraints

- **Never read, print, or commit `firmware/gateway-idf/main/secrets.h`** (holds the WiFi password). It is gitignored. Only non-secret files (`version.txt`, `sdkconfig`, `main.cpp`) are edited.
- **The OTA command `cuddle/control/ota` is published NON-retained.** A retained OTA command re-fires on every gateway reboot/reconnect → reflash loop. (Same reason `cmd` is not retained.)
- **Wire contract is exact.** New/changed topics & payload keys must match the spec verbatim: `cuddle/control/ota` `{"url","version","sha256"}`; `cuddle/<gw>/report` gains `"version"`; `cuddle/<gw>/ota` `{"phase","version","detail"}` with `phase ∈ {start,downloading,ok,failed,rejected}`.
- **`StateFrame` (`core/models.py`) is the only backend↔frontend contract.** Surface OTA data (gateway versions, host OTA base, per-gw phase) by adding fields there.
- **Firmware version single source of truth = `firmware/gateway-idf/version.txt`.** Read at runtime via `esp_app_get_description()->version`; extracted app-side from the `.bin`. Never a `#define` duplicate, never a typed field.
- **Security scope (POC):** plain HTTP over the trusted LAN, `sha256` for integrity only (not authenticity), no secure boot / signing. Do not add TLS/signing in this plan.
- **No frontend build step** (`frontend/js/**` served live; hard-refresh picks up edits). **Python changes need the `cuddle` process restarted; firmware changes need a reflash.**
- **Firmware has no C++ unit-test harness.** Firmware tasks verify via `idf.py build` (compiles clean) plus hardware/MQTT/serial observation — not pytest. Python tasks are TDD with pytest.
- **`esp_app_desc_t` layout (verified against a real build):** app-desc at file offset `0x20`; `magic_word` `0xABCD5432` (LE) at `0x20`; 32-byte NUL-terminated `version` at `0x30`.

---

## File Structure

**Firmware** (`firmware/gateway-idf/`)
- Create `version.txt` — the semver source of truth (ESP-IDF reads it into `PROJECT_VER`).
- Modify `sdkconfig` — enable `CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE`.
- Modify `main/main.cpp` — report version; boot-time rollback health gate; `cuddle/control/ota` handler running `esp_https_ota` + phase reporting.

**App** (`src/cuddle/`)
- Create `hub/ota.py` — pure helpers: parse firmware version from a `.bin`, sha256, LAN-IP detection, filename safety. (No I/O beyond a socket for LAN-IP; trivially testable.)
- Modify `hub/orchestration/world.py` — carry `version` on `GatewayView`.
- Modify `hub/orchestration/orchestrator.py` — gateway version on `gateway_states`; `publish_ota`; subscribe `cuddle/+/ota` + track per-gw phase.
- Modify `core/models.py` — `GatewayState.version`, `GatewayState.ota` (phase), `StateFrame.ota_host` / `ota_url_base`.
- Modify `transport/ws_server.py` — `POST /api/ota`, `GET /firmware/{name}`, loopback guard.
- Modify `transport/app.py` (Engine) — firmware storage dir, LAN-IP into the frame.

**Frontend** (`frontend/`)
- Modify `ops.html` + `js/ops/gateways.js` (and/or a small `js/ops/ota.js`) — host OTA base + per-gateway version + out-of-date flag + "Update fleet" control + live phase.

**Docs**
- Modify `CLAUDE.md` (Conventions), `docs/superpowers/roadmap.md` (mark milestone), `firmware/gateway-idf/README.md` (OTA usage).

**Tests** (`tests/`)
- Create `tests/test_ota.py`; extend `tests/test_orchestration_world.py`, `tests/test_orchestrator.py`, and a ws-server test file for `/api/ota`.

---

## Task 1: Firmware — version source of truth + report it

**Files:**
- Create: `firmware/gateway-idf/version.txt`
- Modify: `firmware/gateway-idf/main/main.cpp` (add `#include "esp_app_desc.h"`; add `"version"` to `buildReportBody()`)

**Interfaces:**
- Produces: `cuddle/<gw>/report` payload gains a top-level `"version": "<semver>"` string, read from `esp_app_get_description()->version`.

- [ ] **Step 1: Create the version file**

`firmware/gateway-idf/version.txt` (single line, no trailing content beyond newline):

```
1.0.0
```

- [ ] **Step 2: Include the app-desc header**

In `main/main.cpp`, with the other ESP-IDF includes near the top, add:

```cpp
#include "esp_app_desc.h"   // esp_app_get_description()->version (from version.txt)
```

- [ ] **Step 3: Add `version` to the report body**

In `buildReportBody()` (currently opens `{"capacity":...,"mode":...,"connected":[...`), insert the version right after `mode`. Change the opening concatenation to include:

```cpp
static String buildReportBody() {
  String s = "{\"capacity\":" + String(MAX_CONNECTIONS) +
             ",\"mode\":\"" + (effectiveManaged() ? "managed" : "opportunistic") + "\"" +
             ",\"version\":\"" + String(esp_app_get_description()->version) + "\"" +
             ",\"connected\":[";
  // ...unchanged below...
```

Note: `esp_app_desc_t.version` is 32 bytes max, always NUL-terminated by IDF — safe for `String()`. Update the `MQTT_BUF_SIZE` comment only if you wish; the added field is <48 bytes and fits existing headroom.

- [ ] **Step 4: Build (verification — no C++ unit harness)**

Run (from `firmware/gateway-idf/`, after `source ./activate.sh`): `idf.py build`
Expected: `Project build complete`. Then confirm the embedded version is now `1.0.0` (not the git-describe fallback):

```bash
python - <<'PY'
d=open("build/cuddle-gateway.bin","rb").read(); off=0x20
print(d[off+0x10:off+0x30].split(b"\x00",1)[0].decode())   # expect: 1.0.0
PY
```

- [ ] **Step 5: Flash + observe (hardware verification)**

Flash board 1 (`idf.py -p /dev/cu.usbserial-A5069RR4 flash`), wait ~20s, then:
`mosquitto_sub -t 'cuddle/+/report' -C 1 -v` → the JSON must contain `"version":"1.0.0"`.

- [ ] **Step 6: Commit**

```bash
git add firmware/gateway-idf/version.txt firmware/gateway-idf/main/main.cpp
git commit -m "Firmware: version.txt as version source, report it in report payload"
```

---

## Task 2: Firmware — enable rollback + boot-time health gate

**Files:**
- Modify: `firmware/gateway-idf/sdkconfig` (`CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE=y`)
- Modify: `firmware/gateway-idf/main/main.cpp` (`#include "esp_ota_ops.h"`; health-gate logic in `setup()`/`loop()`)

**Interfaces:**
- Consumes: MQTT-connected state (existing — the code already tracks `mqtt.connected()`).
- Produces: on a freshly-OTA'd boot, the running image is committed (`esp_ota_mark_app_valid_cancel_rollback()`) only after MQTT is healthy; otherwise the board reboots and the bootloader reverts to the previous slot.

- [ ] **Step 1: Enable rollback in sdkconfig**

In `firmware/gateway-idf/sdkconfig`, change:

```
# CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE is not set
```
to:
```
CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE=y
```

(Setting it in `sdkconfig.defaults` too is optional; editing `sdkconfig` is sufficient and is what the build consumes.)

- [ ] **Step 2: Include OTA ops + add health-gate state**

In `main/main.cpp` add the include:

```cpp
#include "esp_ota_ops.h"   // rollback: mark-app-valid / running-partition state
```

Add file-scope state near the other `static` timers:

```cpp
// Rollback health gate: when this boot is a freshly-OTA'd image the bootloader
// leaves it PENDING_VERIFY. We commit it (cancel rollback) only once MQTT has
// been continuously up for OTA_HEALTHY_MS; if it never gets there within
// OTA_VERIFY_DEADLINE_MS we reboot, and the rollback-enabled bootloader falls
// back to the previous slot. A committed/normal boot skips all of this.
static const unsigned long OTA_HEALTHY_MS = 10000;         // MQTT up this long = healthy
static const unsigned long OTA_VERIFY_DEADLINE_MS = 60000; // else revert
static bool g_pendingVerify = false;   // is this boot an un-committed OTA image?
static unsigned long g_mqttUpSince = 0; // millis() when MQTT last became connected (0 = down)
```

- [ ] **Step 3: Detect pending-verify at boot**

At the end of `setup()` (after NVS/prefs are up, before the main loop), add:

```cpp
{
  const esp_partition_t* running = esp_ota_get_running_partition();
  esp_ota_img_states_t st;
  if (esp_ota_get_state_partition(running, &st) == ESP_OK &&
      st == ESP_OTA_IMG_PENDING_VERIFY) {
    g_pendingVerify = true;
    Serial.println("OTA: running a pending-verify image; will commit once healthy");
  }
}
```

- [ ] **Step 4: Drive the gate from loop()**

At the top of `loop()` (near the existing `updateLed()` call), add health tracking. Assume `mqtt.connected()` is the PubSubClient state (already used in this file):

```cpp
// --- rollback health gate (only meaningful while pending-verify) ---
if (mqtt.connected()) {
  if (g_mqttUpSince == 0) g_mqttUpSince = millis();
} else {
  g_mqttUpSince = 0;
}
if (g_pendingVerify) {
  if (g_mqttUpSince != 0 && millis() - g_mqttUpSince >= OTA_HEALTHY_MS) {
    esp_ota_mark_app_valid_cancel_rollback();
    g_pendingVerify = false;
    Serial.println("OTA: image committed (healthy)");
  } else if (millis() >= OTA_VERIFY_DEADLINE_MS) {
    Serial.println("OTA: not healthy in time; rebooting to roll back");
    esp_restart();
  }
}
```

- [ ] **Step 5: Build + full USB flash both boards (installs the rollback bootloader)**

`idf.py build` → expect `Project build complete`.
Full flash (bootloader changed) each board: `idf.py -p <port> flash` for `/dev/cu.usbserial-A5069RR4` and `/dev/cu.usbserial-3`.
Serial should show a normal boot with NO "pending-verify" line (a USB flash writes a committed image), and the board reports/behaves normally.

- [ ] **Step 6: Commit**

```bash
git add firmware/gateway-idf/sdkconfig firmware/gateway-idf/main/main.cpp
git commit -m "Firmware: enable bootloader rollback + boot-time health gate"
```

---

## Task 3: Firmware — OTA command handler (`esp_https_ota` + phase reporting)

**Files:**
- Modify: `firmware/gateway-idf/main/main.cpp` (`#include "esp_https_ota.h"`, `#include "esp_http_client.h"`; subscribe `cuddle/control/ota`; handle it in the MQTT callback; run OTA)

**Interfaces:**
- Consumes: `cuddle/control/ota` `{"url","version","sha256"}` (parse with the existing whitespace-tolerant `jsonExtract`).
- Produces: `cuddle/<gw>/ota` `{"phase","version","detail"}` published at start / result; `esp_restart()` on success (new image boots pending-verify → Task 2 gate).

- [ ] **Step 1: Includes**

```cpp
#include "esp_https_ota.h"
#include "esp_http_client.h"
```

- [ ] **Step 2: Subscribe to the OTA command topic**

Where the gateway subscribes after MQTT connect (alongside `cuddle/control/mode`, `cuddle/<gw>/cmd`, `cuddle/+/report`), add:

```cpp
mqtt.subscribe("cuddle/control/ota");
```

- [ ] **Step 3: A phase publisher helper**

Near `statusTopic()` / the other topic helpers, add:

```cpp
static String otaTopic() { return "cuddle/" + g_gwid + "/ota"; }

static void publishOtaPhase(const char* phase, const String& version, const String& detail) {
  String p = String("{\"phase\":\"") + phase + "\",\"version\":\"" + version +
             "\",\"detail\":\"" + detail + "\"}";
  mqtt.publish(otaTopic().c_str(), p.c_str(), false);   // NON-retained
}
```

- [ ] **Step 4: The OTA runner**

Add a function (near `connectTo`, outside callbacks — it runs from the MQTT callback which runs in loop()'s context here):

```cpp
// Pull+flash a new image. Blocks for the download (seconds on a LAN); PubSubClient
// keepalive may lapse mid-download and reconnect afterward — acceptable because we
// reboot on success and the pre-flight "start" phase is already published. sha256 is
// carried for integrity; esp_https_ota validates the image header + writes atomically
// to the inactive slot, then we reboot into it (pending-verify -> Task 2 gate).
static void runOta(const String& url, const String& version, const String& sha256) {
  const char* cur = esp_app_get_description()->version;
  if (version == String(cur)) {
    publishOtaPhase("rejected", version, "same version");
    Serial.printf("OTA: rejected (already running %s)\n", cur);
    return;
  }
  publishOtaPhase("start", version, url);
  Serial.printf("OTA: pulling %s -> %s\n", version.c_str(), url.c_str());

  esp_http_client_config_t http = {};
  http.url = url.c_str();
  http.timeout_ms = 15000;
  http.keep_alive_enable = true;
  esp_https_ota_config_t cfg = {};
  cfg.http_config = &http;

  esp_err_t err = esp_https_ota(&cfg);
  if (err == ESP_OK) {
    publishOtaPhase("ok", version, "rebooting");
    Serial.println("OTA: ok, rebooting");
    delay(200);
    esp_restart();
  } else {
    publishOtaPhase("failed", version, esp_err_to_name(err));
    Serial.printf("OTA: failed: %s\n", esp_err_to_name(err));
  }
}
```

- [ ] **Step 5: Route the command in the MQTT callback**

In `mqttCallback`, where topics are dispatched, add a branch for the OTA topic. Convert the payload to a `String p` (the callback already does this for `cmd`). Add:

```cpp
if (String(topic) == "cuddle/control/ota") {
  String url = jsonExtract(p, "url");
  String version = jsonExtract(p, "version");
  String sha256 = jsonExtract(p, "sha256");
  if (url.length() && version.length()) runOta(url, version, sha256);
  else Serial.println("OTA: malformed command (missing url/version)");
  return;
}
```

(Placement: before/after the `cmd` branch; ensure it does not fall through into the per-device cmd handling.)

- [ ] **Step 6: Build (verification)**

`idf.py build` → `Project build complete`. (Hardware OTA is exercised in Task 9.)

- [ ] **Step 7: Commit**

```bash
git add firmware/gateway-idf/main/main.cpp
git commit -m "Firmware: cuddle/control/ota handler (esp_https_ota + phase reporting)"
```

---

## Task 4: App — pure OTA helpers (`hub/ota.py`)

**Files:**
- Create: `src/cuddle/hub/ota.py`
- Test: `tests/test_ota.py`

**Interfaces:**
- Produces:
  - `parse_firmware_version(data: bytes) -> str` — raises `ValueError` on a non-IDF image; returns the embedded semver.
  - `sha256_hex(data: bytes) -> str`.
  - `is_routable_host(ip: str) -> bool` — False for loopback/unspecified/empty.
  - `detect_lan_ip(target: tuple[str, int]) -> str | None` — outbound interface IP toward `target` (e.g. the broker), or None.
  - `safe_firmware_name(version: str) -> str` — `"<version>.bin"` with version validated to `[A-Za-z0-9._-]+`; raises `ValueError` otherwise.

- [ ] **Step 1: Write failing tests**

`tests/test_ota.py`:

```python
import struct
import pytest
from cuddle.hub import ota


def _fake_image(version: str, magic: int = 0xABCD5432) -> bytes:
    buf = bytearray(0x60)
    struct.pack_into("<I", buf, 0x20, magic)          # app-desc magic at file 0x20
    v = version.encode()
    buf[0x30:0x30 + len(v)] = v                        # 32-byte version at 0x30
    return bytes(buf)


def test_parse_firmware_version_reads_embedded_semver():
    assert ota.parse_firmware_version(_fake_image("1.2.3")) == "1.2.3"


def test_parse_firmware_version_rejects_bad_magic():
    with pytest.raises(ValueError):
        ota.parse_firmware_version(_fake_image("1.2.3", magic=0xDEADBEEF))


def test_parse_firmware_version_rejects_too_small():
    with pytest.raises(ValueError):
        ota.parse_firmware_version(b"\x00" * 8)


def test_sha256_hex_is_stable():
    assert ota.sha256_hex(b"abc") == (
        "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    )


@pytest.mark.parametrize("ip,ok", [
    ("192.168.1.212", True), ("10.0.0.5", True),
    ("127.0.0.1", False), ("0.0.0.0", False), ("", False),
])
def test_is_routable_host(ip, ok):
    assert ota.is_routable_host(ip) is ok


def test_safe_firmware_name_ok():
    assert ota.safe_firmware_name("1.2.3") == "1.2.3.bin"


@pytest.mark.parametrize("bad", ["../etc", "a/b", "1 2", "", "a;b"])
def test_safe_firmware_name_rejects_unsafe(bad):
    with pytest.raises(ValueError):
        ota.safe_firmware_name(bad)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_ota.py -q`
Expected: FAIL (`ModuleNotFoundError: cuddle.hub.ota`).

- [ ] **Step 3: Implement `hub/ota.py`**

```python
"""Pure helpers for over-the-air firmware updates.

No app state, no MQTT. The one non-trivial piece is reading the firmware
version straight out of an ESP-IDF app image: IDF places an esp_app_desc_t at
file offset 0x20 whose 32-byte NUL-terminated `version` field (offset 0x30)
carries PROJECT_VER (from version.txt). We validate the magic word before
trusting the file so a non-image upload fails loudly.
"""

from __future__ import annotations

import hashlib
import ipaddress
import re
import socket
import struct

_APP_DESC_OFFSET = 0x20
_APP_DESC_MAGIC = 0xABCD5432
_VERSION_OFFSET = _APP_DESC_OFFSET + 0x10   # 0x30
_VERSION_LEN = 32
_SAFE_VERSION = re.compile(r"^[A-Za-z0-9._-]+$")


def parse_firmware_version(data: bytes) -> str:
    if len(data) < _VERSION_OFFSET + _VERSION_LEN:
        raise ValueError("too small to be an ESP-IDF app image")
    (magic,) = struct.unpack_from("<I", data, _APP_DESC_OFFSET)
    if magic != _APP_DESC_MAGIC:
        raise ValueError(f"bad app-desc magic {magic:#010x}; not an ESP-IDF image")
    raw = data[_VERSION_OFFSET:_VERSION_OFFSET + _VERSION_LEN]
    version = raw.split(b"\x00", 1)[0].decode("ascii", "replace")
    if not version:
        raise ValueError("empty firmware version")
    return version


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def is_routable_host(ip: str) -> bool:
    if not ip:
        return False
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return not (addr.is_loopback or addr.is_unspecified)


def detect_lan_ip(target: tuple[str, int]) -> str | None:
    """The local IP of the interface that routes toward `target` (e.g. the
    broker). A connected UDP socket sends no packets; getsockname reveals the
    chosen source address."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(target)
        ip = s.getsockname()[0]
        return ip if is_routable_host(ip) else None
    except OSError:
        return None
    finally:
        s.close()


def safe_firmware_name(version: str) -> str:
    if not _SAFE_VERSION.match(version):
        raise ValueError(f"unsafe firmware version {version!r}")
    return f"{version}.bin"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_ota.py -q`
Expected: PASS (all).

- [ ] **Step 5: Commit**

```bash
git add src/cuddle/hub/ota.py tests/test_ota.py
git commit -m "OTA: pure helpers (version parse, sha256, LAN-IP, name safety)"
```

---

## Task 5: App — gateway version + OTA phase through world model & orchestrator

**Files:**
- Modify: `src/cuddle/core/models.py` (`GatewayState.version`, `GatewayState.ota`)
- Modify: `src/cuddle/hub/orchestration/world.py` (`GatewayView.version`; parse in `apply_report`)
- Modify: `src/cuddle/hub/orchestration/orchestrator.py` (`gateway_states` version; subscribe `cuddle/+/ota`; `_handle_ota_phase`; `ota_status`; `publish_ota`)
- Test: `tests/test_orchestration_world.py`, `tests/test_orchestrator.py`

**Interfaces:**
- Consumes: `report["version"]` (optional; older firmware omits it → `None`); `cuddle/<gw>/ota` phase messages.
- Produces:
  - `GatewayState.version: str | None`, `GatewayState.ota: OtaPhase | None`.
  - `Orchestrator.publish_ota(url: str, version: str, sha256: str) -> None` — publishes non-retained `cuddle/control/ota`.
  - `Orchestrator.ota_status() -> dict[str, OtaPhase]` (gw → latest phase).

- [ ] **Step 1: Write failing tests**

Add to `tests/test_orchestration_world.py`:

```python
def test_apply_report_carries_version():
    from cuddle.hub.orchestration.world import WorldModel
    w = WorldModel()
    w.apply_report("gw1", {"capacity": 6, "mode": "managed", "version": "1.2.0",
                           "connected": [], "seen": []}, now=1.0)
    assert w.gateways["gw1"].version == "1.2.0"


def test_apply_report_version_absent_is_none():
    from cuddle.hub.orchestration.world import WorldModel
    w = WorldModel()
    w.apply_report("gw1", {"capacity": 6, "mode": "managed",
                           "connected": [], "seen": []}, now=1.0)
    assert w.gateways["gw1"].version is None
```

Add to `tests/test_orchestrator.py` (reuse its existing recorder/publish harness — mirror an existing publish test for the payload assertion, and the existing `_handle_message` style for the phase test):

```python
def test_publish_ota_is_non_retained_control_command(orch_and_recorder):
    orch, sent = orch_and_recorder   # existing fixture pattern in this file
    orch.publish_ota("http://host/firmware/1.3.0.bin", "1.3.0", "abc123")
    topic, payload, qos, retain = sent[-1]
    assert topic == "cuddle/control/ota"
    assert retain is False
    import json
    assert json.loads(payload) == {
        "url": "http://host/firmware/1.3.0.bin", "version": "1.3.0", "sha256": "abc123"}


def test_ota_phase_message_updates_status(orch):   # existing bare-orch fixture
    import json
    orch._handle_message("cuddle/esp32-01/ota",
                         json.dumps({"phase": "start", "version": "1.3.0",
                                     "detail": "url"}).encode(), now=1.0)
    assert orch.ota_status()["esp32-01"].phase == "start"
```

(If the test file lacks those exact fixtures, construct an `Orchestrator` with a `SessionStore()` and monkeypatch `_publish` to append to a list, matching the file's established approach.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_orchestration_world.py tests/test_orchestrator.py -q -k "version or ota"`
Expected: FAIL (`version` attr / `publish_ota` / `ota_status` missing).

- [ ] **Step 3: Add the model fields**

In `core/models.py`, add an OTA phase model and extend `GatewayState`:

```python
class OtaPhase(BaseModel):
    """Latest OTA progress reported by a gateway on cuddle/<gw>/ota."""

    phase: str          # start | downloading | ok | failed | rejected
    version: str
    detail: str = ""


class GatewayState(BaseModel):
    id: str
    online: bool
    mode: str
    capacity: int
    connected: list[ConnectedBand]
    seen: list[SeenBand]
    version: str | None = None
    ota: OtaPhase | None = None
```

(Add `version`/`ota` to the existing `GatewayState` definition — keep all current fields.)

- [ ] **Step 4: Carry version on the world view**

In `world.py`, add `version: str | None` to `GatewayView` and read it in `apply_report`:

```python
@dataclass
class GatewayView:
    id: str
    capacity: int
    mode: str
    online: bool
    connected: dict[str, int | None]
    seen: dict[str, int | None]
    last_report_ts: float
    version: str | None = None
```

In `apply_report`, when constructing the `GatewayView`, add:

```python
            version=payload.get("version"),
```

- [ ] **Step 5: Orchestrator — version on state, OTA publish + phase tracking**

In `orchestrator.py`:

Add phase storage in `__init__`: `self._ota_status: dict[str, OtaPhase] = {}` (import `OtaPhase`).

In `gateway_states()`, pass the version through:

```python
            states.append(
                GatewayState(
                    id=gw_id,
                    online=view.online,
                    mode=view.mode,
                    capacity=view.capacity,
                    connected=connected,
                    seen=seen,
                    version=view.version,
                    ota=self._ota_status.get(gw_id),
                )
            )
```

Add publish + status accessors:

```python
    def publish_ota(self, url: str, version: str, sha256: str) -> None:
        self._publish(
            f"{self._prefix}/control/ota",
            json.dumps({"url": url, "version": version, "sha256": sha256}).encode(),
            qos=1,
            retain=False,
        )

    def ota_status(self) -> dict[str, OtaPhase]:
        return dict(self._ota_status)
```

Route the phase topic in `_handle_message` (it currently accepts `report`/`online` on a 3-part topic — add `ota`):

```python
        elif kind == "ota":
            self._handle_ota_phase(gw, payload)
```

And the handler:

```python
    def _handle_ota_phase(self, gw: str, payload: bytes) -> None:
        try:
            data = json.loads(payload)
        except (ValueError, TypeError):
            return
        if not isinstance(data, dict) or "phase" not in data:
            return
        self._ota_status[gw] = OtaPhase(
            phase=str(data["phase"]),
            version=str(data.get("version", "")),
            detail=str(data.get("detail", "")),
        )
```

Subscribe to the phase topic in `_run` (alongside the existing `+/report`, `+/online`):

```python
                    await client.subscribe(f"{self._prefix}/+/ota")
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `pytest tests/test_orchestration_world.py tests/test_orchestrator.py -q`
Expected: PASS. Then full suite: `pytest -q` (no regressions).

- [ ] **Step 7: Commit**

```bash
git add src/cuddle/core/models.py src/cuddle/hub/orchestration/world.py \
        src/cuddle/hub/orchestration/orchestrator.py \
        tests/test_orchestration_world.py tests/test_orchestrator.py
git commit -m "OTA: carry gateway version + OTA phase through world/orchestrator/frame"
```

---

## Task 6: App — `/api/ota` endpoint, firmware serving, LAN IP on frame

**Files:**
- Modify: `src/cuddle/transport/ws_server.py` (`POST /api/ota`, `GET /firmware/{name}`)
- Modify: `src/cuddle/transport/app.py` (firmware dir; LAN IP into the frame)
- Modify: `src/cuddle/core/models.py` (`StateFrame.ota_url_base: str | None`)
- Test: `tests/test_ws_ota.py` (new)

**Interfaces:**
- Consumes: `Orchestrator.publish_ota` (Task 5); `hub.ota` helpers (Task 4).
- Produces:
  - `POST /api/ota` (multipart `bin`): stores `firmware_ota/<version>.bin`, publishes the command, returns `{"version","sha256","url","gateways"}`; `409/400` on loopback host or non-image upload.
  - `GET /firmware/{name}`: serves a stored image; `404` unknown; rejects unsafe names.
  - `StateFrame.ota_url_base` (e.g. `http://192.168.1.50:8770`) so Ops shows the host.

- [ ] **Step 1: Write failing tests**

`tests/test_ws_ota.py` (use FastAPI `TestClient`; stub the orchestrator's `publish_ota` and inject a routable host):

```python
import struct
from fastapi.testclient import TestClient


def _image(version: str) -> bytes:
    buf = bytearray(0x60)
    struct.pack_into("<I", buf, 0x20, 0xABCD5432)
    buf[0x30:0x30 + len(version)] = version.encode()
    return bytes(buf)


def _client_with_routable_host():
    # Build the app/Engine as the other ws tests do, but force a routable
    # OTA host so the loopback guard passes. Mirror the existing test harness
    # in tests/test_ws_*.py for constructing `app` + a fake orchestrator whose
    # publish_ota records calls.
    ...  # returns (client, published: list, tmp_firmware_dir)


def test_post_ota_stores_publishes_and_returns_version():
    client, published, _ = _client_with_routable_host()
    r = client.post("/api/ota", files={"bin": ("fw.bin", _image("1.4.0"), "application/octet-stream")})
    assert r.status_code == 200
    body = r.json()
    assert body["version"] == "1.4.0"
    assert body["url"].endswith("/firmware/1.4.0.bin")
    assert published and published[-1][1] == "1.4.0"   # (url, version, sha256)


def test_post_ota_rejects_non_image():
    client, _, _ = _client_with_routable_host()
    r = client.post("/api/ota", files={"bin": ("x.bin", b"not an image", "application/octet-stream")})
    assert r.status_code == 400


def test_get_firmware_serves_then_404_and_rejects_traversal():
    client, _, _ = _client_with_routable_host()
    client.post("/api/ota", files={"bin": ("fw.bin", _image("1.4.0"), "application/octet-stream")})
    assert client.get("/firmware/1.4.0.bin").status_code == 200
    assert client.get("/firmware/9.9.9.bin").status_code == 404
    assert client.get("/firmware/..%2fsecrets").status_code in (400, 404)


def test_post_ota_errors_when_host_is_loopback():
    # Same harness but OTA host resolves to loopback -> 409 with a clear message.
    ...
```

(Fill the `...` harness bodies to match the construction already used by the repo's ws-server tests — an `Engine`/app with a fake orchestrator exposing `publish_ota`, `gateway_states`, `ota_status`.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_ws_ota.py -q`
Expected: FAIL (routes missing).

- [ ] **Step 3: Firmware dir + LAN IP in the Engine**

In `transport/app.py` (`Engine`): choose a firmware storage dir (gitignored) — `firmware_ota/` at repo root or under the existing runtime dir — and create it on start. Compute the OTA host once (from `hub.ota.detect_lan_ip((broker_host, broker_port))`, falling back to the configured `--host` if set to a routable address), and store `self.ota_url_base = f"http://{host}:{port}"` when routable else `None`. Include `ota_url_base` when building the `StateFrame`.

Add to `core/models.py` `StateFrame`: `ota_url_base: str | None = None`.

- [ ] **Step 4: Implement the routes**

In `ws_server.py`:

```python
from pathlib import Path
from fastapi import UploadFile, File, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from cuddle.hub import ota as ota_helpers


@app.post("/api/ota")
async def api_ota(bin: UploadFile = File(...)):
    orch = engine.orchestrator
    if orch is None:
        raise HTTPException(503, "OTA requires the orchestrator (run with --orchestrate)")
    if engine.ota_url_base is None:
        raise HTTPException(
            409,
            "app is not serving on a LAN address; start with --host 0.0.0.0 so "
            "gateways can reach the firmware image",
        )
    data = await bin.read()
    try:
        version = ota_helpers.parse_firmware_version(data)
        name = ota_helpers.safe_firmware_name(version)
    except ValueError as e:
        raise HTTPException(400, f"not a valid gateway firmware image: {e}")
    sha = ota_helpers.sha256_hex(data)
    path = engine.firmware_dir / name
    path.write_bytes(data)
    url = f"{engine.ota_url_base}/firmware/{name}"
    orch.publish_ota(url, version, sha)
    gateways = [gw.id for gw in orch.gateway_states()]
    return {"version": version, "sha256": sha, "url": url, "gateways": gateways}


@app.get("/firmware/{name}")
async def api_firmware(name: str):
    try:
        # version is the stem; reuse the same validator to reject traversal
        ota_helpers.safe_firmware_name(name.removesuffix(".bin"))
    except ValueError:
        raise HTTPException(400, "bad firmware name")
    path = engine.firmware_dir / name
    if not path.is_file():
        raise HTTPException(404, "unknown firmware")
    return FileResponse(path, media_type="application/octet-stream")
```

(Adapt `engine`/`orch` accessors to however `ws_server` reaches the Engine and its orchestrator today.)

- [ ] **Step 5: Gitignore the firmware dir**

Add `firmware_ota/` to `.gitignore` (runtime state, like `captures/` and `config/enrollment.yaml`).

- [ ] **Step 6: Run tests to verify they pass**

Run: `pytest tests/test_ws_ota.py -q` → PASS. Then `pytest -q` → no regressions.

- [ ] **Step 7: Commit**

```bash
git add src/cuddle/transport/ws_server.py src/cuddle/transport/app.py \
        src/cuddle/core/models.py tests/test_ws_ota.py .gitignore
git commit -m "OTA: /api/ota upload+publish, /firmware serving, LAN host on frame"
```

---

## Task 7: Frontend — Ops OTA panel (versions, update, live phase)

**Files:**
- Modify: `frontend/ops.html` (an OTA/update control + host line)
- Modify: `frontend/js/ops/gateways.js` (show `gw.version`, out-of-date flag, `gw.ota` phase) and/or create `frontend/js/ops/ota.js` (upload wiring)

**Interfaces:**
- Consumes: `StateFrame.ota_url_base`, `GatewayState.version`, `GatewayState.ota` (from Tasks 5–6).
- Produces: user-facing "Update fleet" flow calling `POST /api/ota` with the selected `.bin`.

- [ ] **Step 1: Show host OTA base + per-gateway version**

In the Ops render (`gateways.js`), display `frame.ota_url_base` once near the gateway section (e.g. "Serving OTA from http://192.168.1.50:8770" or a warning when null: "Not LAN-reachable — start with --host 0.0.0.0"). On each gateway card, render `gw.version` (e.g. `v1.0.0`), and when `gw.ota` is set, show its `phase` (+ `detail` on hover). Add a small out-of-date marker when a gateway's `version` differs from the most recently uploaded version (tracked client-side after a successful upload).

- [ ] **Step 2: The "Update fleet" control**

In `ops.html`, add a hidden `<input type="file" accept=".bin">` and an "Update fleet" button in the gateways section header. Wire (in `gateways.js` or `ota.js`): button → open file picker → on file chosen, `POST /api/ota` as `multipart/form-data` (field name `bin`); show the returned `version` and a transient "pushed to N gateways" note; surface a 4xx/5xx body (e.g. the loopback message) inline. No version is typed — it comes from the response.

```javascript
async function pushFirmware(file) {
  const fd = new FormData();
  fd.append("bin", file);
  const r = await fetch("/api/ota", { method: "POST", body: fd });
  const body = await r.json().catch(() => ({}));
  if (!r.ok) { showOtaError(body.detail || r.statusText); return; }
  markExpectedVersion(body.version);           // drives the out-of-date flag
  showOtaNote(`pushed ${body.version} to ${body.gateways.length} gateway(s)`);
}
```

- [ ] **Step 3: Manual verification (no frontend unit tests)**

With the app running (`--orchestrate`, `--host 0.0.0.0`) and both gateways online: hard-refresh `/ops`. Confirm each card shows `v1.0.0` and the host OTA line renders. (Actual push is exercised in Task 9.)

- [ ] **Step 4: Commit**

```bash
git add frontend/ops.html frontend/js/ops/gateways.js frontend/js/ops/ota.js
git commit -m "Ops: OTA panel — gateway versions, update-fleet control, live phase"
```

---

## Task 8: Docs — CLAUDE.md convention, roadmap, firmware README

**Files:**
- Modify: `CLAUDE.md` (Conventions)
- Modify: `docs/superpowers/roadmap.md` (mark OTA milestone progress)
- Modify: `firmware/gateway-idf/README.md` (OTA usage)

**Interfaces:** none (documentation).

- [ ] **Step 1: CLAUDE.md convention**

Under **## Conventions**, add a bullet:

```markdown
- **Firmware version lives in `firmware/gateway-idf/version.txt` and MUST be
  bumped whenever firmware changes.** It is embedded in the image
  (`esp_app_desc_t`), reported by each gateway (`report.version`), and drives
  OTA's same-version skip + rollback bookkeeping. Shipping a firmware change
  without rolling the version breaks OTA's ability to tell images apart.
```

- [ ] **Step 2: Firmware README — OTA usage**

Add an OTA section: how to bump `version.txt`, `idf.py build`, then push via the Ops "Update fleet" button (or `curl -F bin=@build/cuddle-gateway.bin http://<lan-ip>:8770/api/ota`); note the app must run with `--host 0.0.0.0`; note the first rollback-enabled build needs a one-time USB flash; note the health-gate/auto-rollback behavior.

- [ ] **Step 3: Roadmap**

In `docs/superpowers/roadmap.md`, update the Milestone-5 OTA note to reflect it as implemented (MQTT-triggered fleet pull + rollback), leaving the future items (staggered rollout, per-gw targeting, signing/HTTPS) as follow-ups.

- [ ] **Step 4: Commit**

```bash
git add CLAUDE.md docs/superpowers/roadmap.md firmware/gateway-idf/README.md
git commit -m "Docs: OTA usage + version-roll convention + roadmap"
```

---

## Task 9: Hardware validation (integration)

**Files:** none (validation task — produces a short results note appended to the firmware README or a scratch log).

**Interfaces:** exercises the whole path end-to-end on real boards.

- [ ] **Step 1: Baseline**

Both boards run `1.0.0` (from Tasks 1–3, USB-flashed with the rollback bootloader). Confirm Ops shows `v1.0.0` for both.

- [ ] **Step 2: Happy-path OTA**

Bump `version.txt` → `1.0.1`, `idf.py build`. In Ops (app running `--host 0.0.0.0 --orchestrate`), "Update fleet" → select `build/cuddle-gateway.bin`. Observe on `mosquitto_sub -t 'cuddle/+/ota' -v`: `start` → `ok` per gateway; boards reboot; serial shows "pending-verify … committed (healthy)"; Ops `version` flips to `v1.0.1`. Confirm no duplicate connections and bands re-placed after reboot.

- [ ] **Step 3: Same-version skip**

Re-push `1.0.1` → each gateway publishes `{"phase":"rejected","detail":"same version"}`, no reboot.

- [ ] **Step 4: Rollback**

Build a deliberately-broken `1.0.2` (e.g. a wrong `WIFI_SSID` in a scratch build so it cannot reach MQTT — do NOT commit that change; never touch the real password). Push it to ONE board over Ops. Observe: it flashes, reboots, fails to reach MQTT within the deadline, reboots again, and the bootloader reverts — the board comes back reporting `1.0.1`. Restore `version.txt` to the real next version afterward.

- [ ] **Step 5: Record results**

Append a short pass/fail note (which boards, versions, observed phases, rollback confirmed) to the firmware README's OTA section or a validation log, and report to the human.

---

## Self-Review notes (author)

- **Spec coverage:** version-embedding (T1/T4), rollback+health-gate (T2/T9-step4), OTA command+pull+phase (T3/T5), `/api/ota`+serving+LAN-IP+loopback-guard (T6), Ops versions/update/phase (T7), CLAUDE.md convention (T8), non-retained command (T3/T5 constraint + test), sha256 integrity (T4/T6), security-scope note (T8) — all mapped.
- **Type consistency:** `OtaPhase`/`GatewayState.version`/`GatewayState.ota`/`StateFrame.ota_url_base` defined in T5/T6 and consumed by T7; `publish_ota(url,version,sha256)` and `ota_status()` defined T5, used T6; `hub.ota` signatures defined T4, used T6.
- **Firmware caveat surfaced:** blocking `esp_https_ota` may lapse PubSubClient keepalive mid-download (T3 comment) — acceptable for POC because success reboots and phases bracket the block; if problematic, switch to the chunked `esp_https_ota_begin/perform` API (future).
- **Open item for the human at execution:** the ws-server test harness (`tests/test_ws_ota.py`) and Engine accessors (`engine.orchestrator`, `engine.firmware_dir`, `engine.ota_url_base`) must match the repo's actual `ws_server`/`app.py` shape — the implementer wires them to the existing pattern rather than inventing new plumbing.
