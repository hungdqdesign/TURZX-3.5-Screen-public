#!/bin/sh
# Builds screen/turing_screen: the dashboard UI (ui/) + LVGL, compiled for macOS.
#   screen/build.sh [out]
# Downloads LVGL and ArduinoJson (pinned tags) into ~/.cache/turzx-screen on first run.
set -e
ROOT=$(cd "$(dirname "$0")/.." && pwd)
UI="$ROOT/ui"; STUB="$ROOT/screen/stub"
DEPS=${TURZX_DEPS:-$HOME/.cache/turzx-screen}   # outside the repo (and outside any synced folder)
OUT=${1:-$ROOT/screen/turing_screen}
LVGL="$DEPS/lvgl"; JSON="$DEPS/ArduinoJson/src"
[ -d "$LVGL" ] || git clone -q --depth 1 --branch v9.5.0 https://github.com/lvgl/lvgl.git "$LVGL"
[ -d "$DEPS/ArduinoJson" ] || git clone -q --depth 1 --branch v7.4.3 https://github.com/bblanchon/ArduinoJson.git "$DEPS/ArduinoJson"
USB=$(brew --prefix libusb 2>/dev/null) || { echo "libusb missing: brew install libusb" >&2; exit 1; }
OBJ="$DEPS/obj"; mkdir -p "$OBJ"
export FLAGS="-DLV_CONF_SKIP -DLV_COLOR_DEPTH=16 -DLV_USE_LOG=0 -DLV_USE_SNAPSHOT=0 -DLV_LVGL_H_INCLUDE_SIMPLE -O2 -w"
export OBJ LVGL
# LVGL and the fonts never change: built once.
if [ ! -f "$OBJ/.lib_done" ]; then
    echo "building LVGL (one time, ~1 min)..."
    { find "$LVGL/src" -name '*.c'; ls "$UI"/font_*.c; } | tr '\n' '\0' | xargs -0 -n1 -P8 sh -c \
        'clang -c $FLAGS -I"$LVGL" -I"$LVGL/src" -I"$0" "$1" -o "$OBJ/lib_$(echo "$1" | md5 -q).o"' "$UI"
    touch "$OBJ/.lib_done"
fi
for f in "$UI/ui.cpp" "$UI/theme.cpp" "$UI/payload.cpp" "$ROOT/screen/turing_screen.cpp"; do
    clang++ -std=c++17 $FLAGS -I"$STUB" -I"$LVGL" -I"$LVGL/src" -I"$UI" -I"$JSON" \
        -I"$USB/include/libusb-1.0" -c "$f" -o "$OBJ/$(basename "$f").o"
done
clang++ "$OBJ"/*.o -L"$USB/lib" -lusb-1.0 -o "$OUT"
echo "built $OUT"
