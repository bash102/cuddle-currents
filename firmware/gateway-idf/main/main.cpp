// Cuddle Currents — BLE->WiFi gateway firmware (ESP32-S3, Phase 2 PoC).
//
// Scans for standard BLE Heart Rate Service (0x180D) armbands, connects to up to
// MAX_CONNECTIONS of them, subscribes to Heart Rate Measurement (0x2A37), and
// republishes the RAW notification bytes over MQTT to the app. The app decodes with
// its own ble_parser, so this firmware stays a dumb bridge.
//
// MQTT contract (must match src/cuddle/sources/mqtt_source.py):
//   cuddle/<gw>/hr/<dev>      raw 0x2A37 bytes (binary), not retained
//   cuddle/<gw>/status/<dev>  JSON {"event":"connected"|"disconnected","rssi":<int>}
//   cuddle/<gw>/online        "1"/"0" retained; "0" is the MQTT Last-Will
// <dev> is the band's BLE address (identity is the band; gateway is routing only).
//
// Level B "managed mode" (orchestrator-driven placement, on top of Level A's default
// opportunistic auto-connect):
//   cuddle/<gw>/report        retained, on change + ~2s heartbeat:
//                             {"capacity":int,"mode":"managed"|"opportunistic",
//                              "connected":[{"dev":str,"rssi":int|null}],
//                              "seen":[{"dev":str,"rssi":int|null}],"ts":int}
//   cuddle/<gw>/cmd      <-   {"action":"connect"|"release","dev":str} (not retained)
//   cuddle/control/mode  <-   "managed"|"opportunistic" (global, retained)
//   cuddle/control/online <-  "1"/"0" (global, retained; published once by the
//                             orchestrator, NOT a heartbeat — see effectiveManaged())
// Boot default is opportunistic; managed only takes effect once control/mode says so,
// and auto-reverts to opportunistic if the orchestrator goes quiet for ORCH_GRACE_MS.
//
// Provisioning (runtime, no re-flash): on boot the gateway joins saved Wi-Fi; if it
// has none (or BOOT/GPIO0 is held at reset), it starts a SoftAP + captive portal
// ("Cuddle-Gateway-Setup") where you pick the Wi-Fi and set broker/port/gateway-id
// from a phone. Config persists in NVS. secrets.h only seeds the compile-time
// DEFAULTS for broker/port/gateway-id.
//
// Concurrency: NimBLE callbacks run in the BLE host task, but PubSubClient is not
// thread-safe. Callbacks only enqueue events onto a FreeRTOS queue; loop() is the
// sole MQTT publisher.

#include <Arduino.h>
#include <WiFi.h>
#include <WiFiManager.h>
#include <Preferences.h>
#include <PubSubClient.h>
#include <NimBLEDevice.h>
#include "secrets.h"

// arduino-esp32's initArduino() FREES the BLE controller RAM at startup unless bleInUse()
// says otherwise — and its weak bleInUse() is gated on arduino's OWN BLE library, which
// this build excludes (selective compilation) in favour of esp-nimble-cpp. Without this
// strong override the freed RAM is handed to the heap, and esp_bt_controller_init() (called
// from NimBLEDevice::init) then corrupts it and panics (LoadProhibited in tlsf). Claiming
// BLE keeps the memory reserved for the controller we actually use.
extern "C" bool bleInUse(void) { return true; }

// Concurrent BLE central links. Under this ESP-IDF build the ceiling is set by
// sdkconfig (CONFIG_BT_NIMBLE_MAX_CONNECTIONS host + CONFIG_BT_CTRL_BLE_MAX_ACT
// controller, both in sdkconfig.defaults) — the whole point of the IDF port. The app
// cap tracks the host setting so they can't drift.
#ifndef MAX_CONNECTIONS
#  ifdef CONFIG_BT_NIMBLE_MAX_CONNECTIONS
#    define MAX_CONNECTIONS CONFIG_BT_NIMBLE_MAX_CONNECTIONS
#  else
#    define MAX_CONNECTIONS 3
#  endif
#endif

#define BOOT_BUTTON 0  // GPIO0: hold at reset to force the config portal

static const uint16_t HR_SERVICE = 0x180D;
static const uint16_t HR_MEASUREMENT = 0x2A37;

WiFiClient wifiClient;
PubSubClient mqtt(wifiClient);
Preferences prefs;

// Runtime config (loaded from NVS, defaults from secrets.h, overridable via the portal).
String g_broker;
int g_port;
String g_gwid;

// ---- cross-task event marshalling ------------------------------------------
enum EvtKind : uint8_t { EVT_HR = 0, EVT_CONNECTED = 1, EVT_DISCONNECTED = 2, EVT_SEEN = 3 };

struct GwEvent {
  EvtKind kind;
  char addr[18];       // "aa:bb:cc:dd:ee:ff"
  uint8_t len;         // HR payload length (EVT_HR)
  uint8_t data[32];    // raw 0x2A37 bytes (EVT_HR)
  int rssi;            // EVT_CONNECTED, EVT_SEEN
  uint8_t addrType;    // EVT_SEEN: public/random, preserved for a later connect
};

// A connect request carries the address string AND its type (public/random). Rebuilding
// a NimBLEAddress from the string alone defaults to public, so random-address bands
// (Coospo uses random) fail to connect — we must preserve the advertised type.
struct ConnectReq {
  char addr[18];
  uint8_t type;
};

static QueueHandle_t evtQueue;      // BLE task -> loop() publishes
static QueueHandle_t connectQueue;  // scan callback -> loop() connects

// Slots of currently-held / in-flight connections, tracked by address string
// (plus last-known RSSI and the NimBLEClient* once connect() succeeds, so
// `report` and `cmd:release` can look a peer up by address).
static const int RSSI_UNSET = -32768;  // outside any real RSSI range (~ -100..0 dBm)
struct HeldConn {
  String addr;
  int rssi;
  NimBLEClient* client;
};
static HeldConn held[MAX_CONNECTIONS];
static int heldCount = 0;

static bool isHeld(const String& a) {
  for (int i = 0; i < heldCount; i++) if (held[i].addr == a) return true;
  return false;
}
static void addHeld(const String& a) {
  if (heldCount < MAX_CONNECTIONS && !isHeld(a)) {
    held[heldCount].addr = a;
    held[heldCount].rssi = RSSI_UNSET;
    held[heldCount].client = nullptr;
    heldCount++;
  }
}
static void removeHeld(const String& a) {
  for (int i = 0; i < heldCount; i++) {
    if (held[i].addr == a) {
      held[i] = held[--heldCount];
      held[heldCount] = HeldConn{String(), RSSI_UNSET, nullptr};
      return;
    }
  }
}
static void setHeldClient(const String& a, NimBLEClient* c) {
  for (int i = 0; i < heldCount; i++) if (held[i].addr == a) { held[i].client = c; return; }
}
static void setHeldRssi(const String& a, int rssi) {
  for (int i = 0; i < heldCount; i++) if (held[i].addr == a) { held[i].rssi = rssi; return; }
}
static NimBLEClient* getHeldClient(const String& a) {
  for (int i = 0; i < heldCount; i++) if (held[i].addr == a) return held[i].client;
  return nullptr;
}

// ---- Level B "managed" orchestration ---------------------------------------
// g_configuredManaged is what cuddle/control/mode last asked for (retained,
// boot default false = opportunistic = today's behavior). Whether the gateway
// actually BEHAVES as managed also depends on orchestrator liveness — see
// effectiveManaged() below.
static bool g_configuredManaged = false;
// Orchestrator liveness, tracked by transition (control/online is published
// once, retained, NOT a heartbeat — see effectiveManaged()).
static bool g_online = false;
static unsigned long g_offlineSince = 0;  // set on a true->false transition; 0 = offline since boot
static const unsigned long ORCH_GRACE_MS = 15000;

// True while the gateway should behave as managed (no auto-connect, cmd-only):
// configured managed AND (orchestrator currently online OR still within grace
// of its last offline transition). Once grace elapses with no online signal,
// this flips to false and the gateway auto-reverts to opportunistic; it snaps
// back the instant control/online (or control/mode) says otherwise.
static bool effectiveManaged() {
  if (!g_configuredManaged) return false;
  if (g_online) return true;
  return (millis() - g_offlineSince) < ORCH_GRACE_MS;
}

// Scan cache: every advertised HR band seen recently, whether or not we're
// connected to it. Source for `report.seen` (minus currently-held addrs) and
// for resolving cmd:connect's addr->type lookup (NimBLEAddress needs the
// advertised type to connect to random addresses, e.g. Coospo bands).
struct ScanEntry {
  String addr;
  uint8_t type;
  int rssi;
  unsigned long last_seen;
};
static const int SCAN_CACHE_MAX = 24;
static const unsigned long SEEN_TTL_MS = 10000;
static ScanEntry scanCache[SCAN_CACHE_MAX];
static int scanCount = 0;

static int findScanCacheIndex(const String& addr) {
  for (int i = 0; i < scanCount; i++) if (scanCache[i].addr == addr) return i;
  return -1;
}
static void touchScanCache(const String& addr, uint8_t type, int rssi) {
  unsigned long now = millis();
  int idx = findScanCacheIndex(addr);
  if (idx >= 0) {
    scanCache[idx].type = type;
    scanCache[idx].rssi = rssi;
    scanCache[idx].last_seen = now;
    return;
  }
  if (scanCount < SCAN_CACHE_MAX) {
    scanCache[scanCount++] = ScanEntry{addr, type, rssi, now};
  } else {
    // Cache is full: evict the stalest entry rather than drop the new one.
    int oldest = 0;
    for (int i = 1; i < scanCount; i++) {
      if (scanCache[i].last_seen < scanCache[oldest].last_seen) oldest = i;
    }
    scanCache[oldest] = ScanEntry{addr, type, rssi, now};
  }
}
static void pruneScanCache() {
  unsigned long now = millis();
  int w = 0;
  for (int i = 0; i < scanCount; i++) {
    if (now - scanCache[i].last_seen <= SEEN_TTL_MS) {
      if (w != i) scanCache[w] = scanCache[i];
      w++;
    }
  }
  scanCount = w;
}

// ---- multi-gateway dedup (peer awareness via each other's `report`) ---------
// A multi-connect band (e.g. Scosche Rhythm+) keeps advertising while connected, so every
// in-range gateway grabs it and it ends up on more than one. Rather than rely on the app
// commanding a release (fragile — address-format mismatches make it silently no-op), the
// gateways coordinate directly: each subscribes to the others' `report`s and learns which
// bands they hold. We then (a) never connect a band a peer already holds, and (b) if a
// duplicate forms anyway (both connect within the report-interval race window), the
// weaker-RSSI gateway drops its copy. Self-heals: a peer's holds expire if it stops
// reporting (e.g. goes offline). Address compares are case-insensitive, so a format
// difference between gateway firmwares can't defeat it.
struct RemoteHold { String dev; String gw; int rssi; unsigned long ts; };
static const int REMOTE_HOLD_MAX = 40;
static RemoteHold remoteHeld[REMOTE_HOLD_MAX];
static int remoteHeldCount = 0;
static const unsigned long REMOTE_HOLD_TTL_MS = 8000;  // drop a peer's holds if it stops reporting

static int remoteHolderOf(const String& dev) {  // index of a peer holding dev, else -1
  for (int i = 0; i < remoteHeldCount; i++)
    if (remoteHeld[i].dev.equalsIgnoreCase(dev)) return i;
  return -1;
}
static void removeRemoteHoldsFor(const String& gw) {
  int w = 0;
  for (int i = 0; i < remoteHeldCount; i++)
    if (remoteHeld[i].gw != gw) remoteHeld[w++] = remoteHeld[i];
  remoteHeldCount = w;
}
static void pruneRemoteHeld(unsigned long now) {
  int w = 0;
  for (int i = 0; i < remoteHeldCount; i++)
    if (now - remoteHeld[i].ts <= REMOTE_HOLD_TTL_MS) remoteHeld[w++] = remoteHeld[i];
  remoteHeldCount = w;
}

// Refresh what peer gateway `gw` holds, parsed from its `report` payload's "connected" array.
static void handleRemoteReport(const String& gw, const String& payload) {
  unsigned long now = millis();
  removeRemoteHoldsFor(gw);
  int start = payload.indexOf("\"connected\":[");
  if (start < 0) return;
  int end = payload.indexOf(']', start);
  if (end < 0) return;
  int i = start;
  while (i < end && remoteHeldCount < REMOTE_HOLD_MAX) {
    int d = payload.indexOf("\"dev\":\"", i);
    if (d < 0 || d >= end) break;
    d += 7;
    int de = payload.indexOf('"', d);
    if (de < 0 || de > end) break;
    String dev = payload.substring(d, de);
    int rssi = -127;  // default weak; also covers "rssi":null
    int r = payload.indexOf("\"rssi\":", de);
    if (r >= 0 && r < end && payload.charAt(r + 7) != 'n')
      rssi = payload.substring(r + 7, payload.indexOf('}', r)).toInt();
    remoteHeld[remoteHeldCount++] = RemoteHold{dev, gw, rssi, now};
    i = payload.indexOf('}', de);
    if (i < 0) break;
    i++;
  }
}

// ---- topic helpers ---------------------------------------------------------
static String hrTopic(const String& dev)     { return "cuddle/" + g_gwid + "/hr/" + dev; }
static String statusTopic(const String& dev) { return "cuddle/" + g_gwid + "/status/" + dev; }
static String onlineTopic()                  { return "cuddle/" + g_gwid + "/online"; }
static String cmdTopic()                     { return "cuddle/" + g_gwid + "/cmd"; }
static String reportTopic()                  { return "cuddle/" + g_gwid + "/report"; }
// Orchestrator control topics are global (not gateway-scoped).
static const char* MODE_TOPIC   = "cuddle/control/mode";
static const char* ONLINE_TOPIC = "cuddle/control/online";

// ---- BLE callbacks (run in the NimBLE host task) ---------------------------
static void notifyCB(NimBLERemoteCharacteristic* chr, uint8_t* data, size_t len, bool isNotify) {
  GwEvent e{};
  e.kind = EVT_HR;
  e.len = (uint8_t)min(len, sizeof(e.data));
  memcpy(e.data, data, e.len);
  std::string a = chr->getRemoteService()->getClient()->getPeerAddress().toString();
  strncpy(e.addr, a.c_str(), sizeof(e.addr) - 1);
  xQueueSend(evtQueue, &e, 0);
}

class ClientCB : public NimBLEClientCallbacks {
  void onConnect(NimBLEClient* c) override {
    GwEvent e{};
    e.kind = EVT_CONNECTED;
    std::string a = c->getPeerAddress().toString();
    strncpy(e.addr, a.c_str(), sizeof(e.addr) - 1);
    e.rssi = c->getRssi();
    xQueueSend(evtQueue, &e, 0);
  }
  void onDisconnect(NimBLEClient* c, int reason) override {
    GwEvent e{};
    e.kind = EVT_DISCONNECTED;
    std::string a = c->getPeerAddress().toString();
    strncpy(e.addr, a.c_str(), sizeof(e.addr) - 1);
    xQueueSend(evtQueue, &e, 0);
  }
};
static ClientCB clientCB;

class ScanCB : public NimBLEScanCallbacks {
  void onResult(const NimBLEAdvertisedDevice* dev) override {
    if (!dev->isAdvertisingService(NimBLEUUID(HR_SERVICE))) return;
    String a = dev->getAddress().toString().c_str();
    uint8_t type = dev->getAddress().getType();  // preserve public/random
    int rssi = dev->getRSSI();

    // Marshal the sighting to loop() so the scan cache (String/array) is only
    // ever touched from the task that owns it — same reasoning as the MQTT
    // marshalling elsewhere in this file: this callback runs in the NimBLE
    // host task, not loop()'s task.
    GwEvent e{};
    e.kind = EVT_SEEN;
    strncpy(e.addr, a.c_str(), sizeof(e.addr) - 1);
    e.addrType = type;
    e.rssi = rssi;
    xQueueSend(evtQueue, &e, 0);

    if (effectiveManaged()) return;              // managed: cache only, no auto-connect
    if (isHeld(a)) return;                       // already connected/in-flight
    if (heldCount >= MAX_CONNECTIONS) return;    // at capacity
    ConnectReq req{};
    strncpy(req.addr, a.c_str(), sizeof(req.addr) - 1);
    req.type = type;
    Serial.printf("BLE: HR band advertised %s (type %d, rssi %d) -> queueing connect\n",
                  req.addr, req.type, rssi);
    xQueueSend(connectQueue, &req, 0);           // let loop() do the connect
  }
};

// ---- connection (called from loop(), not from a callback) ------------------
static void connectTo(const char* addrStr, uint8_t type) {
  String addr(addrStr);
  if (isHeld(addr) || heldCount >= MAX_CONNECTIONS) return;
  int rh = remoteHolderOf(addr);
  if (rh >= 0 && remoteHeld[rh].gw != g_gwid) {  // a peer already holds it — don't double-connect
    Serial.printf("BLE: %s already held by %s — not connecting\n", addrStr, remoteHeld[rh].gw.c_str());
    return;
  }
  addHeld(addr);  // reserve the slot up-front so scan doesn't double-queue

  NimBLEDevice::getScan()->stop();  // don't scan while establishing a link
  NimBLEClient* c = NimBLEDevice::createClient();
  c->setClientCallbacks(&clientCB, false);
  // Connect with the preserved address type (public vs random) — critical for Coospo.
  if (!c->connect(NimBLEAddress(std::string(addrStr), type))) {
    Serial.printf("BLE: connect FAILED %s (type %d)\n", addrStr, type);
    NimBLEDevice::deleteClient(c);
    removeHeld(addr);
    return;
  }
  setHeldClient(addr, c);  // so cmd:release / report can find this peer by address
  NimBLERemoteService* svc = c->getService(HR_SERVICE);
  NimBLERemoteCharacteristic* chr = svc ? svc->getCharacteristic(HR_MEASUREMENT) : nullptr;
  if (!chr || !chr->canNotify() || !chr->subscribe(true, notifyCB)) {
    Serial.printf("BLE: subscribe FAILED %s\n", addrStr);
    c->disconnect();  // onDisconnect will clean up the slot
    return;
  }
  Serial.printf("BLE: subscribed to %s\n", addrStr);
  // connected + subscribed; onConnect already emitted the status event.
}

// ---- cmd:release ------------------------------------------------------------
static void releaseDevice(const String& dev) {
  NimBLEClient* c = getHeldClient(dev);
  if (!c) {
    Serial.printf("cmd release: %s not currently held, ignoring\n", dev.c_str());
    return;
  }
  Serial.printf("cmd release: disconnecting %s\n", dev.c_str());
  c->disconnect();  // onDisconnect (EVT_DISCONNECTED) does the held[] cleanup
}

// Drop any band we hold that a stronger peer also holds. Both gateways run this; the loser
// (weaker RSSI, ties broken by lower gateway id) releases, so exactly one keeps the band.
static void resolveDuplicateHolds() {
  for (int i = heldCount - 1; i >= 0; i--) {
    int rh = remoteHolderOf(held[i].addr);
    if (rh < 0 || remoteHeld[rh].gw == g_gwid) continue;
    bool theyWin = (remoteHeld[rh].rssi > held[i].rssi) ||
                   (remoteHeld[rh].rssi == held[i].rssi && remoteHeld[rh].gw < g_gwid);
    if (theyWin) {
      Serial.printf("dedup: %s also held by %s (rssi %d vs ours %d) — releasing ours\n",
                    held[i].addr.c_str(), remoteHeld[rh].gw.c_str(), remoteHeld[rh].rssi, held[i].rssi);
      releaseDevice(held[i].addr);
    }
  }
}

// Tiny hand-rolled extractor for {"action":"...","dev":"..."} — matches the
// rest of this file's approach to JSON (String concatenation, no library).
static String jsonExtract(const String& json, const String& key) {
  String pat = "\"" + key + "\":\"";
  int idx = json.indexOf(pat);
  if (idx < 0) return String();
  idx += pat.length();
  int end = json.indexOf('"', idx);
  if (end < 0) return String();
  return json.substring(idx, end);
}

// ---- config + provisioning -------------------------------------------------

// Last 24 bits of the chip's factory MAC as hex — a stable per-board suffix so a fleet
// flashed from ONE image is auto-unique (MQTT topics key on <gw>, so two identically-named
// gateways would collide on the broker).
static String macSuffix() {
  char buf[8];
  snprintf(buf, sizeof(buf), "%06lx", (unsigned long)(ESP.getEfuseMac() & 0xFFFFFF));
  return String(buf);
}

static void loadConfig() {
  prefs.begin("gwcfg", false);
  g_broker = prefs.getString("broker", MQTT_BROKER);
  g_port   = prefs.getInt("port", MQTT_PORT);
  // Default id = GATEWAY_ID + per-chip MAC suffix (auto-unique across a fleet). A name set
  // explicitly via the portal (persisted in NVS under "gwid") is used verbatim.
  g_gwid   = prefs.getString("gwid", String(GATEWAY_ID) + "-" + macSuffix());
}

static void saveConfig() {
  prefs.putString("broker", g_broker);
  prefs.putInt("port", g_port);
  prefs.putString("gwid", g_gwid);
}

// ---- status LED (onboard RGB, kept intentionally dim) ----------------------
// One addressable NeoPixel — RGB_BUILTIN is GPIO48 on the generic esp32s3 variant
// (confirm on your board; clones vary / some have no RGB). Encodes link + mode +
// load on a single LED, so the states are prioritized (see updateLed() below setup):
//   yellow = Wi-Fi connecting/reconnecting   orange = Wi-Fi up but MQTT down
//   green  = online, opportunistic mode      teal   = online, managed mode (blue accent)
//   (green/teal brighten with the number of connected bands)
//   blue   = captive portal open (set from provision()'s AP callback)
// Channels are capped at LED_MAX on purpose — this is a desk indicator, not a beacon.
static const uint8_t LED_MAX = 24;  // per-channel ceiling; keep it dim

static void setLed(uint8_t r, uint8_t g, uint8_t b) {
  static uint8_t lr = 1, lg = 1, lb = 1;      // impossible triple => first call always writes
  if (r == lr && g == lg && b == lb) return;  // only rewrite on change (no flicker / wasted bit-bangs)
  lr = r; lg = g; lb = b;
  rgbLedWrite(RGB_BUILTIN, r, g, b);
}

// Join saved Wi-Fi, or raise a captive portal (SoftAP + web form) to be provisioned
// from a phone. Hold BOOT (GPIO0) at reset to force the portal and change settings.
static void provision() {
  pinMode(BOOT_BUTTON, INPUT_PULLUP);
#ifdef FORCE_PORTAL
  bool forcePortal = true;  // build-time override for testing the portal without the button
#else
  bool forcePortal = (digitalRead(BOOT_BUTTON) == LOW);
#endif

  // Fleet-friendly: if compile-time Wi-Fi creds are baked in (secrets.h) and the portal
  // isn't forced, try them directly first, so a freshly-flashed gateway joins with no
  // captive portal. Falls through to WiFiManager (saved creds / portal) if it doesn't connect.
#if defined(WIFI_SSID)
  if (!forcePortal && strlen(WIFI_SSID) > 0) {
    setLed(LED_MAX, LED_MAX * 2 / 3, 0);  // yellow while connecting
    Serial.printf("Wi-Fi: trying compile-time creds for '%s'...\n", WIFI_SSID);
    WiFi.mode(WIFI_STA);
    WiFi.begin(WIFI_SSID, WIFI_PASS);
    for (int i = 0; i < 24 && WiFi.status() != WL_CONNECTED; i++) delay(500);  // ~12s
    if (WiFi.status() == WL_CONNECTED) {
      WiFi.setAutoReconnect(true);
      Serial.printf("Wi-Fi ok %s (compile-time creds) | broker %s:%d | gateway %s\n",
                    WiFi.localIP().toString().c_str(), g_broker.c_str(), g_port, g_gwid.c_str());
      return;  // g_broker/g_port/g_gwid already loaded by loadConfig()
    }
    Serial.println("Wi-Fi: compile-time creds didn't connect; falling back to WiFiManager.");
  }
#endif

  WiFiManager wm;
  // The portal blocks here (no loop() yet), so light the LED blue when the AP opens.
  wm.setAPCallback([](WiFiManager*) { setLed(0, 0, LED_MAX); });
  char portStr[8];
  snprintf(portStr, sizeof(portStr), "%d", g_port);
  WiFiManagerParameter p_broker("broker", "MQTT broker (host/IP)", g_broker.c_str(), 40);
  WiFiManagerParameter p_port("port", "MQTT port", portStr, 6);
  WiFiManagerParameter p_gwid("gwid", "Gateway ID", g_gwid.c_str(), 24);
  wm.addParameter(&p_broker);
  wm.addParameter(&p_port);
  wm.addParameter(&p_gwid);
  wm.setConfigPortalTimeout(180);  // seconds to wait in the portal before giving up

  bool ok;
  if (forcePortal) {
    Serial.println("BOOT held -> opening config portal 'Cuddle-Gateway-Setup'");
    ok = wm.startConfigPortal("Cuddle-Gateway-Setup");
  } else {
    Serial.println("Wi-Fi: joining saved network (portal 'Cuddle-Gateway-Setup' if none)...");
    ok = wm.autoConnect("Cuddle-Gateway-Setup");
  }
  if (!ok) {
    Serial.println("provisioning timed out, not connected — restarting");
    delay(1000);
    ESP.restart();
  }

  // Persist any values entered in the portal (unchanged fields keep their defaults).
  g_broker = p_broker.getValue();
  g_port   = atoi(p_port.getValue());
  g_gwid   = p_gwid.getValue();
  saveConfig();
  WiFi.setAutoReconnect(true);
  Serial.printf("Wi-Fi ok %s | broker %s:%d | gateway %s\n",
                WiFi.localIP().toString().c_str(), g_broker.c_str(), g_port, g_gwid.c_str());
}

// ---- MQTT ------------------------------------------------------------------
// Handles cmd (connect/release) and the two control topics. Runs from
// mqtt.loop() on the main task — the same task that owns the MQTT client and
// calls connectTo() from the connectQueue drain — so it's safe to call
// connectTo()/PubSubClient directly here (see the file-header note on
// threading: only BLE callbacks are barred from touching PubSubClient).
static void mqttCallback(char* topic, uint8_t* payload, unsigned int length) {
  String t(topic);
  String p;
  p.reserve(length);
  for (unsigned int i = 0; i < length; i++) p += (char)payload[i];

  if (t == cmdTopic()) {
    String action = jsonExtract(p, "action");
    String dev = jsonExtract(p, "dev");
    if (action == "connect") {
      int idx = findScanCacheIndex(dev);
      if (idx >= 0) {
        connectTo(scanCache[idx].addr.c_str(), scanCache[idx].type);
      } else {
        Serial.printf("cmd connect: %s not in scan cache, ignoring\n", dev.c_str());
      }
    } else if (action == "release") {
      releaseDevice(dev);
    } else {
      Serial.printf("cmd: unknown action '%s'\n", action.c_str());
    }
  } else if (t == MODE_TOPIC) {
    g_configuredManaged = (p == "managed");
    Serial.printf("control/mode -> %s\n", g_configuredManaged ? "managed" : "opportunistic");
  } else if (t == ONLINE_TOPIC) {
    bool nowOnline = (p == "1");
    if (nowOnline && !g_online) {
      g_online = true;
      Serial.println("control/online -> 1 (orchestrator online)");
    } else if (!nowOnline && g_online) {
      g_online = false;
      g_offlineSince = millis();  // transition-based: only (re)start the grace clock here
      Serial.println("control/online -> 0 (orchestrator offline, grace timer started)");
    }
    // Repeats of the current state are a no-op — control/online is published
    // once, retained, not a heartbeat, so re-arriving "1" (e.g. re-delivered
    // on our own reconnect) must never reset anything.
  } else if (t.startsWith("cuddle/") && t.endsWith("/report")) {
    String gw = t.substring(7, t.length() - 7);  // "cuddle/<gw>/report" -> <gw>
    if (gw != g_gwid) handleRemoteReport(gw, p);  // learn what peers hold (dedup)
  }
}

static unsigned long lastMqttTry = 0;
static void ensureMqtt() {
  if (mqtt.connected()) return;
  if (lastMqttTry != 0 && millis() - lastMqttTry < 2000) return;  // throttle retries
  lastMqttTry = millis();
  String will = onlineTopic();
  String clientId = "cuddle-gw-" + g_gwid;
  Serial.printf("MQTT: connecting to %s:%d ...", g_broker.c_str(), g_port);
  if (mqtt.connect(clientId.c_str(), nullptr, nullptr, will.c_str(), 1, true, "0")) {
    Serial.println(" ok");
    mqtt.publish(will.c_str(), (const uint8_t*)"1", 1, true);  // online, retained
    mqtt.subscribe(cmdTopic().c_str());
    mqtt.subscribe(MODE_TOPIC);
    mqtt.subscribe(ONLINE_TOPIC);
    mqtt.subscribe("cuddle/+/report");  // peers' reports -> multi-gateway dedup
  } else {
    Serial.printf(" failed rc=%d\n", mqtt.state());
  }
}

// ---- publish drained events ------------------------------------------------
static void publishEvent(const GwEvent& e) {
  String dev(e.addr);
  switch (e.kind) {
    case EVT_HR:
      mqtt.publish(hrTopic(dev).c_str(), e.data, e.len, false);
      break;
    case EVT_CONNECTED: {
      setHeldRssi(dev, e.rssi);
      String payload = String("{\"event\":\"connected\",\"rssi\":") + e.rssi + "}";
      mqtt.publish(statusTopic(dev).c_str(), payload.c_str(), false);
      Serial.printf("BLE connected: %s (rssi %d)\n", e.addr, e.rssi);
      break;
    }
    case EVT_DISCONNECTED:
      mqtt.publish(statusTopic(dev).c_str(), "{\"event\":\"disconnected\"}", false);
      removeHeld(dev);
      Serial.printf("BLE disconnected: %s\n", e.addr);
      break;
    case EVT_SEEN:
      break;  // handled in loop()'s drain, before publishEvent() is called
  }
}

// ---- report ------------------------------------------------------------
// Retained cuddle/<gw>/report, published on change plus a ~2s heartbeat (see
// maybePublishReport). `ts` is excluded from the change comparison — it ticks
// every call, so comparing the full string (ts included) would defeat "on
// change" and spam a publish every loop().
static String g_lastReportBody;
static unsigned long g_lastReportTime = 0;
static const unsigned long REPORT_HEARTBEAT_MS = 2000;

// buildReportBody() emits at most MAX_CONNECTIONS "connected" entries plus
// SCAN_CACHE_MAX "seen" entries (seen already excludes held addrs, so this
// bound is conservative, not tight). Each entry is
// `{"dev":"aa:bb:cc:dd:ee:ff","rssi":-100},` <= 41 bytes; 48 bytes/entry
// leaves headroom. With the current build (MAX_CONNECTIONS=6,
// SCAN_CACHE_MAX=24) that's 512 + 30*48 = 1952 bytes for the report alone,
// plus room for the {"capacity":...,"mode":...,"ts":...} wrapper and the
// MQTT fixed/variable header + topic ("cuddle/<gwid>/report"). PubSubClient
// silently drops (publish() returns false, sends nothing) any payload that
// doesn't fit the buffer, so this must comfortably exceed the worst case.
#define MQTT_BUF_SIZE (512 + (MAX_CONNECTIONS + SCAN_CACHE_MAX) * 48)

static String buildReportBody() {
  String s = "{\"capacity\":" + String(MAX_CONNECTIONS) +
             ",\"mode\":\"" + (effectiveManaged() ? "managed" : "opportunistic") + "\"" +
             ",\"connected\":[";
  for (int i = 0; i < heldCount; i++) {
    if (i > 0) s += ",";
    s += "{\"dev\":\"" + held[i].addr + "\",\"rssi\":";
    s += (held[i].rssi == RSSI_UNSET ? String("null") : String(held[i].rssi));
    s += "}";
  }
  s += "],\"seen\":[";
  bool first = true;
  for (int i = 0; i < scanCount; i++) {
    if (isHeld(scanCache[i].addr)) continue;  // seen list excludes the currently-connected
    if (!first) s += ",";
    first = false;
    s += "{\"dev\":\"" + scanCache[i].addr + "\",\"rssi\":" + String(scanCache[i].rssi) + "}";
  }
  s += "]";
  return s;
}

static void maybePublishReport() {
  if (!mqtt.connected()) return;
  String body = buildReportBody();
  unsigned long now = millis();
  bool changed = (body != g_lastReportBody);
  bool heartbeatDue = (now - g_lastReportTime >= REPORT_HEARTBEAT_MS);
  if (!changed && !heartbeatDue) return;
  String full = body + ",\"ts\":" + String(now) + "}";
  if (!mqtt.publish(reportTopic().c_str(), full.c_str(), true)) {  // retained
    // PubSubClient sends nothing (not a truncated packet) when the payload
    // exceeds its buffer. Log so an overflow is diagnosable instead of a
    // silent stale/missing report, and don't mark it as sent — retry next
    // loop() with the same body rather than losing this update.
    Serial.printf("MQTT: report publish FAILED (payload %u bytes, buffer %d bytes)\n",
                  (unsigned)full.length(), MQTT_BUF_SIZE);
    return;
  }
  g_lastReportBody = body;
  g_lastReportTime = now;
}

// Derive the status color from current link/mode/load. Called from the top of loop()
// (which early-returns while Wi-Fi is down), on the main task — never a BLE callback.
static void updateLed() {
  if (WiFi.status() != WL_CONNECTED) { setLed(LED_MAX, LED_MAX * 2 / 3, 0); return; }  // yellow
  if (!mqtt.connected())             { setLed(LED_MAX, LED_MAX / 3, 0); return; }       // orange
  uint8_t g = 4 + (uint8_t)heldCount * 3;     // dim green idle, brighter with load
  if (g > LED_MAX) g = LED_MAX;
  setLed(0, g, effectiveManaged() ? 8 : 0);   // teal accent distinguishes managed mode
}

// ---- setup / loop ----------------------------------------------------------
void setup() {
  Serial.begin(115200);
  delay(200);
  setLed(LED_MAX, LED_MAX * 2 / 3, 0);  // yellow: booting / joining Wi-Fi
  Serial.printf("\nCuddle Currents gateway (max %d bands)\n", MAX_CONNECTIONS);

  evtQueue = xQueueCreate(64, sizeof(GwEvent));
  connectQueue = xQueueCreate(16, sizeof(ConnectReq));

  loadConfig();
  provision();  // Wi-Fi via saved creds or captive portal; loads broker/gateway config

  mqtt.setServer(g_broker.c_str(), g_port);
  mqtt.setCallback(mqttCallback);
  mqtt.setBufferSize(MQTT_BUF_SIZE);  // sized for the full report[] worst case; see MQTT_BUF_SIZE
  ensureMqtt();

  NimBLEDevice::init(g_gwid.c_str());
  NimBLEDevice::setPower(9);  // +9 dBm (2.x takes dBm, not the ESP_PWR_LVL_* enum)
  NimBLEScan* scan = NimBLEDevice::getScan();
  // wantDuplicates=TRUE is required for managed mode: the controller's duplicate filter
  // (with dup-cache refresh period 0) would otherwise report each band only ONCE, so a
  // continuously-advertising band drops out of `seen` after SEEN_TTL and can't be re-added
  // until the scan restarts — leaving the orchestrator nothing to place. Reporting every
  // advertisement keeps `seen`/`last_seen` continuously fresh.
  scan->setScanCallbacks(new ScanCB(), /*wantDuplicates=*/true);
  scan->setActiveScan(true);
  scan->setDuplicateFilter(false);  // belt-and-suspenders with wantDuplicates above
  scan->setInterval(100);
  scan->setWindow(80);
  scan->start(0, false);  // continuous background scan (2.x: duration, isContinue)
  Serial.println("BLE scanning for 0x180D...");
}

void loop() {
  updateLed();  // first, so the LED still updates during the Wi-Fi-down early-return below
  if (WiFi.status() != WL_CONNECTED) { WiFi.reconnect(); delay(500); return; }
  ensureMqtt();
  mqtt.loop();

  // Perform any queued connects (kept out of the scan callback).
  ConnectReq req;
  while (xQueueReceive(connectQueue, &req, 0) == pdTRUE) {
    connectTo(req.addr, req.type);
  }

  // Drain and publish BLE events. EVT_SEEN just updates the scan cache and is
  // handled here, before the mqtt.connected() gate, so the cache stays warm
  // even while MQTT is down.
  GwEvent e;
  while (xQueueReceive(evtQueue, &e, 0) == pdTRUE) {
    if (e.kind == EVT_SEEN) {
      touchScanCache(String(e.addr), e.addrType, e.rssi);
      continue;
    }
    if (mqtt.connected()) publishEvent(e);
  }
  pruneScanCache();
  maybePublishReport();

  // Multi-gateway dedup, ~1 Hz (kept off the per-iteration hot path): expire stale peer
  // holds, then drop any band a stronger peer also holds so exactly one gateway keeps it.
  static unsigned long lastDedup = 0;
  if (millis() - lastDedup > 1000) {
    lastDedup = millis();
    pruneRemoteHeld(millis());
    resolveDuplicateHolds();
  }

  // Keep scanning while we have spare capacity. In managed mode, scanning
  // stays on regardless of capacity — it's what keeps report.seen fresh for
  // the orchestrator, since ScanCB never auto-connects there.
  bool wantScan = effectiveManaged() || heldCount < MAX_CONNECTIONS;
  if (wantScan && !NimBLEDevice::getScan()->isScanning()) {
    NimBLEDevice::getScan()->start(0, false);
  }

  delay(10);
}
