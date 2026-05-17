#!/bin/bash
#
# Author  : Gaston Gonzalez
# Date    : 4 October 2024
# Purpose : Wrapper startup/shutdown script around systemd/gpsd 

WAIT=5

GPS_FLAG="/tmp/et-gps-connected"

start() {
  et-log "Waiting to start gpsd for ${WAIT} seconds..."
  sleep ${WAIT}
  /usr/bin/systemctl restart gpsd
  touch "${GPS_FLAG}"
  et-log "GPS flag file created: ${GPS_FLAG}"
}

stop() {
  # Guard: only stop if /dev/et-gps is actually gone (udev remove fires for all tty devices)
  if [ -e /dev/et-gps ]; then
    return 0
  fi
  et-log "Waiting to stop gpsd for ${WAIT} seconds..."
  sleep ${WAIT}
  /usr/bin/systemctl stop gpsd
  rm -f "${GPS_FLAG}"
  et-log "GPS flag file removed: ${GPS_FLAG}"
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
