#!/bin/bash
# wrapper-gps-sync.sh — Auto-sync time from USB GPS dongle on plug-in
# Called by udev when /dev/et-gps is created.
# Runs et-gps-sync in background — waits for gpsd to settle first.

GPS_SYNC="/opt/emcomm-tools/bin/et-gps-sync"
GPS_DEV="/dev/et-gps"
LOG="/tmp/et-gps-sync.log"

# Wait for the symlink to be fully established
for i in $(seq 1 10); do
    [ -e "$GPS_DEV" ] && break
    sleep 1
done

[ -e "$GPS_DEV" ] || exit 1

# Stop chrony so it doesn't fight GPS time
systemctl stop chrony 2>/dev/null

# Run sync in background — udev must not block
"$GPS_SYNC" "$GPS_DEV" > "$LOG" 2>&1 &
