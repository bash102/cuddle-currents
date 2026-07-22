#!/usr/bin/env bash
# Activate ESP-IDF for building this project.  Usage:  . activate.sh
#
# ESP-IDF refuses to run from inside another Python virtualenv, and its own
# python_env here was created with the system python3 (3.9). So we drop any
# ".venv" off PATH, put /usr/bin first (system python3), clear VIRTUAL_ENV,
# then source the IDF export script.
export PATH="/usr/bin:$(printf '%s' "$PATH" | tr ':' '\n' | grep -v '/\.venv/' | paste -sd: -)"
unset VIRTUAL_ENV PYTHONHOME
source "${IDF_PATH:-$HOME/esp/esp-idf}/export.sh"
