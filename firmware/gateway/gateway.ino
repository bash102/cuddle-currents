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
// Concurrency: NimBLE callbacks run in the BLE host task, but PubSubClient is not
// thread-safe. Callbacks only enqueue events onto a FreeRTOS queue; loop() is the
// sole MQTT publisher.
//
// Config (WiFi creds, broker, gateway id) lives in secrets.h — copy secrets.h.example
// to secrets.h and fill it in. secrets.h is gitignored; never commit credentials.

#include <Arduino.h>
#include <WiFi.h>
#include <PubSubClient.h>
#include <NimBLEDevice.h>
#include "secrets.h"

// Concurrent BLE central links. NimBLE-Arduino's default build ceiling is 3; to raise
// this, also compile with -DCONFIG_BT_NIMBLE_MAX_CONNECTIONS=<n> (see README). This is
// the number the roadmap's hardware-validation step sweeps to find the reliable max.
#ifndef MAX_CONNECTIONS
#define MAX_CONNECTIONS 3
#endif

static const uint16_t HR_SERVICE = 0x180D;
static const uint16_t HR_MEASUREMENT = 0x2A37;

WiFiClient wifiClient;
PubSubClient mqtt(wifiClient);

// ---- cross-task event marshalling ------------------------------------------
enum EvtKind : uint8_t { EVT_HR = 0, EVT_CONNECTED = 1, EVT_DISCONNECTED = 2 };

struct GwEvent {
  EvtKind kind;
  char addr[18];       // "aa:bb:cc:dd:ee:ff"
  uint8_t len;         // HR payload length (EVT_HR)
  uint8_t data[32];    // raw 0x2A37 bytes (EVT_HR)
  int rssi;            // EVT_CONNECTED
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
static String hrTopic(const String& dev)     { return String("cuddle/") + GATEWAY_ID + "/hr/" + dev; }
static String statusTopic(const String& dev) { return String("cuddle/") + GATEWAY_ID + "/status/" + dev; }
static String onlineTopic()                  { return String("cuddle/") + GATEWAY_ID + "/online"; }

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
  void onDisconnect(NimBLEClient* c) override {
    GwEvent e{};
    e.kind = EVT_DISCONNECTED;
    std::string a = c->getPeerAddress().toString();
    strncpy(e.addr, a.c_str(), sizeof(e.addr) - 1);
    xQueueSend(evtQueue, &e, 0);
  }
};
static ClientCB clientCB;

class ScanCB : public NimBLEAdvertisedDeviceCallbacks {
  void onResult(NimBLEAdvertisedDevice* dev) override {
    if (!dev->isAdvertisingService(NimBLEUUID(HR_SERVICE))) return;
    String a = dev->getAddress().toString().c_str();
    if (isHeld(a)) return;                       // already connected/in-flight
    if (heldCount >= MAX_CONNECTIONS) return;    // at capacity
    char buf[18];
    strncpy(buf, a.c_str(), sizeof(buf) - 1);
    buf[17] = 0;
    xQueueSend(connectQueue, buf, 0);            // let loop() do the connect
  }
};

// ---- connection (called from loop(), not from a callback) ------------------
static void connectTo(const String& addr) {
  if (isHeld(addr) || heldCount >= MAX_CONNECTIONS) return;
  addHeld(addr);  // reserve the slot up-front so scan doesn't double-queue

  NimBLEClient* c = NimBLEDevice::createClient();
  c->setClientCallbacks(&clientCB, false);
  if (!c->connect(NimBLEAddress(std::string(addr.c_str())))) {
    NimBLEDevice::deleteClient(c);
    removeHeld(addr);
    return;
  }
  NimBLERemoteService* svc = c->getService(HR_SERVICE);
  NimBLERemoteCharacteristic* chr = svc ? svc->getCharacteristic(HR_MEASUREMENT) : nullptr;
  if (!chr || !chr->canNotify() || !chr->subscribe(true, notifyCB)) {
    c->disconnect();  // onDisconnect will clean up the slot
    return;
  }
  // connected + subscribed; onConnect already emitted the status event.
}

// ---- WiFi / MQTT -----------------------------------------------------------
static void ensureWifi() {
  if (WiFi.status() == WL_CONNECTED) return;
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  Serial.printf("WiFi: connecting to %s", WIFI_SSID);
  uint32_t start = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - start < 20000) {
    delay(250);
    Serial.print(".");
  }
  Serial.println(WiFi.status() == WL_CONNECTED ? String(" ok ") + WiFi.localIP().toString()
                                               : " FAILED");
}

static void ensureMqtt() {
  if (mqtt.connected()) return;
  String will = onlineTopic();
  String clientId = String("cuddle-gw-") + GATEWAY_ID;
  Serial.printf("MQTT: connecting to %s:%d ...", MQTT_BROKER, MQTT_PORT);
  // connect with Last-Will "0" (retained) so a hard drop marks this gateway offline.
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
  Serial.printf("\nCuddle Currents gateway '%s' (max %d bands)\n", GATEWAY_ID, MAX_CONNECTIONS);

  evtQueue = xQueueCreate(64, sizeof(GwEvent));
  connectQueue = xQueueCreate(16, sizeof(char[18]));

  ensureWifi();
  mqtt.setServer(MQTT_BROKER, MQTT_PORT);
  mqtt.setBufferSize(256);
  ensureMqtt();

  NimBLEDevice::init(GATEWAY_ID);
  NimBLEDevice::setPower(ESP_PWR_LVL_P9);
  NimBLEScan* scan = NimBLEDevice::getScan();
  scan->setAdvertisedDeviceCallbacks(new ScanCB(), /*wantDuplicates=*/false);
  scan->setActiveScan(true);
  scan->setInterval(100);
  scan->setWindow(80);
  scan->start(0, nullptr, false);  // continuous background scan
  Serial.println("BLE scanning for 0x180D...");
}

void loop() {
  ensureWifi();
  ensureMqtt();
  mqtt.loop();

  // Perform any queued connects (kept out of the scan callback).
  char addrBuf[18];
  while (xQueueReceive(connectQueue, addrBuf, 0) == pdTRUE) {
    connectTo(String(addrBuf));
  }

  // Drain and publish BLE events.
  GwEvent e;
  while (xQueueReceive(evtQueue, &e, 0) == pdTRUE) {
    if (mqtt.connected()) publishEvent(e);
  }

  // Keep scanning while we have spare capacity.
  if (heldCount < MAX_CONNECTIONS && !NimBLEDevice::getScan()->isScanning()) {
    NimBLEDevice::getScan()->start(0, nullptr, false);
  }

  delay(10);
}
