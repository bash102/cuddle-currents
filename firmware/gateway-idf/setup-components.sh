#!/usr/bin/env bash
# Vendor the two non-registry Arduino libraries into components/ as IDF components.
# arduino-esp32 and esp-nimble-cpp come from the registry (main/idf_component.yml);
# PubSubClient and WiFiManager aren't published there, so we copy them from the
# arduino-cli libraries dir (installed for the ../gateway sketch) and add a CMake
# shim to each. Re-runnable; run once before the first build.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ARDUINO_LIBS="${ARDUINO_LIBS:-$HOME/Documents/Arduino/libraries}"
# The Arduino core, as a managed component, registers under this name (namespace__name).
ARDUINO_COMPONENT="${ARDUINO_COMPONENT:-espressif__arduino-esp32}"
COMP="$HERE/components"
mkdir -p "$COMP"

# ---- PubSubClient (MQTT client) ----------------------------------------
rm -rf "$COMP/PubSubClient"
mkdir -p "$COMP/PubSubClient"
cp -R "$ARDUINO_LIBS/PubSubClient/src" "$COMP/PubSubClient/src"
cat > "$COMP/PubSubClient/CMakeLists.txt" <<EOF
idf_component_register(
    SRCS "src/PubSubClient.cpp"
    INCLUDE_DIRS "src"
    REQUIRES $ARDUINO_COMPONENT
)
# Vendored Arduino code: silence warnings so IDF's -Werror doesn't reject it
# (e.g. %d vs uint32_t format checks that are benign on 32-bit int).
target_compile_options(\${COMPONENT_LIB} PRIVATE -w)
EOF

# ---- WiFiManager (captive-portal provisioning) -------------------------
rm -rf "$COMP/WiFiManager"
mkdir -p "$COMP/WiFiManager/src"
cp "$ARDUINO_LIBS/WiFiManager/"*.cpp "$ARDUINO_LIBS/WiFiManager/"*.h "$COMP/WiFiManager/src/"
cat > "$COMP/WiFiManager/CMakeLists.txt" <<EOF
idf_component_register(
    SRCS "src/WiFiManager.cpp"
    INCLUDE_DIRS "src"
    REQUIRES $ARDUINO_COMPONENT
)
# Vendored Arduino code: silence warnings so IDF's -Werror doesn't reject it
# (e.g. %d vs uint32_t format checks that are benign on 32-bit int).
target_compile_options(\${COMPONENT_LIB} PRIVATE -w)
EOF

echo "Vendored PubSubClient + WiFiManager into components/ (REQUIRES $ARDUINO_COMPONENT)"
