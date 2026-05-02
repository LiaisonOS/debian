#!/usr/bin/env python3
"""
et-synching - Bluetooth GPS Sync
Author: Sylvain Deguire (VA2OPS)
Date: April 2026

Standalone Flask microservice for syncing time and GPS position from an
Android device running the LiaisonGPS app over Bluetooth RFCOMM.

Two modes:
  - Sync: connect, read time + coordinates, update OS clock + user.json, disconnect
  - GPS:  stay connected, symlink /dev/et-gps -> /dev/rfcomm0, start gpsd
"""

import warnings
warnings.filterwarnings("ignore", category=DeprecationWarning)
warnings.filterwarnings("ignore", message=".*pkg_resources.*")

import os
import sys
import json
import subprocess
import threading
import time
import serial
import logging
from pathlib import Path
from flask import Flask, render_template, request, jsonify, Response, redirect

log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)

app = Flask(__name__)
app.secret_key = 'emcomm-tools-synching-2026'

# Paths
ET_CONFIG_FILE = Path.home() / ".config" / "emcomm-tools" / "user.json"
ET_SYNCHING_CONFIG = Path.home() / ".config" / "emcomm-tools" / "bt-synching.json"
RADIOS_CONF_DIR = Path("/opt/emcomm-tools/conf/radios.d")
GPS_FLAG = Path("/tmp/et-gps-connected")
RFCOMM_DEV = "/dev/rfcomm0"
ET_GPS_LINK = "/dev/et-gps"
SERVICE_NAME = "LiaisonGPS"
PORT = 5053

# ============================================================================
# Helpers
# ============================================================================

def get_gps_offset(device_name):
    """Look up gps_offset from radio conf files matching the BT device name.
    Returns integer offset in seconds (0 if not found or not set)."""
    try:
        for conf_file in RADIOS_CONF_DIR.glob("*.bt.json"):
            with open(conf_file) as f:
                conf = json.load(f)
            bt = conf.get("bluetooth", {})
            if bt.get("deviceName", "") == device_name:
                return int(bt.get("gps_offset", 0))
    except Exception as e:
        print(f"[CONF] Error reading radio conf: {e}")
    return 0


def load_synching_config():
    """Load saved MAC address from config."""
    if ET_SYNCHING_CONFIG.exists():
        try:
            with open(ET_SYNCHING_CONFIG) as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_synching_config(data):
    """Save MAC address to config."""
    ET_SYNCHING_CONFIG.parent.mkdir(parents=True, exist_ok=True)
    existing = load_synching_config()
    existing.update(data)
    with open(ET_SYNCHING_CONFIG, 'w') as f:
        json.dump(existing, f, indent=2)


def get_paired_devices():
    """Return list of paired BT devices as [{'mac': ..., 'name': ...}]."""
    devices = []
    try:
        result = subprocess.run(
            ['bluetoothctl', 'devices', 'Paired'],
            capture_output=True, text=True, timeout=10
        )
        for line in result.stdout.splitlines():
            # Format: "Device AA:BB:CC:DD:EE:FF Device Name"
            parts = line.strip().split(' ', 2)
            if len(parts) >= 3 and parts[0] == 'Device':
                devices.append({'mac': parts[1], 'name': parts[2]})
    except Exception as e:
        print(f"[BT] Error listing devices: {e}")
    return devices


def sdp_find_channel(mac):
    """Browse SDP records for mac, return (channel, service_name) or (None, None).
    Matches LiaisonGPS (Android app) or Serial Port (Kenwood HT GPS output).
    """
    # Priority order: LiaisonGPS first, Serial Port as fallback
    SERVICE_NAMES = ['LiaisonGPS', 'Serial Port']
    try:
        result = subprocess.run(
            ['sdptool', 'browse', mac],
            capture_output=True, text=True, timeout=15
        )
        output = result.stdout
        # Collect all (service_name, channel) pairs
        found = {}
        in_service = None
        for line in output.splitlines():
            for name in SERVICE_NAMES:
                if name in line:
                    in_service = name
            if in_service and 'Channel:' in line:
                ch = int(line.strip().split()[-1])
                if in_service not in found:
                    found[in_service] = ch
                in_service = None
        # Return highest priority match
        for name in SERVICE_NAMES:
            if name in found:
                return found[name], name
        return None, None
    except Exception as e:
        print(f"[SDP] Error: {e}")
        return None, None


def rfcomm_connect(mac, channel):
    """Start rfcomm connect in background. Returns Popen process."""
    return subprocess.Popen(
        ['sudo', 'rfcomm', 'connect', '0', mac, str(channel)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )


def rfcomm_disconnect():
    """Release rfcomm0."""
    subprocess.run(['sudo', 'rfcomm', 'release', '0'],
                   capture_output=True)


def latlon_to_grid(lat, lon):
    """Convert lat/lon to 6-char Maidenhead grid square."""
    lon = lon + 180
    lat = lat + 90
    field_lon = int(lon / 20)
    field_lat = int(lat / 10)
    square_lon = int((lon - field_lon * 20) / 2)
    square_lat = int(lat - field_lat * 10)
    sub_lon = int((lon - field_lon * 20 - square_lon * 2) / (2 / 24))
    sub_lat = int((lat - field_lat * 10 - square_lat) / (1 / 24))
    return (chr(ord('A') + field_lon) + chr(ord('A') + field_lat) +
            str(square_lon) + str(square_lat) +
            chr(ord('a') + sub_lon) + chr(ord('a') + sub_lat))


def parse_nmea_gga(sentence):
    """Parse $GPGGA or $GNGGA, return (lat, lon) or (None, None)."""
    try:
        parts = sentence.split(',')
        if len(parts) < 6:
            return None, None
        lat_raw = parts[2]
        lat_dir = parts[3]
        lon_raw = parts[4]
        lon_dir = parts[5]
        if not lat_raw or not lon_raw:
            return None, None
        lat_deg = float(lat_raw[:2]) + float(lat_raw[2:]) / 60
        lon_deg = float(lon_raw[:3]) + float(lon_raw[3:]) / 60
        if lat_dir == 'S':
            lat_deg = -lat_deg
        if lon_dir == 'W':
            lon_deg = -lon_deg
        return lat_deg, lon_deg
    except Exception:
        return None, None


def parse_nmea_rmc(sentence):
    """Parse $GPRMC or $GNRMC, return (lat, lon, utc_time_str) or (None, None, None)."""
    try:
        parts = sentence.split(',')
        if len(parts) < 10 or parts[2] != 'A':
            return None, None, None
        utc_str = parts[1]     # HHMMSS.ss
        lat_raw = parts[3]
        lat_dir = parts[4]
        lon_raw = parts[5]
        lon_dir = parts[6]
        date_str = parts[9]    # DDMMYY
        if not lat_raw or not lon_raw:
            return None, None, None
        lat_deg = float(lat_raw[:2]) + float(lat_raw[2:]) / 60
        lon_deg = float(lon_raw[:3]) + float(lon_raw[3:]) / 60
        if lat_dir == 'S':
            lat_deg = -lat_deg
        if lon_dir == 'W':
            lon_deg = -lon_deg
        # Build date string: DD/MM/YYYYHH:MM:SS
        hh = utc_str[0:2]
        mm = utc_str[2:4]
        ss = utc_str[4:6]
        dd = date_str[0:2]
        mo = date_str[2:4]
        yy = date_str[4:6]
        dt_str = f"20{yy}-{mo}-{dd} {hh}:{mm}:{ss}"
        return lat_deg, lon_deg, dt_str
    except Exception:
        return None, None, None


def update_user_json(lat, lon):
    """Write lat, lon, grid to user.json."""
    grid = latlon_to_grid(lat, lon)
    try:
        config = {}
        if ET_CONFIG_FILE.exists():
            with open(ET_CONFIG_FILE) as f:
                config = json.load(f)
        config['grid'] = grid
        config['latitude'] = str(round(lat, 6))
        config['longitude'] = str(round(lon, 6))
        ET_CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(ET_CONFIG_FILE, 'w') as f:
            json.dump(config, f, indent=2)
        return grid
    except Exception as e:
        print(f"[SYNC] Error updating user.json: {e}")
        return None


def set_system_time(dt_str, receive_time):
    """Set OS clock compensating for time elapsed since NMEA sentence was received.

    dt_str:       GPS time parsed from NMEA (YYYY-MM-DD HH:MM:SS)
    receive_time: time.time() captured the instant readline() returned

    By the time we call date -s, some milliseconds have passed since we
    received the sentence. We add that elapsed time so the clock lands
    exactly on the GPS second.
    """
    try:
        from datetime import datetime, timedelta
        dt = datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S")
        elapsed_ms = (time.time() - receive_time) * 1000
        dt = dt + timedelta(milliseconds=elapsed_ms)
        adjusted = dt.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        print(f"[SYNC] GPS time: {dt_str}, elapsed: {elapsed_ms:.0f}ms, setting: {adjusted}")
        subprocess.run(
            ['sudo', 'date', '-s', adjusted],
            capture_output=True, check=True
        )
        subprocess.run(['sudo', 'hwclock', '--systohc'], capture_output=True)
        return True
    except Exception as e:
        print(f"[SYNC] Error setting time: {e}")
        return False


def setup_gps_mode():
    """Symlink /dev/et-gps -> /dev/rfcomm0 and start gpsd."""
    subprocess.run(
        ['sudo', 'ln', '-sf', RFCOMM_DEV, ET_GPS_LINK],
        capture_output=True
    )
    subprocess.Popen(
        ['/opt/emcomm-tools/sbin/wrapper-gpsd.sh', 'start'],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    # Start watchdog to detect when Android stops streaming
    threading.Thread(target=gps_watchdog, daemon=True).start()


def gps_watchdog():
    """Watch for rfcomm0 disappearing — clean up when Android stops."""
    print("[GPS] Watchdog started — monitoring /dev/rfcomm0")
    # Wait until rfcomm0 exists first
    for _ in range(30):
        if os.path.exists(RFCOMM_DEV):
            break
        time.sleep(1)

    # Now watch for it to disappear
    while os.path.exists(RFCOMM_DEV):
        time.sleep(2)

    print("[GPS] rfcomm0 gone — Android stopped. Cleaning up.")
    gps_cleanup()


# ============================================================================
# Routes
# ============================================================================

@app.route('/')
def index():
    """Step 1 — Instructions page, skipped if user has connected before."""
    cfg = load_synching_config()
    if cfg.get('last_mac'):
        return redirect('/select')
    return render_template('instructions.html')


@app.route('/instructions')
def instructions():
    """Instructions page — always accessible via back button."""
    return render_template('instructions.html')


@app.route('/select')
def select():
    """Step 2 — Device selection page."""
    devices = get_paired_devices()
    cfg = load_synching_config()
    last_mac = cfg.get('last_mac', '')
    return render_template('select.html', devices=devices, last_mac=last_mac)


@app.route('/action')
def action():
    """Step 3 — Action page (SSE driven)."""
    mac = request.args.get('mac', '')
    mode = request.args.get('mode', 'sync')
    return render_template('action.html', mac=mac, mode=mode)


@app.route('/api/connect/stream')
def api_connect_stream():
    """Step 3 — SSE stream: SDP lookup, rfcomm connect, sync or GPS mode."""
    mac = request.args.get('mac', '').strip()
    mode = request.args.get('mode', 'sync')  # 'sync' or 'gps'

    def sse(payload):
        return f"data: {json.dumps(payload)}\n\n"

    def generate():
        if not mac:
            yield sse({'done': True, 'success': False, 'error': 'No device selected'})
            return

        # Save MAC for next time
        save_synching_config({'last_mac': mac})

        # Step 1: SDP lookup
        yield sse({'step': 'sdp', 'msg': f'Looking up GPS service on {mac}...'})
        channel, service_name = sdp_find_channel(mac)
        if channel is None:
            yield sse({'done': True, 'success': False,
                       'error': f'No GPS service found on {mac}. Is LiaisonGPS running, or GPS output enabled on your HT?'})
            return
        yield sse({'step': 'sdp', 'msg': f'Found {service_name} on channel {channel}'})

        # Step 2: rfcomm connect
        yield sse({'step': 'rfcomm', 'msg': f'Connecting to {mac} on channel {channel}...'})
        rfcomm_proc = rfcomm_connect(mac, channel)

        # Wait for /dev/rfcomm0 to appear (up to 15s)
        for _ in range(30):
            if os.path.exists(RFCOMM_DEV):
                break
            time.sleep(0.5)
        else:
            rfcomm_proc.terminate()
            yield sse({'done': True, 'success': False,
                       'error': 'rfcomm0 did not appear — check Bluetooth connection'})
            return
        yield sse({'step': 'rfcomm', 'msg': 'RFCOMM connected — reading GPS data...'})

        # Step 3: Run et-gps-sync (C binary) — handles NMEA reading and settimeofday
        # Stop chrony first so it does not fight our GPS time
        subprocess.run(['sudo', 'systemctl', 'stop', 'chrony'], capture_output=True)
        yield sse({'step': 'time', 'msg': 'Time sync service stopped'})

        # Look up GPS offset from radio conf if this is an HT (Serial Port service)
        gps_offset = 0
        if service_name == 'Serial Port':
            device_name = next((d['name'] for d in get_paired_devices() if d['mac'] == mac), '')
            gps_offset = get_gps_offset(device_name)
            if gps_offset != 0:
                yield sse({'step': 'time', 'msg': f'GPS offset for {device_name}: {gps_offset:+d}s'})

        # Binary outputs: STATUS:, FIX: lat lon grid, TIME: ..., or ERROR: ...
        GPS_SYNC_BIN = '/opt/emcomm-tools/bin/et-gps-sync'
        lat = lon = grid = dt_str = None
        cmd = ['sudo', GPS_SYNC_BIN, RFCOMM_DEV]
        if gps_offset != 0:
            cmd.append(str(gps_offset))
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True
            )
            for line in proc.stdout:
                line = line.rstrip()
                print(f"[GPS-SYNC] {line}")
                if line.startswith('STATUS:'):
                    yield sse({'step': 'nmea', 'msg': line[7:].strip()})
                elif line.startswith('FIX:'):
                    # FIX: lat lon grid
                    parts = line.split()
                    if len(parts) >= 4:
                        lat = float(parts[1])
                        lon = float(parts[2])
                        grid = parts[3]
                        yield sse({'step': 'nmea', 'msg': f'Position: {lat:.5f}, {lon:.5f} ({grid})'})
                elif line.startswith('TIME:'):
                    # TIME: YYYY-MM-DD HH:MM:SS UTC +Xus elapsed
                    dt_str = line[6:25].strip()  # "YYYY-MM-DD HH:MM:SS"
                    yield sse({'step': 'time', 'msg': f'System time set: {dt_str} UTC'})
                elif line.startswith('ERROR:'):
                    yield sse({'step': 'error', 'msg': line[6:].strip()})
            proc.wait()
            if proc.returncode == 1:
                rfcomm_proc.terminate()
                yield sse({'done': True, 'success': False, 'error': 'Cannot open GPS device'})
                return
            elif proc.returncode == 2:
                rfcomm_proc.terminate()
                yield sse({'done': True, 'success': False,
                           'error': 'No valid GPS fix received — make sure the phone has GPS signal'})
                return
            elif proc.returncode == 3:
                yield sse({'step': 'time', 'msg': 'Warning: could not set system time (sudo required)'})
        except Exception as e:
            rfcomm_proc.terminate()
            yield sse({'done': True, 'success': False, 'error': f'GPS sync error: {e}'})
            return

        if lat is None:
            rfcomm_proc.terminate()
            yield sse({'done': True, 'success': False,
                       'error': 'No valid GPS fix received — make sure the phone has GPS signal'})
            return

        # Step 4: Update user.json
        if grid is None:
            grid = update_user_json(lat, lon)
        else:
            update_user_json(lat, lon)
        yield sse({'step': 'grid', 'msg': f'Grid updated: {grid}'})

        # Step 6: GPS mode or disconnect
        if mode == 'gps':
            setup_gps_mode()
            yield sse({'step': 'gps', 'msg': 'GPS mode active — /dev/et-gps linked, gpsd started'})
            yield sse({'done': True, 'success': True, 'mode': 'gps',
                       'lat': lat, 'lon': lon, 'grid': grid})
        else:
            # Sync only — disconnect
            rfcomm_proc.terminate()
            time.sleep(1)
            rfcomm_disconnect()
            yield sse({'done': True, 'success': True, 'mode': 'sync',
                       'lat': lat, 'lon': lon, 'grid': grid, 'time': dt_str})

    return Response(generate(), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


def gps_cleanup():
    """Stop gpsd, remove symlink, disconnect BT and stop reconnect loop."""
    subprocess.run(['sudo', 'rm', '-f', ET_GPS_LINK], capture_output=True)
    subprocess.run(
        ['/opt/emcomm-tools/sbin/wrapper-gpsd.sh', 'stop'],
        capture_output=True
    )
    GPS_FLAG.unlink(missing_ok=True)
    rfcomm_disconnect()
    # Tell bluetoothd to disconnect — stops the OS reconnect loop
    cfg = load_synching_config()
    mac = cfg.get('last_mac', '')
    if mac:
        subprocess.run(['bluetoothctl', 'disconnect', mac], capture_output=True)
    print("[GPS] Cleanup complete.")


@app.route('/api/disconnect', methods=['POST'])
def api_disconnect():
    """Explicitly stop GPS — called when user closes the window."""
    threading.Thread(target=gps_cleanup, daemon=True).start()
    return jsonify({'ok': True})


@app.route('/api/quit', methods=['POST'])
def api_quit():
    """Close the window only — Flask and rfcomm keep running in GPS mode."""
    return jsonify({'ok': True})


class WindowApi:
    """Exposed to JavaScript via pywebview for window control."""
    def __init__(self):
        self._window = None

    def set_window(self, window):
        self._window = window

    def close(self):
        if self._window:
            self._window.destroy()


def run_flask(port):
    app.run(host='127.0.0.1', port=port, debug=False)


# ============================================================================
# Main
# ============================================================================

def wait_for_port(port, timeout=15):
    """Wait until Flask is accepting connections."""
    import socket
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(('127.0.0.1', port), timeout=1):
                return True
        except OSError:
            time.sleep(0.2)
    return False


if __name__ == '__main__':
    flask_thread = threading.Thread(target=run_flask, args=(PORT,), daemon=True)
    flask_thread.start()
    wait_for_port(PORT)

    try:
        import webview

        win_w, win_h = 660, 750

        win_api = WindowApi()
        window = webview.create_window(
            'Bluetooth GPS Sync — LiaisonOS',
            f'http://127.0.0.1:{PORT}',
            width=win_w,
            height=win_h,
            resizable=True,
            min_size=(480, 500),
            frameless=False,
            js_api=win_api
        )
        win_api.set_window(window)

        def on_closing():
            """Called when user closes the window — clean up GPS if active."""
            if GPS_FLAG.exists():
                print("[GPS] Window closed with GPS active — cleaning up...")
                threading.Thread(target=gps_cleanup, daemon=True).start()

        window.events.closing += on_closing
        webview.start()

    except ImportError:
        print("PyWebView not installed, falling back to browser.")
        import webbrowser
        webbrowser.open(f'http://127.0.0.1:{PORT}')
        # Keep Flask alive
        flask_thread.join()
