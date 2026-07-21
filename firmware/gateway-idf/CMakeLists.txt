# ESP-IDF project for the cuddle-currents BLE->WiFi gateway.
# This exists (vs the arduino-cli sketch in ../gateway) purely to raise the BLE
# concurrent-connection ceiling past 3 — set in sdkconfig.defaults, which the Arduino
# toolchain's precompiled libraries can't override. Same firmware logic (main/main.cpp).
cmake_minimum_required(VERSION 3.16)
set(EXTRA_COMPONENT_DIRS "${CMAKE_CURRENT_LIST_DIR}/components")
include($ENV{IDF_PATH}/tools/cmake/project.cmake)
# esp-nimble-cpp must see ARDUINO_ARCH_ESP32 globally (the arduino component defines it
# only for its own sources). Without it, esp-nimble-cpp lets the Arduino core RELEASE the
# BT controller memory, then NimBLEDevice::init() corrupts the heap and panics in
# esp_bt_controller_init (LoadProhibited in tlsf). Per h2zero/esp-nimble-cpp docs.
add_compile_definitions(ARDUINO_ARCH_ESP32=1)
project(cuddle-gateway)
