#!/bin/sh
# Stops both background jobs and removes them; keeps nothing running.
for label in com.turzx-screen.display com.turzx-screen.collector; do
    launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || true
    rm -f "$HOME/Library/LaunchAgents/$label.plist"
done
rm -rf "$HOME/Library/Application Support/turzx-screen"
echo "Removed. (Build cache left at ~/.cache/turzx-screen — delete it if you like.)"
