#!/usr/bin/env python3
"""
et-repeater - LiaisonOS Repeater Directory
Date: March 2026

Flask-based web UI for browsing repeaters from RepeaterBook CSV exports.
Import CSV files, filter by band/distance, program radio via rigctld.
Fully offline — no API key required.
"""

import warnings
warnings.filterwarnings("ignore", category=DeprecationWarning)
warnings.filterwarnings("ignore", message=".*pkg_resources.*")

import os
import sys
import csv
import io
import json
import subprocess
import webbrowser
import threading
import time
import logging
import math
from pathlib import Path
from datetime import datetime
from flask import Flask, render_template, request, jsonify

# Suppress Flask development server warning
log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)

app = Flask(__name__)
app.secret_key = 'emcomm-tools-repeater-2026'

# Configuration paths
ET_CONFIG_DIR = Path.home() / ".config" / "emcomm-tools"
ET_CONFIG_FILE = ET_CONFIG_DIR / "user.json"
CACHE_DIR = ET_CONFIG_DIR / "repeaters"
CACHE_FILE = CACHE_DIR / "repeaters.json"
FAVORITES_FILE = CACHE_DIR / "favorites.json"
FILTERS_FILE = CACHE_DIR / "filters.json"

# Import progress tracking
import_progress = {
    'active': False,
    'total': 0,
    'current': 0,
    'current_location': '',
    'phase': '',  # 'parsing', 'geocoding', 'saving', 'done'
    'result': None,
}


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


def get_user_position():
    """Get user lat/lon from user.json (direct coords or grid fallback)."""
    config = load_user_config()

    lat = config.get('latitude', '')
    lon = config.get('longitude', '')
    if lat and lon:
        try:
            return float(lat), float(lon)
        except (ValueError, TypeError):
            pass

    grid = config.get('grid', config.get('grid_square', ''))
    if grid and len(grid) >= 4:
        result = grid_to_latlon(grid)
        if result:
            return result

    return None, None


def grid_to_latlon(grid):
    """Convert Maidenhead grid square to (latitude, longitude)."""
    grid = grid.strip().upper()
    if len(grid) < 4:
        return None
    if not ("A" <= grid[0] <= "R" and "A" <= grid[1] <= "R"):
        return None
    if not (grid[2].isdigit() and grid[3].isdigit()):
        return None

    lon = (ord(grid[0]) - ord("A")) * 20 - 180
    lat = (ord(grid[1]) - ord("A")) * 10 - 90
    lon += int(grid[2]) * 2
    lat += int(grid[3]) * 1

    if len(grid) >= 6:
        sub_lon = grid[4].upper()
        sub_lat = grid[5].upper()
        if "A" <= sub_lon <= "X" and "A" <= sub_lat <= "X":
            lon += (ord(sub_lon) - ord("A")) * (2.0 / 24)
            lat += (ord(sub_lat) - ord("A")) * (1.0 / 24)
            lon += 1.0 / 24
            lat += 0.5 / 24
        else:
            lon += 1
            lat += 0.5
    else:
        lon += 1
        lat += 0.5

    return (round(lat, 6), round(lon, 6))


# ============================================================================
# Distance Calculation
# ============================================================================

def haversine_km(lat1, lon1, lat2, lon2):
    """Calculate distance between two points in km using Haversine formula."""
    R = 6371.0  # Earth radius in km
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2 +
         math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) *
         math.sin(dlon / 2) ** 2)
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return R * c


# ============================================================================
# Geocoding & Grid Conversion
# ============================================================================

def latlon_to_grid(lat, lon):
    """Convert latitude/longitude to 6-char Maidenhead grid square."""
    lon = lon + 180
    lat = lat + 90

    field_lon = int(lon / 20)
    field_lat = int(lat / 10)

    square_lon = int((lon - field_lon * 20) / 2)
    square_lat = int((lat - field_lat * 10) / 1)

    sub_lon = int((lon - field_lon * 20 - square_lon * 2) / (2 / 24))
    sub_lat = int((lat - field_lat * 10 - square_lat * 1) / (1 / 24))

    grid = (chr(ord('A') + field_lon) + chr(ord('A') + field_lat) +
            str(square_lon) + str(square_lat) +
            chr(ord('a') + sub_lon) + chr(ord('a') + sub_lat))
    return grid


# Geocode cache to avoid hitting Nominatim for duplicate locations
_geocode_cache = {}


def geocode_location(location, state):
    """Geocode a location string to (lat, lon) using Nominatim."""
    import urllib.request
    import urllib.parse

    # Clean up location (remove stuff after " - ")
    clean_loc = location.split(' - ')[0].strip() if ' - ' in location else location

    cache_key = f"{clean_loc}, {state}"
    if cache_key in _geocode_cache:
        return _geocode_cache[cache_key]

    query = f"{clean_loc}, {state}"
    params = urllib.parse.urlencode({
        'q': query,
        'format': 'json',
        'limit': 1,
    })
    url = f"https://nominatim.openstreetmap.org/search?{params}"

    try:
        req = urllib.request.Request(url)
        req.add_header('User-Agent', 'LiaisonOS')
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode('utf-8'))

        if data:
            lat = float(data[0]['lat'])
            lon = float(data[0]['lon'])
            _geocode_cache[cache_key] = (lat, lon)
            return lat, lon
    except Exception:
        pass

    _geocode_cache[cache_key] = (None, None)
    return None, None


def geocode_repeaters(repeaters):
    """Add grid, lat, lon to repeaters by geocoding their location."""
    # Group by unique location+state to minimize API calls
    unique_locations = {}
    for r in repeaters:
        loc = r.get('location', '')
        state = r.get('state', '')
        if loc:
            key = f"{loc.split(' - ')[0].strip()}|{state}"
            if key not in unique_locations:
                unique_locations[key] = (loc, state)

    # Geocode unique locations (with rate limiting for Nominatim: 1 req/sec)
    location_coords = {}
    total_locs = len(unique_locations)
    current_loc = 0
    for key, (loc, state) in unique_locations.items():
        current_loc += 1
        import_progress['current'] = current_loc
        import_progress['total'] = total_locs
        import_progress['current_location'] = loc.split(' - ')[0].strip()

        lat, lon = geocode_location(loc, state)
        if lat is not None:
            location_coords[key] = (lat, lon, latlon_to_grid(lat, lon))
        time.sleep(1.1)  # Nominatim rate limit: max 1 request per second

    # Apply to repeaters
    geocoded = 0
    for r in repeaters:
        loc = r.get('location', '')
        state = r.get('state', '')
        key = f"{loc.split(' - ')[0].strip()}|{state}"
        if key in location_coords:
            lat, lon, grid = location_coords[key]
            r['lat'] = lat
            r['lon'] = lon
            r['grid'] = grid
            geocoded += 1
        else:
            r['lat'] = None
            r['lon'] = None
            r['grid'] = ''

    return geocoded


# ============================================================================
# CSV Import & Cache
# ============================================================================

def parse_repeaterbook_csv(csv_text):
    """Parse RepeaterBook CSV export into repeater list.

    CSV columns: Output Freq, Input Freq, Offset, Uplink Tone, Downlink Tone,
                 Call, Location, County, State, Modes, Digital Access
    """
    repeaters = []
    reader = csv.DictReader(io.StringIO(csv_text))

    for row in reader:
        try:
            freq = float(row.get('Output Freq', 0))
        except (ValueError, TypeError):
            continue

        if freq <= 0:
            continue

        # Parse offset direction and value
        input_freq = row.get('Input Freq', '')
        offset = row.get('Offset', '')
        offset_mhz = ''
        try:
            inp = float(input_freq) if input_freq else 0
            if inp > 0:
                diff = round(inp - freq, 4)
                if diff > 0:
                    offset_mhz = f"+{diff}"
                elif diff < 0:
                    offset_mhz = str(diff)
        except (ValueError, TypeError):
            if offset in ('+', '-'):
                offset_mhz = offset

        # Uplink tone (what you transmit)
        tone = row.get('Uplink Tone', '').strip()
        if tone in ('', 'CSQ', 'None'):
            tone = ''

        repeaters.append({
            'callsign': row.get('Call', '').strip(),
            'frequency': freq,
            'input_freq': input_freq,
            'offset': offset_mhz,
            'tone': tone,
            'downlink_tone': row.get('Downlink Tone', '').strip(),
            'location': row.get('Location', '').strip().strip('"'),
            'county': row.get('County', '').strip(),
            'state': row.get('State', '').strip(),
            'modes': row.get('Modes', '').strip(),
            'digital_access': row.get('Digital Access', '').strip(),
        })

    return repeaters


def load_cached_repeaters():
    """Load all cached repeater data. Returns (list, metadata) or ([], {})."""
    if CACHE_FILE.exists():
        try:
            with open(CACHE_FILE, 'r') as f:
                data = json.load(f)
                return data.get('repeaters', []), data.get('meta', {})
        except Exception:
            pass
    return [], {}


def save_cached_repeaters(repeaters, source_name=''):
    """Save repeater data to cache, merging with existing data."""
    existing, meta = load_cached_repeaters()

    # Merge: use callsign+frequency as unique key
    seen = {}
    for r in existing:
        key = f"{r.get('callsign', '')}_{r.get('frequency', 0)}"
        seen[key] = r

    new_count = 0
    for r in repeaters:
        key = f"{r.get('callsign', '')}_{r.get('frequency', 0)}"
        if key not in seen:
            new_count += 1
        seen[key] = r

    merged = list(seen.values())

    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    imports = meta.get('imports', [])
    imports.append({
        'file': source_name,
        'date': datetime.now().strftime('%Y-%m-%d %H:%M'),
        'count': len(repeaters),
        'new': new_count,
    })

    data = {
        'meta': {
            'last_updated': datetime.now().strftime('%Y-%m-%d %H:%M'),
            'total': len(merged),
            'imports': imports,
        },
        'repeaters': merged
    }

    with open(CACHE_FILE, 'w') as f:
        json.dump(data, f)

    return len(merged), new_count


def clear_cached_repeaters():
    """Clear all cached repeater data."""
    if CACHE_FILE.exists():
        CACHE_FILE.unlink()


def update_repeater(old_callsign, old_frequency, updates):
    """Update a repeater entry in the cache. Returns success."""
    repeaters, meta = load_cached_repeaters()
    if not repeaters:
        return False

    # Find the repeater by original callsign+frequency
    found = False
    try:
        old_freq_float = float(old_frequency)
    except (ValueError, TypeError):
        old_freq_float = 0
    for r in repeaters:
        if r.get('callsign', '') == old_callsign and r.get('frequency', 0) == old_freq_float:
            for field, value in updates.items():
                if field == 'frequency':
                    try:
                        r[field] = float(value)
                    except (ValueError, TypeError):
                        pass
                else:
                    r[field] = value

            # Recalculate lat/lon if grid was changed
            if 'grid' in updates and updates['grid']:
                coords = grid_to_latlon(updates['grid'])
                if coords:
                    r['lat'] = coords[0]
                    r['lon'] = coords[1]

            found = True
            break

    if found:
        # Update favorites key if callsign or frequency changed
        old_key = f"{old_callsign}_{old_frequency}"
        favorites = load_favorites()
        if old_key in favorites:
            new_key = f"{updates.get('callsign', old_callsign)}_{updates.get('frequency', old_frequency)}"
            if new_key != old_key:
                favorites.discard(old_key)
                favorites.add(new_key)
                save_favorites(favorites)

        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        data = {'meta': meta, 'repeaters': repeaters}
        with open(CACHE_FILE, 'w') as f:
            json.dump(data, f)

    return found


def delete_repeater(callsign, frequency):
    """Delete a repeater from the cache."""
    repeaters, meta = load_cached_repeaters()
    if not repeaters:
        return False

    try:
        freq_float = float(frequency)
    except (ValueError, TypeError):
        freq_float = 0
    new_list = [r for r in repeaters
                if not (r.get('callsign', '') == callsign and r.get('frequency', 0) == freq_float)]

    if len(new_list) == len(repeaters):
        return False

    # Remove from favorites too
    key = f"{callsign}_{frequency}"
    favorites = load_favorites()
    if key in favorites:
        favorites.discard(key)
        save_favorites(favorites)

    meta['total'] = len(new_list)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    with open(CACHE_FILE, 'w') as f:
        json.dump({'meta': meta, 'repeaters': new_list}, f)

    return True


# ============================================================================
# Favorites
# ============================================================================

def load_favorites():
    """Load favorites set (callsign_frequency keys)."""
    if FAVORITES_FILE.exists():
        try:
            with open(FAVORITES_FILE, 'r') as f:
                return set(json.load(f))
        except Exception:
            pass
    return set()


def save_favorites(favorites):
    """Save favorites set."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    with open(FAVORITES_FILE, 'w') as f:
        json.dump(list(favorites), f)


def toggle_favorite(callsign, frequency):
    """Toggle a repeater as favorite. Returns new state."""
    key = f"{callsign}_{frequency}"
    favorites = load_favorites()
    if key in favorites:
        favorites.discard(key)
        is_fav = False
    else:
        favorites.add(key)
        is_fav = True
    save_favorites(favorites)
    return is_fav


# ============================================================================
# Repeater Filtering
# ============================================================================

def filter_repeaters(repeaters, user_lat, user_lon, band_filter='all',
                     max_distance=50, mode_filter='all'):
    """Filter, calculate distance, and sort repeaters."""
    processed = []

    for r in repeaters:
        freq = r.get('frequency', 0)
        if freq <= 0:
            continue

        # Band filter
        if band_filter == '2m' and not (144 <= freq <= 148):
            continue
        elif band_filter == '70cm' and not (420 <= freq <= 450):
            continue
        elif band_filter == '1.25m' and not (222 <= freq <= 225):
            continue
        elif band_filter == '6m' and not (50 <= freq <= 54):
            continue
        elif band_filter == '10m' and not (28 <= freq <= 29.7):
            continue

        # Mode filter
        if mode_filter != 'all':
            modes = r.get('modes', '').upper()
            if mode_filter == 'fm' and 'FM' not in modes:
                continue
            elif mode_filter == 'dmr' and 'DMR' not in modes:
                continue
            elif mode_filter == 'dstar' and 'D-STAR' not in modes:
                continue
            elif mode_filter == 'ysf' and 'YSF' not in modes:
                continue
            elif mode_filter == 'p25' and 'P25' not in modes:
                continue
            elif mode_filter == 'nxdn' and 'NXDN' not in modes:
                continue

        # Skip repeaters with no callsign (test/unverified entries)
        if not r.get('callsign', '').strip():
            continue

        # Skip repeaters with no grid (bad geocoding / bad data)
        if not r.get('grid', '').strip():
            continue

        # Skip repeaters with no coordinates (no distance possible)
        if r.get('lat') is None or r.get('lon') is None:
            continue

        # Distance calculation from geocoded coordinates
        distance = None
        if user_lat is not None and user_lon is not None:
            rlat = r.get('lat')
            rlon = r.get('lon')
            if rlat is not None and rlon is not None:
                distance = round(haversine_km(user_lat, user_lon, rlat, rlon), 1)
                if max_distance and distance > max_distance:
                    continue

        entry = dict(r)
        entry['distance'] = distance
        processed.append(entry)

    # Sort by distance if available, else by frequency
    has_distances = any(x.get('distance') is not None for x in processed)
    if has_distances:
        processed.sort(key=lambda x: (x['distance'] is None,
                                       x['distance'] if x['distance'] is not None else 99999))
    else:
        processed.sort(key=lambda x: x.get('frequency', 0))

    return processed


# ============================================================================
# Radio Programming via shared rig client
# ============================================================================

import sys as _sys
_sys.path.insert(0, "/opt/emcomm-tools/lib")
try:
    from et_supervisor.rig_client import rig as _rig
except ImportError:
    _rig = None


def program_radio(freq_mhz, offset_mhz, tone):
    """Program radio via shared rig client (persistent TCP, no subprocess)."""
    if not _rig:
        return False, "rig client not available"

    freq_hz = int(float(freq_mhz) * 1_000_000)
    errors = []

    import time
    # Set frequency, brief delay for radio to process, then force FM mode
    if not _rig.set_freq(freq_hz):
        errors.append("Failed to set frequency")
    time.sleep(0.3)
    if not _rig.set_mode("FM", 0):
        errors.append("Failed to set mode")

    # CTCSS tone
    if tone:
        try:
            tone_val = float(tone)
            tone_tenth = int(tone_val * 10)
            if not _rig.set_ctcss_tone(tone_tenth):
                errors.append("Failed to set CTCSS tone")
        except (ValueError, TypeError):
            pass

    # Repeater offset
    if offset_mhz:
        try:
            offset_val = float(offset_mhz)
            if offset_val > 0:
                _rig.set_rptr_shift('+')
            elif offset_val < 0:
                _rig.set_rptr_shift('-')
            else:
                _rig.set_rptr_shift('0')
            offset_hz = int(abs(offset_val) * 1_000_000)
            _rig.set_rptr_offset(offset_hz)
        except (ValueError, TypeError):
            pass

    if errors:
        return False, "; ".join(errors)
    return True, None


# ============================================================================
# Translations
# ============================================================================

TRANSLATIONS = {
    'en': {
        'title': 'Repeater Directory',
        'subtitle': 'Import RepeaterBook CSV exports — fully offline',
        'import_csv': 'Import CSV',
        'importing': 'Importing...',
        'clear_data': 'Clear All',
        'export_json': 'Export',
        'band': 'Band',
        'mode': 'Mode',
        'all_bands': 'All',
        'all_modes': 'All',
        'max_distance': 'Max Distance',
        'km': 'km',
        'distance': 'Dist',
        'callsign': 'Callsign',
        'frequency': 'Freq',
        'offset': 'Offset',
        'tone': 'Tone',
        'location': 'Location',
        'modes': 'Modes',
        'action': 'Action',
        'set_radio': 'Set',
        'no_position': 'Set your position in User Config for distance sorting',
        'no_data': 'No repeater data. Import a CSV from RepeaterBook.',
        'no_results': 'No repeaters match your filters.',
        'data_info': '{count} repeaters loaded',
        'last_updated': 'Last updated',
        'grid': 'Grid',
        'import_success': 'Imported {count} repeaters ({new} new, {geocoded} geocoded)',
        'import_error': 'Import failed: {error}',
        'invalid_csv': 'Invalid CSV file — must be a RepeaterBook export',
        'clear_confirm': 'Clear all repeater data?',
        'cleared': 'All repeater data cleared',
        'radio_set': 'Radio set to {freq} MHz ({call})',
        'radio_error': 'Failed to set radio: {error}',
        'no_radio': 'rigctld not running - start a radio mode first',
        'how_to': 'How to get data',
        'how_to_1': '1. Go to repeaterbook.com',
        'how_to_2': '2. Search repeaters for your region',
        'how_to_3': '3. Click "Export" to download CSV',
        'how_to_4': '4. Import the CSV file here',
        'import_multiple': 'You can import multiple CSV files — data is merged automatically',
        'search': 'Search',
        'search_placeholder': 'Callsign, frequency, location or grid...',
        'favorites': 'Favorites',
        'show_favorites': 'Favorites only',
        'importing_progress': 'Geocoding locations...',
        'parsing': 'Parsing CSV...',
        'geocoding': 'Geocoding: {location} ({current}/{total})',
        'saving': 'Saving...',
        'edit': 'Edit',
        'save': 'Save',
        'cancel': 'Cancel',
        'edit_repeater': 'Edit Repeater',
        'edit_success': 'Repeater updated',
        'edit_error': 'Failed to update repeater',
        'delete': 'Delete',
        'delete_confirm': 'Delete this repeater?',
        'delete_success': 'Repeater deleted',
    },
    'fr': {
        'title': 'Répertoire Répéteurs',
        'subtitle': 'Importez les exports CSV de RepeaterBook — entièrement hors ligne',
        'import_csv': 'Importer CSV',
        'importing': 'Importation...',
        'clear_data': 'Tout effacer',
        'export_json': 'Exporter',
        'band': 'Bande',
        'mode': 'Mode',
        'all_bands': 'Toutes',
        'all_modes': 'Tous',
        'max_distance': 'Distance max',
        'km': 'km',
        'distance': 'Dist',
        'callsign': 'Indicatif',
        'frequency': 'Fréq',
        'offset': 'Décalage',
        'tone': 'Tonalité',
        'location': 'Lieu',
        'modes': 'Modes',
        'action': 'Action',
        'set_radio': 'Prog',
        'no_position': 'Définissez votre position dans Config Utilisateur pour le tri par distance',
        'no_data': 'Aucune donnée. Importez un CSV de RepeaterBook.',
        'no_results': 'Aucun répéteur ne correspond à vos filtres.',
        'data_info': '{count} répéteurs chargés',
        'last_updated': 'Dernière mise à jour',
        'grid': 'Grille',
        'import_success': '{count} répéteurs importés ({new} nouveaux, {geocoded} géolocalisés)',
        'import_error': 'Échec de l\'importation : {error}',
        'invalid_csv': 'Fichier CSV invalide — doit être un export RepeaterBook',
        'clear_confirm': 'Effacer toutes les données de répéteurs ?',
        'cleared': 'Toutes les données effacées',
        'radio_set': 'Radio réglée sur {freq} MHz ({call})',
        'radio_error': 'Échec de la programmation : {error}',
        'no_radio': 'rigctld non démarré - lancez un mode radio d\'abord',
        'how_to': 'Comment obtenir les données',
        'how_to_1': '1. Allez sur repeaterbook.com',
        'how_to_2': '2. Cherchez les répéteurs de votre région',
        'how_to_3': '3. Cliquez "Export" pour télécharger le CSV',
        'how_to_4': '4. Importez le fichier CSV ici',
        'import_multiple': 'Vous pouvez importer plusieurs fichiers CSV — les données sont fusionnées automatiquement',
        'search': 'Recherche',
        'search_placeholder': 'Indicatif, fréquence, lieu ou grille...',
        'favorites': 'Favoris',
        'show_favorites': 'Favoris seulement',
        'importing_progress': 'Géolocalisation en cours...',
        'parsing': 'Lecture du CSV...',
        'geocoding': 'Géolocalisation : {location} ({current}/{total})',
        'saving': 'Sauvegarde...',
        'edit': 'Édit',
        'save': 'Sauver',
        'cancel': 'Annuler',
        'edit_repeater': 'Modifier Répéteur',
        'edit_success': 'Répéteur mis à jour',
        'edit_error': 'Échec de la mise à jour',
        'delete': 'Supprimer',
        'delete_confirm': 'Supprimer ce répéteur ?',
        'delete_success': 'Répéteur supprimé',
    }
}


def get_translations(lang=None):
    """Get translations for current language."""
    if not lang:
        config = load_user_config()
        lang = config.get('language', 'en')
    return TRANSLATIONS.get(lang, TRANSLATIONS['en']), lang


# ============================================================================
# Routes
# ============================================================================

@app.route('/')
def index():
    """Main page."""
    config = load_user_config()
    t, lang = get_translations(config.get('language', 'en'))
    user_lat, user_lon = get_user_position()

    repeaters, meta = load_cached_repeaters()

    user_grid = ''
    if user_lat is not None and user_lon is not None:
        user_grid = latlon_to_grid(user_lat, user_lon)

    # Load saved filters
    saved_filters = {}
    if FILTERS_FILE.exists():
        try:
            with open(FILTERS_FILE, 'r') as f:
                saved_filters = json.load(f)
        except Exception:
            pass

    return render_template('index.html',
                           t=t,
                           lang=lang,
                           has_position=(user_lat is not None),
                           user_grid=user_grid,
                           has_data=(len(repeaters) > 0),
                           total_count=len(repeaters),
                           last_updated=meta.get('last_updated', ''),
                           saved_filters=saved_filters,
                           touch_mode=config.get('touch_mode', False))


def _import_worker(csv_text, filename):
    """Background worker for CSV import with geocoding."""
    global import_progress
    try:
        import_progress['phase'] = 'parsing'
        repeaters = parse_repeaterbook_csv(csv_text)
        if not repeaters:
            import_progress['phase'] = 'done'
            import_progress['active'] = False
            import_progress['result'] = {'success': False, 'error': 'No repeaters found'}
            return

        import_progress['phase'] = 'geocoding'
        geocoded = geocode_repeaters(repeaters)

        import_progress['phase'] = 'saving'
        total, new_count = save_cached_repeaters(repeaters, filename)

        import_progress['phase'] = 'done'
        import_progress['active'] = False
        import_progress['result'] = {
            'success': True,
            'count': len(repeaters),
            'new': new_count,
            'geocoded': geocoded,
            'total': total
        }
    except Exception as e:
        import_progress['phase'] = 'done'
        import_progress['active'] = False
        import_progress['result'] = {'success': False, 'error': str(e)}


@app.route('/api/import', methods=['POST'])
def api_import():
    """Import repeater data from CSV file upload."""
    global import_progress
    t, _ = get_translations()

    if import_progress['active']:
        return jsonify({'success': False, 'error': 'Import already in progress'})

    if 'file' not in request.files:
        return jsonify({'success': False, 'error': t['invalid_csv']})

    file = request.files['file']
    if not file.filename:
        return jsonify({'success': False, 'error': t['invalid_csv']})

    try:
        csv_text = file.read().decode('utf-8')
    except UnicodeDecodeError:
        try:
            file.seek(0)
            csv_text = file.read().decode('latin-1')
        except Exception:
            return jsonify({'success': False, 'error': t['invalid_csv']})

    # Validate it looks like a RepeaterBook CSV
    first_line = csv_text.split('\n')[0] if csv_text else ''
    if 'Output Freq' not in first_line or 'Call' not in first_line:
        return jsonify({'success': False, 'error': t['invalid_csv']})

    # Start background import
    import_progress = {
        'active': True,
        'total': 0,
        'current': 0,
        'current_location': '',
        'phase': 'parsing',
        'result': None,
    }

    thread = threading.Thread(target=_import_worker, args=(csv_text, file.filename),
                              daemon=True)
    thread.start()

    return jsonify({'success': True, 'started': True})


@app.route('/api/import-progress', methods=['GET'])
def api_import_progress():
    """Return current import progress."""
    pct = 0
    if import_progress['phase'] == 'parsing':
        pct = 5
    elif import_progress['phase'] == 'geocoding' and import_progress['total'] > 0:
        pct = 5 + int((import_progress['current'] / import_progress['total']) * 90)
    elif import_progress['phase'] == 'saving':
        pct = 95
    elif import_progress['phase'] == 'done':
        pct = 100

    return jsonify({
        'active': import_progress['active'],
        'phase': import_progress['phase'],
        'percent': pct,
        'current': import_progress['current'],
        'total': import_progress['total'],
        'current_location': import_progress['current_location'],
        'result': import_progress['result'],
    })


@app.route('/api/repeaters', methods=['GET'])
def api_repeaters():
    """Return filtered repeater list from cache."""
    band = request.args.get('band', 'all')
    mode = request.args.get('mode', 'all')
    max_dist = request.args.get('max_distance', '')

    try:
        max_dist = float(max_dist) if max_dist else None
    except ValueError:
        max_dist = None

    repeaters, meta = load_cached_repeaters()
    if not repeaters:
        return jsonify({'repeaters': [], 'count': 0, 'total': 0})

    user_lat, user_lon = get_user_position()
    processed = filter_repeaters(repeaters, user_lat, user_lon,
                                 band_filter=band, mode_filter=mode,
                                 max_distance=max_dist)

    # Remove any entry with no callsign, no grid, or no distance
    processed = [r for r in processed
                 if r.get('callsign', '').strip()
                 and r.get('grid', '').strip()
                 and r.get('distance') is not None]

    # Add favorite flags
    favorites = load_favorites()
    for r in processed:
        key = f"{r.get('callsign', '')}_{r.get('frequency', 0)}"
        r['favorite'] = key in favorites

    return jsonify({
        'repeaters': processed,
        'total': len(repeaters),
        'count': len(processed),
        'last_updated': meta.get('last_updated', ''),
    })


@app.route('/api/filters', methods=['GET'])
def api_get_filters():
    """Load saved filter state."""
    if FILTERS_FILE.exists():
        try:
            with open(FILTERS_FILE, 'r') as f:
                return jsonify(json.load(f))
        except Exception:
            pass
    return jsonify({})


@app.route('/api/filters', methods=['POST'])
def api_save_filters():
    """Save filter state."""
    data = request.get_json()
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    with open(FILTERS_FILE, 'w') as f:
        json.dump(data, f)
    return jsonify({'success': True})


@app.route('/api/tracking-status', methods=['GET'])
def api_tracking_status():
    """Check if GPS tracking is active (flag file from et-dashboard)."""
    tracking = os.path.exists('/tmp/et-gps-tracking')
    user_lat, user_lon = get_user_position()
    grid = ''
    if user_lat is not None and user_lon is not None:
        grid = latlon_to_grid(user_lat, user_lon)
    return jsonify({
        'tracking': tracking,
        'lat': user_lat,
        'lon': user_lon,
        'grid': grid,
    })


@app.route('/api/clear', methods=['POST'])
def api_clear():
    """Clear all cached repeater data."""
    clear_cached_repeaters()
    t, _ = get_translations()
    return jsonify({'success': True, 'message': t['cleared']})


@app.route('/api/update-repeater', methods=['POST'])
def api_update_repeater():
    """Update a repeater entry."""
    data = request.get_json()
    old_callsign = data.get('old_callsign', '')
    old_frequency = data.get('old_frequency', '')
    updates = data.get('updates', {})

    if not old_callsign or not old_frequency:
        return jsonify({'success': False, 'error': 'Missing repeater identifier'})

    success = update_repeater(old_callsign, old_frequency, updates)
    return jsonify({'success': success})


@app.route('/api/delete-repeater', methods=['POST'])
def api_delete_repeater():
    """Delete a repeater entry."""
    data = request.get_json()
    callsign = data.get('callsign', '')
    frequency = data.get('frequency', '')
    success = delete_repeater(callsign, frequency)
    return jsonify({'success': success})


@app.route('/api/toggle-favorite', methods=['POST'])
def api_toggle_favorite():
    """Toggle a repeater as favorite."""
    data = request.get_json()
    callsign = data.get('callsign', '')
    frequency = data.get('frequency', '')
    is_fav = toggle_favorite(callsign, frequency)
    return jsonify({'success': True, 'favorite': is_fav})


@app.route('/api/set-radio', methods=['POST'])
def api_set_radio():
    """Program radio via rigctld."""
    data = request.get_json()
    freq = data.get('frequency', '')
    offset = data.get('offset', '')
    tone = data.get('tone', '')
    callsign = data.get('callsign', '')

    if not freq:
        return jsonify({'success': False, 'error': 'No frequency'})

    # Check if rigctld is reachable via rig client
    if _rig:
        _rig.refresh()
        if not _rig.connected:
            t, _ = get_translations()
            return jsonify({'success': False, 'error': t['no_radio']})
    else:
        t, _ = get_translations()
        return jsonify({'success': False, 'error': t['no_radio']})

    success, error = program_radio(freq, offset, tone)
    if success:
        return jsonify({'success': True, 'frequency': freq,
                        'callsign': callsign})
    return jsonify({'success': False, 'error': error})


@app.route('/set-language', methods=['POST'])
def set_language():
    """Set language preference."""
    data = request.get_json()
    lang = data.get('language', 'en')
    config = load_user_config()
    config['language'] = lang

    ET_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with open(ET_CONFIG_FILE, 'w') as f:
        json.dump(config, f, indent=2)

    return jsonify({'success': True, 'language': lang})


@app.route('/shutdown', methods=['POST'])
def shutdown():
    """Shutdown the Flask server."""
    func = request.environ.get('werkzeug.server.shutdown')
    if func is None:
        os._exit(0)
    func()
    return 'Server shutting down...'


# ============================================================================
# Main
# ============================================================================

def run_flask(port):
    """Run Flask server in background thread."""
    app.run(host='127.0.0.1', port=port, debug=False, use_reloader=False, threaded=True)


def open_browser(port):
    """Open browser after short delay."""
    time.sleep(1)
    webbrowser.open(f'http://127.0.0.1:{port}')


if __name__ == '__main__':
    port = 5058

    if '--no-browser' in sys.argv:
        app.run(host='127.0.0.1', port=port, debug=False, threaded=True)
    elif '--browser' in sys.argv:
        threading.Thread(target=open_browser, args=(port,), daemon=True).start()
        print(f"Starting Repeater Directory on http://127.0.0.1:{port}")
        app.run(host='127.0.0.1', port=port, debug=False, threaded=True)
    elif '--help' in sys.argv:
        print("Usage: et-repeater [OPTIONS]")
        print("")
        print("Options:")
        print("  --no-browser    Start server only (no window)")
        print("  --browser       Open in default web browser")
        print("  --help          Show this help message")
        print("")
        print("Default: Opens in native PyWebView window")
        sys.exit(0)
    else:
        # Default: PyWebView native window
        try:
            import webview

            win_width = 700
            win_height = 600

            try:
                import gi
                gi.require_version('Gdk', '3.0')
                from gi.repository import Gdk

                screen = Gdk.Screen.get_default()
                screen_width = screen.get_width()
                screen_height = screen.get_height()

                panel_height = 60
                if screen_height <= 800:
                    win_width = min(700, screen_width - 40)
                    win_height = screen_height - panel_height - 40
                else:
                    win_width = 700
                    win_height = min(700, screen_height - panel_height - 60)

                x = (screen_width - win_width) // 2
                y = 30

                print(f"[WINDOW] Screen: {screen_width}x{screen_height}, "
                      f"Window: {win_width}x{win_height} at ({x},{y})")
            except Exception as e:
                print(f"[WINDOW] Could not detect screen size: {e}")
                x = None
                y = None

            flask_thread = threading.Thread(target=run_flask, args=(port,),
                                            daemon=True)
            flask_thread.start()
            time.sleep(1)

            window = webview.create_window(
                'LiaisonOS - Repeater Directory',
                f'http://127.0.0.1:{port}',
                width=win_width,
                height=win_height,
                resizable=True,
                min_size=(500, 400),
                x=x,
                y=y,
                frameless=False,
                maximized=True
            )

            webview.start()

        except ImportError:
            print("PyWebView not installed. Falling back to browser mode.")
            threading.Thread(target=open_browser, args=(port,),
                             daemon=True).start()
            app.run(host='127.0.0.1', port=port, debug=False, threaded=True)
