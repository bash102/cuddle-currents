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
enum EvtKind : uint8_t { EVT_HR = 0, EVT_CONNECTED = 1, EVT_DISCONNECTED = 2 };

struct GwEvent {
  EvtKind kind;
  char addr[18];       // "aa:bb:cc:dd:ee:ff"
  uint8_t len;         // HR payload length (EVT_HR)
  uint8_t data[32];    // raw 0x2A37 bytes (EVT_HR)
  int rssi;            // EVT_CONNECTED
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

// Slots of currently-held / in-flight connections, tracked by address string.
static String heldAddrs[MAX_CONNECTIONS];
static int heldCount = 0;

static bool isHeld(const String& a) {
  for (int i = 0; i < heldCount; i++) if (heldAddrs[i] == a) return true;
  return false;
}
static void addHeld(const String& a) {
  if (heldCount < MAX_CONNECTIONS && !isHeld(a)) heldAddrs[heldCount++] = a;
}
static void removeHeld(const String& a) {
  for (int i = 0; i < heldCount; i++) {
    if (heldAddrs[i] == a) {
      heldAddrs[i] = heldAddrs[--heldCount];
      heldAddrs[heldCount] = String();
      return;
    }
  }
}

// ---- topic helpers ---------------------------------------------------------
static String hrTopic(const String& dev)     { return "cuddle/" + g_gwid + "/hr/" + dev; }
static String statusTopic(const String& dev) { return "cuddle/" + g_gwid + "/status/" + dev; }
static String onlineTopic()                  { return "cuddle/" + g_gwid + "/online"; }

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
    if (isHeld(a)) return;                       // already connected/in-flight
    if (heldCount >= MAX_CONNECTIONS) return;    // at capacity
    ConnectReq req{};
    strncpy(req.addr, a.c_str(), sizeof(req.addr) - 1);
    req.type = dev->getAddress().getType();      // preserve public/random
    Serial.printf("BLE: HR band advertised %s (type %d, rssi %d) -> queueing connect\n",
                  req.addr, req.type, dev->getRSSI());
    xQueueSend(connectQueue, &req, 0);           // let loop() do the connect
  }
};

// ---- connection (called from loop(), not from a callback) ------------------
static void connectTo(const char* addrStr, uint8_t type) {
  String addr(addrStr);
  if (isHeld(addr) || heldCount >= MAX_CONNECTIONS) return;
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

// ---- config + provisioning -------------------------------------------------
static void loadConfig() {
  prefs.begin("gwcfg", false);
  g_broker = prefs.getString("broker", MQTT_BROKER);
  g_port   = prefs.getInt("port", MQTT_PORT);
  g_gwid   = prefs.getString("gwid", GATEWAY_ID);
}

static void saveConfig() {
  prefs.putString("broker", g_broker);
  prefs.putInt("port", g_port);
  prefs.putString("gwid", g_gwid);
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

  WiFiManager wm;
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
  }
}

// ---- setup / loop ----------------------------------------------------------
void setup() {
  Serial.begin(115200);
  delay(200);
  Serial.printf("\nCuddle Currents gateway (max %d bands)\n", MAX_CONNECTIONS);

  evtQueue = xQueueCreate(64, sizeof(GwEvent));
  connectQueue = xQueueCreate(16, sizeof(ConnectReq));

  loadConfig();
  provision();  // Wi-Fi via saved creds or captive portal; loads broker/gateway config

  mqtt.setServer(g_broker.c_str(), g_port);
  mqtt.setBufferSize(256);
  ensureMqtt();

  NimBLEDevice::init(g_gwid.c_str());
  NimBLEDevice::setPower(9);  // +9 dBm (2.x takes dBm, not the ESP_PWR_LVL_* enum)
  NimBLEScan* scan = NimBLEDevice::getScan();
  scan->setScanCallbacks(new ScanCB(), /*wantDuplicates=*/false);
  scan->setActiveScan(true);
  scan->setInterval(100);
  scan->setWindow(80);
  scan->start(0, false);  // continuous background scan (2.x: duration, isContinue)
  Serial.println("BLE scanning for 0x180D...");
}

void loop() {
  if (WiFi.status() != WL_CONNECTED) { WiFi.reconnect(); delay(500); return; }
  ensureMqtt();
  mqtt.loop();

  // Perform any queued connects (kept out of the scan callback).
  ConnectReq req;
  while (xQueueReceive(connectQueue, &req, 0) == pdTRUE) {
    connectTo(req.addr, req.type);
  }

  // Drain and publish BLE events.
  GwEvent e;
  while (xQueueReceive(evtQueue, &e, 0) == pdTRUE) {
    if (mqtt.connected()) publishEvent(e);
  }

  // Keep scanning while we have spare capacity.
  if (heldCount < MAX_CONNECTIONS && !NimBLEDevice::getScan()->isScanning()) {
    NimBLEDevice::getScan()->start(0, false);
  }

  delay(10);
}
