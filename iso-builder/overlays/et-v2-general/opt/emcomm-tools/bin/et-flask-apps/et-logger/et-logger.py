#!/usr/bin/env python3
"""
et-logger - LiaisonOS POTA/SOTA Field Logger
Date: March 2026

Flask-based QSO logger with POTA/SOTA support.
Features: radio auto-detect, callsign lookup, GPS position,
nearest park finder, map view, ADIF export.
Fully offline — no internet needed in the field.
"""

import warnings
warnings.filterwarnings("ignore", category=DeprecationWarning)
warnings.filterwarnings("ignore", message=".*pkg_resources.*")

import os
import sys
import csv
import io
import json
import re
import signal
import sqlite3
import subprocess
import wave
import webbrowser
import threading
import time
import logging
import math
import urllib.request
import urllib.parse
import xml.etree.ElementTree as ET
from pathlib import Path
from datetime import datetime, timezone
from flask import Flask, render_template, request, jsonify, Response, send_file

# Antenna profiles + NEC-driven "best antenna for a target" — imports the
# sibling modules in this directory. PyNEC is loaded lazily inside the NEC
# module so importing this here doesn't crash if PyNEC isn't installed on
# the machine (routes catch and 503 when NEC actually gets called).
try:
    import antenna_service
    ANTENNA_SERVICE_OK = True
except ImportError as _e:
    antenna_service = None
    ANTENNA_SERVICE_OK = False
    _antenna_import_error = str(_e)

# CQ WW live-score engine (cty.dat parser + scorer). Non-fatal if the
# module or cty.dat is missing — the score endpoints just report unavailable.
try:
    import cqww_scorer
    CQWW_SCORER_OK = True
except ImportError:
    cqww_scorer = None
    CQWW_SCORER_OK = False

# Suppress Flask dev server warning
log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)

# Anchor the working directory to this script's own folder BEFORE creating the
# Flask app. Flask() calls os.getcwd() to locate its root/instance path; if the
# process was launched from a directory that has since disappeared (e.g. a USB
# that unmounted), getcwd() raises "OSError: [Errno 5] Input/output error" and
# the app dies before it starts. Our own folder is always present, so chdir
# here makes startup independent of where et-logger was launched from.
try:
    _APP_DIR = os.path.dirname(os.path.abspath(__file__))
    os.chdir(_APP_DIR)
except OSError:
    try:
        os.chdir(os.path.expanduser('~'))
    except OSError:
        pass

app = Flask(__name__)
app.secret_key = 'emcomm-tools-logger-2026'


@app.context_processor
def _inject_asset_version():
    """Cache-busting token for static assets.

    The kiosk webview can't be hard-refreshed and caches /static/ by URL —
    including an empty/failed response grabbed mid-restart, which shows as an
    unstyled page. Appending ?v=<mtime> makes the URL change whenever the CSS
    or JS changes, forcing a fresh fetch.
    """
    v = 0
    for rel in ("css/logger.css", "js/app.js"):
        try:
            m = int(os.path.getmtime(os.path.join(app.static_folder, rel)))
            if m > v:
                v = m
        except OSError:
            pass
    return {"asset_v": v}

# ============================================================================
# Configuration paths
# ============================================================================

ET_HOME = Path("/opt/emcomm-tools")
ET_CONFIG_DIR = Path.home() / ".config" / "liaisonos"
ET_CONFIG_FILE = ET_CONFIG_DIR / "user.json"
LOGGER_DIR = ET_CONFIG_DIR / "logger"
DB_FILE = LOGGER_DIR / "qso.db"
POTA_PARKS_FILE = LOGGER_DIR / "pota-parks.csv"
SOTA_SUMMITS_FILE = LOGGER_DIR / "sota-summits.csv"

# Callsign databases — search multiple known locations
_DATA_PATHS = [
    Path("/opt/emcomm-tools-api/data"),
    ET_HOME / "data",
    Path("/opt/emcomm-tools/conf/data"),
]

def _find_data_file(name):
    for p in _DATA_PATHS:
        f = p / name
        if f.exists():
            return f
    return None

LICENSE_US = _find_data_file("license.csv")
LICENSE_CA = _find_data_file("license-ca.csv")
ZIP2GEO = _find_data_file("zip2geo.csv")


# ============================================================================
# User Config & Position
# ============================================================================

def load_user_config():
    """Load user configuration."""
    if ET_CONFIG_FILE.exists():
        try:
            with open(ET_CONFIG_FILE, 'r') as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def get_gps_position():
    """Read current GPS position from gpspipe. Returns (lat, lon) or (None, None)."""
    try:
        result = subprocess.run(
            ['gpspipe', '-w', '-n', '5'],
            capture_output=True, text=True, timeout=5)
        for line in result.stdout.strip().split('\n'):
            if '"lat"' in line and '"lon"' in line:
                data = json.loads(line)
                lat = data.get('lat')
                lon = data.get('lon')
                if lat is not None and lon is not None:
                    return float(lat), float(lon)
    except Exception:
        pass
    return None, None


def get_position():
    """Get position from GPS first, then user.json fallback."""
    lat, lon = get_gps_position()
    if lat is not None:
        return lat, lon, 'gps'

    config = load_user_config()
    lat = config.get('latitude', '')
    lon = config.get('longitude', '')
    if lat and lon:
        try:
            return float(lat), float(lon), 'config'
        except (ValueError, TypeError):
            pass

    grid = config.get('grid', '')
    if grid and len(grid) >= 4:
        coords = grid_to_latlon(grid)
        if coords:
            return coords[0], coords[1], 'grid'

    return None, None, None


def grid_to_latlon(grid):
    """Convert Maidenhead grid square to (lat, lon)."""
    grid = grid.strip().upper()
    if len(grid) < 4:
        return None
    lon = (ord(grid[0]) - ord("A")) * 20 - 180
    lat = (ord(grid[1]) - ord("A")) * 10 - 90
    lon += int(grid[2]) * 2
    lat += int(grid[3]) * 1
    if len(grid) >= 6:
        lon += (ord(grid[4]) - ord("A")) * (2.0 / 24) + 1.0 / 24
        lat += (ord(grid[5]) - ord("A")) * (1.0 / 24) + 0.5 / 24
    else:
        lon += 1
        lat += 0.5
    return (round(lat, 6), round(lon, 6))


def latlon_to_grid(lat, lon):
    """Convert lat/lon to 6-char Maidenhead grid."""
    lon = lon + 180
    lat = lat + 90
    field_lon = int(lon / 20)
    field_lat = int(lat / 10)
    square_lon = int((lon - field_lon * 20) / 2)
    square_lat = int((lat - field_lat * 10) / 1)
    sub_lon = int((lon - field_lon * 20 - square_lon * 2) / (2 / 24))
    sub_lat = int((lat - field_lat * 10 - square_lat * 1) / (1 / 24))
    return (chr(ord('A') + field_lon) + chr(ord('A') + field_lat) +
            str(square_lon) + str(square_lat) +
            chr(ord('a') + sub_lon) + chr(ord('a') + sub_lat))


def haversine_km(lat1, lon1, lat2, lon2):
    """Distance between two points in km."""
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2 +
         math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) *
         math.sin(dlon / 2) ** 2)
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


# ============================================================================
# Radio Integration (via shared rig client)
# ============================================================================

# Import shared rig client
sys.path.insert(0, str(ET_HOME / "lib"))
try:
    from et_supervisor.rig_client import rig
except ImportError:
    rig = None


def get_radio_info():
    """Get radio state from shared rig client (cached, no subprocess)."""
    if not rig:
        return {'freq': None, 'mode': None, 'band': None, 'power': None, 'connected': False}
    return rig.get_all()


# Start background polling — auto-detects QSY
if rig:
    rig.start_polling(interval=2)


# ============================================================================
# Database
# ============================================================================

def init_db():
    """Initialize SQLite QSO database."""
    LOGGER_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_FILE))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            program TEXT DEFAULT 'POTA',
            my_sig TEXT DEFAULT 'POTA',
            my_sig_info TEXT DEFAULT '',
            my_callsign TEXT DEFAULT '',
            my_grid TEXT DEFAULT '',
            my_lat REAL,
            my_lon REAL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            notes TEXT DEFAULT '',
            contest TEXT DEFAULT '',
            my_exch TEXT DEFAULT ''
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS qsos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id INTEGER NOT NULL,
            qso_date TEXT NOT NULL,
            time_on TEXT NOT NULL,
            call TEXT NOT NULL,
            freq REAL,
            band TEXT,
            mode TEXT,
            rst_sent TEXT DEFAULT '59',
            rst_rcvd TEXT DEFAULT '59',
            tx_pwr REAL,
            gridsquare TEXT,
            name TEXT,
            state TEXT,
            country TEXT,
            sig TEXT DEFAULT '',
            sig_info TEXT DEFAULT '',
            comment TEXT DEFAULT '',
            qso_lat REAL,
            qso_lon REAL,
            exch_rcvd TEXT DEFAULT '',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (session_id) REFERENCES sessions(id)
        )
    """)
    # Our own growing callsign database. Every station we resolve via QRZ (and
    # work) is cached here so future lookups are instant + OFFLINE, and so our
    # DB keeps growing beyond the bundled US/CA license file. Keyed by call.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS worked_callsigns (
            call TEXT PRIMARY KEY,
            name TEXT DEFAULT '',
            grid TEXT DEFAULT '',
            lat REAL,
            lon REAL,
            state TEXT DEFAULT '',
            country TEXT DEFAULT '',
            cqzone TEXT DEFAULT '',
            source TEXT DEFAULT 'qrz',
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    # Antenna profiles + pattern cache — module lives alongside et-logger.py.
    # Import failure is non-fatal so et-logger still starts on machines
    # without PyNEC (dev boxes); the /api/antennas/* routes gate on the flag.
    if ANTENNA_SERVICE_OK:
        antenna_service.init_schema(conn)
    # Additive migrations for pre-existing DBs (contest + exchange fields).
    # New columns are harmless to older code paths that don't reference them.
    for table, col in (("sessions", "contest"), ("sessions", "my_exch"),
                       ("qsos", "exch_rcvd")):
        have = [r[1] for r in conn.execute("PRAGMA table_info(%s)" % table)]
        if col not in have:
            conn.execute("ALTER TABLE %s ADD COLUMN %s TEXT DEFAULT ''" % (table, col))
    conn.commit()
    conn.close()


def get_db():
    """Get database connection."""
    conn = sqlite3.connect(str(DB_FILE))
    conn.row_factory = sqlite3.Row
    return conn


# ============================================================================
# Callsign Lookup
# ============================================================================

# In-memory callsign cache (loaded on first search)
_callsign_cache = {'us': None, 'ca': None, 'zip2geo': None}


def _load_callsign_db():
    """Load callsign databases into memory."""
    if _callsign_cache['us'] is not None:
        return

    _callsign_cache['us'] = {}
    _callsign_cache['ca'] = {}
    _callsign_cache['zip2geo'] = {}


    # Merged license DB: US = CALL|NAME|CITY|ZIP|STATE, CA = CALL|NAME|CITY|POSTAL|PROV|LAT|LON
    if LICENSE_US and LICENSE_US.exists():
        try:
            with open(LICENSE_US, 'r', errors='ignore') as f:
                for line in f:
                    parts = line.strip().split('|')
                    if len(parts) >= 5:
                        entry = {
                            'call': parts[0], 'name': parts[1],
                            'city': parts[2], 'zip': parts[3],
                            'state': parts[4],
                        }
                        # Canadian entries have lat/lon in fields 5,6
                        if len(parts) >= 7 and parts[5]:
                            try:
                                entry['lat'] = float(parts[5])
                                entry['lon'] = float(parts[6])
                            except (ValueError, TypeError):
                                pass
                        _callsign_cache['us'][parts[0].upper()] = entry
        except Exception:
            pass


    # Canadian license: CALL|NAME|CITY|POSTAL|PROV|LAT|LON
    if LICENSE_CA and LICENSE_CA.exists():
        try:
            with open(LICENSE_CA, 'r', errors='ignore') as f:
                for line in f:
                    parts = line.strip().split('|')
                    if len(parts) >= 7:
                        _callsign_cache['ca'][parts[0].upper()] = {
                            'call': parts[0], 'name': parts[1],
                            'city': parts[2], 'state': parts[4],
                            'lat': float(parts[5]) if parts[5] else None,
                            'lon': float(parts[6]) if parts[6] else None,
                        }
        except Exception:
            pass


    # ZIP to geo: ZIP|LAT|LON
    if ZIP2GEO and ZIP2GEO.exists():
        try:
            with open(ZIP2GEO, 'r', errors='ignore') as f:
                for line in f:
                    parts = line.strip().split('|')
                    if len(parts) >= 3:
                        _callsign_cache['zip2geo'][parts[0]] = {
                            'lat': float(parts[1]),
                            'lon': float(parts[2]),
                        }
        except Exception:
            pass


CA_PREFIXES = ('VA', 'VE', 'VO', 'VY', 'CF', 'CG', 'CH', 'CI', 'CJ', 'CK', 'CY', 'CZ')

def _is_canadian_call(call):
    """Check if a callsign is Canadian by prefix."""
    return call[:2] in CA_PREFIXES

def lookup_callsign(call):
    """Look up a callsign in the merged US/CA database."""
    _load_callsign_db()
    call = call.upper().strip()

    # Check merged database (us cache has both US and CA)
    info = _callsign_cache['us'].get(call)
    if not info:
        # Also check ca cache if it was loaded separately
        info = _callsign_cache['ca'].get(call)
        if info:
            return {**info, 'country': 'CA'}

    if info:
        country = 'CA' if _is_canadian_call(call) else 'US'
        result = {**info, 'country': country, 'lat': None, 'lon': None}
        # Canadian entries have lat/lon directly
        if info.get('lat'):
            result['lat'] = info['lat']
            result['lon'] = info['lon']
        else:
            # US entries: get coordinates from zip code
            geo = _callsign_cache['zip2geo'].get(info.get('zip', ''))
            if geo:
                result['lat'] = geo['lat']
                result['lon'] = geo['lon']
        return result

    return None


def search_callsigns(partial, limit=50):
    """Search callsigns by partial match."""
    _load_callsign_db()
    partial = partial.upper().strip()
    if len(partial) < 2:
        return []

    results = []
    for db_name in ('us', 'ca'):
        for call, info in _callsign_cache[db_name].items():
            if call.startswith(partial):
                country = 'CA' if _is_canadian_call(call) else 'US'
                entry = {**info, 'country': country}
                # Add coordinates from zip if not already present
                if not info.get('lat'):
                    geo = _callsign_cache['zip2geo'].get(info.get('zip', ''))
                    if geo:
                        entry['lat'] = geo['lat']
                        entry['lon'] = geo['lon']
                results.append(entry)
                if len(results) >= limit:
                    break
        if len(results) >= limit:
            break
    results.sort(key=lambda x: x.get('call', ''))
    return results


# ============================================================================
# QRZ.com XML lookup (online enrichment — offline-safe)
# ============================================================================
# LiaisonOS is offline-first: the US/CA license DB works with no internet.
# When internet IS available and the operator has set QRZ credentials, we add
# a third lookup tier so calls not in the local DB (DX, etc.) still resolve —
# including the CQ zone, which is gold during CQ WW. Everything here fails
# silently to None when offline / unconfigured so the logger never blocks.

QRZ_CONFIG = LOGGER_DIR / "qrz.json"
QRZ_API = "https://xmldata.qrz.com/xml/current/"

_qrz_state = {"key": None, "ts": 0.0}   # cached session key (QRZ keys last ~24h)
_qrz_cache = {}                          # call -> result dict | None (per process)


def load_qrz_config():
    try:
        with open(QRZ_CONFIG) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_qrz_config(cfg):
    LOGGER_DIR.mkdir(parents=True, exist_ok=True)
    tmp = QRZ_CONFIG.with_suffix(".json.tmp")
    with open(tmp, "w") as f:
        json.dump(cfg, f, indent=2)
    tmp.replace(QRZ_CONFIG)
    try:
        os.chmod(QRZ_CONFIG, 0o600)  # password inside — keep private
    except OSError:
        pass


def _qrz_local(tag):
    """Strip the XML namespace QRZ wraps every tag in."""
    return tag.split("}")[-1].lower()


def _qrz_parse(xml_bytes):
    root = ET.fromstring(xml_bytes)
    out = {}
    for section in root:
        sect = _qrz_local(section.tag)
        d = out.setdefault(sect, {})
        for child in section:
            d[_qrz_local(child.tag)] = (child.text or "").strip()
    return out


def _qrz_login(user, password, timeout=6):
    """Return (session_key, error). Network/parse failure raises."""
    url = QRZ_API + "?" + urllib.parse.urlencode({
        "username": user, "password": password,
        "agent": "liaisonos-etlogger1.0",
    })
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        data = _qrz_parse(resp.read())
    sess = data.get("session", {})
    if sess.get("key"):
        return sess["key"], None
    return None, sess.get("error", "Unknown QRZ login error")


def _qrz_session_key(force=False):
    cfg = load_qrz_config()
    if not (cfg.get("enabled") and cfg.get("user") and cfg.get("password")):
        return None, "QRZ not configured"
    now = time.time()
    if not force and _qrz_state["key"] and (now - _qrz_state["ts"] < 12 * 3600):
        return _qrz_state["key"], None
    key, err = _qrz_login(cfg["user"], cfg["password"])
    if key:
        _qrz_state["key"] = key
        _qrz_state["ts"] = now
    return key, err


def _qrz_fetch(call, key, timeout=6):
    url = QRZ_API + "?" + urllib.parse.urlencode({"s": key, "callsign": call})
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return _qrz_parse(resp.read())


def qrz_lookup(call, timeout=6):
    """Look a call up on QRZ. Returns (result_or_None, status) where status is
    one of: 'found', 'notfound' (QRZ reached, no such call), 'unconfigured',
    'offline' (couldn't reach QRZ), 'error' (bad creds / QRZ error). The status
    lets the caller distinguish a bogus callsign from 'we couldn't check'."""
    call = (call or "").upper().strip()
    if not call:
        return None, "notfound"
    if call in _qrz_cache:
        return _qrz_cache[call]

    out = (None, "offline")
    try:
        key, err = _qrz_session_key()
        if not key:
            out = (None, "unconfigured" if err == "QRZ not configured" else "error")
        else:
            data = _qrz_fetch(call, key, timeout)
            # Session expired mid-use → QRZ returns an error, re-login once.
            if not data.get("callsign") and data.get("session", {}).get("error"):
                key, err = _qrz_session_key(force=True)
                if key:
                    data = _qrz_fetch(call, key, timeout)
            c = data.get("callsign")
            if c:
                name = (c.get("fname", "") + " " + c.get("name", "")).strip()
                try:
                    lat = float(c["lat"]) if c.get("lat") else None
                    lon = float(c["lon"]) if c.get("lon") else None
                except (ValueError, TypeError):
                    lat = lon = None
                out = ({
                    "call": call, "name": name,
                    "city": c.get("addr2", ""), "state": c.get("state", ""),
                    "zip": c.get("zip", ""), "country": c.get("country", ""),
                    "grid": c.get("grid", ""), "cqzone": c.get("cqzone", ""),
                    "lat": lat, "lon": lon, "source": "qrz",
                }, "found")
            else:
                # QRZ answered but has no such callsign → genuinely not found.
                out = (None, "notfound")
    except Exception:
        out = (None, "offline")  # network failure — can't validate, stay silent

    _qrz_cache[call] = out
    return out


def _lookup_session_history(call, session_id):
    """Most recent QSO with this call in the session — reuses the operator's
    own ground truth (name/QTH and, crucially, the CQ zone they entered)."""
    if not session_id:
        return None
    try:
        conn = get_db()
        row = conn.execute(
            "SELECT name, state, country, gridsquare, exch_rcvd FROM qsos "
            "WHERE session_id = ? AND call = ? ORDER BY id DESC LIMIT 1",
            (session_id, call.upper().strip())).fetchone()
        conn.close()
    except Exception:
        return None
    if not row:
        return None
    return {"name": row["name"], "state": row["state"],
            "country": row["country"], "grid": row["gridsquare"],
            "zone": row["exch_rcvd"]}


def lookup_worked_callsign(call):
    """Our own growing DB — a station we've previously resolved + worked.
    Returns a dict (source='mydb') or None."""
    call = (call or "").upper().strip()
    if not call:
        return None
    try:
        conn = get_db()
        row = conn.execute(
            "SELECT call,name,grid,lat,lon,state,country,cqzone "
            "FROM worked_callsigns WHERE call = ?", (call,)).fetchone()
        conn.close()
    except Exception:
        return None
    if not row:
        return None
    return {"call": row["call"], "name": row["name"], "grid": row["grid"],
            "lat": row["lat"], "lon": row["lon"], "state": row["state"],
            "country": row["country"], "cqzone": row["cqzone"],
            "source": "mydb"}


def save_worked_callsign(info, conn=None):
    """Upsert a resolved station into our own growing DB. Only stores entries
    that carry something useful (coords or grid). Derives lat/lon from grid if
    QRZ gave a grid but no coordinates. Safe no-op on failure."""
    if not info:
        return
    call = (info.get("call") or "").upper().strip()
    if not call:
        return
    lat, lon = info.get("lat"), info.get("lon")
    grid = info.get("grid") or ""
    if (lat is None or lon is None) and grid:
        try:
            g = grid_to_latlon(grid)
            if g:
                lat, lon = g
        except (ValueError, IndexError, TypeError):
            pass
    if lat is None and lon is None and not grid:
        return  # nothing worth remembering
    own = conn is None
    try:
        if own:
            conn = get_db()
        conn.execute("""
            INSERT INTO worked_callsigns
                (call,name,grid,lat,lon,state,country,cqzone,source,updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)
            ON CONFLICT(call) DO UPDATE SET
                name=excluded.name, grid=excluded.grid,
                lat=excluded.lat, lon=excluded.lon,
                state=excluded.state, country=excluded.country,
                cqzone=excluded.cqzone, source=excluded.source,
                updated_at=CURRENT_TIMESTAMP
        """, (call, info.get("name", ""), grid, lat, lon,
              info.get("state", ""), info.get("country", ""),
              str(info.get("cqzone", "") or ""), info.get("source", "qrz")))
        if own:
            conn.commit()
    except Exception:
        pass
    finally:
        if own and conn:
            conn.close()


def lookup_callsign_tiered(call, session_id=None, allow_qrz=True, meta=None):
    """Resolve a call through the operator's tiers:
    internal US/CA DB → OUR growing DB (worked_callsigns) → current session log
    → QRZ.com. Returns a merged dict (or None). 'source' names the primary tier;
    'known_zone' carries a CQ zone the operator already logged for this call
    (beats QRZ's default). If 'meta' is passed it gets 'qrz_status'. A fresh QRZ
    hit is saved into our growing DB."""
    call = (call or "").upper().strip()
    info = lookup_callsign(call)
    if info:
        info = dict(info)
        info.setdefault("source", "db")

    # Our own growing DB — fills gaps (and often avoids a QRZ round-trip).
    mine = lookup_worked_callsign(call)
    if mine:
        if info is None:
            info = dict(mine)
        else:
            for k in ("name", "grid", "lat", "lon", "state", "country", "cqzone"):
                if not info.get(k) and mine.get(k) not in (None, ""):
                    info[k] = mine[k]

    hist = _lookup_session_history(call, session_id)

    # QRZ fills remaining gaps (grid/cqzone/name/coords) or stands in. A fresh
    # QRZ result is remembered in our own DB so we never pay for it twice.
    if allow_qrz and (info is None or not info.get("grid") or not info.get("cqzone")
                      or info.get("lat") is None):
        q, qstatus = qrz_lookup(call)
        if meta is not None:
            meta["qrz_status"] = qstatus
        if q:
            save_worked_callsign(q)
            if info is None:
                info = dict(q)
            else:
                for k in ("name", "city", "state", "country",
                          "grid", "cqzone", "lat", "lon"):
                    if not info.get(k) and q.get(k):
                        info[k] = q[k]
                info["qrz"] = True

    # The operator's own prior log entry is the most trustworthy CQ zone.
    if hist:
        if info is None:
            info = {"call": call, "source": "log"}
        if hist.get("zone"):
            info["known_zone"] = hist["zone"]
        for k in ("name", "state", "country", "grid"):
            if not info.get(k) and hist.get(k):
                info[k] = hist[k]

    # Always give coordinates when a grid is known but QRZ/DB gave none — so
    # the station maps immediately (QRZ often returns a grid without lat/lon).
    if info and (info.get("lat") is None or info.get("lon") is None) and info.get("grid"):
        try:
            g = grid_to_latlon(info["grid"])
            if g:
                info["lat"], info["lon"] = g
        except (ValueError, IndexError, TypeError):
            pass

    return info


# ============================================================================
# POTA Parks
# ============================================================================

PARKS_DB = LOGGER_DIR / "pota-parks.db"


def _get_parks_db():
    """Get parks database connection."""
    if not PARKS_DB.exists():
        return None
    conn = sqlite3.connect(str(PARKS_DB))
    conn.row_factory = sqlite3.Row
    return conn


def lookup_park_location(park_ref):
    """Look up a POTA park's lat/lon by reference (e.g. 'K-1234')."""
    conn = _get_parks_db()
    if not conn or not park_ref:
        return None, None
    row = conn.execute(
        "SELECT latitude, longitude FROM parks WHERE reference = ?",
        (park_ref.strip().upper(),)).fetchone()
    conn.close()
    if row and row['latitude'] and row['longitude']:
        return row['latitude'], row['longitude']
    return None, None


def load_pota_parks():
    """Load all active POTA parks."""
    conn = _get_parks_db()
    if not conn:
        return []
    rows = conn.execute(
        "SELECT * FROM parks WHERE active = 1").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def find_nearest_parks(lat, lon, max_km=100):
    """Find nearest POTA parks to a position using bounding box pre-filter."""
    conn = _get_parks_db()
    if not conn:
        return []

    # Rough bounding box pre-filter (1 degree ~ 111km)
    deg = max_km / 111.0
    rows = conn.execute("""
        SELECT * FROM parks WHERE active = 1
        AND latitude BETWEEN ? AND ?
        AND longitude BETWEEN ? AND ?
    """, (lat - deg, lat + deg, lon - deg, lon + deg)).fetchall()
    conn.close()

    results = []
    for r in rows:
        dist = haversine_km(lat, lon, r['latitude'], r['longitude'])
        if dist <= max_km:
            results.append({
                'reference': r['reference'],
                'name': r['name'],
                'lat': r['latitude'],
                'lon': r['longitude'],
                'locationDesc': r['locationDesc'],
                'grid': r['grid'],
                'active': '1',
                'distance': round(dist, 1),
            })

    results.sort(key=lambda x: x['distance'])
    return results


# ============================================================================
# ADIF Export
# ============================================================================

def export_adif(session_id, park_override=None):
    """Export a session's QSOs as ADIF string.

    Args:
        session_id: session to export
        park_override: if set, use this as MY_SIG_INFO instead of session default
                       (for multi-park exports — one file per park)
    """
    conn = get_db()
    session = conn.execute("SELECT * FROM sessions WHERE id = ?",
                           (session_id,)).fetchone()
    if not session:
        conn.close()
        return None

    qsos = conn.execute(
        "SELECT * FROM qsos WHERE session_id = ? ORDER BY qso_date, time_on",
        (session_id,)).fetchall()
    conn.close()

    my_sig_info = park_override or session['my_sig_info']

    lines = []
    # Header
    lines.append(f"<ADIF_VER:5>3.1.7")
    lines.append(f"<PROGRAMID:9>ET-Logger")
    lines.append(f"<PROGRAMVERSION:5>1.0.0")
    ts = datetime.now(timezone.utc).strftime('%Y%m%d %H%M%S')
    lines.append(f"<CREATED_TIMESTAMP:15>{ts}")
    lines.append(f"<EOH>")
    lines.append("")

    for q in qsos:
        fields = []

        def add(name, value):
            if value:
                v = str(value)
                fields.append(f"<{name}:{len(v)}>{v}")

        add("STATION_CALLSIGN", session['my_callsign'])
        add("MY_SIG", session['my_sig'])
        add("MY_SIG_INFO", my_sig_info)
        add("MY_GRIDSQUARE", session['my_grid'])
        add("QSO_DATE", q['qso_date'])
        add("TIME_ON", q['time_on'])
        add("CALL", q['call'])
        if q['freq']:
            freq_mhz = f"{q['freq']:.6f}"
            add("FREQ", freq_mhz)
        add("BAND", q['band'])
        add("MODE", q['mode'])
        add("RST_SENT", q['rst_sent'])
        add("RST_RCVD", q['rst_rcvd'])
        if q['tx_pwr']:
            add("TX_PWR", str(int(q['tx_pwr'])))
        add("GRIDSQUARE", q['gridsquare'])
        add("NAME", q['name'])
        add("STATE", q['state'])
        add("COUNTRY", q['country'])
        # P2P parks — sig_info may be comma-separated for multi-park
        if q['sig'] and q['sig_info']:
            p2p_parks = q['sig_info'].split(',')
            add("SIG", q['sig'])
            add("SIG_INFO", p2p_parks[0].strip())
        add("COMMENT", q['comment'])

        fields.append("<EOR>")
        lines.append(" ".join(fields))
        lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Cabrillo contest log export (https://cqww.com/cabrillo.htm)
# ---------------------------------------------------------------------------
# Map our stored mode -> Cabrillo 2-char mode code. Anything not explicitly
# phone/CW/RTTY/FM is treated as a digital mode (DG).
_CABRILLO_MODE = {
    'CW': 'CW',
    'SSB': 'PH', 'USB': 'PH', 'LSB': 'PH', 'AM': 'PH', 'PH': 'PH',
    'FM': 'FM',
    'RTTY': 'RY', 'RY': 'RY',
}


def _cabrillo_mode(mode):
    return _CABRILLO_MODE.get((mode or '').upper(), 'DG')


def export_cabrillo(session_id):
    """Render a session as a Cabrillo 3.0 contest log.

    The operator sets CONTEST + their sent exchange (my_exch) on the session;
    each QSO carries the received exchange (exch_rcvd). Frequency is emitted in
    kHz (we store MHz). Category/score headers are sensible defaults the
    operator can edit before submitting the log to the sponsor.
    """
    conn = get_db()
    session = conn.execute("SELECT * FROM sessions WHERE id = ?",
                           (session_id,)).fetchone()
    if not session:
        conn.close()
        return None
    qsos = conn.execute(
        "SELECT * FROM qsos WHERE session_id = ? ORDER BY qso_date, time_on",
        (session_id,)).fetchall()
    conn.close()

    mycall = (session['my_callsign'] or 'NOCALL').upper()
    contest = (session['contest'] or session['name'] or 'CONTEST').strip()
    my_exch = (session['my_exch'] or '').strip()
    grid = (session['my_grid'] or '').strip()

    lines = [
        "START-OF-LOG: 3.0",
        "CREATED-BY: LiaisonOS et-logger",
        "CONTEST: %s" % contest,
        "CALLSIGN: %s" % mycall,
        "OPERATORS: %s" % mycall,
    ]
    if grid:
        lines.append("GRID-LOCATOR: %s" % grid)
    lines += [
        "CATEGORY-OPERATOR: SINGLE-OP",
        "CATEGORY-BAND: ALL",
        "CATEGORY-POWER: LOW",
        "CATEGORY-MODE: MIXED",
        "CLAIMED-SCORE: 0",
    ]

    for q in qsos:
        try:
            khz = str(int(round(float(q['freq']) * 1000))) if q['freq'] else "0"
        except (TypeError, ValueError):
            khz = "0"
        mode = _cabrillo_mode(q['mode'])
        d = q['qso_date'] or ''
        date = "%s-%s-%s" % (d[0:4], d[4:6], d[6:8]) if len(d) >= 8 else d
        t = (q['time_on'] or '')[:4]
        rcall = (q['call'] or '').upper()
        rst_s = q['rst_sent'] or '59'
        rst_r = q['rst_rcvd'] or '59'
        exch_r = q['exch_rcvd'] or ''
        lines.append(
            ("QSO: %5s %-2s %s %-4s %-13s %-3s %-6s %-13s %-3s %-6s" % (
                khz, mode, date, t,
                mycall, rst_s, my_exch,
                rcall, rst_r, exch_r)).rstrip()
        )
    lines.append("END-OF-LOG:")
    return "\n".join(lines) + "\n"


# ============================================================================
# Translations
# ============================================================================

TRANSLATIONS = {
    'en': {
        'title': 'Field Logger',
        'subtitle': 'POTA / SOTA QSO Logger',
        'new_session': 'New Session',
        'sessions': 'Sessions',
        'log_qso': 'Log QSO',
        'callsign': 'Callsign',
        'rst_sent': 'RST Sent',
        'rst_rcvd': 'RST Rcvd',
        'frequency': 'Frequency',
        'band': 'Band',
        'mode': 'Mode',
        'power': 'Power (W)',
        'comment': 'Comment',
        'save': 'Save',
        'cancel': 'Cancel',
        'delete': 'Delete',
        'export_adif': 'Export ADIF',
        'export_cabrillo': 'Export Cabrillo',
        'contest': 'Contest',
        'contest_none': 'None',
        'contest_custom': 'Custom…',
        'contest_custom_name': 'Contest name',
        'my_exch': 'My exchange (sent, no RST)',
        'my_exch_ph': 'zone / section / serial — e.g. 04',
        'exch_rcvd': 'Exchange received (no RST)',
        'exch_rcvd_ph': 'their zone / section / serial — e.g. 05',
        'my_cq_zone': 'My CQ Zone (sent)',
        'my_cq_zone_ph': 'e.g. 05',
        'cq_zone_rcvd': 'CQ Zone received',
        'cq_zone_rcvd_ph': 'their zone — e.g. 04',
        'zone_lbl': 'Zone',
        'sb_qsos': 'QSOs',
        'sb_points': 'Points',
        'sb_zones': 'Zones',
        'sb_countries': 'Countries',
        'sb_score': 'Score',
        'sb_pts_short': 'pts',
        'new_zone': 'NEW ZONE',
        'new_country': 'NEW COUNTRY',
        'qrz_settings': 'QRZ.com lookup',
        'qrz_help': 'Add your QRZ.com XML-API credentials to look up callsigns online (name, QTH, grid and CQ zone) when internet is available. The local callsign database still works offline.',
        'qrz_user': 'QRZ username',
        'qrz_pass': 'QRZ password',
        'qrz_pass_ph': 'your QRZ password',
        'qrz_pass_keep': '•••••• (leave blank to keep)',
        'qrz_enable': 'Enable QRZ.com lookups',
        'qrz_test': 'Test',
        'qrz_testing': 'Testing…',
        'qrz_ok': 'QRZ login OK',
        'qrz_unreachable': 'QRZ unreachable — no internet?',
        'qrz_saved': 'QRZ settings saved',
        'qrz_setup_later': 'QRZ lookup stays off — you can set it up anytime with the ⚙ button, top right.',
        'src_db': 'local DB',
        'src_log': 'logged',
        'refresh_loc': '↻ Refresh Locations',
        'refresh_loc_help': 'Look up missing QSO locations (local DB → your DB → QRZ) and plot them on the map',
        'refresh_loc_working': '↻ Looking up…',
        'refresh_loc_done': 'Locations refreshed — {u} added, {m} still unknown',
        'val_invalid': 'NOT FOUND on QRZ',
        'val_unverified': 'unverified (QRZ offline)',
        'val_log_anyway': 'Log it anyway?',
        'dup_title': 'Duplicate on this band',
        'dup_no': 'No',
        'dup_yes': 'Yes, log it',
        'dup_already': 'Already worked on',
        'dup_thisband': 'this band',
        'dup_ask': 'Log the duplicate anyway?',
        'park_reference': 'Park Reference',
        'summit_reference': 'Summit Reference',
        'nearest_parks': 'Nearest Parks',
        'qso_count': 'QSOs',
        'activation_progress': 'Activation Progress',
        'valid_activation': 'Valid Activation!',
        'need_more': 'Need {n} more QSOs',
        'no_sessions': 'No sessions. Create a new one to start logging.',
        'session_name': 'Session Name',
        'program': 'Program',
        'my_callsign': 'My Callsign',
        'search_callsign': 'Search callsign...',
        'radio_connected': 'Radio connected',
        'radio_disconnected': 'Radio not connected',
        'gps_active': 'GPS active',
        'map': 'Map',
        'log': 'Log',
        'search': 'Search',
        'name': 'Name',
        'location': 'Location',
        'grid': 'Grid',
        'distance': 'Distance',
        'time_utc': 'Time (UTC)',
        'date': 'Date',
        'voice_keyer': 'Voice Keyer',
        'record': 'Record',
        'stop_rec': 'Stop',
        'play_air': 'Play on Air',
        'preview': 'Preview',
        'repeat': 'Repeat',
        'delay_seconds': 'Delay (s)',
        'recording': 'Recording...',
        'playing': 'Playing...',
        'no_messages': 'No voice messages recorded yet.',
        'rename': 'Rename',
        'confirm_delete_msg': 'Delete this message?',
        'audio_device_missing': 'ET_AUDIO device not found',
        'input_device': 'Input Device',
        'message_name': 'Message Name',
        'stop_play': 'Stop',
        'air_recorder': 'Air Recorder',
        'air_record': 'Record Air',
        'air_stop': 'Stop',
        'air_recording': 'Recording air...',
        'no_air_recordings': 'No air recordings yet.',
        'air_rec_name': 'Recording Name',
        # Antenna profiles
        'antennas': 'Antennas',
        'add_antenna': 'Add Antenna',
        'edit_antenna': 'Edit Antenna',
        'antenna_type': 'Type',
        'ant_sloper_efhw': 'Sloper EFHW',
        'ant_vertical_omni': 'Vertical (omni)',
        'ant_inverted_v_nvis': 'Inverted-V (NVIS)',
        'ant_delta_loop': 'Delta loop (vertical, apex up)',
        'ant_horizontal_loop': 'Horizontal loop (skywarmer)',
        'ant_horizontal_efhw': 'Horizontal EFHW (NVIS)',
        'is_omni': 'Omnidirectional',
        'feed_h_ft': 'Feed height (ft)',
        'far_h_ft': 'Far-end height (ft)',
        'wire_len_ft': 'Wire length (ft)',
        'orientation_deg': 'Orientation (° true, 0=N)',
        'bands_hint': 'Bands (comma-separated, e.g. 40m, 20m, 10m)',
        'site_lat': 'Site latitude',
        'site_lon': 'Site longitude',
        'notes': 'Notes',
        'active_deployed': 'Active (deployed)',
        'show_pattern': 'Show on map',
        'hide_pattern': 'Hide',
        'no_antennas': 'No antennas configured. Add one to enable "best antenna" recommendations.',
        'antenna_gain': 'Gain',
        'antenna_beam_to': 'Beam heading to',
        'antenna_best_for': 'Best antenna for',
        'pattern_at_band': 'Pattern at',
    },
    'fr': {
        'title': 'Journal de Terrain',
        'subtitle': 'Journal QSO POTA / SOTA',
        'new_session': 'Nouvelle Session',
        'sessions': 'Sessions',
        'log_qso': 'Enregistrer QSO',
        'callsign': 'Indicatif',
        'rst_sent': 'RST Envoyé',
        'rst_rcvd': 'RST Reçu',
        'frequency': 'Fréquence',
        'band': 'Bande',
        'mode': 'Mode',
        'power': 'Puissance (W)',
        'comment': 'Commentaire',
        'save': 'Sauver',
        'cancel': 'Annuler',
        'delete': 'Supprimer',
        'export_adif': 'Exporter ADIF',
        'export_cabrillo': 'Exporter Cabrillo',
        'contest': 'Concours',
        'contest_none': 'Aucun',
        'contest_custom': 'Personnalisé…',
        'contest_custom_name': 'Nom du concours',
        'my_exch': 'Mon échange (envoyé, sans RST)',
        'my_exch_ph': 'zone / section / numéro — ex. 04',
        'exch_rcvd': 'Échange reçu (sans RST)',
        'exch_rcvd_ph': 'sa zone / section / numéro — ex. 05',
        'my_cq_zone': 'Ma zone CQ (envoyée)',
        'my_cq_zone_ph': 'ex. 05',
        'cq_zone_rcvd': 'Zone CQ reçue',
        'cq_zone_rcvd_ph': 'sa zone — ex. 04',
        'zone_lbl': 'Zone',
        'sb_qsos': 'QSO',
        'sb_points': 'Points',
        'sb_zones': 'Zones',
        'sb_countries': 'Pays',
        'sb_score': 'Pointage',
        'sb_pts_short': 'pts',
        'new_zone': 'NOUV. ZONE',
        'new_country': 'NOUV. PAYS',
        'qrz_settings': 'Recherche QRZ.com',
        'qrz_help': 'Ajoutez vos identifiants de l\'API XML QRZ.com pour rechercher les indicatifs en ligne (nom, QTH, grille et zone CQ) lorsqu\'Internet est disponible. La base d\'indicatifs locale fonctionne toujours hors ligne.',
        'qrz_user': 'Nom d\'utilisateur QRZ',
        'qrz_pass': 'Mot de passe QRZ',
        'qrz_pass_ph': 'votre mot de passe QRZ',
        'qrz_pass_keep': '•••••• (laisser vide pour conserver)',
        'qrz_enable': 'Activer les recherches QRZ.com',
        'qrz_test': 'Tester',
        'qrz_testing': 'Test en cours…',
        'qrz_ok': 'Connexion QRZ réussie',
        'qrz_unreachable': 'QRZ injoignable — pas d\'Internet ?',
        'qrz_saved': 'Réglages QRZ enregistrés',
        'qrz_setup_later': 'Recherche QRZ désactivée — vous pouvez la configurer à tout moment avec le bouton ⚙, en haut à droite.',
        'src_db': 'base locale',
        'src_log': 'déjà au journal',
        'refresh_loc': '↻ Rafraîchir positions',
        'refresh_loc_help': 'Rechercher les positions manquantes des QSO (base locale → votre base → QRZ) et les tracer sur la carte',
        'refresh_loc_working': '↻ Recherche…',
        'refresh_loc_done': 'Positions rafraîchies — {u} ajoutées, {m} encore inconnues',
        'val_invalid': 'INTROUVABLE sur QRZ',
        'val_unverified': 'non vérifié (QRZ hors ligne)',
        'val_log_anyway': 'Enregistrer quand même ?',
        'dup_title': 'Doublon sur cette bande',
        'dup_no': 'Non',
        'dup_yes': 'Oui, enregistrer',
        'dup_already': 'Déjà contacté sur',
        'dup_thisband': 'cette bande',
        'dup_ask': 'Enregistrer le doublon quand même ?',
        'park_reference': 'Référence Parc',
        'summit_reference': 'Référence Sommet',
        'nearest_parks': 'Parcs à proximité',
        'qso_count': 'QSOs',
        'activation_progress': 'Progression activation',
        'valid_activation': 'Activation valide!',
        'need_more': 'Encore {n} QSOs requis',
        'no_sessions': 'Aucune session. Créez-en une pour commencer.',
        'session_name': 'Nom de session',
        'program': 'Programme',
        'my_callsign': 'Mon indicatif',
        'search_callsign': 'Chercher indicatif...',
        'radio_connected': 'Radio connectée',
        'radio_disconnected': 'Radio non connectée',
        'gps_active': 'GPS actif',
        'map': 'Carte',
        'log': 'Journal',
        'search': 'Recherche',
        'name': 'Nom',
        'location': 'Lieu',
        'grid': 'Grille',
        'distance': 'Distance',
        'time_utc': 'Heure (UTC)',
        'date': 'Date',
        'voice_keyer': 'Clé vocale',
        'record': 'Enregistrer',
        'stop_rec': 'Arrêter',
        'play_air': 'Jouer sur l\'air',
        'preview': 'Aperçu',
        'repeat': 'Répéter',
        'delay_seconds': 'Délai (s)',
        'recording': 'Enregistrement...',
        'playing': 'En cours...',
        'no_messages': 'Aucun message vocal enregistré.',
        'rename': 'Renommer',
        'confirm_delete_msg': 'Supprimer ce message?',
        'audio_device_missing': 'Périphérique ET_AUDIO introuvable',
        'input_device': 'Source audio',
        'message_name': 'Nom du message',
        'stop_play': 'Arrêter',
        'air_recorder': 'Enregistreur d\'air',
        'air_record': 'Enregistrer l\'air',
        'air_stop': 'Arrêter',
        'air_recording': 'Enregistrement en cours...',
        'no_air_recordings': 'Aucun enregistrement d\'air.',
        'air_rec_name': 'Nom de l\'enregistrement',
        # Antenna profiles
        'antennas': 'Antennes',
        'add_antenna': 'Ajouter une antenne',
        'edit_antenna': 'Modifier l\'antenne',
        'antenna_type': 'Type',
        'ant_sloper_efhw': 'Sloper EFHW',
        'ant_vertical_omni': 'Verticale (omni)',
        'ant_inverted_v_nvis': 'Inverted-V (NVIS)',
        'ant_delta_loop': 'Boucle delta (verticale, sommet en haut)',
        'ant_horizontal_loop': 'Boucle horizontale (skywarmer)',
        'ant_horizontal_efhw': 'EFHW horizontal (NVIS)',
        'is_omni': 'Omnidirectionnelle',
        'feed_h_ft': 'Hauteur d\'alimentation (pi)',
        'far_h_ft': 'Hauteur au loin (pi)',
        'wire_len_ft': 'Longueur du fil (pi)',
        'orientation_deg': 'Orientation (° true, 0=N)',
        'bands_hint': 'Bandes (séparées par virgule, ex: 40m, 20m, 10m)',
        'site_lat': 'Latitude du site',
        'site_lon': 'Longitude du site',
        'notes': 'Notes',
        'active_deployed': 'Active (déployée)',
        'show_pattern': 'Voir sur carte',
        'hide_pattern': 'Masquer',
        'no_antennas': 'Aucune antenne configurée. Ajoutes-en une pour activer les recommandations « meilleure antenne ».',
        'antenna_gain': 'Gain',
        'antenna_beam_to': 'Cap antenne vers',
        'antenna_best_for': 'Meilleure antenne pour',
        'pattern_at_band': 'Patron à',
    }
}


def get_translations(lang=None):
    if not lang:
        config = load_user_config()
        lang = config.get('language', 'en')
    return TRANSLATIONS.get(lang, TRANSLATIONS['en']), lang


# ============================================================================
# Routes
# ============================================================================

@app.route('/')
def index():
    config = load_user_config()
    t, lang = get_translations(config.get('language', 'en'))
    lat, lon, pos_source = get_position()
    my_grid = latlon_to_grid(lat, lon) if lat else ''
    my_callsign = config.get('callsign', '')

    # Get sessions list
    conn = get_db()
    sessions = conn.execute(
        "SELECT s.*, COUNT(q.id) as qso_count FROM sessions s "
        "LEFT JOIN qsos q ON s.id = q.session_id "
        "GROUP BY s.id ORDER BY s.created_at DESC"
    ).fetchall()
    conn.close()

    return render_template('index.html',
                           t=t, lang=lang,
                           my_callsign=my_callsign,
                           my_grid=my_grid,
                           my_lat=(lat if lat is not None else ''),
                           my_lon=(lon if lon is not None else ''),
                           has_position=(lat is not None),
                           sessions=[dict(s) for s in sessions])


@app.route('/api/sessions', methods=['GET'])
def api_sessions():
    """Get all sessions with QSO counts."""
    conn = get_db()
    sessions = conn.execute(
        "SELECT s.*, COUNT(q.id) as qso_count FROM sessions s "
        "LEFT JOIN qsos q ON s.id = q.session_id "
        "GROUP BY s.id ORDER BY s.created_at DESC"
    ).fetchall()
    conn.close()
    return jsonify({'sessions': [dict(s) for s in sessions]})


@app.route('/api/radio', methods=['GET'])
def api_radio():
    """Get current radio info — returns cached state (no radio query)."""
    if not rig:
        return jsonify({'freq': None, 'mode': None, 'band': None, 'power': None, 'connected': False})
    return jsonify({
        'freq': rig.freq,
        'freq_mhz': rig.freq_mhz,
        'mode': rig.mode,
        'mode_raw': rig.mode_raw,
        'band': rig.band,
        'power': rig.power,
        'passband': rig.passband,
        'connected': rig.connected,
    })


@app.route('/api/radio/stream')
def api_radio_stream():
    """SSE stream — pushes radio state to browser on every change."""
    def generate():
        last_freq = None
        last_mode = None
        while True:
            if rig and rig.connected:
                if rig.freq != last_freq or rig.mode != last_mode:
                    last_freq = rig.freq
                    last_mode = rig.mode
                    data = json.dumps({
                        'freq': rig.freq,
                        'freq_mhz': rig.freq_mhz,
                        'mode': rig.mode,
                        'mode_raw': rig.mode_raw,
                        'band': rig.band,
                        'power': rig.power,
                        'connected': True,
                    })
                    yield f"data: {data}\n\n"
            time.sleep(1)
    return Response(generate(), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


@app.route('/api/position', methods=['GET'])
def api_position():
    """Get current GPS/config position.

    Grid-source precedence (matters when user.json has stale lat/lon from
    a previous GPS session and the operator has since edited only the
    'grid' field in their profile — deriving grid from those stale coords
    would silently ignore the manual edit):
      1. Live GPS fix if gpsd is running (source='gps') — always wins
         because it's the operator explicitly enabling GPS tracking.
      2. user.json 'grid' field verbatim (source='config') — the operator's
         declared grid, authoritative for the top-right display.
      3. Grid derived from user.json lat/lon (source='config-latlon') —
         only when no 'grid' is set at all.

    Optional ?session_id=<id>: when that session is a POTA activation
    (my_sig_info set), the operator's location IS the activated park — return
    the session's stored park lat/lon/grid (source='park') so the map pin,
    centre, distance/bearing and the grid badge all reflect the park rather
    than the (possibly stale or unset) home grid.
    """
    session_id = request.args.get('session_id', '').strip()
    if session_id:
        try:
            conn = get_db()
            row = conn.execute(
                "SELECT my_sig_info, my_lat, my_lon, my_grid FROM sessions WHERE id = ?",
                (session_id,)).fetchone()
            conn.close()
            if row and (row['my_sig_info'] or '').strip() and row['my_lat'] is not None:
                return jsonify({'lat': row['my_lat'], 'lon': row['my_lon'],
                                'grid': row['my_grid'] or '', 'source': 'park'})
        except Exception:
            pass  # any DB hiccup → fall through to live GPS/grid, never 500

    lat, lon, coord_source = get_position()

    config = load_user_config()
    manual_grid = (config.get('grid') or '').strip().upper()

    if coord_source == 'gps':
        grid = latlon_to_grid(lat, lon) if lat else ''
        source = 'gps'
    elif manual_grid:
        grid = manual_grid
        source = 'config'
    else:
        grid = latlon_to_grid(lat, lon) if lat else ''
        source = 'config-latlon' if grid else None

    return jsonify({'lat': lat, 'lon': lon, 'grid': grid, 'source': source})


@app.route('/api/callsign/<call>', methods=['GET'])
def api_callsign_lookup(call):
    """Look up a single callsign — internal DB → session log, and QRZ only when
    ?qrz=1 (sent on a committed call, not on every keystroke, to respect QRZ's
    API limits).

    Always 200 with a validation verdict so the UI can warn on bogus calls:
      found        : resolved in DB/log/QRZ
      source       : 'db' | 'log' | 'qrz' | None
      qrz_status   : found | notfound | offline | unconfigured | error | None
      verdict      : 'ok'        resolved somewhere
                     'invalid'   QRZ reached and says no such call  → WARN hard
                     'unverified' not resolved and QRZ couldn't check (offline/
                                  disabled) → soft note, don't block offline-first
    """
    session_id = request.args.get('session_id')
    allow_qrz = request.args.get('qrz') in ('1', 'true', 'yes')
    meta = {}
    info = lookup_callsign_tiered(call, session_id=session_id,
                                  allow_qrz=allow_qrz, meta=meta)
    qrz_status = meta.get('qrz_status')
    resp = dict(info or {})
    resp['found'] = bool(info)
    resp['qrz_status'] = qrz_status
    if info:
        resp['verdict'] = 'ok'
    elif qrz_status == 'notfound':
        resp['verdict'] = 'invalid'
    else:
        resp['verdict'] = 'unverified'
    return jsonify(resp)


@app.route('/api/qrz/config', methods=['GET'])
def api_qrz_config_get():
    """Report QRZ setup (never returns the password)."""
    cfg = load_qrz_config()
    return jsonify({
        'user': cfg.get('user', ''),
        'enabled': bool(cfg.get('enabled', False)),
        'has_password': bool(cfg.get('password')),
    })


@app.route('/api/qrz/config', methods=['POST'])
def api_qrz_config_set():
    """Save QRZ credentials. A blank password keeps the stored one."""
    data = request.get_json(silent=True) or {}
    cfg = load_qrz_config()
    cfg['user'] = (data.get('user') or '').strip()
    if data.get('password'):
        cfg['password'] = data['password']
    if not cfg.get('user'):
        cfg['password'] = ''
    cfg['enabled'] = bool(data.get('enabled', False))
    save_qrz_config(cfg)
    # Force a fresh login + clear cached lookups so new creds take effect now.
    _qrz_state['key'] = None
    _qrz_cache.clear()
    return jsonify({'ok': True})


@app.route('/api/qrz/test', methods=['POST'])
def api_qrz_test():
    """Try a QRZ login with the given (or stored) creds — no internet-safe."""
    data = request.get_json(silent=True) or {}
    cfg = load_qrz_config()
    user = (data.get('user') or cfg.get('user', '')).strip()
    password = data.get('password') or cfg.get('password', '')
    if not user or not password:
        return jsonify({'ok': False, 'error': 'Enter your QRZ username and password first'})
    try:
        key, err = _qrz_login(user, password)
    except Exception:
        return jsonify({'ok': False, 'error': 'QRZ unreachable — no internet?'})
    if key:
        return jsonify({'ok': True})
    return jsonify({'ok': False, 'error': err or 'Login failed'})


@app.route('/api/callsign/search', methods=['GET'])
def api_callsign_search():
    """Search callsigns by partial match."""
    q = request.args.get('q', '')
    results = search_callsigns(q)
    return jsonify({'results': results})


@app.route('/api/parks/count', methods=['GET'])
def api_parks_count():
    """Debug: how many parks loaded."""
    parks = load_pota_parks()
    return jsonify({'count': len(parks)})


@app.route('/api/parks/search', methods=['GET'])
def api_parks_search():
    """Search parks by reference or name using SQLite."""
    q = request.args.get('q', '').strip()
    if len(q) < 2:
        return jsonify({'parks': []})

    conn = _get_parks_db()
    if not conn:
        return jsonify({'parks': []})

    search = f"%{q}%"
    rows = conn.execute("""
        SELECT * FROM parks WHERE active = 1
        AND (reference LIKE ? OR name LIKE ?)
        LIMIT 30
    """, (search, search)).fetchall()
    conn.close()

    lat, lon, _ = get_position()
    results = []
    for r in rows:
        entry = {
            'reference': r['reference'], 'name': r['name'],
            'lat': r['latitude'], 'lon': r['longitude'],
            'locationDesc': r['locationDesc'], 'grid': r['grid'],
            'active': '1',
        }
        if lat is not None:
            entry['distance'] = round(haversine_km(lat, lon, r['latitude'], r['longitude']), 1)
        results.append(entry)

    results.sort(key=lambda x: x.get('distance', 99999))
    return jsonify({'parks': results})


@app.route('/api/parks/nearest', methods=['GET'])
def api_nearest_parks():
    """Find nearest POTA parks."""
    lat, lon, _ = get_position()
    if lat is None:
        return jsonify({'parks': []})
    max_km = float(request.args.get('max_km', 100))
    parks = find_nearest_parks(lat, lon, max_km=max_km)
    return jsonify({'parks': parks})


@app.route('/api/session', methods=['POST'])
def api_create_session():
    """Create a new logging session."""
    data = request.get_json()
    config = load_user_config()
    sig_info = data.get('sig_info', '')
    lat, lon, _ = get_position()
    # POTA activation: the operator's location IS the activated park, so use the
    # park's coordinates instead of GPS/home grid. Off-grid the home grid is
    # often stale or unset and GPS may be unavailable — but the operator always
    # knows which park they're on. Non-POTA sessions fall back to get_position()
    # (GPS → user.json → profile grid), exactly as before.
    if sig_info:
        first_park = sig_info.split(',')[0].strip()
        plat, plon = lookup_park_location(first_park)
        if plat is not None:
            lat, lon = plat, plon
    grid = latlon_to_grid(lat, lon) if lat else config.get('grid', '')

    conn = get_db()
    cursor = conn.execute("""
        INSERT INTO sessions (name, program, my_sig, my_sig_info,
                              my_callsign, my_grid, my_lat, my_lon, notes,
                              contest, my_exch)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        data.get('name', f"Session {datetime.now().strftime('%Y-%m-%d %H:%M')}"),
        data.get('program', 'POTA'),
        data.get('program', 'POTA'),
        data.get('sig_info', ''),
        data.get('my_callsign', config.get('callsign', '')),
        grid,
        lat, lon,
        data.get('notes', ''),
        data.get('contest', ''),
        data.get('my_exch', ''),
    ))
    conn.commit()
    session_id = cursor.lastrowid
    conn.close()
    return jsonify({'success': True, 'id': session_id})


@app.route('/api/session/<int:session_id>', methods=['GET'])
def api_get_session(session_id):
    """Get session details with QSOs."""
    conn = get_db()
    session = conn.execute("SELECT * FROM sessions WHERE id = ?",
                           (session_id,)).fetchone()
    if not session:
        conn.close()
        return jsonify({'error': 'Not found'}), 404

    qsos = conn.execute(
        "SELECT * FROM qsos WHERE session_id = ? ORDER BY qso_date DESC, time_on DESC",
        (session_id,)).fetchall()
    conn.close()

    return jsonify({
        'session': dict(session),
        'qsos': [dict(q) for q in qsos],
        'count': len(qsos),
    })


@app.route('/api/session/<int:session_id>', methods=['DELETE'])
def api_delete_session(session_id):
    """Delete a session and all its QSOs."""
    conn = get_db()
    conn.execute("DELETE FROM qsos WHERE session_id = ?", (session_id,))
    conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
    conn.commit()
    conn.close()
    return jsonify({'success': True})


def _cty_centroid(call):
    """Last-resort MAP location from the DXCC entity centroid (cty.dat, prefix
    only — no QRZ). Country-level, approximate. Returns (lat, lon) or (None,None)."""
    if not CQWW_SCORER_OK:
        return None, None
    try:
        r = cqww_scorer.country_latlon(call)
        if r:
            return r[0], r[1]
    except Exception:
        pass
    return None, None


def _p2p_or_callsign_location(sig_info, call_info, call=None):
    """Return (lat, lon) — park location if P2P, otherwise the callsign's home
    QTH, falling back to the country centroid (cty.dat) so even a station with
    no DB/QRZ/grid match still gets an approximate map pin."""
    if sig_info:
        # Use first P2P park reference
        first_park = sig_info.split(',')[0].strip()
        if first_park:
            lat, lon = lookup_park_location(first_park)
            if lat is not None:
                return lat, lon
    # Callsign lookup location
    if call_info and call_info.get('lat') is not None and call_info.get('lon') is not None:
        return call_info.get('lat'), call_info.get('lon')
    # Last resort: DXCC country centroid (approximate, offline, no QRZ)
    c = call or (call_info or {}).get('call')
    if c:
        return _cty_centroid(c)
    return None, None


@app.route('/api/qso', methods=['POST'])
def api_log_qso():
    """Log one or more QSOs.

    Accepts a list of callsigns ('calls') and comma-separated their-parks
    ('sig_info'), and logs the cross-product as individual QSO rows:
    N callsigns x M P2P parks = N*M contacts (POTA P2P n-fer — e.g. 2 calls
    at a 2-park station = 4 QSOs). Each row carries a single call and a single
    park so every park gets its own ADIF record. Falls back to a single 'call'
    string for backward compatibility.
    """
    data = request.get_json()
    now = datetime.now(timezone.utc)

    # Use values from frontend (no radio query — already pre-filled)
    freq = data.get('freq')
    band = data.get('band', '')
    mode = data.get('mode', 'SSB')
    session_id = data.get('session_id')

    # Callsigns — a list ('calls'), or a single 'call' (back-compat).
    raw_calls = data.get('calls') or [data.get('call', '')]
    calls = []
    for c in raw_calls:
        c = (c or '').upper().strip()
        if c and c not in calls:
            calls.append(c)
    if not calls or not session_id:
        return jsonify({'success': False, 'error': 'no callsign'}), 400

    # 'force' = operator confirmed a per-band duplicate on purpose (YES in the
    # warning box) — log it anyway, bypassing the dedup guard below.
    force = bool(data.get('force', False))

    # Their P2P parks — one QSO row per park so each gets its own ADIF record.
    sig = data.get('sig', '')
    parks = [p.strip() for p in data.get('sig_info', '').split(',') if p.strip()]
    if not parks:
        parks = ['']  # non-P2P: single row per call, no park

    qso_date = data.get('qso_date', now.strftime('%Y%m%d'))
    time_on = data.get('time_on', now.strftime('%H%M'))
    rst_sent = data.get('rst_sent', '59')
    rst_rcvd = data.get('rst_rcvd', '59')
    tx_pwr = data.get('tx_pwr')
    comment = data.get('comment', '')
    exch_rcvd = data.get('exch_rcvd', '')
    # Trust frontend-provided name/loc only for a single call (they came from
    # one lookup); for multiple calls, look each up per row.
    single = len(calls) == 1

    conn = get_db()
    last_id = None
    inserted = 0
    dupes = []
    for call in calls:
        # Per-band duplicate guard — a station may be worked only ONCE per band
        # in a session (contest rule, and a sane default for POTA too). Skip a
        # call already present on this band and report it back so the UI can
        # warn + offer to log it anyway (force). Only enforced when a band is
        # known; the P2P n-fer (same call + band across several parks) is
        # unaffected because those rows are inserted together in ONE submit,
        # before any of them exist. 'force' bypasses the guard entirely.
        if band and not force and conn.execute(
                "SELECT 1 FROM qsos WHERE session_id = ? AND call = ? AND band = ? LIMIT 1",
                (session_id, call, band)).fetchone():
            dupes.append(call)
            continue
        # Tiered enrichment (DB → QRZ) so DX calls not in the local license DB
        # still log a name/QTH/grid when online. Cache is warmed by live typing,
        # so this is a cache hit at log time; offline it silently returns DB-only.
        call_info = lookup_callsign_tiered(call) or {}
        # Grow our own DB: remember every worked station that carries a
        # location (coords or grid), so our DB keeps expanding beyond the
        # bundled license file and future lookups work offline.
        save_worked_callsign(call_info)
        name = (data.get('name', '') if single else '') or call_info.get('name', '')
        state = (data.get('state', '') if single else '') or call_info.get('state', '')
        country = (data.get('country', '') if single else '') or call_info.get('country', '')
        gridsquare = (data.get('gridsquare', '') if single else '') or call_info.get('grid', '')
        for park in parks:
            row_sig = sig if park else ''
            # P2P: use park location, otherwise callsign home QTH
            qso_lat, qso_lon = _p2p_or_callsign_location(park, call_info, call)
            cursor = conn.execute("""
                INSERT INTO qsos (session_id, qso_date, time_on, call, freq, band,
                                  mode, rst_sent, rst_rcvd, tx_pwr, gridsquare,
                                  name, state, country, sig, sig_info, comment,
                                  qso_lat, qso_lon, exch_rcvd)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                session_id, qso_date, time_on, call, freq, band, mode,
                rst_sent, rst_rcvd, tx_pwr, gridsquare, name, state, country,
                row_sig, park, comment, qso_lat, qso_lon, exch_rcvd,
            ))
            last_id = cursor.lastrowid
            inserted += 1
    conn.commit()

    # Get updated count
    count = conn.execute("SELECT COUNT(*) FROM qsos WHERE session_id = ?",
                         (session_id,)).fetchone()[0]
    conn.close()

    return jsonify({'success': True, 'id': last_id, 'count': count,
                    'inserted': inserted, 'dupes': dupes, 'band': band})


@app.route('/api/qso/<int:qso_id>', methods=['GET'])
def api_get_qso(qso_id):
    """Get a single QSO."""
    conn = get_db()
    qso = conn.execute("SELECT * FROM qsos WHERE id = ?", (qso_id,)).fetchone()
    conn.close()
    if qso:
        return jsonify(dict(qso))
    return jsonify({'error': 'Not found'}), 404


@app.route('/api/qso/<int:qso_id>', methods=['PUT'])
def api_update_qso(qso_id):
    """Update a QSO."""
    data = request.get_json()
    conn = get_db()
    conn.execute("""
        UPDATE qsos SET call=?, qso_date=?, time_on=?, freq=?, band=?,
            mode=?, rst_sent=?, rst_rcvd=?, tx_pwr=?, name=?, state=?,
            comment=?, sig=?, sig_info=?, exch_rcvd=?
        WHERE id=?
    """, (
        data.get('call', ''),
        data.get('qso_date', ''),
        data.get('time_on', ''),
        data.get('freq'),
        data.get('band', ''),
        data.get('mode', ''),
        data.get('rst_sent', ''),
        data.get('rst_rcvd', ''),
        data.get('tx_pwr'),
        data.get('name', ''),
        data.get('state', ''),
        data.get('comment', ''),
        data.get('sig', ''),
        data.get('sig_info', ''),
        data.get('exch_rcvd', ''),
        qso_id,
    ))
    conn.commit()
    conn.close()
    return jsonify({'success': True})


@app.route('/api/qso/<int:qso_id>', methods=['DELETE'])
def api_delete_qso(qso_id):
    """Delete a QSO."""
    conn = get_db()
    conn.execute("DELETE FROM qsos WHERE id = ?", (qso_id,))
    conn.commit()
    conn.close()
    return jsonify({'success': True})


@app.route('/api/export/<int:session_id>', methods=['GET'])
def api_export_session(session_id):
    """Export session as ADIF file(s).

    Single park: returns one .adi file
    Multi-park: returns a .zip with separate .adi per park
    Naming: callsign@park-yyyymmdd.adi
    """
    conn = get_db()
    session = conn.execute("SELECT * FROM sessions WHERE id = ?",
                           (session_id,)).fetchone()
    if not session:
        conn.close()
        return jsonify({'error': 'Session not found'}), 404

    my_parks = [p.strip() for p in session['my_sig_info'].split(',') if p.strip()]
    callsign = session['my_callsign'] or 'NOCALL'
    date_str = session['created_at'][:10].replace('-', '') if session['created_at'] else ''
    conn.close()

    if len(my_parks) <= 1:
        # Single park — one ADIF file
        adif = export_adif(session_id)
        park = my_parks[0] if my_parks else 'general'
        filename = f"{callsign}@{park}-{date_str}.adi"
        return Response(
            adif,
            mimetype='text/plain',
            headers={'Content-Disposition': f'attachment; filename="{filename}"'}
        )
    else:
        # Multi-park — ZIP with separate ADIF per park
        import zipfile
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zf:
            for park in my_parks:
                adif = export_adif(session_id, park_override=park)
                filename = f"{callsign}@{park}-{date_str}.adi"
                zf.writestr(filename, adif)

        zip_buffer.seek(0)
        zip_name = f"{callsign}-multipark-{date_str}.zip"
        return Response(
            zip_buffer.getvalue(),
            mimetype='application/zip',
            headers={'Content-Disposition': f'attachment; filename="{zip_name}"'}
        )


def _cabrillo_filename(session):
    callsign = (session['my_callsign'] or 'NOCALL').upper()
    contest = (session['contest'] or session['name'] or 'contest').strip()
    contest = re.sub(r'[^A-Za-z0-9._-]+', '-', contest).strip('-') or 'contest'
    date_str = session['created_at'][:10].replace('-', '') if session['created_at'] else ''
    return f"{callsign}-{contest}-{date_str}.cbr"


@app.route('/api/export-cabrillo/<int:session_id>', methods=['GET'])
def api_export_cabrillo(session_id):
    """Export a contest session as a Cabrillo (.cbr) log download."""
    conn = get_db()
    session = conn.execute("SELECT * FROM sessions WHERE id = ?",
                           (session_id,)).fetchone()
    if not session:
        conn.close()
        return jsonify({'error': 'Session not found'}), 404
    filename = _cabrillo_filename(session)
    conn.close()

    cab = export_cabrillo(session_id)
    if cab is None:
        return jsonify({'error': 'Session not found'}), 404
    return Response(
        cab,
        mimetype='text/plain',
        headers={'Content-Disposition': f'attachment; filename="{filename}"'}
    )


@app.route('/api/export-cabrillo-save/<int:session_id>', methods=['POST'])
def api_export_cabrillo_save(session_id):
    """Export a contest session as Cabrillo and save it to ~/Documents/."""
    conn = get_db()
    session = conn.execute("SELECT * FROM sessions WHERE id = ?",
                           (session_id,)).fetchone()
    if not session:
        conn.close()
        return jsonify({'success': False, 'error': 'Session not found'})
    filename = _cabrillo_filename(session)
    conn.close()

    cab = export_cabrillo(session_id)
    if cab is None:
        return jsonify({'success': False, 'error': 'Session not found'})

    docs_dir = Path.home() / "Documents"
    docs_dir.mkdir(parents=True, exist_ok=True)
    with open(docs_dir / filename, 'w') as f:
        f.write(cab)
    return jsonify({'success': True, 'filename': filename, 'path': str(docs_dir)})


@app.route('/api/export-save/<int:session_id>', methods=['POST'])
def api_export_save(session_id):
    """Export session and save ADIF file(s) to ~/Documents/."""
    conn = get_db()
    session = conn.execute("SELECT * FROM sessions WHERE id = ?",
                           (session_id,)).fetchone()
    if not session:
        conn.close()
        return jsonify({'success': False, 'error': 'Session not found'})

    my_parks = [p.strip() for p in session['my_sig_info'].split(',') if p.strip()]
    callsign = session['my_callsign'] or 'NOCALL'
    date_str = session['created_at'][:10].replace('-', '') if session['created_at'] else ''
    conn.close()

    docs_dir = Path.home() / "Documents"
    docs_dir.mkdir(parents=True, exist_ok=True)

    saved_files = []
    if len(my_parks) <= 1:
        adif = export_adif(session_id)
        park = my_parks[0] if my_parks else 'general'
        filename = f"{callsign}@{park}-{date_str}.adi"
        filepath = docs_dir / filename
        with open(filepath, 'w') as f:
            f.write(adif)
        saved_files.append(filename)
    else:
        for park in my_parks:
            adif = export_adif(session_id, park_override=park)
            filename = f"{callsign}@{park}-{date_str}.adi"
            filepath = docs_dir / filename
            with open(filepath, 'w') as f:
                f.write(adif)
            saved_files.append(filename)

    return jsonify({
        'success': True,
        'filename': ', '.join(saved_files),
        'path': str(docs_dir),
        'count': len(saved_files),
    })


@app.route('/api/open-folder', methods=['POST'])
def api_open_folder():
    """Open a folder in the file manager."""
    data = request.get_json()
    folder = data.get('path', '')
    if folder and os.path.isdir(folder):
        try:
            subprocess.Popen(['xdg-open', folder],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return jsonify({'success': True})
        except Exception:
            pass
    return jsonify({'success': False})


@app.route('/api/duplicate-check', methods=['GET'])
def api_duplicate_check():
    """Check if a QSO is a duplicate.

    Per-band when a 'band' is supplied (a station may be worked once per band);
    falls back to a session-wide call match when no band is given.
    """
    session_id = request.args.get('session_id')
    call = request.args.get('call', '').upper().strip()
    band = request.args.get('band', '').strip()
    if not session_id or not call:
        return jsonify({'duplicate': False})

    conn = get_db()
    if band:
        existing = conn.execute(
            "SELECT id, time_on, band FROM qsos "
            "WHERE session_id = ? AND call = ? AND band = ?",
            (session_id, call, band)).fetchone()
    else:
        existing = conn.execute(
            "SELECT id, time_on, band FROM qsos WHERE session_id = ? AND call = ?",
            (session_id, call)).fetchone()
    conn.close()

    if existing:
        return jsonify({'duplicate': True, 'id': existing['id'],
                        'time': existing['time_on'], 'band': existing['band']})
    return jsonify({'duplicate': False})


# ============================================================================
# CQ WW live score
# ============================================================================

def _build_cqww_scorer(session, conn):
    """Replay the whole session log into a fresh CqwwScorer.

    Always rebuilt from scratch (per spec: incremental math is unsafe when a
    QSO is edited/deleted). Returns the scorer, or None if not a CQ WW
    session / engine unavailable.
    """
    if not (CQWW_SCORER_OK and cqww_scorer.is_cqww(session['contest'])):
        return None
    try:
        my_zone = int((session['my_exch'] or '').strip() or 0)
    except (ValueError, TypeError):
        my_zone = 0
    scorer = cqww_scorer.CqwwScorer(session['my_callsign'] or '', my_zone)
    rows = conn.execute(
        "SELECT call, band, freq, exch_rcvd FROM qsos WHERE session_id = ? "
        "ORDER BY qso_date, time_on, id", (session['id'],)).fetchall()
    for r in rows:
        try:
            zr = int((r['exch_rcvd'] or '').strip() or 0)
        except (ValueError, TypeError):
            zr = 0
        band_int = cqww_scorer.resolve_band(r['band'], r['freq'])
        scorer.add_qso(r['call'], band_int, zr)
    return scorer


@app.route('/api/score/<int:session_id>', methods=['GET'])
def api_score(session_id):
    """Live claimed score for a CQ WW session (rebuilt from the full log)."""
    conn = get_db()
    session = conn.execute("SELECT * FROM sessions WHERE id = ?",
                           (session_id,)).fetchone()
    if not session:
        conn.close()
        return jsonify({'scoring': False, 'error': 'Session not found'}), 404
    scorer = _build_cqww_scorer(session, conn)
    conn.close()
    if scorer is None:
        # Not a CQ WW session (or cty.dat missing) — no live score.
        return jsonify({'scoring': False,
                        'cty_ok': bool(CQWW_SCORER_OK and cqww_scorer
                                       and cqww_scorer.get_lookup().ok)})
    out = scorer.summary()
    out['scoring'] = True
    out['cty_ok'] = scorer.lookup.ok
    return jsonify(out)


@app.route('/api/score-preview', methods=['GET'])
def api_score_preview():
    """Pre-log preview for the entry screen: points + NEW ZONE / NEW COUNTRY
    for the currently typed call/band/zone, without mutating the log."""
    session_id = request.args.get('session_id')
    call = request.args.get('call', '')
    band = request.args.get('band', '')
    freq = request.args.get('freq', '')
    try:
        zone = int((request.args.get('zone', '') or '').strip() or 0)
    except ValueError:
        zone = 0
    if not session_id:
        return jsonify({'scoring': False})
    conn = get_db()
    session = conn.execute("SELECT * FROM sessions WHERE id = ?",
                           (session_id,)).fetchone()
    if not session:
        conn.close()
        return jsonify({'scoring': False})
    scorer = _build_cqww_scorer(session, conn)
    conn.close()
    if scorer is None:
        return jsonify({'scoring': False})
    # Probe against the rebuilt state WITHOUT committing the QSO.
    r = scorer.add_qso(call, cqww_scorer.resolve_band(band, freq), zone)
    r['scoring'] = True
    return jsonify(r)


@app.route('/api/cty-info', methods=['GET'])
def api_cty_info():
    """Report cty.dat availability + source path (for diagnostics/UI)."""
    if not CQWW_SCORER_OK:
        return jsonify({'ok': False})
    lk = cqww_scorer.get_lookup()
    return jsonify({'ok': lk.ok, 'path': lk.path,
                    'prefixes': len(lk.prefix), 'exact': len(lk.exact)})


def refresh_qso_locations(session_id=None, only_missing=True, limit=None):
    """Backfill qso_lat/qso_lon (and grid/name/country) on existing QSOs by
    resolving each call through the tiers (DB → our DB → QRZ). Also grows our
    own DB as a side effect. Returns a summary dict.

    only_missing=True  -> only QSOs with no coords (default; faster, QRZ-friendly)
    session_id=None    -> all sessions; else just that one.
    """
    conn = get_db()
    where = []
    args = []
    if session_id:
        where.append("session_id = ?"); args.append(session_id)
    if only_missing:
        where.append("(qso_lat IS NULL OR qso_lon IS NULL)")
    sql = "SELECT id, call, gridsquare, exch_rcvd FROM qsos"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id"
    if limit:
        sql += " LIMIT %d" % int(limit)
    rows = conn.execute(sql, args).fetchall()

    updated = 0
    stillmissing = 0
    missing_calls = []
    for r in rows:
        call = r["call"]
        info = lookup_callsign_tiered(call) or {}
        lat, lon = info.get("lat"), info.get("lon")
        grid = info.get("grid") or r["gridsquare"] or ""
        if (lat is None or lon is None) and grid:
            try:
                g = grid_to_latlon(grid)
                if g:
                    lat, lon = g
            except (ValueError, IndexError, TypeError):
                pass
        # Next-to-last: DXCC country centroid (from the prefix; no QRZ needed).
        if lat is None or lon is None:
            lat, lon = _cty_centroid(call)
        # Final resort for CONTEST QSOs: the logged received CQ zone gives at
        # least a regional point when the call's prefix doesn't resolve (special
        # / portable / event calls). exch_rcvd holds the CQ zone for CQ-WW logs.
        if (lat is None or lon is None) and CQWW_SCORER_OK:
            try:
                zll = cqww_scorer.cq_zone_latlon(r["exch_rcvd"])
                if zll:
                    lat, lon = zll
            except Exception:
                pass
        if lat is None or lon is None:
            stillmissing += 1
            if call not in missing_calls:
                missing_calls.append(call)
            continue
        conn.execute(
            "UPDATE qsos SET qso_lat=?, qso_lon=?, "
            "gridsquare=COALESCE(NULLIF(gridsquare,''), ?), "
            "name=COALESCE(NULLIF(name,''), ?), "
            "country=COALESCE(NULLIF(country,''), ?), "
            "state=COALESCE(NULLIF(state,''), ?) "
            "WHERE id=?",
            (lat, lon, grid, info.get("name", ""), info.get("country", ""),
             info.get("state", ""), r["id"]))
        updated += 1
    conn.commit()
    conn.close()
    return {"scanned": len(rows), "updated": updated,
            "still_missing": stillmissing, "missing_calls": missing_calls}


@app.route('/api/refresh-locations', methods=['POST'])
def api_refresh_locations():
    """Backfill map coordinates on existing QSOs (DB → our DB → QRZ).
    JSON body: {session_id?: int, only_missing?: bool (default true)}.
    Needs QRZ configured+online to resolve calls not already in a local DB."""
    data = request.get_json(silent=True) or {}
    sid = data.get('session_id')
    only_missing = data.get('only_missing', True)
    try:
        result = refresh_qso_locations(session_id=sid, only_missing=only_missing)
        result['ok'] = True
        return jsonify(result)
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/api/mydb-info', methods=['GET'])
def api_mydb_info():
    """How many stations are in our own growing DB."""
    try:
        conn = get_db()
        n = conn.execute("SELECT COUNT(*) FROM worked_callsigns").fetchone()[0]
        withloc = conn.execute(
            "SELECT COUNT(*) FROM worked_callsigns WHERE lat IS NOT NULL").fetchone()[0]
        conn.close()
        return jsonify({'ok': True, 'count': n, 'with_location': withloc})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)})


# ============================================================================
# Voice Keyer
# ============================================================================

_vk_record_proc = None
_vk_play_proc = None
_vk_play_thread = None
_vk_stop_event = threading.Event()
_vk_state = 'idle'  # idle, recording, playing


def get_cq_msg_dir():
    """Get CQ message storage directory. Prefers USB persistence if mounted."""
    try:
        for user_dir in Path("/media").iterdir():
            emcomm = user_dir / "EMCOMM-DATA"
            if emcomm.exists():
                usb = emcomm / "CQ-MSG"
                usb.mkdir(parents=True, exist_ok=True)
                return usb
    except (PermissionError, OSError):
        pass
    d = LOGGER_DIR / "cq-messages"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _safe_filename(name):
    """Sanitize filename — alphanumeric, hyphens, underscores only."""
    name = re.sub(r'[^\w\-]', '_', name.strip())
    return name[:100] if name else 'message'


def _get_wav_duration(filepath):
    """Get WAV file duration in seconds."""
    try:
        with wave.open(str(filepath), 'r') as wf:
            frames = wf.getnframes()
            rate = wf.getframerate()
            if rate > 0:
                return round(frames / rate, 1)
    except Exception:
        pass
    return 0


def _list_capture_devices():
    """List available ALSA capture devices via arecord -l.
    Excludes ET_AUDIO (radio USB audio) — that's for playback to radio only."""
    devices = []
    try:
        result = subprocess.run(['arecord', '-l'], capture_output=True,
                                text=True, timeout=5)
        for line in result.stdout.split('\n'):
            m = re.match(r'^card (\d+): (\w+) \[(.+?)\], device (\d+): (.+)', line)
            if m:
                card_num, card_id, card_name, dev_num, dev_name = m.groups()
                if card_id == 'ET_AUDIO':
                    continue
                devices.append({
                    'id': f'plughw:{card_num},{dev_num}',
                    'name': f'{card_name} - {dev_name.strip()}',
                    'card': card_id,
                })
    except Exception:
        pass
    return devices


def _play_over_air(filepath, repeat=False, delay=3.0):
    """Play a WAV file over the air with PTT. Runs in a thread."""
    global _vk_play_proc, _vk_state
    _vk_stop_event.clear()
    _vk_state = 'playing'
    try:
        while True:
            if rig:
                rig.set_ptt(True)
            time.sleep(0.3)
            _vk_play_proc = subprocess.Popen(
                ['aplay', '-D', 'plughw:ET_AUDIO,0', str(filepath)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            _vk_play_proc.wait()
            _vk_play_proc = None
            if rig:
                rig.set_ptt(False)
            if not repeat or _vk_stop_event.is_set():
                break
            if _vk_stop_event.wait(timeout=delay):
                break
    finally:
        if rig:
            rig.set_ptt(False)
        _vk_play_proc = None
        _vk_state = 'idle'


@app.route('/api/voicekeyer/devices', methods=['GET'])
def api_vk_devices():
    """List available ALSA capture devices."""
    return jsonify({'devices': _list_capture_devices()})


@app.route('/api/voicekeyer/messages', methods=['GET'])
def api_vk_messages():
    """List all recorded voice messages."""
    msg_dir = get_cq_msg_dir()
    messages = []
    for f in sorted(msg_dir.glob('*.wav')):
        messages.append({
            'filename': f.name,
            'title': f.stem,
            'size': f.stat().st_size,
            'duration': _get_wav_duration(f),
        })
    return jsonify({'messages': messages, 'state': _vk_state,
                    'path': str(msg_dir)})


@app.route('/api/voicekeyer/record', methods=['POST'])
def api_vk_record_start():
    """Start recording a voice message."""
    global _vk_record_proc, _vk_state
    if _vk_state != 'idle':
        return jsonify({'success': False, 'error': f'Busy: {_vk_state}'}), 409

    data = request.get_json()
    name = _safe_filename(data.get('filename', 'message'))
    device = data.get('device', 'default')
    msg_dir = get_cq_msg_dir()
    filepath = msg_dir / f'{name}.wav'

    # Don't overwrite — append number
    counter = 1
    while filepath.exists():
        filepath = msg_dir / f'{name}_{counter}.wav'
        counter += 1

    try:
        _vk_record_proc = subprocess.Popen(
            ['arecord', '-D', device, '-f', 'S16_LE', '-r', '44100',
             '-c', '1', str(filepath)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        _vk_state = 'recording'
        return jsonify({'success': True, 'filename': filepath.name})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/voicekeyer/record', methods=['DELETE'])
def api_vk_record_stop():
    """Stop recording."""
    global _vk_record_proc, _vk_state
    if _vk_record_proc:
        _vk_record_proc.terminate()
        try:
            _vk_record_proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            _vk_record_proc.kill()
        _vk_record_proc = None
    _vk_state = 'idle'
    return jsonify({'success': True})


@app.route('/api/voicekeyer/play', methods=['POST'])
def api_vk_play_start():
    """Play a message over the air with PTT."""
    global _vk_play_thread
    if _vk_state != 'idle':
        return jsonify({'success': False, 'error': f'Busy: {_vk_state}'}), 409

    data = request.get_json()
    filename = data.get('filename', '')
    repeat = data.get('repeat', False)
    delay = float(data.get('delay', 3.0))

    filepath = get_cq_msg_dir() / filename
    if not filepath.exists() or not filepath.suffix == '.wav':
        return jsonify({'success': False, 'error': 'File not found'}), 404

    _vk_play_thread = threading.Thread(
        target=_play_over_air, args=(filepath, repeat, delay), daemon=True)
    _vk_play_thread.start()
    return jsonify({'success': True})


@app.route('/api/voicekeyer/play', methods=['DELETE'])
def api_vk_play_stop():
    """Stop playback and unkey PTT."""
    global _vk_play_proc, _vk_state
    _vk_stop_event.set()
    if _vk_play_proc:
        try:
            _vk_play_proc.terminate()
        except Exception:
            pass
    if rig:
        rig.set_ptt(False)
    _vk_state = 'idle'
    return jsonify({'success': True})


@app.route('/api/voicekeyer/preview/<filename>', methods=['GET'])
def api_vk_preview(filename):
    """Stream WAV file for browser preview (laptop speakers, no PTT)."""
    filepath = get_cq_msg_dir() / filename
    if not filepath.exists():
        return jsonify({'error': 'Not found'}), 404
    return send_file(str(filepath), mimetype='audio/wav')


@app.route('/api/voicekeyer/message/<filename>', methods=['DELETE'])
def api_vk_delete(filename):
    """Delete a voice message."""
    filepath = get_cq_msg_dir() / filename
    if filepath.exists():
        filepath.unlink()
        return jsonify({'success': True})
    return jsonify({'error': 'Not found'}), 404


@app.route('/api/voicekeyer/message/<filename>', methods=['PUT'])
def api_vk_rename(filename):
    """Rename a voice message."""
    data = request.get_json()
    new_name = _safe_filename(data.get('new_name', ''))
    if not new_name:
        return jsonify({'success': False, 'error': 'Invalid name'}), 400

    msg_dir = get_cq_msg_dir()
    old_path = msg_dir / filename
    new_path = msg_dir / f'{new_name}.wav'

    if not old_path.exists():
        return jsonify({'error': 'Not found'}), 404
    if new_path.exists():
        return jsonify({'success': False, 'error': 'Name already exists'}), 409

    old_path.rename(new_path)
    return jsonify({'success': True, 'filename': new_path.name})


# ============================================================================
# Air Recorder
# ============================================================================

_ar_record_proc = None
_ar_state = 'idle'  # idle, recording


def get_air_rec_dir():
    """Get air recordings storage directory. Prefers USB persistence if mounted."""
    try:
        for user_dir in Path("/media").iterdir():
            emcomm = user_dir / "EMCOMM-DATA"
            if emcomm.exists():
                usb = emcomm / "AIR-REC"
                usb.mkdir(parents=True, exist_ok=True)
                return usb
    except (PermissionError, OSError):
        pass
    d = LOGGER_DIR / "air-recordings"
    d.mkdir(parents=True, exist_ok=True)
    return d


@app.route('/api/airrecorder/messages', methods=['GET'])
def api_ar_messages():
    """List all air recordings."""
    rec_dir = get_air_rec_dir()
    recordings = []
    for f in sorted(rec_dir.glob('*.wav'), key=lambda x: x.stat().st_mtime,
                    reverse=True):
        recordings.append({
            'filename': f.name,
            'title': f.stem,
            'size': f.stat().st_size,
            'duration': _get_wav_duration(f),
        })
    return jsonify({'recordings': recordings, 'state': _ar_state,
                    'path': str(rec_dir)})


@app.route('/api/airrecorder/record', methods=['POST'])
def api_ar_record_start():
    """Start recording from ET_AUDIO (radio RX audio)."""
    global _ar_record_proc, _ar_state
    if _ar_state != 'idle':
        return jsonify({'success': False, 'error': f'Busy: {_ar_state}'}), 409

    data = request.get_json()
    name = _safe_filename(data.get('filename', ''))
    if not name:
        name = datetime.now().strftime('air_%Y%m%d_%H%M%S')
    rec_dir = get_air_rec_dir()
    filepath = rec_dir / f'{name}.wav'

    counter = 1
    while filepath.exists():
        filepath = rec_dir / f'{name}_{counter}.wav'
        counter += 1

    try:
        _ar_record_proc = subprocess.Popen(
            ['arecord', '-D', 'plughw:ET_AUDIO,0', '-f', 'S16_LE',
             '-r', '44100', '-c', '1', str(filepath)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        _ar_state = 'recording'
        return jsonify({'success': True, 'filename': filepath.name})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/airrecorder/record', methods=['DELETE'])
def api_ar_record_stop():
    """Stop air recording."""
    global _ar_record_proc, _ar_state
    if _ar_record_proc:
        _ar_record_proc.terminate()
        try:
            _ar_record_proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            _ar_record_proc.kill()
        _ar_record_proc = None
    _ar_state = 'idle'
    return jsonify({'success': True})


@app.route('/api/airrecorder/preview/<filename>', methods=['GET'])
def api_ar_preview(filename):
    """Stream air recording WAV for browser playback."""
    filepath = get_air_rec_dir() / filename
    if not filepath.exists():
        return jsonify({'error': 'Not found'}), 404
    return send_file(str(filepath), mimetype='audio/wav')


@app.route('/api/airrecorder/message/<filename>', methods=['DELETE'])
def api_ar_delete(filename):
    """Delete an air recording."""
    filepath = get_air_rec_dir() / filename
    if filepath.exists():
        filepath.unlink()
        return jsonify({'success': True})
    return jsonify({'error': 'Not found'}), 404


@app.route('/api/airrecorder/device', methods=['GET'])
def api_ar_device():
    """Check if ET_AUDIO capture device is available."""
    try:
        result = subprocess.run(['arecord', '-l'], capture_output=True,
                                text=True, timeout=5)
        available = 'ET_AUDIO' in result.stdout
        return jsonify({'available': available})
    except Exception:
        return jsonify({'available': False})


@app.route('/api/airrecorder/message/<filename>', methods=['PUT'])
def api_ar_rename(filename):
    """Rename an air recording."""
    data = request.get_json()
    new_name = _safe_filename(data.get('new_name', ''))
    if not new_name:
        return jsonify({'success': False, 'error': 'Invalid name'}), 400

    rec_dir = get_air_rec_dir()
    old_path = rec_dir / filename
    new_path = rec_dir / f'{new_name}.wav'

    if not old_path.exists():
        return jsonify({'error': 'Not found'}), 404
    if new_path.exists():
        return jsonify({'success': False, 'error': 'Name already exists'}), 409

    old_path.rename(new_path)
    return jsonify({'success': True, 'filename': new_path.name})


# ============================================================================
# Main
# ============================================================================

# ============================================================================
# Antennas — profile CRUD, precomputed patterns, best-antenna-for-target
# ============================================================================

def _antenna_unavailable():
    return jsonify({
        "ok": False,
        "error": "antenna feature unavailable — antenna_service import failed",
        "detail": _antenna_import_error if not ANTENNA_SERVICE_OK else "",
    }), 503


def _get_session_qth(session_id):
    """Look up (my_lat, my_lon) for a session so best-for uses the
    operator's actual QTH instead of the antenna profile's site coords.
    Returns (lat, lon) or (None, None) if session missing / no coords set."""
    conn = get_db()
    row = conn.execute(
        "SELECT my_lat, my_lon FROM sessions WHERE id = ?", (session_id,)
    ).fetchone()
    conn.close()
    if row and row["my_lat"] is not None and row["my_lon"] is not None:
        return float(row["my_lat"]), float(row["my_lon"])
    return None, None


@app.route('/api/antennas', methods=['GET'])
def api_antennas_list():
    if not ANTENNA_SERVICE_OK:
        return _antenna_unavailable()
    conn = get_db()
    try:
        return jsonify({"ok": True, "antennas": antenna_service.list_profiles(conn)})
    finally:
        conn.close()


@app.route('/api/antennas', methods=['POST'])
def api_antennas_create():
    if not ANTENNA_SERVICE_OK:
        return _antenna_unavailable()
    payload = request.get_json(force=True, silent=True) or {}
    conn = get_db()
    try:
        new_id, warnings = antenna_service.create_profile(conn, payload)
        return jsonify({"ok": True, "id": new_id, "warnings": warnings})
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    except ImportError as e:
        return jsonify({"ok": False,
                        "error": f"PyNEC not available: {e}"}), 503
    except Exception as e:
        return jsonify({"ok": False,
                        "error": f"{type(e).__name__}: {e}"}), 500
    finally:
        conn.close()


@app.route('/api/antennas/<int:profile_id>', methods=['GET'])
def api_antennas_get(profile_id):
    if not ANTENNA_SERVICE_OK:
        return _antenna_unavailable()
    conn = get_db()
    try:
        p = antenna_service.get_profile(conn, profile_id)
        if p is None:
            return jsonify({"ok": False, "error": "not found"}), 404
        return jsonify({"ok": True, "antenna": p})
    finally:
        conn.close()


@app.route('/api/antennas/<int:profile_id>', methods=['PUT'])
def api_antennas_update(profile_id):
    if not ANTENNA_SERVICE_OK:
        return _antenna_unavailable()
    payload = request.get_json(force=True, silent=True) or {}
    conn = get_db()
    try:
        if antenna_service.get_profile(conn, profile_id) is None:
            return jsonify({"ok": False, "error": "not found"}), 404
        warnings = antenna_service.update_profile(conn, profile_id, payload)
        return jsonify({"ok": True, "warnings": warnings})
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    except ImportError as e:
        return jsonify({"ok": False,
                        "error": f"PyNEC not available: {e}"}), 503
    except Exception as e:
        return jsonify({"ok": False,
                        "error": f"{type(e).__name__}: {e}"}), 500
    finally:
        conn.close()


@app.route('/api/antennas/<int:profile_id>', methods=['DELETE'])
def api_antennas_delete(profile_id):
    if not ANTENNA_SERVICE_OK:
        return _antenna_unavailable()
    conn = get_db()
    try:
        if antenna_service.get_profile(conn, profile_id) is None:
            return jsonify({"ok": False, "error": "not found"}), 404
        antenna_service.delete_profile(conn, profile_id)
        return jsonify({"ok": True})
    finally:
        conn.close()


@app.route('/api/antennas/best-for', methods=['GET'])
def api_antennas_best_for():
    if not ANTENNA_SERVICE_OK:
        return _antenna_unavailable()
    try:
        target_lat = float(request.args["lat"])
        target_lon = float(request.args["lon"])
        band_name = request.args["band"]
    except (KeyError, TypeError, ValueError):
        return jsonify({
            "ok": False,
            "error": "lat, lon, band query params required (band like '20m')"
        }), 400

    session_id = request.args.get("session_id", type=int)
    takeoff = request.args.get("takeoff", type=float)
    qth_lat, qth_lon = (_get_session_qth(session_id)
                        if session_id else (None, None))

    conn = get_db()
    try:
        results = antenna_service.best_for(
            conn, target_lat, target_lon, band_name,
            qth_lat=qth_lat, qth_lon=qth_lon,
            takeoff_deg=takeoff,
        )
        return jsonify({
            "ok": True,
            "results": results,
            "qth_source": "session" if qth_lat is not None else "profile",
        })
    finally:
        conn.close()


@app.route('/api/antennas/<int:profile_id>/pattern', methods=['GET'])
def api_antennas_pattern(profile_id):
    if not ANTENNA_SERVICE_OK:
        return _antenna_unavailable()
    band = request.args.get("band")
    takeoff = request.args.get("takeoff", type=float,
                                default=antenna_service.DEFAULT_TAKEOFF_DEG)
    if not band:
        return jsonify({"ok": False,
                        "error": "band query param required"}), 400
    conn = get_db()
    try:
        pattern = antenna_service.get_pattern_json(conn, profile_id,
                                                     band, takeoff)
        if pattern is None:
            return jsonify({"ok": False,
                            "error": "profile or band not found"}), 404
        return jsonify({"ok": True, "pattern": pattern,
                        "band": band, "takeoff_deg": takeoff})
    except ImportError as e:
        return jsonify({"ok": False,
                        "error": f"PyNEC not available: {e}"}), 503
    except Exception as e:
        return jsonify({"ok": False,
                        "error": f"{type(e).__name__}: {e}"}), 500
    finally:
        conn.close()


# ============================================================================
# Flask startup
# ============================================================================

def run_flask(port):
    app.run(host='127.0.0.1', port=port, debug=False,
            use_reloader=False, threaded=True)


def ensure_mbtileserver():
    """Start the local raster tile server (mbtileserver on :1982) if it isn't
    already running, so the Map tab has OFFLINE tiles without depending on
    et-predict-app to have been opened first. Mirrors et-predict's launcher:
    if the port is closed and tilesets exist, launch mbtileserver detached."""
    import socket, glob, shutil
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            if s.connect_ex(('127.0.0.1', 1982)) == 0:
                return  # already running (e.g. started by et-predict)
    except OSError:
        pass
    tiles_dir = os.path.realpath(
        os.path.expanduser('~/.local/share/emcomm-tools/mbtileserver/tilesets'))
    if not glob.glob(os.path.join(tiles_dir, '*.mbtiles')):
        return  # no tilesets to serve yet
    binpath = shutil.which('mbtileserver') or '/usr/local/bin/mbtileserver'
    try:
        subprocess.Popen(
            [binpath, '--port', '1982', '-d', tiles_dir, '--enable-fs-watch'],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True)   # detach so it outlives et-logger
        time.sleep(2)
    except (OSError, ValueError):
        pass


if __name__ == '__main__':
    port = 5059
    init_db()
    ensure_mbtileserver()   # ensure offline map tiles are served

    if '--no-browser' in sys.argv:
        app.run(host='127.0.0.1', port=port, debug=False, threaded=True)
    elif '--browser' in sys.argv:
        threading.Thread(target=lambda: (time.sleep(1),
            webbrowser.open(f'http://127.0.0.1:{port}')), daemon=True).start()
        app.run(host='127.0.0.1', port=port, debug=False, threaded=True)
    elif '--help' in sys.argv:
        print("Usage: et-logger [OPTIONS]")
        print("  --no-browser    Server only")
        print("  --browser       Open in browser")
        print("  --help          Show help")
        print("Default: PyWebView window")
        sys.exit(0)
    else:
        try:
            import webview
            flask_thread = threading.Thread(target=run_flask, args=(port,), daemon=True)
            flask_thread.start()
            time.sleep(1)
            window = webview.create_window(
                'LiaisonOS - Field Logger',
                f'http://127.0.0.1:{port}',
                width=900, height=700,
                resizable=True, min_size=(600, 400),
                maximized=True, frameless=False)
            webview.start()
        except ImportError:
            threading.Thread(target=lambda: (time.sleep(1),
                webbrowser.open(f'http://127.0.0.1:{port}')), daemon=True).start()
            app.run(host='127.0.0.1', port=port, debug=False, threaded=True)
