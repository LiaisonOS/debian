#!/bin/bash
#
# Author  : Gaston Gonzalez
# Date    : 4 October 2024
# Purpose : Wrapper startup/shutdown script around systemd/gpsd 

WAIT=5
GPS_NOTIFY_SOCK="/tmp/et-gps-notify.sock"
PULSE_INTERVAL=15

gps_notify() {
  python3 -c "
import socket, sys
s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
try:
    s.sendto(sys.argv[1].encode(), '$GPS_NOTIFY_SOCK')
except OSError:
    pass
s.close()
" "$1"
}

start() {
  et-log "Waiting to start gpsd for ${WAIT} seconds..."
  sleep ${WAIT}
  /usr/bin/systemctl restart gpsd
  gps_notify "gps:start"
  et-log "GPS notify sent: gps:start"
  # Pulse loop — send gps:running every PULSE_INTERVAL seconds while gpsd is active
  while /usr/bin/systemctl is-active --quiet gpsd; do
    sleep ${PULSE_INTERVAL}
    /usr/bin/systemctl is-active --quiet gpsd && gps_notify "gps:running"
  done
}

stop() {
  # Guard: only stop if /dev/et-gps is actually gone (udev remove fires for all tty devices)
  if [ -e /dev/et-gps ]; then
    return 0
  fi
  et-log "Waiting to stop gpsd for ${WAIT} seconds..."
  sleep ${WAIT}
  /usr/bin/systemctl stop gpsd
  gps_notify "gps:stop"
  et-log "GPS notify sent: gps:stop"
}

usage() {
  echo "usage: $(basename $0) <cmd>"
  echo "  <cmd>  [start|stop]"
}

if [ $# -ne 1 ]; then
  usage
  exit 1
fi

case $1 in
  start)
    start
    ;;
  stop)
    stop
    ;;
  *)
    usage
  ;;
esac
