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

# Suppress Flask dev server warning
log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)

app = Flask(__name__)
app.secret_key = 'emcomm-tools-logger-2026'

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
            notes TEXT DEFAULT ''
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
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (session_id) REFERENCES sessions(id)
        )
    """)
    # Antenna profiles + pattern cache — module lives alongside et-logger.py.
    # Import failure is non-fatal so et-logger still starts on machines
    # without PyNEC (dev boxes); the /api/antennas/* routes gate on the flag.
    if ANTENNA_SERVICE_OK:
        antenna_service.init_schema(conn)
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
    """
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
    """Look up a single callsign."""
    info = lookup_callsign(call)
    if info:
        return jsonify(info)
    return jsonify({}), 404


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
    lat, lon, _ = get_position()
    grid = latlon_to_grid(lat, lon) if lat else config.get('grid', '')

    conn = get_db()
    cursor = conn.execute("""
        INSERT INTO sessions (name, program, my_sig, my_sig_info,
                              my_callsign, my_grid, my_lat, my_lon, notes)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        data.get('name', f"Session {datetime.now().strftime('%Y-%m-%d %H:%M')}"),
        data.get('program', 'POTA'),
        data.get('program', 'POTA'),
        data.get('sig_info', ''),
        data.get('my_callsign', config.get('callsign', '')),
        grid,
        lat, lon,
        data.get('notes', ''),
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


def _p2p_or_callsign_location(sig_info, call_info):
    """Return (lat, lon) — park location if P2P, otherwise callsign home QTH."""
    if sig_info:
        # Use first P2P park reference
        first_park = sig_info.split(',')[0].strip()
        if first_park:
            lat, lon = lookup_park_location(first_park)
            if lat is not None:
                return lat, lon
    # Fallback to callsign lookup location
    if call_info:
        return call_info.get('lat'), call_info.get('lon')
    return None, None


@app.route('/api/qso', methods=['POST'])
def api_log_qso():
    """Log a new QSO."""
    data = request.get_json()
    now = datetime.now(timezone.utc)

    # Use values from frontend (no radio query — already pre-filled)
    freq = data.get('freq')
    band = data.get('band', '')
    mode = data.get('mode', 'SSB')

    # Lookup callsign for name/location
    call_info = lookup_callsign(data.get('call', ''))
    # P2P: use park location, otherwise callsign home QTH
    qso_lat, qso_lon = _p2p_or_callsign_location(data.get('sig_info', ''), call_info)

    conn = get_db()
    cursor = conn.execute("""
        INSERT INTO qsos (session_id, qso_date, time_on, call, freq, band,
                          mode, rst_sent, rst_rcvd, tx_pwr, gridsquare,
                          name, state, country, sig, sig_info, comment,
                          qso_lat, qso_lon)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        data.get('session_id'),
        data.get('qso_date', now.strftime('%Y%m%d')),
        data.get('time_on', now.strftime('%H%M')),
        data.get('call', '').upper().strip(),
        freq,
        band,
        mode,
        data.get('rst_sent', '59'),
        data.get('rst_rcvd', '59'),
        data.get('tx_pwr'),
        data.get('gridsquare', ''),
        data.get('name', '') or (call_info.get('name', '') if call_info else ''),
        data.get('state', '') or (call_info.get('state', '') if call_info else ''),
        data.get('country', '') or (call_info.get('country', '') if call_info else ''),
        data.get('sig', ''),
        data.get('sig_info', ''),
        data.get('comment', ''),
        qso_lat,
        qso_lon,
    ))
    conn.commit()
    qso_id = cursor.lastrowid

    # Get updated count
    count = conn.execute("SELECT COUNT(*) FROM qsos WHERE session_id = ?",
                         (data.get('session_id'),)).fetchone()[0]
    conn.close()

    return jsonify({'success': True, 'id': qso_id, 'count': count})


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
            comment=?, sig=?, sig_info=?
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
    """Check if a QSO is a duplicate."""
    session_id = request.args.get('session_id')
    call = request.args.get('call', '').upper().strip()
    if not session_id or not call:
        return jsonify({'duplicate': False})

    conn = get_db()
    existing = conn.execute(
        "SELECT id, time_on FROM qsos WHERE session_id = ? AND call = ?",
        (session_id, call)).fetchone()
    conn.close()

    if existing:
        return jsonify({'duplicate': True, 'time': existing['time_on']})
    return jsonify({'duplicate': False})


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


if __name__ == '__main__':
    port = 5059
    init_db()

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
