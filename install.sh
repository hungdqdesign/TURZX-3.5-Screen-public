#!/bin/sh
# One-step install on macOS: builds the screen driver, sets up the usage
# collector, and runs both in the background (LaunchAgents, start at login).
#
#   ./install.sh                     # collector + screen, landscape
#   ./install.sh --orientation 3     # picture upside down? use the other landscape
#   ./install.sh --brightness 50     # 0-100, default 70
#   ./install.sh --no-collector      # something else already writes the snapshot
set -e
ROOT=$(cd "$(dirname "$0")" && pwd)
APP="$HOME/Library/Application Support/turzx-screen"
AGENTS="$HOME/Library/LaunchAgents"
LOGS="$HOME/Library/Logs"
UID_=$(id -u)

COLLECTOR=1
SCREEN_ARGS=""
while [ $# -gt 0 ]; do
    case "$1" in
        --no-collector) COLLECTOR=0 ;;
        --orientation|--brightness) SCREEN_ARGS="$SCREEN_ARGS $1 $2"; shift ;;
        *) echo "unknown option: $1" >&2; exit 2 ;;
    esac
    shift
done

[ "$(uname)" = Darwin ] || { echo "macOS only" >&2; exit 1; }
xcode-select -p >/dev/null 2>&1 || { echo "Install the compiler first: xcode-select --install" >&2; exit 1; }
command -v brew >/dev/null || { echo "Install Homebrew first: https://brew.sh" >&2; exit 1; }
brew list libusb >/dev/null 2>&1 || brew install libusb

mkdir -p "$APP" "$AGENTS" "$LOGS"
sh "$ROOT/screen/build.sh" "$APP/turing_screen"

# $1 label, $2 log name, rest: program + args
write_agent() {
    label=$1; log=$2; shift 2
    args=""
    for a in "$@"; do args="$args        <string>$a</string>
"; done
    cat > "$AGENTS/$label.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$label</string>
    <key>ProgramArguments</key>
    <array>
$args    </array>
    <key>WorkingDirectory</key>
    <string>$APP</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>ThrottleInterval</key>
    <integer>10</integer>
    <key>StandardOutPath</key>
    <string>$LOGS/$log.log</string>
    <key>StandardErrorPath</key>
    <string>$LOGS/$log.log</string>
</dict>
</plist>
EOF
    launchctl bootout "gui/$UID_/$label" 2>/dev/null || true
    launchctl bootstrap "gui/$UID_" "$AGENTS/$label.plist"
}

if [ "$COLLECTOR" = 1 ]; then
    cp "$ROOT"/host/*.py "$ROOT/host/requirements.txt" "$APP/"
    [ -x "$APP/venv/bin/python" ] || /usr/bin/python3 -m venv "$APP/venv"
    "$APP/venv/bin/pip" install -q -r "$APP/requirements.txt"
    write_agent com.turzx-screen.collector turzx-collector "$APP/venv/bin/python" "$APP/collector.py"
fi
# shellcheck disable=SC2086
write_agent com.turzx-screen.display turzx-screen "$APP/turing_screen" $SCREEN_ARGS

echo
echo "Installed. Plug the screen into a USB-A port (with a data cable)."
echo "Logs: tail -f ~/Library/Logs/turzx-screen.log ~/Library/Logs/turzx-collector.log"
