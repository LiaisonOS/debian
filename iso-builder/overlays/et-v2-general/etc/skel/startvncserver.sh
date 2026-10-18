#!/usr/bin/env bash
# startvncserver.sh — one-click launcher for the LiaisonOS VNC server.
# Thin wrapper around 'et-vnc': installs if needed (normally already there),
# configures the password on first use (from your Winlink profile), then starts.
# Re-run anytime — if it's already set up, it just starts the server.
#
#   ./startvncserver.sh          start (reuse saved password)
#   ./startvncserver.sh --ask    set a new VNC password, then start
#   ./startvncserver.sh --stop   stop the server
ETVNC="$(command -v et-vnc 2>/dev/null || echo /opt/emcomm-tools/bin/et-vnc)"
exec "$ETVNC" "$@"
