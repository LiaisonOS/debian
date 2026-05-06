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

@app.after_request
def no_cache(r):
    r.headers['Cache-Control'] = 'no-store'
    return r

# Paths
ET_CONFIG_FILE = Path.home() / ".config" / "emcomm-tools" / "user.json"
ET_SYNCHING_CONFIG = Path.home() / ".config" / "emcomm-tools" / "bt-synching.json"
RADIOS_CONF_DIR = Path("/opt/emcomm-tools/conf/radios.d")
GPS_NOTIFY_SOCK = "/tmp/et-gps-notify.sock"
RFCOMM_DEV = "/dev/rfcomm0"
ET_GPS_LINK = "/dev/et-gps"
SERVICE_NAME = "LiaisonGPS"
PORT = 5053

# Global rfcomm process — kept alive in GPS mode, killed on cleanup
_rfcomm_proc         = None
_gpsd_wrapper_proc   = None   # wrapper-gpsd.sh start — killed in cleanup
_gps_active          = False  # guard against double-cleanup

# ============================================================================
# Helpers
# ============================================================================

def _notify_gps(msg: str):
    """Send GPS state notification to dashboard (best-effort, silent if dashboard not running)."""
    import socket as _socket
    s = _socket.socket(_socket.AF_UNIX, _socket.SOCK_DGRAM)
    try:
        s.sendto(msg.encode(), GPS_NOTIFY_SOCK)
    except OSError:
        pass
    finally:
        s.close()

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
    """Bind rfcomm0 to MAC/channel — no background process needed.
    Unlike 'rfcomm connect', bind uses RELEASE_ONHUP without REUSE_DLC.
    The actual BT connection is initiated by gpsd opening /dev/rfcomm0.
    When gpsd stops and closes the fd, the slot auto-releases cleanly."""
    r = subprocess.run(
        ['sudo', 'rfcomm', 'bind', '0', mac, str(channel)],
        capture_output=True
    )
    print(f"[GPS] rfcomm bind: rc={r.returncode} {r.stderr.decode().strip()}")
    return None


def rfcomm_disconnect():
    """Release rfcomm0."""
    r = subprocess.run(['sudo', 'rfcomm', 'release', '0'], capture_output=True)
    print(f"[GPS] rfcomm release: rc={r.returncode} {r.stderr.decode().strip()}")


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
    global _gps_active, _gpsd_wrapper_proc
    _gps_active = True
    subprocess.run(
        ['sudo', 'ln', '-sf', RFCOMM_DEV, ET_GPS_LINK],
        capture_output=True
    )
    _gpsd_wrapper_proc = subprocess.Popen(
        ['/opt/emcomm-tools/sbin/wrapper-gpsd.sh', 'start'],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    threading.Thread(target=rfcomm_watcher, daemon=True).start()
    threading.Thread(target=_gps_pulse_loop, daemon=True).start()


def rfcomm_is_connected():
    """Return True only if rfcomm show 0 output contains 'connected'.
    After Android closes: output shows 'closed' — this returns False."""
    try:
        r = subprocess.run(['rfcomm', 'show', '0'],
                           capture_output=True, text=True, timeout=3)
        result = 'connected' in r.stdout.lower()
        print(f"[GPS] rfcomm show 0: {r.stdout.strip()} → {'UP' if result else 'DOWN'}")
        return result
    except Exception as e:
        print(f"[GPS] rfcomm show error: {e}")
        return False


def rfcomm_watcher():
    """Poll rfcomm show 0 every 2s. 'closed' in output = Android disconnected."""
    _wait = threading.Event()
    # Wait up to 30s for connection to become active
    for _ in range(30):
        if rfcomm_is_connected():
            break
        _wait.wait(timeout=1)

    print("[GPS] rfcomm0 active — watching for disconnect...")
    _notify_gps("gps:start")

    while _gps_active and rfcomm_is_connected():
        _wait.wait(timeout=2)

    if _gps_active:
        print("[GPS] rfcomm0 closed — Android disconnected. Running cleanup...")
        gps_cleanup()


def _gps_pulse_loop():
    """Every 10s check if gpsd is receiving NMEA sentences.
    Send gps:running if data flowing, gps:warn if not."""
    _wait = threading.Event()
    while _gps_active:
        try:
            import json as _json
            from datetime import datetime, timezone
            r = subprocess.run(
                ['gpspipe', '-w', '-n', '10'],
                capture_output=True, text=True, timeout=8
            )
            has_data = False
            for line in r.stdout.splitlines():
                try:
                    obj = _json.loads(line)
                    if obj.get('class') == 'TPV' and 'time' in obj:
                        fix_time = datetime.fromisoformat(obj['time'].replace('Z', '+00:00'))
                        age = (datetime.now(timezone.utc) - fix_time).total_seconds()
                        if age < 30:
                            has_data = True
                        break
                except Exception:
                    continue
        except Exception:
            has_data = False
        if has_data:
            _notify_gps("gps:running")
        else:
            _notify_gps("gps:warn")
        _wait.wait(timeout=10)


# ============================================================================
# Routes
# ============================================================================

@app.route('/')
def index():
    """Step 1 — Instructions page, skipped if user has connected before.
    If GPS is already active, go straight to the GPS active screen."""
    if _gps_active:
        return redirect('/gps-active')
    cfg = load_synching_config()
    if cfg.get('last_mac'):
        return redirect('/select')
    return render_template('instructions.html')


@app.route('/gps-active')
def gps_active_page():
    """Resume view — shown when GPS is already running in background."""
    if not _gps_active:
        return redirect('/')
    return render_template('gps_active.html')


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

    def sse_done(payload):
        return f"data: {json.dumps(payload)}\n\n"

    def generate():
        if not mac:
            yield sse_done({'done': True, 'success': False, 'error': 'No device selected'})
            return

        # Save MAC for next time
        save_synching_config({'last_mac': mac})

        # Step 1: SDP lookup
        yield sse({'step': 'sdp', 'msg': f'Looking up GPS service on {mac}...'})
        channel, service_name = sdp_find_channel(mac)
        if channel is None:
            yield sse_done({'done': True, 'success': False,
                       'error': f'No GPS service found on {mac}. Is LiaisonGPS running, or GPS output enabled on your HT?'})
            return
        yield sse({'step': 'sdp', 'msg': f'Found {service_name} on channel {channel}'})

        # Step 2: rfcomm connect
        yield sse({'step': 'rfcomm', 'msg': f'Connecting to {mac} on channel {channel}...'})
        global _rfcomm_proc
        rfcomm_proc = rfcomm_connect(mac, channel)
        _rfcomm_proc = rfcomm_proc

        # Wait for /dev/rfcomm0 to appear (up to 15s)
        for _ in range(30):
            if os.path.exists(RFCOMM_DEV):
                break
            time.sleep(0.5)
        else:
            if rfcomm_proc: rfcomm_proc.terminate()
            yield sse_done({'done': True, 'success': False,
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
                if rfcomm_proc: rfcomm_proc.terminate()
                yield sse_done({'done': True, 'success': False, 'error': 'Cannot open GPS device'})
                return
            elif proc.returncode == 2:
                if rfcomm_proc: rfcomm_proc.terminate()
                yield sse_done({'done': True, 'success': False,
                           'error': 'No valid GPS fix received — make sure the phone has GPS signal'})
                return
            elif proc.returncode == 3:
                yield sse({'step': 'time', 'msg': 'Warning: could not set system time (sudo required)'})
        except Exception as e:
            if rfcomm_proc: rfcomm_proc.terminate()
            yield sse_done({'done': True, 'success': False, 'error': f'GPS sync error: {e}'})
            return

        if lat is None:
            if rfcomm_proc: rfcomm_proc.terminate()
            yield sse_done({'done': True, 'success': False,
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
            yield sse_done({'done': True, 'success': True, 'mode': 'gps',
                       'lat': lat, 'lon': lon, 'grid': grid})
        else:
            # Sync only — disconnect
            if rfcomm_proc: rfcomm_proc.terminate()
            rfcomm_disconnect()
            yield sse_done({'done': True, 'success': True, 'mode': 'sync',
                       'lat': lat, 'lon': lon, 'grid': grid, 'time': dt_str})

    return Response(generate(), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


def gps_cleanup():
    """Stop GPS continuous mode cleanly.
    rfcomm bind (used at connect time) sets RELEASE_ONHUP without REUSE_DLC,
    so rfcomm release 0 works cleanly after gpsd stops — no hciconfig needed."""
    global _rfcomm_proc, _gpsd_wrapper_proc, _gps_active
    if not _gps_active:
        print("[GPS] cleanup() called but already inactive — skipping")
        return
    _gps_active = False

    print("[GPS] cleanup: step 1 — kill wrapper-gpsd.sh")
    if _gpsd_wrapper_proc is not None:
        try:
            _gpsd_wrapper_proc.terminate()
            _gpsd_wrapper_proc.wait(timeout=2)
        except Exception:
            pass
        _gpsd_wrapper_proc = None
    subprocess.run(['sudo', 'pkill', '-KILL', '-f', 'wrapper-gpsd.sh'], capture_output=True)

    print("[GPS] cleanup: step 2 — stop gpsd + gpsd.socket")
    subprocess.run(['sudo', 'systemctl', 'stop', 'gpsd', 'gpsd.socket'], capture_output=True)
    # Wait for gpsd to fully stop (up to 5s) before releasing rfcomm
    for _ in range(10):
        r = subprocess.run(['systemctl', 'is-active', 'gpsd'], capture_output=True)
        if r.stdout.decode().strip() not in ('active', 'deactivating'):
            break
        time.sleep(0.5)
    else:
        print("[GPS] warning: gpsd did not stop within 5s")

    print("[GPS] cleanup: step 3 — rfcomm release 0 (bind used RELEASE_ONHUP, no reuse-dlc)")
    rfcomm_disconnect()

    print("[GPS] cleanup: step 4 — remove symlink, notify dashboard")
    subprocess.run(['sudo', 'rm', '-f', ET_GPS_LINK], capture_output=True)
    _notify_gps("gps:stop")

    print("[GPS] Cleanup complete.")


@app.route('/api/disconnect', methods=['POST'])
def api_disconnect():
    """Explicitly stop GPS — called when user clicks Stop GPS."""
    def _do_disconnect():
        # Notify dashboard even if _gps_active is False in this process
        # (second window process where _gps_active was never set True)
        _notify_gps("gps:stop")
        gps_cleanup()
    threading.Thread(target=_do_disconnect, daemon=True).start()
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
    import socket as _sock
    _port_in_use = False
    try:
        with _sock.create_connection(('127.0.0.1', PORT), timeout=1):
            _port_in_use = True
    except OSError:
        pass

    if not _port_in_use:
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
            """Called when user closes the window.
            If GPS is active, do NOT clean up — the rfcomm_watcher keeps running
            in the background (process stays alive via the while _gps_active loop)
            and will call gps_cleanup() when Android disconnects."""
            if _gps_active:
                print("[GPS] Window closed with GPS active — staying alive in background.")

        window.events.closing += on_closing
        webview.start()

        # Window closed — if GPS is still active, keep process alive so
        # rfcomm_watcher can detect Android disconnect and run cleanup
        while _gps_active:
            time.sleep(1)

    except ImportError:
        print("PyWebView not installed, falling back to browser.")
        import webbrowser
        webbrowser.open(f'http://127.0.0.1:{PORT}')
        # Keep Flask alive
        flask_thread.join()
