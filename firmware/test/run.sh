#!/usr/bin/env bash
# Host-side firmware tests — no ESP toolchain, no board required.
#
# Covers the captive-portal field rules that decide what gets written to NVS, by
# compiling the real firmware header on the host. Everything else in the firmware
# (BLE, MQTT, OTA) still needs `idf.py build` and a flashed board.
set -euo pipefail
cd "$(dirname "$0")"

IDF_HDR=../gateway-idf/main/portal_fields.h
INO_HDR=../gateway/portal_fields.h

# arduino-cli only compiles headers inside the sketch dir, so portal_fields.h exists
# in both build trees. Fail loudly if they drift apart.
if ! cmp -s "$IDF_HDR" "$INO_HDR"; then
  echo "FAIL: portal_fields.h copies differ — re-sync them:"
  echo "  cp $IDF_HDR $INO_HDR"
  diff -u "$IDF_HDR" "$INO_HDR" || true
  exit 1
fi
echo "portal_fields.h copies in sync"

out=$(mktemp -d)/test_portal_fields
g++ -std=c++17 -Wall -Wextra -Werror -o "$out" test_portal_fields.cpp
"$out"
