#!/usr/bin/env python3
"""
LiaisonOS First Boot Wizard
Author: Sylvain Deguire (VA2OPS)
Date: January 2026
Version: 2.1.0 - 2026-02-08 - Tilesets: seed from /etc/skel/ before USB merge

Flow:
1. Check for USB persistence (emcomm-data/)
   - If found with config: Show "Welcome back!" with Load/Fresh Start options
   - If not found: Continue normal flow
2. Welcome + Language selection
3. User setup (callsign, grid, Winlink password)
4. Radio selection + show settings
5. Drive selection (local or USB) - BEFORE downloads
6. Download tiles (World mandatory + optional US, CA, EU tilesets)
7. Download OSM maps (Canada provinces list)
8. Download Wikipedia ZIM files (EN + FR)
9. Create symlinks if USB selected
10. Save config to USB persistence
11. Complete
"""

import os
import sys
import json
import subprocess
import threading
import time
import re
import grp
import pwd
from pathlib import Path
from flask import Flask, render_template, request, jsonify, redirect, url_for, session, Response

# =============================================================================
# PERSISTENCE SUPPORT (USB "Plug and Communicate")
# =============================================================================
# Persistence is handled directly using session['usb_path'] from drive_setup
# No external module needed - uses same USB the user already selected

app = Flask(__name__)
app.secret_key = 'emcomm-tools-firstboot-va2ops'
app.jinja_env.filters['basename'] = os.path.basename

# =============================================================================
# CONFIGURATION
# =============================================================================

try:
    SCRIPT_DIR = Path(__file__).parent.resolve()
except NameError:
    SCRIPT_DIR = Path('.').resolve()

ET_BASE = SCRIPT_DIR.parent.parent.parent

if not (ET_BASE / "conf" / "radios.d").exists():
    SCRIPT_DIR = Path('.').resolve()
    ET_BASE = SCRIPT_DIR.parent.parent.parent
    
if not (ET_BASE / "conf" / "radios.d").exists():
    cwd = Path.cwd()
    for _ in range(6):
        if (cwd / "conf" / "radios.d").exists():
            ET_BASE = cwd
            break
        cwd = cwd.parent

USER_CONF = Path.home() / ".config" / "emcomm-tools" / "user.json"
RADIOS_DIR = ET_BASE / "conf" / "radios.d"
ACTIVE_RADIO_LINK = RADIOS_DIR / "active-radio.json"
TILESET_DIR = Path.home() / ".local/share/emcomm-tools/mbtileserver/tilesets"
SKEL_TILESET_DIR = Path("/etc/skel/.local/share/emcomm-tools/mbtileserver/tilesets")
SKEL_PBF_MAP_DIR = Path("/etc/skel/my-maps")
SKEL_NAVIT_MAP_DIR = Path("/etc/skel/.navit/maps")
PBF_MAP_DIR = Path.home() / "my-maps"
NAVIT_MAP_DIR = Path.home() / ".navit/maps"
ZIM_DIR = Path.home() / "wikipedia"

# Group for shared data access
ET_DATA_GROUP = "et-data"

print(f"ET_BASE resolved to: {ET_BASE}")
print(f"RADIOS_DIR: {RADIOS_DIR} (exists: {RADIOS_DIR.exists()})")

def load_tile_config():
    """Load tile catalog from shared conf/tiles.json."""
    tiles_json = ET_BASE / "conf" / "tiles.json"
    with open(tiles_json) as f:
        config = json.load(f)
    return config["base_url"], config["files"]

TILE_BASE_URL, TILE_FILES = load_tile_config()


def migrate_osm_to_tt(directory):
    """Rename old osm-* tileset files to tt-* naming convention.

    Previous versions used osm- prefix (e.g. osm-us-zoom0to10.mbtiles).
    Current versions use tt- prefix. Rename so users don't end up with
    duplicate files taking up disk space.
    """
    directory = Path(directory)
    if not directory.exists():
        return
    for old_file in directory.glob("osm-*.mbtiles"):
        new_name = "tt-" + old_file.name[4:]  # strip "osm-" prefix
        new_file = directory / new_name
        if not new_file.exists():
            old_file.rename(new_file)
            print(f"[TILES] Renamed {old_file.name} → {new_name}")
        else:
            # tt- version already exists, remove the old osm- duplicate
            old_file.unlink()
            print(f"[TILES] Removed duplicate {old_file.name} (tt- version exists)")


OSM_CANADA_URL = "http://download.geofabrik.de/north-america/canada.html"
OSM_USA_URL = "http://download.geofabrik.de/north-america/us.html"
OSM_BASE_CANADA = "http://download.geofabrik.de/north-america/canada"
OSM_BASE_USA = "http://download.geofabrik.de/north-america/us"
KIWIX_URL = "http://download.kiwix.org/zim/wikipedia"

# =============================================================================
# TRANSLATIONS
# =============================================================================

TRANSLATIONS = {
    'en': {
        'welcome': 'Welcome to LiaisonOS',
        'welcome_msg': 'This wizard will help you configure your emergency communications system.',
        'welcome_back': 'Welcome Back',
        'welcome_back_msg': 'Your configuration was found on USB storage.',
        'load_config': 'Load My Config',
        'load_config_desc': 'Restore your settings from USB',
        'fresh_start': 'Fresh Start',
        'fresh_start_desc': 'Set up as new (ignore saved config)',
        'config_restored': 'Configuration restored!',
        'config_saved': 'Configuration saved to USB',
        'select_language': 'Select Language',
        'next': 'Next', 'back': 'Back', 'skip': 'Skip', 'finish': 'Finish',
        'step': 'Step', 'of': 'of',
        'user_setup': 'User Setup',
        'callsign': 'Callsign', 'callsign_placeholder': 'e.g., W1ABC',
        'grid_square': 'Grid Square', 'grid_placeholder': 'e.g., FN35fl',
        'winlink_password': 'Winlink Password', 'password_placeholder': 'Your Winlink password',
        'password_not_set': 'Not set', 'password_set': 'Set',
        'radio_setup': 'Radio Setup', 'select_radio': 'Select Your Radio',
        'no_radios': 'No radios configured', 'radio_settings': 'Radio Settings',
        'manufacturer': 'Manufacturer', 'model': 'Model', 'baud_rate': 'Baud Rate',
        'data_bits': 'Data Bits', 'stop_bits': 'Stop Bits', 'notes': 'Notes',
        'drive_setup': 'Storage Setup', 'select_drive': 'Select Download Destination',
        'local_drive': 'Local Drive', 'local_desc': 'Download files to your local hard drive',
        'usb_drive': 'USB/External Drive', 'usb_desc': 'Download files to external storage',
        'select_usb': 'Select USB Drive', 'no_usb': 'No USB drives detected', 'refresh': 'Refresh',
        'usb_checking': 'Checking write access...', 'usb_write_ok': 'Drive is writable',
        'usb_write_protected': 'Write-protected',
        'usb_read_only': 'This drive is read-only or write-protected. Please unlock it or choose another drive.',
        'usb_help_title': 'How to fix a write-protected drive',
        'usb_help_step1': 'Check if your USB drive has a physical write-protect switch and slide it to unlock.',
        'usb_help_step2': 'Or run this command in a terminal:',
        'usb_help_step3': 'Then click on the drive again to retry.',
        'usb_help_close': 'Close', 'usb_help_copy': 'Copy', 'usb_help_copied': 'Copied!',
        'usb_how_to_fix': 'How to fix?',
        'download_tiles': 'Download Map Tiles',
        'tiles_desc': 'Select offline map tilesets to download',
        'download_osm': 'Download OSM Maps', 'osm_desc': 'Select a region for offline navigation',
        'select_country': 'Select Country', 'canada': 'Canada', 'usa': 'United States',
        'select_region': 'Select Province/State', 'select_province': 'Select Province',
        'select_state': 'Select State', 'download_wiki': 'Download Wikipedia',
        'wiki_desc': 'Select offline Wikipedia files', 'english': 'English', 'french': 'French',
        'downloading': 'Downloading', 'processing': 'Processing', 'complete': 'Complete',
        'error': 'Error', 'download_complete': 'Download Complete',
        'creating_symlinks': 'Creating Symlinks', 'setup_complete': 'Setup Complete!',
        'complete_msg': 'Your LiaisonOS system is ready to use.',
        'restart_note': 'Your system is ready! You can run this wizard again anytime from the applications menu.',
        # Data Transfer translations
        'data_transfer': 'Data Transfer',
        'data_transfer_title': 'Copy Data to Hard Drive',
        'data_transfer_desc': 'Your USB drive contains offline data files. Would you like to copy them to your hard drive for faster access?',
        'data_transfer_note': 'Files will remain on USB as backup.',
        'usb_storage': 'USB Storage',
        'hdd_storage': 'Hard Drive',
        'total': 'Total',
        'free': 'Free',
        'used': 'Used',
        'map_tiles': 'Map Tiles',
        'osm_maps': 'OSM Maps',
        'wikipedia_files': 'Wikipedia',
        'yaac_mapcache': 'YAAC Map Cache',
        'copy_selected': 'Copy Selected',
        'skip_copy': 'Keep on USB Only',
        'copying': 'Copying',
        'copy_complete': 'Copy complete!',
        'insufficient_space': 'Insufficient disk space',
        'space_ok': 'Space OK',
        'no_data_to_copy': 'No data files found on USB',
        'find_my_grid': 'Find my grid',
        'coordinates': 'Station Coordinates',
        'coordinates_optional': '(optional — overrides grid square)',
        'latitude_placeholder': 'e.g., 45.5017',
        'longitude_placeholder': 'e.g., -73.5673',
        'create_account': 'Create account / Reset password',
        'show_password': 'Show password',
        'saved_to': 'Saved to',
        'radio_config_saved_to': 'Radio configuration saved to',
        'restore_wifi_bt': 'Restore WiFi & Bluetooth',
        'restore_wifi_bt_launched': 'Launched...',
        'navit_maps_optional': 'Navit Maps (Optional)',
        'navit_maps_desc': 'Navit navigation maps were found on your USB drive but are too large to copy automatically. To use them, open a terminal and run:',
        'hw_restore_title': 'WiFi & Bluetooth configurations were found on your USB drive.',
        'hw_restore_desc': 'Click the button below to restore them. A terminal window will open.',
        'internet_check': 'Internet Check',
        'download_maps_files': 'Download Maps & Files',
        'internet_connected': 'Internet Connected',
        'internet_connected_msg': 'You can now download offline maps and Wikipedia files.',
        'no_internet': 'No Internet Connection',
        'no_internet_msg': 'Connect to the internet to download maps and files, or skip this step.',
        'retry': 'Retry',
    },
    'fr': {
        'welcome': 'Bienvenue à LiaisonOS',
        'welcome_msg': 'Cet assistant vous aidera à configurer votre système de communications d\'urgence.',
        'welcome_back': 'Bon retour',
        'welcome_back_msg': 'Votre configuration a été trouvée sur le stockage USB.',
        'load_config': 'Charger ma config',
        'load_config_desc': 'Restaurer vos paramètres depuis USB',
        'fresh_start': 'Nouveau départ',
        'fresh_start_desc': 'Configurer comme nouveau (ignorer la config sauvegardée)',
        'config_restored': 'Configuration restaurée!',
        'config_saved': 'Configuration sauvegardée sur USB',
        'select_language': 'Choisir la langue',
        'next': 'Suivant', 'back': 'Retour', 'skip': 'Passer', 'finish': 'Terminer',
        'step': 'Étape', 'of': 'de',
        'user_setup': 'Configuration utilisateur',
        'callsign': 'Indicatif', 'callsign_placeholder': 'ex: VE2ABC',
        'grid_square': 'Carré de grille', 'grid_placeholder': 'ex: FN35fl',
        'winlink_password': 'Mot de passe Winlink', 'password_placeholder': 'Votre mot de passe Winlink',
        'password_not_set': 'Non défini', 'password_set': 'Défini',
        'radio_setup': 'Configuration radio', 'select_radio': 'Sélectionnez votre radio',
        'no_radios': 'Aucune radio configurée', 'radio_settings': 'Paramètres radio',
        'manufacturer': 'Fabricant', 'model': 'Modèle', 'baud_rate': 'Débit en bauds',
        'data_bits': 'Bits de données', 'stop_bits': 'Bits d\'arrêt', 'notes': 'Notes',
        'drive_setup': 'Configuration stockage', 'select_drive': 'Sélectionnez la destination',
        'local_drive': 'Disque local', 'local_desc': 'Télécharger sur le disque dur local',
        'usb_drive': 'Clé USB/Disque externe', 'usb_desc': 'Télécharger sur un stockage externe',
        'select_usb': 'Sélectionnez le disque USB', 'no_usb': 'Aucun disque USB détecté', 'refresh': 'Actualiser',
        'usb_checking': 'Vérification de l\'accès en écriture...', 'usb_write_ok': 'Disque inscriptible',
        'usb_write_protected': 'Protégé en écriture',
        'usb_read_only': 'Ce disque est en lecture seule ou protégé en écriture. Veuillez le déverrouiller ou choisir un autre disque.',
        'usb_help_title': 'Comment réparer un disque protégé',
        'usb_help_step1': 'Vérifiez si votre clé USB a un interrupteur de protection physique et glissez-le pour déverrouiller.',
        'usb_help_step2': 'Ou exécutez cette commande dans un terminal:',
        'usb_help_step3': 'Puis cliquez à nouveau sur le disque pour réessayer.',
        'usb_help_close': 'Fermer', 'usb_help_copy': 'Copier', 'usb_help_copied': 'Copié!',
        'usb_how_to_fix': 'Comment réparer?',
        'download_tiles': 'Télécharger les tuiles',
        'tiles_desc': 'Sélectionnez les tuiles de carte à télécharger',
        'download_osm': 'Télécharger cartes OSM', 'osm_desc': 'Sélectionnez une région pour la navigation hors ligne',
        'select_country': 'Sélectionnez le pays', 'canada': 'Canada', 'usa': 'États-Unis',
        'select_region': 'Sélectionnez la province/état', 'select_province': 'Sélectionnez la province',
        'select_state': 'Sélectionnez l\'état', 'download_wiki': 'Télécharger Wikipédia',
        'wiki_desc': 'Sélectionnez les fichiers Wikipédia hors ligne', 'english': 'Anglais', 'french': 'Français',
        'downloading': 'Téléchargement', 'processing': 'Traitement', 'complete': 'Terminé',
        'error': 'Erreur', 'download_complete': 'Téléchargement terminé',
        'creating_symlinks': 'Création des liens symboliques', 'setup_complete': 'Configuration terminée!',
        'complete_msg': 'Votre système LiaisonOS est prêt.',
        'restart_note': 'Votre système est prêt! Vous pouvez relancer cet assistant à tout moment depuis le menu des applications.',
        # Data Transfer translations
        'data_transfer': 'Transfert de données',
        'data_transfer_title': 'Copier les données sur le disque dur',
        'data_transfer_desc': 'Votre clé USB contient des fichiers de données hors ligne. Voulez-vous les copier sur votre disque dur pour un accès plus rapide?',
        'data_transfer_note': 'Les fichiers resteront sur USB comme sauvegarde.',
        'usb_storage': 'Stockage USB',
        'hdd_storage': 'Disque dur',
        'total': 'Total',
        'free': 'Libre',
        'used': 'Utilisé',
        'map_tiles': 'Tuiles de carte',
        'osm_maps': 'Cartes OSM',
        'wikipedia_files': 'Wikipédia',
        'yaac_mapcache': 'Cache de cartes YAAC',
        'copy_selected': 'Copier la sélection',
        'skip_copy': 'Garder sur USB seulement',
        'copying': 'Copie en cours',
        'copy_complete': 'Copie terminée!',
        'insufficient_space': 'Espace disque insuffisant',
        'space_ok': 'Espace OK',
        'no_data_to_copy': 'Aucun fichier de données trouvé sur USB',
        'find_my_grid': 'Trouver mon grid',
        'coordinates': 'Coordonnées de la station',
        'coordinates_optional': '(optionnel — remplace le carré de grille)',
        'latitude_placeholder': 'ex: 45.5017',
        'longitude_placeholder': 'ex: -73.5673',
        'create_account': 'Créer un compte / Réinitialiser',
        'show_password': 'Afficher le mot de passe',
        'saved_to': 'Sauvegardé dans',
        'radio_config_saved_to': 'Configuration radio sauvegardée dans',
        'restore_wifi_bt': 'Restaurer WiFi et Bluetooth',
        'restore_wifi_bt_launched': 'Lancé...',
        'navit_maps_optional': 'Cartes Navit (Optionnel)',
        'navit_maps_desc': 'Des cartes de navigation Navit ont été trouvées sur votre clé USB mais sont trop volumineuses pour être copiées automatiquement. Pour les utiliser, ouvrez un terminal et exécutez:',
        'hw_restore_title': 'Des configurations WiFi et Bluetooth ont été trouvées sur votre clé USB.',
        'hw_restore_desc': 'Cliquez sur le bouton ci-dessous pour les restaurer. Une fenêtre de terminal s\'ouvrira.',
        'internet_check': 'Vérification Internet',
        'download_maps_files': 'Télécharger cartes et fichiers',
        'internet_connected': 'Internet connecté',
        'internet_connected_msg': 'Vous pouvez maintenant télécharger les cartes et fichiers Wikipedia hors-ligne.',
        'no_internet': 'Pas de connexion Internet',
        'no_internet_msg': 'Connectez-vous à Internet pour télécharger les cartes et fichiers, ou passez cette étape.',
        'retry': 'Réessayer',
    }
}

def t(key):
    lang = session.get('lang', 'en')
    return TRANSLATIONS.get(lang, TRANSLATIONS['en']).get(key, key)

@app.context_processor
def utility_processor():
    return dict(t=t, lang=session.get('lang', 'en'))

# =============================================================================
# HELPER FUNCTIONS
# =============================================================================

def get_current_user():
    """Get the actual current user, not 'user' from live build."""
    return os.environ.get('USER') or os.environ.get('LOGNAME') or pwd.getpwuid(os.getuid()).pw_name

def get_et_data_gid():
    """Get the GID for et-data group, or None if it doesn't exist."""
    try:
        return grp.getgrnam(ET_DATA_GROUP).gr_gid
    except KeyError:
        return None

def fix_ownership(path, recursive=True):
    """
    Fix ownership of a path to current_user:et-data with group read/write.
    This ensures files created during firstboot are accessible after install.
    """
    try:
        current_user = get_current_user()
        uid = pwd.getpwnam(current_user).pw_uid
        gid = get_et_data_gid()
        
        if gid is None:
            print(f"[OWNERSHIP] Warning: {ET_DATA_GROUP} group not found, skipping ownership fix")
            return False
        
        path = Path(path)
        if not path.exists():
            return False
        
        if recursive and path.is_dir():
            # Use chown -R for efficiency
            subprocess.run(
                ['sudo', 'chown', '-R', f'{current_user}:{ET_DATA_GROUP}', str(path)],
                check=False, capture_output=True, timeout=60
            )
            # Set group read/write permissions
            subprocess.run(
                ['sudo', 'chmod', '-R', 'g+rwX', str(path)],
                check=False, capture_output=True, timeout=60
            )
        else:
            subprocess.run(
                ['sudo', 'chown', f'{current_user}:{ET_DATA_GROUP}', str(path)],
                check=False, capture_output=True, timeout=10
            )
            subprocess.run(
                ['sudo', 'chmod', 'g+rw', str(path)],
                check=False, capture_output=True, timeout=10
            )
        
        print(f"[OWNERSHIP] Fixed: {path} -> {current_user}:{ET_DATA_GROUP}")
        return True
    except Exception as e:
        print(f"[OWNERSHIP] Error fixing {path}: {e}")
        return False

def fix_usb_ownership(usb_path):
    """
    Fix ownership of all LiaisonOS directories on USB drive.
    Called when USB drive is selected and when setup completes.
    """
    usb = Path(usb_path)
    if not usb.exists():
        return
    
    for dirname in ["tilesets", "my-maps", "wikipedia"]:
        dir_path = usb / dirname
        if dir_path.exists():
            fix_ownership(dir_path, recursive=True)

def seed_tilesets_from_skel():
    """
    If tilesets exist in /etc/skel/ (baked into ISO), create per-file symlinks
    in the user's tilesets directory pointing to skel.
    
    This runs BEFORE any USB symlink logic so baked-in maps are always visible.
    USB files are then merged on top (only adding files not already present).
    
    Returns: list of result messages
    """
    results = []
    
    if not SKEL_TILESET_DIR.exists():
        return results
    
    skel_files = list(SKEL_TILESET_DIR.glob("*.mbtiles"))
    if not skel_files:
        return results
    
    TILESET_DIR.mkdir(parents=True, exist_ok=True)
    
    for skel_file in skel_files:
        user_file = TILESET_DIR / skel_file.name
        
        # Skip if already exists (real file or working symlink)
        if user_file.exists():
            results.append(f"Exists: {skel_file.name}")
            continue
        
        # Remove broken symlinks
        if user_file.is_symlink():
            user_file.unlink()
        
        # Create symlink to skel file
        user_file.symlink_to(skel_file)
        results.append(f"Linked: {skel_file.name} -> {skel_file}")
    
    if results:
        print(f"[SKEL] Seeded {len(skel_files)} tilesets from /etc/skel/")
    
    return results

def seed_maps_from_skel():
    """
    If OSM .pbf maps exist in /etc/skel/my-maps/ (baked into ISO), create
    per-file symlinks in the user's ~/my-maps/ directory pointing to skel.
    
    Returns: list of result messages
    """
    results = []
    
    if not SKEL_PBF_MAP_DIR.exists():
        return results
    
    skel_files = list(SKEL_PBF_MAP_DIR.glob("*.pbf"))
    if not skel_files:
        return results
    
    PBF_MAP_DIR.mkdir(parents=True, exist_ok=True)
    
    for skel_file in skel_files:
        user_file = PBF_MAP_DIR / skel_file.name
        
        if user_file.exists():
            results.append(f"Exists: {skel_file.name}")
            continue
        
        if user_file.is_symlink():
            user_file.unlink()
        
        user_file.symlink_to(skel_file)
        results.append(f"Linked: {skel_file.name} -> {skel_file}")
    
    if results:
        print(f"[SKEL] Seeded {len(skel_files)} OSM maps from /etc/skel/")
    
    return results

def seed_navit_from_skel():
    """
    If Navit .bin maps exist in /etc/skel/.navit/maps/ (baked into ISO), create
    per-file symlinks in the user's ~/.navit/maps/ directory pointing to skel.
    
    Returns: list of result messages
    """
    results = []
    
    if not SKEL_NAVIT_MAP_DIR.exists():
        return results
    
    skel_files = list(SKEL_NAVIT_MAP_DIR.glob("*.bin"))
    if not skel_files:
        return results
    
    NAVIT_MAP_DIR.mkdir(parents=True, exist_ok=True)
    
    for skel_file in skel_files:
        user_file = NAVIT_MAP_DIR / skel_file.name
        
        if user_file.exists():
            results.append(f"Exists: {skel_file.name}")
            continue
        
        if user_file.is_symlink():
            user_file.unlink()
        
        user_file.symlink_to(skel_file)
        results.append(f"Linked: {skel_file.name} -> {skel_file}")
    
    if results:
        print(f"[SKEL] Seeded {len(skel_files)} Navit maps from /etc/skel/")
    
    return results

def load_user_config():
    if USER_CONF.exists():
        try:
            with open(USER_CONF) as f:
                return json.load(f)
        except Exception as e:
            print(f"[load_user_config] Failed to read {USER_CONF}: {e}")
    return {'callsign': 'N0CALL', 'grid': '', 'latitude': '', 'longitude': '', 'winlinkPasswd': ''}

def save_user_config(config):
    try:
        USER_CONF.parent.mkdir(parents=True, exist_ok=True)
        with open(USER_CONF, 'w') as f:
            json.dump(config, f, indent=2)
    except Exception as e:
        print(f"[SAVE_CONFIG] ERROR: {e}")

def set_active_radio(radio_id):
    if not radio_id or radio_id == 'none':
        return True
    target = RADIOS_DIR / f"{radio_id}.json"
    if not target.exists():
        return False
    try:
        if ACTIVE_RADIO_LINK.exists() or ACTIVE_RADIO_LINK.is_symlink():
            ACTIVE_RADIO_LINK.unlink()
        os.symlink(target.name, str(ACTIVE_RADIO_LINK))
        return True
    except Exception as e:
        print(f"[set_active_radio] Symlink failed ({e}), retrying with sudo")
        try:
            subprocess.run(['sudo', 'rm', '-f', str(ACTIVE_RADIO_LINK)], check=False, timeout=10)
            subprocess.run(['sudo', 'ln', '-sf', target.name, str(ACTIVE_RADIO_LINK)], check=True, cwd=str(RADIOS_DIR), timeout=10)
            return True
        except Exception as e2:
            print(f"[set_active_radio] sudo fallback also failed: {e2}")
            return False

def get_radios():
    radios = []
    if not RADIOS_DIR.exists():
        return radios
    for f in sorted(RADIOS_DIR.glob("*.json")):
        if f.name == "active-radio.json":
            continue
        try:
            with open(f) as fp:
                data = json.load(fp)
                data['filename'] = f.stem
                radios.append(data)
        except Exception as e:
            print(f"[get_radios] Skipping {f.name}: {e}")
    return radios

def get_usb_drives():
    drives = []
    try:
        result = subprocess.run(['lsblk', '-J', '-o', 'NAME,SIZE,MOUNTPOINT,LABEL,HOTPLUG'], capture_output=True, text=True, timeout=10)
        data = json.loads(result.stdout)
        for device in data.get('blockdevices', []):
            for child in device.get('children', []):
                if child.get('hotplug') and child.get('mountpoint'):
                    drives.append({'name': child.get('label') or child.get('name'), 'path': child.get('mountpoint'), 'size': child.get('size')})
    except Exception as e:
        print(f"[get_usb_drives] lsblk failed: {e}")
    media_path = Path(f"/media/{get_current_user()}")
    if media_path.exists():
        for d in media_path.iterdir():
            if d.is_mount() and not any(drv['path'] == str(d) for drv in drives):
                drives.append({'name': d.name, 'path': str(d), 'size': ''})
    return drives

def check_path_writable(path):
    if not path:
        return False
    test_file = Path(path) / ".emcomm-write-test"
    try:
        test_file.touch()
        test_file.unlink()
        return True
    except Exception as e:
        print(f"[check_path_writable] {path} not writable: {e}")
        return False

def create_symlinks(usb_path=None):
    if usb_path is None:
        usb_path = session.get('usb_path', '')
    usb_path = Path(usb_path) if usb_path else Path('')

    # Validate: must be an absolute, existing path — not cwd fallback
    if not usb_path.is_absolute() or not usb_path.exists():
        msg = f"Warning: USB path not available ({usb_path}) — skipping USB symlinks"
        print(f"[SYMLINKS] {msg}")
        results = [msg]
        # Still seed from skel (ISO baked-in tilesets work without USB)
        results.extend(seed_tilesets_from_skel())
        results.extend(seed_maps_from_skel())
        results.extend(seed_navit_from_skel())
        return results

    results = []
    print(f"[SYMLINKS] Creating symlinks from USB: {usb_path}")

    # YAAC tiledir for offline map tiles (slippy map format)
    YAAC_TILEDIR = Path.home() / "YAAC" / "tiledir"
    # Navit maps directory for .bin navigation maps
    NAVIT_MAPS = Path.home() / ".navit" / "maps"

    # =========================================================================
    # TILESETS: Smart merge (skel + USB, per-file symlinks)
    # If ISO has baked-in tilesets in /etc/skel/, symlink those first,
    # then add USB files that aren't already present.
    # =========================================================================
    skel_results = seed_tilesets_from_skel()
    results.extend(skel_results)

    usb_tilesets = usb_path / "tilesets"
    if not usb_tilesets.exists():
        try:
            usb_tilesets.mkdir(parents=True, exist_ok=True)
            results.append(f"Created: {usb_tilesets}")
        except Exception as e:
            results.append(f"Error creating {usb_tilesets}: {e}")

    if usb_tilesets.exists():
        try:
            TILESET_DIR.mkdir(parents=True, exist_ok=True)
            mbtiles_files = list(usb_tilesets.glob("*.mbtiles"))
            if not mbtiles_files:
                results.append(f"Note: No .mbtiles found in {usb_tilesets}")
            for usb_file in mbtiles_files:
                user_file = TILESET_DIR / usb_file.name
                # Skip if already exists (from skel or elsewhere)
                if user_file.exists():
                    continue
                # Remove broken symlinks
                if user_file.is_symlink():
                    user_file.unlink()
                try:
                    user_file.symlink_to(usb_file)
                    results.append(f"Linked: {usb_file.name} -> {usb_file}")
                except Exception as e:
                    results.append(f"Error linking {usb_file.name}: {e}")
                    print(f"[SYMLINKS] tileset link error: {e}")
        except Exception as e:
            results.append(f"Error processing tilesets: {e}")
            print(f"[SYMLINKS] tilesets error: {e}")

    # =========================================================================
    # MY-MAPS: Smart merge (skel + USB, per-file symlinks)
    # =========================================================================
    skel_map_results = seed_maps_from_skel()
    results.extend(skel_map_results)

    usb_maps = usb_path / "my-maps"
    if not usb_maps.exists():
        try:
            usb_maps.mkdir(parents=True, exist_ok=True)
            results.append(f"Created: {usb_maps}")
        except Exception as e:
            results.append(f"Error creating {usb_maps}: {e}")

    if usb_maps.exists():
        try:
            PBF_MAP_DIR.mkdir(parents=True, exist_ok=True)
            for usb_file in usb_maps.glob("*.pbf"):
                user_file = PBF_MAP_DIR / usb_file.name
                if user_file.exists():
                    continue
                if user_file.is_symlink():
                    user_file.unlink()
                try:
                    user_file.symlink_to(usb_file)
                    results.append(f"Linked: {usb_file.name} -> {usb_file}")
                except Exception as e:
                    results.append(f"Error linking {usb_file.name}: {e}")
        except Exception as e:
            results.append(f"Error processing my-maps: {e}")

    # =========================================================================
    # NAVIT MAPS: Smart merge (skel + USB, per-file symlinks)
    # =========================================================================
    skel_navit_results = seed_navit_from_skel()
    results.extend(skel_navit_results)

    usb_navit = usb_path / "navit-maps"
    if not usb_navit.exists():
        try:
            usb_navit.mkdir(parents=True, exist_ok=True)
            results.append(f"Created: {usb_navit}")
        except Exception as e:
            results.append(f"Error creating {usb_navit}: {e}")

    if usb_navit.exists():
        try:
            NAVIT_MAP_DIR.mkdir(parents=True, exist_ok=True)
            for usb_file in usb_navit.glob("*.bin"):
                user_file = NAVIT_MAP_DIR / usb_file.name
                if user_file.exists():
                    continue
                if user_file.is_symlink():
                    user_file.unlink()
                try:
                    user_file.symlink_to(usb_file)
                    results.append(f"Linked: {usb_file.name} -> {usb_file}")
                except Exception as e:
                    results.append(f"Error linking {usb_file.name}: {e}")
        except Exception as e:
            results.append(f"Error processing navit-maps: {e}")

    # =========================================================================
    # OTHER DIRS: Directory-level symlinks to USB (unchanged behavior)
    # =========================================================================
    for src_name, dest_dir in [("tiledir", YAAC_TILEDIR)]:
        usb_dir = usb_path / src_name

        # Create the USB directory if it doesn't exist
        if not usb_dir.exists():
            try:
                usb_dir.mkdir(parents=True, exist_ok=True)
                results.append(f"Created: {usb_dir}")
            except Exception as e:
                results.append(f"Error creating {usb_dir}: {e}")
                continue

        if usb_dir.exists():
            try:
                dest_dir.parent.mkdir(parents=True, exist_ok=True)
                if dest_dir.exists() and not dest_dir.is_symlink():
                    backup = dest_dir.with_name(f"{src_name}.backup.{int(time.time())}")
                    dest_dir.rename(backup)
                    results.append(f"Backed up: {backup}")
                if dest_dir.is_symlink():
                    dest_dir.unlink()
                dest_dir.symlink_to(usb_dir)
                results.append(f"Linked: {dest_dir} -> {usb_dir}")
            except Exception as e:
                results.append(f"Error linking {src_name}: {e}")
                print(f"[SYMLINKS] {src_name} link error: {e}")

    # =========================================================================
    # YAAC MAPCACHE: Per-file symlinks to USB (.db tile caches)
    # Replace local files with symlinks — USB version has cached tiles
    # =========================================================================
    usb_mapcache = usb_path / "YAAC-MapCache"
    if usb_mapcache.exists():
        try:
            YAAC_MAPCACHE = Path.home() / "YAAC" / "mapcache"
            YAAC_MAPCACHE.mkdir(parents=True, exist_ok=True)
            for db_file in usb_mapcache.glob("*.db"):
                local_file = YAAC_MAPCACHE / db_file.name
                if local_file.is_symlink():
                    local_file.unlink()
                elif local_file.exists():
                    local_file.unlink()
                try:
                    local_file.symlink_to(db_file)
                    results.append(f"Linked: {db_file.name} -> {db_file}")
                except Exception as e:
                    results.append(f"Error linking {db_file.name}: {e}")
        except Exception as e:
            results.append(f"Error processing YAAC-MapCache: {e}")

    usb_wiki = usb_path / "wikipedia"
    if usb_wiki.exists():
        try:
            ZIM_DIR.mkdir(parents=True, exist_ok=True)
            for item in ZIM_DIR.iterdir():
                if item.is_symlink():
                    try:
                        if not item.resolve().exists():
                            item.unlink()
                    except Exception as e:
                        print(f"[symlink cleanup] Removing broken symlink {item}: {e}")
                        item.unlink()
            for zim_file in usb_wiki.glob("*.zim"):
                local_file = ZIM_DIR / zim_file.name
                if local_file.is_symlink():
                    local_file.unlink()
                if not local_file.exists():
                    try:
                        local_file.symlink_to(zim_file)
                        results.append(f"Linked: {zim_file.name}")
                    except Exception as e:
                        results.append(f"Error linking {zim_file.name}: {e}")
        except Exception as e:
            results.append(f"Error processing wikipedia: {e}")
            print(f"[SYMLINKS] wikipedia error: {e}")
    return results

def create_symlinks_for_uncopied(usb_path, copied_categories):
    """
    Create symlinks for data categories that were NOT copied to HDD.
    This ensures apps can still access data on USB drive.
    
    Args:
        usb_path: Path to USB drive
        copied_categories: List of categories that WERE copied (e.g., ['tiles', 'maps'])
    
    Returns:
        List of results
    """
    usb_path = Path(usb_path)
    if not usb_path.exists():
        return []
    
    results = []
    all_categories = ['tiles', 'maps', 'wikipedia', 'yaac_mapcache']
    uncopied = [cat for cat in all_categories if cat not in copied_categories]
    
    print(f"[SYMLINKS] Copied: {copied_categories}, Uncopied (need symlinks): {uncopied}")
    
    # YAAC tiledir for offline map tiles (slippy map format)
    YAAC_TILEDIR = Path.home() / "YAAC" / "tiledir"
    # Navit maps directory for .bin navigation maps
    NAVIT_MAPS = Path.home() / ".navit" / "maps"
    
    for cat in uncopied:
        try:
            if cat == 'tiles':
                # Tilesets: seed from skel first, then merge USB extras
                skel_results = seed_tilesets_from_skel()
                results.extend(skel_results)
                
                usb_dir = usb_path / "tilesets"
                if usb_dir.exists():
                    TILESET_DIR.mkdir(parents=True, exist_ok=True)
                    for usb_file in usb_dir.glob("*.mbtiles"):
                        user_file = TILESET_DIR / usb_file.name
                        if user_file.exists():
                            continue
                        if user_file.is_symlink():
                            user_file.unlink()
                        user_file.symlink_to(usb_file)
                        results.append(f"Linked: {usb_file.name} -> {usb_file}")
                    
                # YAAC tiledir symlink
                usb_tiledir = usb_path / "tiledir"
                if usb_tiledir.exists():
                    YAAC_TILEDIR.parent.mkdir(parents=True, exist_ok=True)
                    if YAAC_TILEDIR.is_symlink():
                        YAAC_TILEDIR.unlink()
                    if not YAAC_TILEDIR.exists():
                        YAAC_TILEDIR.symlink_to(usb_tiledir)
                        results.append(f"Linked: {YAAC_TILEDIR} -> {usb_tiledir}")
                        
            elif cat == 'maps':
                # OSM maps: seed from skel first, then merge USB extras
                skel_map_results = seed_maps_from_skel()
                results.extend(skel_map_results)
                
                usb_dir = usb_path / "my-maps"
                if usb_dir.exists():
                    PBF_MAP_DIR.mkdir(parents=True, exist_ok=True)
                    for usb_file in usb_dir.glob("*.pbf"):
                        user_file = PBF_MAP_DIR / usb_file.name
                        if user_file.exists():
                            continue
                        if user_file.is_symlink():
                            user_file.unlink()
                        user_file.symlink_to(usb_file)
                        results.append(f"Linked: {usb_file.name} -> {usb_file}")
                    
                # Navit maps: seed from skel first, then merge USB extras
                skel_navit_results = seed_navit_from_skel()
                results.extend(skel_navit_results)
                
                usb_navit = usb_path / "navit-maps"
                if usb_navit.exists():
                    NAVIT_MAPS.mkdir(parents=True, exist_ok=True)
                    for usb_file in usb_navit.glob("*.bin"):
                        user_file = NAVIT_MAPS / usb_file.name
                        if user_file.exists():
                            continue
                        if user_file.is_symlink():
                            user_file.unlink()
                        user_file.symlink_to(usb_file)
                        results.append(f"Linked: {usb_file.name} -> {usb_file}")
                        
            elif cat == 'wikipedia':
                # Wikipedia ZIM files - create individual symlinks
                usb_wiki = usb_path / "wikipedia"
                if usb_wiki.exists():
                    ZIM_DIR.mkdir(parents=True, exist_ok=True)
                    # Clean broken symlinks first
                    for item in ZIM_DIR.iterdir():
                        if item.is_symlink():
                            try:
                                if not item.resolve().exists():
                                    item.unlink()
                            except Exception as e:
                                print(f"[symlink cleanup] Removing broken symlink {item}: {e}")
                                item.unlink()
                    # Create symlinks for each zim file
                    for zim_file in usb_wiki.glob("*.zim"):
                        local_file = ZIM_DIR / zim_file.name
                        if local_file.is_symlink():
                            local_file.unlink()
                        if not local_file.exists():
                            local_file.symlink_to(zim_file)
                            results.append(f"Linked: {zim_file.name}")

            elif cat == 'yaac_mapcache':
                usb_mapcache = usb_path / "YAAC-MapCache"
                if usb_mapcache.exists():
                    YAAC_MAPCACHE = Path.home() / "YAAC" / "mapcache"
                    YAAC_MAPCACHE.mkdir(parents=True, exist_ok=True)
                    for db_file in usb_mapcache.glob("*.db"):
                        local_file = YAAC_MAPCACHE / db_file.name
                        if local_file.is_symlink():
                            local_file.unlink()
                        elif local_file.exists():
                            local_file.unlink()
                        local_file.symlink_to(db_file)
                        results.append(f"Linked: {db_file.name} -> {db_file}")

        except Exception as e:
            results.append(f"Error creating symlink for {cat}: {e}")
            print(f"[SYMLINKS] Error: {e}")
    
    return results

def generate_radio_config_document():
    """
    Generate a radio configuration document and save to ~/Documents/.
    Reads the active radio config and creates a readable text file
    with radio settings and configuration notes.

    Returns: Path to generated document, or None if no radio configured.
    """
    try:
        # Find the active radio
        if not ACTIVE_RADIO_LINK.exists() and not ACTIVE_RADIO_LINK.is_symlink():
            print("[RADIO_DOC] No active radio configured")
            return None

        # Read the active radio config
        with open(ACTIVE_RADIO_LINK) as f:
            radio = json.load(f)

        # Load user config for callsign/grid
        user_config = load_user_config()
        callsign = user_config.get('callsign', 'N0CALL')
        grid = user_config.get('grid', '')

        vendor = radio.get('vendor', 'Unknown')
        model = radio.get('model', 'Unknown')
        rigctrl = radio.get('rigctrl', {})
        notes = radio.get('notes', [])
        field_notes = radio.get('fieldNotes', [])

        # Build the document
        lines = []
        lines.append("=" * 60)
        lines.append("  LiaisonOS - Radio Configuration")
        lines.append("=" * 60)
        lines.append("")
        lines.append(f"  Callsign:    {callsign}")
        if grid:
            lines.append(f"  Grid Square: {grid}")
        lines.append("")
        lines.append("-" * 60)
        lines.append(f"  Radio: {vendor} {model}")
        lines.append("-" * 60)
        lines.append("")

        if rigctrl:
            lines.append("  Rigctrl Settings:")
            if rigctrl.get('id'):
                lines.append(f"    Rig ID:    {rigctrl['id']}")
            if rigctrl.get('baud'):
                lines.append(f"    Baud Rate: {rigctrl['baud']}")
            if rigctrl.get('ptt'):
                lines.append(f"    PTT:       {rigctrl['ptt']}")
            lines.append("")

        if notes:
            lines.append("  Configuration Notes:")
            lines.append("")
            if isinstance(notes, str):
                lines.append(f"    {notes}")
            else:
                for note in notes:
                    lines.append(f"    - {note}")
            lines.append("")

        if field_notes:
            lines.append("  Field Notes:")
            lines.append("")
            for note in field_notes:
                lines.append(f"    - {note}")
            lines.append("")

        lines.append("-" * 60)
        lines.append(f"  Generated: {time.strftime('%Y-%m-%d %H:%M')}")
        lines.append("=" * 60)
        lines.append("")

        # Save to ~/Documents/
        docs_dir = Path.home() / "Documents"
        docs_dir.mkdir(parents=True, exist_ok=True)

        safe_model = f"{vendor}-{model}".replace(' ', '_').replace('/', '-')
        doc_path = docs_dir / f"liaisonos-radio-{safe_model}.txt"
        with open(doc_path, 'w') as f:
            f.write('\n'.join(lines))

        print(f"[RADIO_DOC] Saved radio config to {doc_path}")
        return doc_path

    except Exception as e:
        print(f"[RADIO_DOC] Error generating document: {e}")
        return None

def check_internet():
    import socket
    for host in ["8.8.8.8", "1.1.1.1"]:
        try:
            socket.create_connection((host, 53), timeout=3)
            return True
        except OSError:
            pass
    return False

def is_live_boot():
    """Check if running in live boot mode (not installed on HDD)."""
    return Path("/run/live").exists()

def get_disk_space(path):
    """Get disk space info for a path. Returns dict with total, used, free in bytes."""
    try:
        import shutil
        total, used, free = shutil.disk_usage(path)
        return {
            'total': total,
            'used': used,
            'free': free,
            'total_gb': round(total / (1024**3), 1),
            'used_gb': round(used / (1024**3), 1),
            'free_gb': round(free / (1024**3), 1)
        }
    except Exception as e:
        print(f"[DISK_SPACE] Error getting space for {path}: {e}")
        return None

def get_data_files_info(usb_path):
    """
    Get info about large data files on USB that can be copied to HDD.
    Returns dict with categories and their sizes.
    """
    usb = Path(usb_path)
    info = {
        'tiles': {'files': [], 'total_bytes': 0, 'total_mb': 0},
        'maps': {'files': [], 'total_bytes': 0, 'total_mb': 0},
        'wikipedia': {'files': [], 'total_bytes': 0, 'total_mb': 0},
        'yaac_mapcache': {'files': [], 'total_bytes': 0, 'total_mb': 0},
    }
    
    # Tiles (mbtiles)
    tiles_dir = usb / "tilesets"
    if tiles_dir.exists():
        for f in tiles_dir.glob("*.mbtiles"):
            size = f.stat().st_size
            info['tiles']['files'].append({'name': f.name, 'size': size, 'size_mb': round(size / (1024**2), 1)})
            info['tiles']['total_bytes'] += size
        info['tiles']['total_mb'] = round(info['tiles']['total_bytes'] / (1024**2), 1)
    
    # OSM Maps (pbf) + Navit Maps (bin)
    maps_dir = usb / "my-maps"
    if maps_dir.exists():
        for f in maps_dir.glob("*.pbf"):
            size = f.stat().st_size
            info['maps']['files'].append({'name': f.name, 'size': size, 'size_mb': round(size / (1024**2), 1)})
            info['maps']['total_bytes'] += size
    navit_dir = usb / "navit-maps"
    if navit_dir.exists():
        for f in navit_dir.glob("*.bin"):
            size = f.stat().st_size
            info['maps']['files'].append({'name': f.name, 'size': size, 'size_mb': round(size / (1024**2), 1)})
            info['maps']['total_bytes'] += size
    info['maps']['total_mb'] = round(info['maps']['total_bytes'] / (1024**2), 1)
    
    # Wikipedia (zim)
    wiki_dir = usb / "wikipedia"
    if wiki_dir.exists():
        for f in wiki_dir.glob("*.zim"):
            size = f.stat().st_size
            info['wikipedia']['files'].append({'name': f.name, 'size': size, 'size_mb': round(size / (1024**2), 1)})
            info['wikipedia']['total_bytes'] += size
        info['wikipedia']['total_mb'] = round(info['wikipedia']['total_bytes'] / (1024**2), 1)

    # YAAC MapCache (.db tile caches)
    mapcache_dir = usb / "YAAC-MapCache"
    if mapcache_dir.exists():
        for f in mapcache_dir.glob("*.db"):
            size = f.stat().st_size
            info['yaac_mapcache']['files'].append({'name': f.name, 'size': size, 'size_mb': round(size / (1024**2), 1)})
            info['yaac_mapcache']['total_bytes'] += size
        info['yaac_mapcache']['total_mb'] = round(info['yaac_mapcache']['total_bytes'] / (1024**2), 1)

    # Calculate total
    info['total_bytes'] = (info['tiles']['total_bytes'] + info['maps']['total_bytes'] +
                           info['wikipedia']['total_bytes'] + info['yaac_mapcache']['total_bytes'])
    info['total_mb'] = round(info['total_bytes'] / (1024**2), 1)
    info['total_gb'] = round(info['total_bytes'] / (1024**3), 2)
    
    return info

# =============================================================================
# PERSISTENCE HELPER FUNCTIONS
# =============================================================================

def detect_persistence():
    """
    Detect USB persistence by checking common mount points for et-config/user.json.
    Returns: (found: bool, config: dict, usb_path: str)
    """
    from pathlib import Path
    
    # Check common USB mount locations
    search_paths = []
    
    # Add /media/username paths
    media_path = Path("/media")
    if media_path.exists():
        for user_dir in media_path.iterdir():
            if user_dir.is_dir():
                for drive in user_dir.iterdir():
                    if drive.is_dir():
                        search_paths.append(drive)
    
    # Add /run/media paths
    run_media = Path("/run/media")
    if run_media.exists():
        for user_dir in run_media.iterdir():
            if user_dir.is_dir():
                for drive in user_dir.iterdir():
                    if drive.is_dir():
                        search_paths.append(drive)
    
    # Check each potential USB for et-config/user.json
    for usb_path in search_paths:
        user_json = usb_path / "et-config" / "user.json"
        
        if user_json.exists():
            try:
                with open(user_json) as f:
                    config = json.load(f)
                
                # Check if actually configured
                if config.get('configured', False) or config.get('callsign', 'N0CALL') != 'N0CALL':
                    print(f"[PERSISTENCE] Found configured user: {config.get('callsign', 'N0CALL')} at {usb_path}")
                    return True, config, str(usb_path)
            except Exception as e:
                print(f"[PERSISTENCE] Error reading {user_json}: {e}")
    
    print("[PERSISTENCE] No et-config/user.json found on USB")
    return False, {}, ""

def restore_from_persistence():
    """
    Restore user config from USB persistence.
    Calls the et-persistence-restore script to do the actual work.
    Returns: True if successful
    """
    usb_path = session.get('persistence_usb_path', '')
    if not usb_path:
        print("[PERSISTENCE] No USB path to restore from")
        return False
    
    try:
        restore_script = "/opt/emcomm-tools/bin/et-persistence/et-persistence-restore"
        
        if os.path.exists(restore_script):
            print("[PERSISTENCE] Calling et-persistence-restore...")
            result = subprocess.run([restore_script], capture_output=True, text=True, timeout=300)
            print(result.stdout)
            if result.stderr:
                print(result.stderr)
            
            if result.returncode == 0:
                print("[PERSISTENCE] Restore completed successfully")
                return True
            else:
                print(f"[PERSISTENCE] Restore script returned error code: {result.returncode}")
                return False
        else:
            print(f"[PERSISTENCE] Restore script not found: {restore_script}")
            return False
        
    except Exception as e:
        print(f"[PERSISTENCE] Restore error: {e}")
        return False

def save_to_persistence():
    """
    Save current user config to USB persistence.
    Uses the USB path already selected by user in drive_setup.
    Copies the local user.json and VarAC license flag to USB et-config folder.
    
    SAFETY: Never overwrite a valid callsign with N0CALL!
    
    Returns: True if successful
    """
    usb_path = session.get('usb_path', '')
    if not usb_path:
        print("[PERSISTENCE] No USB path in session, skipping save")
        return False
    
    try:
        from pathlib import Path
        import shutil
        
        persistence_dir = Path(usb_path) / "et-config"
        configs_dir = persistence_dir / "configs"
        varac_dir = persistence_dir / "varac"
        
        # Create directories if needed
        persistence_dir.mkdir(parents=True, exist_ok=True)
        configs_dir.mkdir(parents=True, exist_ok=True)
        varac_dir.mkdir(parents=True, exist_ok=True)
        
        # Copy the local user.json to USB (this has the real data!)
        local_user_json = USER_CONF
        usb_user_json = persistence_dir / "user.json"
        
        if local_user_json.exists():
            # Read local config
            with open(local_user_json) as f:
                local_config = json.load(f)
            local_callsign = local_config.get('callsign', 'N0CALL')
            
            # SAFETY CHECK: Don't overwrite good config with N0CALL
            if local_callsign == 'N0CALL' and usb_user_json.exists():
                try:
                    with open(usb_user_json) as f:
                        usb_config = json.load(f)
                    usb_callsign = usb_config.get('callsign', 'N0CALL')
                    
                    if usb_callsign != 'N0CALL':
                        print(f"[PERSISTENCE] SAFETY: Not overwriting {usb_callsign} with N0CALL!")
                        return False
                except Exception as e:
                    print(f"[save_to_persistence] USB config unreadable, allowing overwrite: {e}")
            
            # Preserve Winlink password from USB if local doesn't have one
            if not local_config.get('winlinkPasswd') and usb_user_json.exists():
                try:
                    with open(usb_user_json) as f:
                        usb_config = json.load(f)
                    usb_passwd = usb_config.get('winlinkPasswd', '')
                    if usb_passwd:
                        local_config['winlinkPasswd'] = usb_passwd
                        # Write merged config back to local before copying
                        with open(local_user_json, 'w') as f:
                            json.dump(local_config, f, indent=2)
                        print(f"[PERSISTENCE] Preserved Winlink password from USB")
                except Exception:
                    pass

            # Safe to copy
            shutil.copy2(local_user_json, usb_user_json)
            print(f"[PERSISTENCE] Copied {local_user_json} to {usb_user_json}")
            
            # Read it back to verify and create manifest
            with open(usb_user_json) as f:
                config = json.load(f)
            
            print(f"[PERSISTENCE] Callsign: {config.get('callsign', 'EMPTY!')}")
        else:
            print(f"[PERSISTENCE] Local user.json not found: {local_user_json}")
            config = {}
        
        # Copy VarAC license flag if it exists
        local_varac_license = Path.home() / ".config" / "emcomm-tools" / "varac" / "license.flag"
        usb_varac_license = varac_dir / "license.flag"
        
        if local_varac_license.exists():
            shutil.copy2(local_varac_license, usb_varac_license)
            print(f"[PERSISTENCE] Copied VarAC license flag to {usb_varac_license}")
        else:
            print(f"[PERSISTENCE] VarAC license not yet accepted (no flag file)")
        
        # Copy VarAC audit log if it exists
        local_varac_audit = Path.home() / ".config" / "emcomm-tools" / "varac" / "license-audit.log"
        usb_varac_audit = varac_dir / "license-audit.log"
        
        if local_varac_audit.exists():
            shutil.copy2(local_varac_audit, usb_varac_audit)
            print(f"[PERSISTENCE] Copied VarAC audit log")
        
        # Save manifest
        manifest = {
            "version": "2.0.0",
            "callsign": config.get('callsign', ''),
            "varac_license_saved": local_varac_license.exists(),
            "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "last_save": time.strftime("%Y-%m-%dT%H:%M:%S")
        }
        
        manifest_json = persistence_dir / "manifest.json"
        with open(manifest_json, 'w') as f:
            json.dump(manifest, f, indent=2)
        
        print(f"[PERSISTENCE] Saved config to {persistence_dir}")
        return True
        
    except Exception as e:
        print(f"[PERSISTENCE] Save error: {e}")
        return False

# =============================================================================
# ROUTES
# =============================================================================

@app.route('/')
def index():
    # Check for USB persistence first
    found, config, usb_path = detect_persistence()
    if found:
        # Store persistence info in session
        session['persistence_found'] = True
        session['persistence_callsign'] = config.get('callsign', '')
        session['persistence_grid'] = config.get('grid', '')
        session['persistence_lang'] = config.get('language', 'en')
        session['persistence_usb_path'] = usb_path
        # Redirect to welcome back page
        return redirect(url_for('welcome_back'))
    
    # No persistence found, show normal language selection
    return render_template('index.html')

@app.route('/welcome_back')
def welcome_back():
    """Show welcome back screen for returning users with USB persistence."""
    callsign = session.get('persistence_callsign', 'N0CALL')
    lang = session.get('persistence_lang', 'en')
    session['lang'] = lang  # Set language from saved config
    return render_template('welcome_back.html', callsign=callsign)

@app.route('/restore_config')
def restore_config():
    """Restore configuration from USB and go to complete screen."""
    if restore_from_persistence():
        # Also set session variables from restored config
        config = load_user_config()
        session['callsign'] = config.get('callsign', '')
        session['grid'] = config.get('grid', '')
        session['lang'] = session.get('persistence_lang', 'en')
        
        # Set USB path for symlinks if we have it
        usb_path = session.get('persistence_usb_path', '')
        if usb_path:
            session['drive_type'] = 'usb'
            session['usb_path'] = usb_path
        
        # Mark as restored and go to complete
        session['restored_from_usb'] = True
        return redirect(url_for('complete'))
    else:
        # Restore failed, go to normal setup
        return redirect(url_for('user_setup'))

@app.route('/fresh_start')
def fresh_start():
    """User chose fresh start - clear persistence session and go to normal flow."""
    session.pop('persistence_found', None)
    session.pop('persistence_callsign', None)
    session.pop('persistence_grid', None)
    session.pop('persistence_lang', None)
    session.pop('persistence_usb_path', None)
    return render_template('index.html')

@app.route('/api/restore_stream')
def api_restore_stream():
    """Stream restore progress to the client for real-time feedback."""
    # Capture session values BEFORE entering generator
    usb_path = session.get('persistence_usb_path', '')
    
    def generate():
        import json as json_module
        
        if not usb_path:
            yield f"data: {json_module.dumps({'error': 'No USB path found'})}\n\n"
            return
        
        restore_script = "/opt/emcomm-tools/bin/et-persistence/et-persistence-restore"
        
        if not os.path.exists(restore_script):
            yield f"data: {json_module.dumps({'error': 'Restore script not found'})}\n\n"
            return
        
        yield f"data: {json_module.dumps({'status': 'Starting restore...', 'progress': 5})}\n\n"
        
        try:
            # Run restore script and stream output
            process = subprocess.Popen(
                [restore_script],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1
            )
            
            progress = 10
            for line in iter(process.stdout.readline, ''):
                line = line.strip()
                if not line:
                    continue
                
                # Send log line
                yield f"data: {json_module.dumps({'log': line})}\n\n"
                
                # Update progress based on content
                if '✓ Restored:' in line:
                    progress = min(progress + 5, 90)
                    yield f"data: {json_module.dumps({'progress': progress})}\n\n"
                elif 'Firefox' in line:
                    yield f"data: {json_module.dumps({'status': 'Restoring Firefox...', 'progress': 60})}\n\n"
                elif 'WiFi' in line:
                    yield f"data: {json_module.dumps({'status': 'Restoring WiFi...', 'progress': 80})}\n\n"
                elif 'RESTORE COMPLETE' in line:
                    yield f"data: {json_module.dumps({'status': 'Restore complete!', 'progress': 100})}\n\n"
            
            process.wait()
            
            if process.returncode == 0:
                yield f"data: {json_module.dumps({'complete': True, 'progress': 100})}\n\n"
            else:
                yield f"data: {json_module.dumps({'error': f'Restore failed with code {process.returncode}'})}\n\n"
                
        except Exception as e:
            yield f"data: {json_module.dumps({'error': str(e)})}\n\n"
    
    return Response(generate(), mimetype='text/event-stream')

@app.route('/api/restore_complete', methods=['POST'])
def api_restore_complete():
    """Called after streaming restore completes to set session variables."""
    usb_path = session.get('persistence_usb_path', '')
    config = load_user_config()
    session['callsign'] = config.get('callsign', '')
    session['grid'] = config.get('grid', '')
    session['lang'] = session.get('persistence_lang', 'en')
    session['drive_type'] = 'usb'
    session['usb_path'] = usb_path
    session['restored_from_usb'] = True
    return jsonify({'success': True})

@app.route('/post_restore')
def post_restore():
    """
    Called after restore completes.
    - On HDD install: redirect to data_transfer to optionally copy Maps/Tiles/Wikipedia
    - On Live boot: redirect directly to complete (data stays on USB)
    """
    if is_live_boot():
        # Live mode - skip data transfer, keep everything on USB
        print("[POST_RESTORE] Live boot detected - skipping data transfer")
        return redirect(url_for('complete'))
    else:
        # HDD install - offer to copy data from USB to HDD
        print("[POST_RESTORE] HDD install detected - checking for data to transfer")
        usb_path = session.get('usb_path', '') or session.get('persistence_usb_path', '')
        
        if usb_path:
            data_info = get_data_files_info(usb_path)
            if data_info['total_bytes'] > 0:
                print(f"[POST_RESTORE] Found {data_info['total_gb']} GB of data to potentially transfer")
                return redirect(url_for('data_transfer'))
        
        # No data to transfer
        return redirect(url_for('complete'))

@app.route('/lang/<lang>')
def set_language(lang):
    session['lang'] = lang if lang in TRANSLATIONS else 'en'
    config = load_user_config()
    config['language'] = session['lang']
    save_user_config(config)
    return redirect(url_for('user_setup'))

@app.route('/user', methods=['GET', 'POST'])
def user_setup():
    config = load_user_config()
    if request.method == 'POST':
        config['callsign'] = request.form.get('callsign', 'N0CALL').upper().strip()
        config['grid'] = request.form.get('grid', '').strip()
        lat_str = request.form.get('latitude', '').strip()
        lon_str = request.form.get('longitude', '').strip()
        config['latitude'] = ''
        config['longitude'] = ''
        if lat_str:
            try:
                config['latitude'] = float(lat_str)
            except ValueError:
                pass
        if lon_str:
            try:
                config['longitude'] = float(lon_str)
            except ValueError:
                pass
        if request.form.get('winlink_password'):
            config['winlinkPasswd'] = request.form.get('winlink_password')
        save_user_config(config)
        # Also save to session for persistence
        session['callsign'] = config['callsign']
        session['grid'] = config['grid']
        session['name'] = config.get('name', '')
        return redirect(url_for('radio_setup'))
    return render_template('user_setup.html', config=config)

@app.route('/radio', methods=['GET', 'POST'])
def radio_setup():
    radios = get_radios()
    if request.method == 'POST':
        selected = request.form.get('radio')
        if selected and selected != 'none':
            set_active_radio(selected)
            return redirect(url_for('radio_settings', radio_id=selected))
        return redirect(url_for('internet_check'))
    return render_template('radio_setup.html', radios=radios)

@app.route('/radio/<radio_id>')
def radio_settings(radio_id):
    radio_file = RADIOS_DIR / f"{radio_id}.json"
    if not radio_file.exists():
        return redirect(url_for('radio_setup'))
    with open(radio_file) as f:
        radio = json.load(f)
    saved_file = radio_file.name
    return render_template('radio_settings.html', radio=radio, saved_file=saved_file, lang=session.get('lang', 'en'))

@app.route('/internet')
def internet_check():
    return render_template('internet_check.html', has_internet=check_internet(), lang=session.get('lang', 'en'))

@app.route('/drive', methods=['GET', 'POST'])
def drive_setup():
    error_message = None
    if request.method == 'POST':
        drive_type = request.form.get('drive_type', 'local')
        session['drive_type'] = drive_type
        if drive_type == 'usb':
            usb_path = request.form.get('usb_path', '')
            if usb_path:
                if not check_path_writable(usb_path):
                    return render_template('drive_setup.html', drives=get_usb_drives(), error_message=t('usb_read_only'))
                session['usb_path'] = usb_path
                try:
                    usb = Path(usb_path)
                    for d in ["tilesets", "my-maps", "wikipedia"]:
                        (usb / d).mkdir(exist_ok=True)
                    # Fix ownership immediately after creating directories
                    fix_usb_ownership(usb_path)
                except Exception as e:
                    print(f"[usb_setup] Failed to create directories on USB: {e}")
                    return render_template('drive_setup.html', drives=get_usb_drives(), error_message=t('usb_read_only'))
        return redirect(url_for('download_tiles'))
    return render_template('drive_setup.html', drives=get_usb_drives(), error_message=None)

@app.route('/download/tiles', methods=['GET', 'POST'])
def download_tiles():
    if request.method == 'POST':
        # Fix ownership after tile downloads
        if session.get('drive_type') == 'usb':
            fix_usb_ownership(session.get('usb_path', ''))
        return redirect(url_for('download_osm'))
    dest = Path(session.get('usb_path', '')) / "tilesets" if session.get('drive_type') == 'usb' else TILESET_DIR
    # Migrate old osm-* files to tt-* naming convention
    migrate_osm_to_tt(dest)
    migrate_osm_to_tt(SKEL_TILESET_DIR)
    # Check which files already exist at destination, on USB, or in skel
    existing_files = []
    for fname in TILE_FILES:
        dest_file = dest / fname
        skel_file = SKEL_TILESET_DIR / fname
        if dest_file.exists() or skel_file.exists():
            existing_files.append(fname)
    return render_template('download_tiles.html', tile_files=TILE_FILES, dest_path=dest, existing_files=existing_files)

@app.route('/api/download/tile', methods=['POST'])
def api_download_tile():
    filename = request.json.get('file')
    if not filename or filename not in TILE_FILES.keys():
        return jsonify({'success': False, 'error': 'Invalid file'})
    dest_dir = Path(session.get('usb_path', '')) / "tilesets" if session.get('drive_type') == 'usb' else TILESET_DIR
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_file = dest_dir / filename

    # Check if already exists at destination
    if dest_file.exists():
        return jsonify({'success': True, 'skipped': True})

    # Check if baked into ISO via /etc/skel — no need to download
    skel_file = SKEL_TILESET_DIR / filename
    if skel_file.exists():
        # If downloading to local (not USB), just symlink to skel
        if session.get('drive_type') != 'usb':
            dest_file.symlink_to(skel_file)
            return jsonify({'success': True, 'skipped': True})
        # If downloading to USB, still need to actually download for USB portability

    # Try Cloudflare R2 first, failover to SourceForge
    cf_url = f"https://releases.liaisonos.com/MAPS/{filename}"
    sf_url = f"{TILE_BASE_URL}/{filename}/download"
    sources = [
        (cf_url, ['-H', 'User-Agent: LiaisonOS-MapClient/2.2']),
        (sf_url, []),
    ]
    last_returncode = 0
    for url, extra_args in sources:
        try:
            subprocess.run(
                ['curl', '-L', '-f', '-o', str(dest_file)] + extra_args + [url],
                check=True, capture_output=True, timeout=1800
            )
            fix_ownership(dest_file, recursive=False)
            return jsonify({'success': True})
        except subprocess.CalledProcessError as e:
            last_returncode = e.returncode
            if dest_file.exists():
                dest_file.unlink()
    return jsonify({'success': False, 'error': {6:'No internet',7:'Server error',22:'Not found',23:'Disk full',28:'Timeout'}.get(last_returncode, f'Error {last_returncode}')})

@app.route('/download/osm', methods=['GET', 'POST'])
def download_osm():
    if request.method == 'POST':
        selected = request.form.getlist('regions')
        if selected:
            session['osm_regions'] = selected
            return redirect(url_for('download_osm_progress'))
        return redirect(url_for('download_wiki'))
    dest = Path(session.get('usb_path', '')) / "my-maps" if session.get('drive_type') == 'usb' else PBF_MAP_DIR
    existing = [f.stem.replace('-latest.osm', '') for f in dest.glob("*.pbf")] if dest.exists() else []
    return render_template('download_osm.html', dest_path=dest, existing_files=existing)

@app.route('/api/osm/regions/<country>')
def api_osm_regions(country):
    url = OSM_CANADA_URL if country == 'canada' else OSM_USA_URL
    base = "canada" if country == 'canada' else "us"
    try:
        import urllib.request
        with urllib.request.urlopen(url, timeout=30) as resp:
            html = resp.read().decode('utf-8')
        regions = []
        for m in re.findall(r'href="([^"]+\.osm\.pbf)"', html):
            if f"{base}-latest" not in m:
                name = m.split('/')[-1].replace('-latest.osm.pbf', '')
                regions.append({'id': name, 'name': name.replace('-', ' ').title()})
        seen, unique = set(), []
        for r in sorted(regions, key=lambda x: x['name']):
            if r['id'] not in seen:
                seen.add(r['id'])
                unique.append(r)
        return jsonify({'regions': unique})
    except Exception as e:
        return jsonify({'error': str(e), 'regions': []})

@app.route('/download/osm/progress')
def download_osm_progress():
    return render_template('download_osm_progress.html', regions=session.get('osm_regions', []))

@app.route('/api/download/osm', methods=['POST'])
def api_download_osm():
    region, country = request.json.get('region'), request.json.get('country', 'canada')
    if not region:
        return jsonify({'success': False, 'error': 'No region'})
    dest_dir = Path(session.get('usb_path', '')) / "my-maps" if session.get('drive_type') == 'usb' else PBF_MAP_DIR
    dest_dir.mkdir(parents=True, exist_ok=True)
    pbf_filename = f"{region}-latest.osm.pbf"
    url = f"{OSM_BASE_CANADA if country=='canada' else OSM_BASE_USA}/{pbf_filename}"
    dest_file = dest_dir / pbf_filename
    
    if dest_file.exists():
        return jsonify({'success': True, 'skipped': True})
    
    # Check if baked into ISO via /etc/skel
    skel_file = SKEL_PBF_MAP_DIR / pbf_filename
    if skel_file.exists():
        if session.get('drive_type') != 'usb':
            dest_file.symlink_to(skel_file)
            return jsonify({'success': True, 'skipped': True})
    
    try:
        r = subprocess.run(['curl', '-L', '-f', '-o', str(dest_file), url], capture_output=True, timeout=1800)
        if r.returncode != 0:
            dest_file.unlink(missing_ok=True)
            return jsonify({'success': False, 'error': f'Error {r.returncode}'})
        # Fix ownership of downloaded file
        fix_ownership(dest_file, recursive=False)
        return jsonify({'success': True})
    except Exception as e:
        print(f"[api_download_kiwix] Download failed: {e}")
        dest_file.unlink(missing_ok=True)
        return jsonify({'success': False, 'error': str(e)})

@app.route('/download/wiki', methods=['GET', 'POST'])
def download_wiki():
    if request.method == 'POST':
        selected = request.form.getlist('wiki_files')
        if selected:
            session['wiki_files'] = selected
            return redirect(url_for('download_wiki_progress'))
        return redirect(url_for('complete'))
    dest = Path(session.get('usb_path', '')) / "wikipedia" if session.get('drive_type') == 'usb' else ZIM_DIR
    return render_template('download_wiki.html', dest_path=dest)

@app.route('/api/wiki/files')
def api_wiki_files():
    files = {'en': [], 'fr': []}
    existing = set()
    for p in [Path(session.get('usb_path', '')) / "wikipedia", ZIM_DIR]:
        if p.exists():
            existing.update(f.name for f in p.glob("*.zim"))
    try:
        import urllib.request
        with urllib.request.urlopen(KIWIX_URL, timeout=30) as resp:
            html = resp.read().decode('utf-8')
        for fn, sz in re.findall(r'<a href="(wikipedia_(?:en|fr)[^"]+\.zim)">[^<]+</a>\s+[\d-]+\s+[\d:]+\s+([\d.]+[KMGT]?)', html):
            lang = 'en' if 'wikipedia_en' in fn else 'fr'
            files[lang].append({'name': fn, 'display': fn.replace('.zim',''), 'size': sz+('' if sz[-1] in 'KMGT' else 'B'), 'exists': fn in existing})
        files['en'].sort(key=lambda x: x['name'])
        files['fr'].sort(key=lambda x: x['name'])
    except Exception as e:
        print(f"[api_wiki_files] Failed to fetch wiki file list: {e}")
    return jsonify({'files': files})

@app.route('/download/wiki/progress')
def download_wiki_progress():
    return render_template('download_wiki_progress.html', files=session.get('wiki_files', []))

@app.route('/api/download/wiki', methods=['POST'])
def api_download_wiki():
    fn = request.json.get('file')
    if not fn:
        return jsonify({'success': False, 'error': 'No file'})
    dest_dir = Path(session.get('usb_path', '')) / "wikipedia" if session.get('drive_type') == 'usb' else ZIM_DIR
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_file = dest_dir / fn
    if dest_file.exists():
        return jsonify({'success': True, 'skipped': True})
    try:
        subprocess.run(['curl', '-L', '-f', '-o', str(dest_file), f"{KIWIX_URL}/{fn}"], check=True, capture_output=True, timeout=1800)
        # Fix ownership of downloaded file
        fix_ownership(dest_file, recursive=False)
        return jsonify({'success': True})
    except Exception as e:
        print(f"[api_download_wiki] Download of {fn} failed: {e}")
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/drive/check', methods=['POST'])
def api_check_drive():
    path = request.json.get('path', '')
    if not path:
        return jsonify({'writable': False, 'error': 'No path'})
    if check_path_writable(path):
        return jsonify({'writable': True})
    return jsonify({'writable': False, 'error': t('usb_read_only')})

# =============================================================================
# DATA TRANSFER (HDD Install only - copy Maps/Tiles/Wikipedia from USB to HDD)
# =============================================================================

@app.route('/data_transfer')
def data_transfer():
    """
    Show data transfer options for HDD install.
    Only shown when:
    - NOT in live boot mode (installed on HDD)
    - USB drive has data files (tiles, maps, wikipedia)
    """
    usb_path = session.get('usb_path', '') or session.get('persistence_usb_path', '')
    
    if not usb_path:
        return redirect(url_for('complete'))
    
    # Get data files info from USB
    data_info = get_data_files_info(usb_path)
    
    # If no data to copy, skip to complete
    if data_info['total_bytes'] == 0:
        return redirect(url_for('complete'))
    
    # Get HDD space (home directory)
    hdd_space = get_disk_space(str(Path.home()))
    
    # Check if there's enough space for all data
    space_ok = hdd_space and hdd_space['free'] > data_info['total_bytes']
    
    return render_template('data_transfer.html',
                          usb_path=usb_path,
                          data_info=data_info,
                          hdd_space=hdd_space,
                          space_ok=space_ok,
                          lang=session.get('lang', 'en'))

# --- Data transfer progress tracking ---
_copy_progress = {
    'active': False,
    'current_file': '',
    'current_bytes': 0,
    'file_size': 0,
    'files_done': 0,
    'files_total': 0,
    'bytes_done': 0,
    'bytes_total': 0,
    'results': [],
    'done': False,
    'error': None,
}


def _copy_with_progress(src_path, dst_path, progress):
    """Copy a file with byte-level progress updates."""
    import shutil
    file_size = src_path.stat().st_size
    progress['current_file'] = src_path.name
    progress['file_size'] = file_size
    progress['current_bytes'] = 0

    buf_size = 1024 * 1024  # 1MB chunks
    with open(src_path, 'rb') as fsrc, open(dst_path, 'wb') as fdst:
        while True:
            buf = fsrc.read(buf_size)
            if not buf:
                break
            fdst.write(buf)
            progress['current_bytes'] += len(buf)
            progress['bytes_done'] += len(buf)

    # Preserve timestamps
    import shutil
    shutil.copystat(src_path, dst_path)


def _collect_files(usb, categories):
    """Build list of (src, dst, category) tuples and compute total size."""
    files = []
    YAAC_TILEDIR = Path.home() / "YAAC" / "tiledir"
    YAAC_MAPCACHE = Path.home() / "YAAC" / "mapcache"

    for cat in categories:
        if cat == 'tiles':
            src = usb / "tilesets"
            if src.exists():
                TILESET_DIR.mkdir(parents=True, exist_ok=True)
                for f in src.glob("*.mbtiles"):
                    dst_file = TILESET_DIR / f.name
                    if not dst_file.exists():
                        files.append((f, dst_file, cat))

            usb_tiledir = usb / "tiledir"
            if usb_tiledir.exists() and not YAAC_TILEDIR.exists():
                # Collect all files in tiledir tree
                for f in usb_tiledir.rglob("*"):
                    if f.is_file():
                        rel = f.relative_to(usb_tiledir)
                        dst_file = YAAC_TILEDIR / rel
                        files.append((f, dst_file, 'tiledir'))

        elif cat == 'maps':
            src = usb / "my-maps"
            if src.exists():
                PBF_MAP_DIR.mkdir(parents=True, exist_ok=True)
                for f in src.glob("*.pbf"):
                    dst_file = PBF_MAP_DIR / f.name
                    if not dst_file.exists():
                        files.append((f, dst_file, cat))

            navit_src = usb / "navit-maps"
            if navit_src.exists():
                NAVIT_MAP_DIR.mkdir(parents=True, exist_ok=True)
                for f in navit_src.glob("*.bin"):
                    dst_file = NAVIT_MAP_DIR / f.name
                    if not dst_file.exists():
                        files.append((f, dst_file, cat))

        elif cat == 'wikipedia':
            src = usb / "wikipedia"
            if src.exists():
                ZIM_DIR.mkdir(parents=True, exist_ok=True)
                for f in src.glob("*.zim"):
                    dst_file = ZIM_DIR / f.name
                    if not dst_file.exists():
                        files.append((f, dst_file, cat))

        elif cat == 'yaac_mapcache':
            src = usb / "YAAC-MapCache"
            if src.exists():
                YAAC_MAPCACHE.mkdir(parents=True, exist_ok=True)
                for f in src.glob("*.db"):
                    dst_file = YAAC_MAPCACHE / f.name
                    files.append((f, dst_file, cat))

    return files


def _copy_worker(usb_path, categories):
    """Background worker for data transfer copy."""
    global _copy_progress
    try:
        usb = Path(usb_path)
        YAAC_TILEDIR = Path.home() / "YAAC" / "tiledir"
        YAAC_MAPCACHE = Path.home() / "YAAC" / "mapcache"

        # Collect all files and compute total
        file_list = _collect_files(usb, categories)
        total_bytes = sum(f[0].stat().st_size for f in file_list)

        _copy_progress['files_total'] = len(file_list)
        _copy_progress['bytes_total'] = total_bytes

        if not file_list:
            _copy_progress['results'].append('⏭ Nothing to copy')
            _copy_progress['done'] = True
            _copy_progress['active'] = False
            return

        for src_file, dst_file, cat in file_list:
            try:
                # Handle yaac_mapcache: remove existing symlink/file first
                if cat == 'yaac_mapcache':
                    if dst_file.is_symlink() or dst_file.exists():
                        dst_file.unlink()

                # Handle tiledir: remove symlink, create parent dirs
                if cat == 'tiledir':
                    if YAAC_TILEDIR.is_symlink():
                        YAAC_TILEDIR.unlink()
                    dst_file.parent.mkdir(parents=True, exist_ok=True)

                _copy_with_progress(src_file, dst_file, _copy_progress)
                fix_ownership(dst_file, recursive=False)
                _copy_progress['files_done'] += 1
                _copy_progress['results'].append(f"✓ Copied: {src_file.name}")

            except Exception as e:
                _copy_progress['files_done'] += 1
                _copy_progress['results'].append(f"✗ Error: {src_file.name}: {e}")

        # Fix tiledir ownership if it was copied
        if YAAC_TILEDIR.exists() and not YAAC_TILEDIR.is_symlink():
            fix_ownership(YAAC_TILEDIR, recursive=True)

        _copy_progress['done'] = True
        _copy_progress['active'] = False

    except Exception as e:
        _copy_progress['error'] = str(e)
        _copy_progress['done'] = True
        _copy_progress['active'] = False


@app.route('/api/data_transfer/copy', methods=['POST'])
def api_data_transfer_copy():
    """Start copying selected data categories from USB to HDD (background)."""
    global _copy_progress
    usb_path = session.get('usb_path', '') or session.get('persistence_usb_path', '')
    categories = request.json.get('categories', [])

    if not usb_path or not categories:
        return jsonify({'success': False, 'error': 'No USB path or categories'})

    if _copy_progress['active']:
        return jsonify({'success': False, 'error': 'Copy already in progress'})

    # Reset progress
    _copy_progress = {
        'active': True,
        'current_file': '',
        'current_bytes': 0,
        'file_size': 0,
        'files_done': 0,
        'files_total': 0,
        'bytes_done': 0,
        'bytes_total': 0,
        'results': [],
        'done': False,
        'error': None,
    }

    # Save categories to session
    session['copied_categories'] = categories

    thread = threading.Thread(target=_copy_worker, args=(usb_path, categories),
                              daemon=True)
    thread.start()

    return jsonify({'success': True, 'started': True})


@app.route('/api/data_transfer/progress', methods=['GET'])
def api_data_transfer_progress():
    """Return current data transfer copy progress."""
    pct = 0
    if _copy_progress['bytes_total'] > 0:
        pct = int((_copy_progress['bytes_done'] / _copy_progress['bytes_total']) * 100)

    return jsonify({
        'active': _copy_progress['active'],
        'percent': min(pct, 100),
        'current_file': _copy_progress['current_file'],
        'current_bytes': _copy_progress['current_bytes'],
        'file_size': _copy_progress['file_size'],
        'files_done': _copy_progress['files_done'],
        'files_total': _copy_progress['files_total'],
        'bytes_done': _copy_progress['bytes_done'],
        'bytes_total': _copy_progress['bytes_total'],
        'results': _copy_progress['results'],
        'done': _copy_progress['done'],
        'error': _copy_progress['error'],
    })

def configure_yaac_position():
    """Configure YAAC Beacons/MYCALL prefs with user's position and GPS setting.

    Reads callsign and grid from user config, converts grid to lat/lon,
    detects GPS, and updates the YAAC prefs.xml accordingly.
    """
    try:
        # Import grid conversion
        sys.path.insert(0, str(ET_BASE / "lib"))
        from et_supervisor.grid_utils import grid_to_latlon

        config = load_user_config()
        callsign = config.get('callsign', 'N0CALL')
        grid = config.get('grid', '')

        # YAAC Beacons/MYCALL prefs path
        prefs_dir = Path.home() / ".java" / ".userPrefs" / "org" / "ka2ddo" / "yaac" / "Beacons" / "MYCALL"
        prefs_file = prefs_dir / "prefs.xml"

        if not prefs_file.exists():
            print(f"[YAAC] Beacons/MYCALL prefs.xml not found, skipping")
            return

        content = prefs_file.read_text()

        # Update callsign in beaconName
        if callsign and callsign != 'N0CALL':
            content = re.sub(
                r'<entry key="beaconName" value="[^"]*"/>',
                f'<entry key="beaconName" value="{callsign}"/>',
                content)

        # Resolve position: prefer explicit coordinates, fall back to grid
        user_lat = config.get('latitude', '')
        user_lon = config.get('longitude', '')
        coords = None
        if user_lat and user_lon and isinstance(user_lat, (int, float)) and isinstance(user_lon, (int, float)):
            coords = (float(user_lat), float(user_lon))
            print(f"[YAAC] Position set from coordinates: lat={user_lat}, lon={user_lon}")
        elif grid:
            coords = grid_to_latlon(grid)
            if coords:
                print(f"[YAAC] Position set from grid {grid}: lat={coords[0]}, lon={coords[1]}")

        if coords:
            lat, lon = coords
            content = re.sub(
                r'<entry key="latitude" value="[^"]*"/>',
                f'<entry key="latitude" value="{lat}"/>',
                content)
            content = re.sub(
                r'<entry key="longitude" value="[^"]*"/>',
                f'<entry key="longitude" value="{lon}"/>',
                content)
            print(f"[YAAC] YAAC position updated: lat={lat}, lon={lon}")

            # Remove saved map Center from main YAAC prefs so YAAC
            # falls back to beacon lat/lon on next startup
            main_prefs = Path.home() / ".java" / ".userPrefs" / "org" / "ka2ddo" / "yaac" / "prefs.xml"
            if main_prefs.exists():
                main_content = main_prefs.read_text()
                if 'key="Center"' in main_content:
                    main_content = re.sub(
                        r'\s*<entry key="Center" value="[^"]*"/>', '',
                        main_content)
                    main_prefs.write_text(main_content)
                    print(f"[YAAC] Removed stale map Center from main prefs")

        # Detect GPS and set useGpsForPosition
        has_gps = os.path.exists('/dev/et-gps')
        gps_value = "true" if has_gps else "false"
        content = re.sub(
            r'<entry key="useGpsForPosition" value="[^"]*"/>',
            f'<entry key="useGpsForPosition" value="{gps_value}"/>',
            content)
        print(f"[YAAC] GPS {'detected' if has_gps else 'not detected'}, useGpsForPosition={gps_value}")

        prefs_file.write_text(content)
        print(f"[YAAC] Beacons/MYCALL prefs updated")

    except Exception as e:
        print(f"[YAAC] Failed to configure position: {e}")

@app.route('/complete')
def complete():
    usb_path = session.get('usb_path', '') or session.get('persistence_usb_path', '')
    
    # Fix ownership of all USB data before creating symlinks
    if session.get('drive_type') == 'usb' and usb_path:
        fix_usb_ownership(usb_path)
    
    symlinks = []
    
    # Check if we came from data_transfer (HDD install with partial copy)
    copied_categories = session.get('copied_categories', None)
    
    if copied_categories is not None and usb_path:
        # HDD install - create symlinks for categories NOT copied
        print(f"[COMPLETE] Data transfer completed, copied: {copied_categories}")
        symlinks = create_symlinks_for_uncopied(usb_path, copied_categories)
    elif session.get('drive_type') == 'usb':
        # USB mode (normal flow or live boot) - create all symlinks
        symlinks = create_symlinks(usb_path)
    elif session.get('restored_from_usb', False) and usb_path:
        # Restored from USB but skipped data_transfer - create all symlinks
        print(f"[COMPLETE] Restored from USB, creating all symlinks")
        symlinks = create_symlinks_for_uncopied(usb_path, [])  # Empty list = nothing copied = all symlinks
    
    # Save config to USB persistence if available (and not just restored)
    persistence_saved = False
    if not session.get('restored_from_usb', False):
        persistence_saved = save_to_persistence()
        if persistence_saved:
            print("[COMPLETE] Config saved to USB persistence")
    
    # Enable Save & Reboot/Shutdown menu items when USB persistence is active
    if session.get('drive_type') == 'usb' or session.get('restored_from_usb', False):
        enable_save_menu_script = "/opt/emcomm-tools/bin/et-persistence/et-enable-save-menu"
        if os.path.exists(enable_save_menu_script):
            try:
                subprocess.run([enable_save_menu_script], capture_output=True, text=True, timeout=10)
                print("[COMPLETE] Save menu enabled")
            except Exception as e:
                print(f"[COMPLETE] Failed to enable save menu: {e}")
    
    # Generate radio configuration document in ~/Documents/
    radio_doc = generate_radio_config_document()

    # Pre-build Lucene index for et-api if it doesn't exist (non-blocking)
    index_dir = '/opt/emcomm-tools-api/index/license'
    api_bin = '/opt/emcomm-tools-api/bin/et-api'
    if not os.path.isdir(index_dir) and os.path.isfile(api_bin):
        print("[COMPLETE] Lucene index missing, starting et-api in background to build it...")
        try:
            subprocess.Popen([api_bin], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as e:
            print(f"[COMPLETE] Failed to start et-api: {e}")

    # Check for WiFi/BT configs on USB (for HDD installs where they were skipped)
    has_hw_configs = False
    if session.get('restored_from_usb', False) and not os.path.exists('/run/live'):
        persistence_path = session.get('persistence_usb_path', '')
        if persistence_path:
            wifi_dir = os.path.join(persistence_path, 'configs', 'wifi')
            bt_dir = os.path.join(persistence_path, 'configs', 'bluetooth')
            has_wifi = os.path.isdir(wifi_dir) and os.listdir(wifi_dir)
            has_bt = os.path.isdir(bt_dir) and os.listdir(bt_dir)
            if has_wifi or has_bt:
                has_hw_configs = True

    # Configure YAAC position from user's grid square and GPS detection
    configure_yaac_position()

    flag = Path.home() / ".config" / "emcomm-tools" / ".firstboot-complete"
    flag.parent.mkdir(parents=True, exist_ok=True)
    flag.touch()

    return render_template('complete.html',
                          symlinks=symlinks,
                          has_osm_maps=bool(session.get('osm_regions')),
                          lang=session.get('lang', 'en'),
                          restored=session.get('restored_from_usb', False),
                          persistence_saved=persistence_saved,
                          radio_doc=radio_doc,
                          has_hw_configs=has_hw_configs)

@app.route('/api/run_restore_hw', methods=['POST'])
def api_run_restore_hw():
    subprocess.Popen([
        "xfce4-terminal", "--title=Restore Hardware",
        "-e", "bash -c 'sudo /opt/emcomm-tools/bin/et-persistence/et-restore-hw; echo; echo Press Enter to close...; read'"
    ])
    return jsonify({'success': True})

@app.route('/api/quit', methods=['POST'])
def api_quit():
    os._exit(0)

# =============================================================================
# MAIN
# =============================================================================

def run_flask(port):
    app.run(host='127.0.0.1', port=port, debug=False, use_reloader=False, threaded=True)

def wait_for_flask(port, timeout=5):
    """Poll until Flask is accepting connections or timeout expires."""
    import socket as _socket
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with _socket.create_connection(('127.0.0.1', port), timeout=0.1):
                return
        except OSError:
            time.sleep(0.05)

def main():
    import argparse
    parser = argparse.ArgumentParser(description='LiaisonOS First Boot Wizard')
    parser.add_argument('--port', type=int, default=5000)
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--debug', action='store_true')
    parser.add_argument('--force', action='store_true')
    args = parser.parse_args()
    
    # Remove installer icon on HDD install (before flag check so it always runs)
    if not Path("/run/live").exists():
        cal = Path.home() / "Desktop" / "calamares-install-debian.desktop"
        if cal.exists():
            try: cal.unlink()
            except Exception as e: print(f"[main] Failed to remove calamares desktop icon: {e}")

    if not args.force:
        flag = Path.home() / ".config" / "emcomm-tools" / ".firstboot-complete"
        if flag.exists():
            print("Already completed. Use --force to run again.")
            sys.exit(0)
    
    if args.debug:
        app.run(host=args.host, port=args.port, debug=True)
    else:
        try:
            import webview
            try:
                import gi
                gi.require_version('Gdk', '3.0')
                from gi.repository import Gdk
                screen = Gdk.Screen.get_default()
                w, h = screen.get_width(), screen.get_height()
                win_w = min(600, w-40) if w <= 1024 else 650
                win_h = h - 98
                x, y = (w - win_w) // 2, 10
            except Exception as e:
                print(f"[main] Screen detection failed, using defaults: {e}")
                win_w, win_h, x, y = 650, 550, None, None
            
            threading.Thread(target=run_flask, args=(args.port,), daemon=True).start()
            wait_for_flask(args.port)
            webview.create_window('LiaisonOS', f'http://127.0.0.1:{args.port}', width=win_w, height=win_h, resizable=True, min_size=(500, 450), x=x, y=y, frameless=False)
            webview.start()
        except ImportError:
            print("PyWebView not installed")
            sys.exit(1)

if __name__ == '__main__':
    main()
