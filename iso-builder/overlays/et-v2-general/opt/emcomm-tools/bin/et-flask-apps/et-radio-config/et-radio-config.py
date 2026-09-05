#!/usr/bin/env python3
"""
et-radio-config - Radio Configuration Editor
Author: Sylvain Deguire (VA2OPS)
Date: March 2026

Flask-based web UI for viewing, editing, and creating radio configuration profiles.
"""

import warnings
warnings.filterwarnings("ignore", category=DeprecationWarning)
warnings.filterwarnings("ignore", message=".*pkg_resources.*")

import os
import sys
import json
import re
import webbrowser
import threading
import time
import logging
from pathlib import Path
from flask import Flask, render_template, request, jsonify, redirect, url_for

# Suppress Flask development server warning
log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)

app = Flask(__name__)
app.secret_key = 'emcomm-tools-radio-config-2026'

# Configuration paths
RADIOS_DIR = Path("/opt/emcomm-tools/conf/radios.d")
USER_RADIOS_DIR = Path.home() / ".config" / "emcomm-tools" / "radios.d"
ACTIVE_RADIO_LINK = RADIOS_DIR / "active-radio.json"
ET_CONFIG_FILE = Path.home() / ".config" / "emcomm-tools" / "user.json"


def get_language():
    """Get language preference from user config."""
    if ET_CONFIG_FILE.exists():
        try:
            with open(ET_CONFIG_FILE, 'r') as f:
                config = json.load(f)
                return config.get('language', 'en')
        except Exception:
            pass
    return 'en'


def slugify(text):
    """Convert text to a slug suitable for filenames."""
    text = text.lower().strip()
    text = re.sub(r'[()]+', '', text)
    text = re.sub(r'[^a-z0-9]+', '-', text)
    text = text.strip('-')
    text = re.sub(r'-+', '-', text)
    return text


def get_active_radio_filename():
    """Get the filename of the currently active radio."""
    if ACTIVE_RADIO_LINK.exists() or ACTIVE_RADIO_LINK.is_symlink():
        try:
            target = os.readlink(str(ACTIVE_RADIO_LINK))
            return os.path.basename(target)
        except Exception:
            pass
    return None


def get_connection_type(data):
    """Determine radio connection type from config data."""
    if 'bluetooth' in data:
        return 'bt'
    if 'rigctrl' in data:
        if data['rigctrl'].get('id') == '1':
            return 'audio_only'
        return 'usb'
    return 'audio_only'


def load_all_radios():
    """Load all radio configs, merging stock and custom. Custom wins on conflict."""
    radios = {}

    # Load from system radios dir (includes stock files and symlinks to user dir)
    if RADIOS_DIR.exists():
        for f in sorted(RADIOS_DIR.glob("*.json")):
            if f.name == "active-radio.json":
                continue
            try:
                is_custom = f.is_symlink()
                with open(f, 'r') as fh:
                    data = json.load(fh)
                radios[f.name] = {
                    'filename': f.name,
                    'data': data,
                    'custom': is_custom,
                    'conn_type': get_connection_type(data),
                }
            except Exception as e:
                print(f"Error loading {f}: {e}")

    # Load user radios not yet symlinked into system dir
    if USER_RADIOS_DIR.exists():
        for f in sorted(USER_RADIOS_DIR.glob("*.json")):
            if f.name not in radios:
                try:
                    with open(f, 'r') as fh:
                        data = json.load(fh)
                    radios[f.name] = {
                        'filename': f.name,
                        'data': data,
                        'custom': True,
                        'conn_type': get_connection_type(data),
                    }
                except Exception:
                    pass

    # Sort by vendor then model
    return sorted(radios.values(),
                  key=lambda r: (r['data'].get('vendor', '').lower(),
                                 r['data'].get('model', '').lower()))


def save_radio(data, filename):
    """Save radio JSON to user dir and create symlink in system dir."""
    USER_RADIOS_DIR.mkdir(parents=True, exist_ok=True)

    user_file = USER_RADIOS_DIR / filename
    system_file = RADIOS_DIR / filename

    # Write to user dir
    with open(user_file, 'w') as f:
        json.dump(data, f, indent=2)
        f.write('\n')

    # Create symlink in system dir
    try:
        if system_file.exists() or system_file.is_symlink():
            system_file.unlink()
        system_file.symlink_to(user_file)
    except OSError as e:
        print(f"Warning: Could not create symlink in {RADIOS_DIR}: {e}")

    return True


def delete_radio(filename):
    """Delete a radio config from both user and system dirs."""
    system_file = RADIOS_DIR / filename
    user_file = USER_RADIOS_DIR / filename

    try:
        if system_file.is_symlink() or system_file.exists():
            system_file.unlink()
    except OSError:
        pass

    try:
        if user_file.exists():
            user_file.unlink()
    except OSError:
        pass

    return True


# ============================================================================
# Translations
# ============================================================================

TRANSLATIONS = {
    'en': {
        'title': 'Radio Configuration',
        'subtitle': 'View, edit, and create radio profiles',
        'new_radio': 'New Radio',
        'edit': 'Edit',
        'duplicate': 'Duplicate',
        'delete': 'Delete',
        'save': 'Save',
        'cancel': 'Cancel',
        'back': 'Back',
        'active': 'Active',
        'custom': 'Custom',
        'stock': 'Stock',
        'confirm_delete': 'Delete this radio configuration?',
        'confirm_delete_msg': 'This action cannot be undone.',
        'cannot_delete_active': 'Cannot delete the active radio. Select a different radio first.',
        'no_radios': 'No radio configurations found.',
        'vendor': 'Vendor',
        'model': 'Model',
        'bands': 'Bands',
        'core_section': 'Core Settings',
        'rigctrl_section': 'CAT Control (rigctrl)',
        'varafm_section': 'VARA FM',
        'bluetooth_section': 'Bluetooth TNC',
        'audio_section': 'Audio',
        'notes_section': 'Configuration Notes',
        'fieldnotes_section': 'Field Notes',
        'hamlib_id': 'Hamlib Rig ID',
        'baud_rate': 'Baud Rate',
        'ptt_method': 'PTT Method',
        'conf': 'Hamlib --set-conf',
        'ptt_only': 'PTT Only Mode',
        'prime_rig': 'Prime Rig on Connect',
        'ptt_port': 'PTT Port (Wine COM)',
        'ptt_via': 'PTT Via',
        'dtr': 'DTR',
        'rts': 'RTS',
        'bt_device': 'Device Name',
        'bt_channel': 'RFCOMM Channel',
        'bt_mac': 'MAC Address',
        'audio_script': 'ALSA Setup Script',
        'notes_hint': 'One note per line',
        'fieldnotes_hint': 'One note per line (internal/developer notes)',
        'vendor_required': 'Vendor is required',
        'model_required': 'Model is required',
        'rigctrl_id_required': 'Hamlib Rig ID is required when CAT Control is enabled',
        'saved': 'Radio configuration saved.',
        'deleted': 'Radio configuration deleted.',
        'create_title': 'New Radio',
        'edit_title': 'Edit Radio',
        'duplicate_title': 'Duplicate Radio',
        'connection': 'Connection',
        'usb': 'USB/CAT',
        'bt': 'Bluetooth',
        'audio_only': 'Audio Only',
        'enable_section': 'Enable',
    },
    'fr': {
        'title': 'Configuration Radio',
        'subtitle': 'Voir, modifier et creer des profils radio',
        'new_radio': 'Nouvelle Radio',
        'edit': 'Modifier',
        'duplicate': 'Dupliquer',
        'delete': 'Supprimer',
        'save': 'Sauvegarder',
        'cancel': 'Annuler',
        'back': 'Retour',
        'active': 'Active',
        'custom': 'Personnalise',
        'stock': 'Stock',
        'confirm_delete': 'Supprimer cette configuration radio?',
        'confirm_delete_msg': 'Cette action est irreversible.',
        'cannot_delete_active': 'Impossible de supprimer la radio active. Selectionnez une autre radio d\'abord.',
        'no_radios': 'Aucune configuration radio trouvee.',
        'vendor': 'Fabricant',
        'model': 'Modele',
        'bands': 'Bandes',
        'core_section': 'Parametres de base',
        'rigctrl_section': 'Controle CAT (rigctrl)',
        'varafm_section': 'VARA FM',
        'bluetooth_section': 'Bluetooth TNC',
        'audio_section': 'Audio',
        'notes_section': 'Notes de configuration',
        'fieldnotes_section': 'Notes de terrain',
        'hamlib_id': 'ID Rig Hamlib',
        'baud_rate': 'Debit en bauds',
        'ptt_method': 'Methode PTT',
        'conf': 'Hamlib --set-conf',
        'ptt_only': 'Mode PTT uniquement',
        'prime_rig': 'Initialiser a la connexion',
        'ptt_port': 'Port PTT (Wine COM)',
        'ptt_via': 'PTT via',
        'dtr': 'DTR',
        'rts': 'RTS',
        'bt_device': 'Nom de l\'appareil',
        'bt_channel': 'Canal RFCOMM',
        'bt_mac': 'Adresse MAC',
        'audio_script': 'Script audio ALSA',
        'notes_hint': 'Une note par ligne',
        'fieldnotes_hint': 'Une note par ligne (notes internes/developpeur)',
        'vendor_required': 'Le fabricant est requis',
        'model_required': 'Le modele est requis',
        'rigctrl_id_required': 'L\'ID Rig Hamlib est requis lorsque le controle CAT est active',
        'saved': 'Configuration radio sauvegardee.',
        'deleted': 'Configuration radio supprimee.',
        'create_title': 'Nouvelle Radio',
        'edit_title': 'Modifier la Radio',
        'duplicate_title': 'Dupliquer la Radio',
        'connection': 'Connexion',
        'usb': 'USB/CAT',
        'bt': 'Bluetooth',
        'audio_only': 'Audio seul',
        'enable_section': 'Activer',
    }
}


def get_translations():
    """Get translations for current language."""
    lang = get_language()
    return TRANSLATIONS.get(lang, TRANSLATIONS['en']), lang


# ============================================================================
# Routes
# ============================================================================

@app.route('/')
def index():
    """Radio list page."""
    t, lang = get_translations()
    radios = load_all_radios()
    active_filename = get_active_radio_filename()
    return render_template('index.html', t=t, lang=lang, radios=radios,
                           active_filename=active_filename)


@app.route('/edit/<filename>')
def edit(filename):
    """Edit an existing radio config."""
    t, lang = get_translations()
    radios = load_all_radios()
    radio = None
    for r in radios:
        if r['filename'] == filename:
            radio = r
            break

    if not radio:
        return redirect(url_for('index'))

    return render_template('editor.html', t=t, lang=lang, radio=radio['data'],
                           filename=filename, mode='edit',
                           page_title=t['edit_title'])


@app.route('/new')
def new():
    """Create a new radio config."""
    t, lang = get_translations()
    empty_radio = {
        'id': '',
        'vendor': '',
        'model': '',
        'bands': [],
    }
    return render_template('editor.html', t=t, lang=lang, radio=empty_radio,
                           filename='', mode='create',
                           page_title=t['create_title'])


@app.route('/duplicate/<filename>')
def duplicate(filename):
    """Duplicate an existing radio config."""
    t, lang = get_translations()
    radios = load_all_radios()
    radio = None
    for r in radios:
        if r['filename'] == filename:
            radio = r
            break

    if not radio:
        return redirect(url_for('index'))

    # Modify for duplicate
    data = json.loads(json.dumps(radio['data']))  # deep copy
    data['model'] = data.get('model', '') + ' (Copy)'
    new_id = slugify(data.get('vendor', '') + '-' + data.get('model', ''))
    data['id'] = new_id

    return render_template('editor.html', t=t, lang=lang, radio=data,
                           filename='', mode='create',
                           page_title=t['duplicate_title'])


@app.route('/api/save', methods=['POST'])
def api_save():
    """Save a radio configuration."""
    t, _ = get_translations()
    data = request.get_json()
    if not data:
        return jsonify({'success': False, 'error': 'No data received'}), 400

    vendor = data.get('vendor', '').strip()
    model = data.get('model', '').strip()

    if not vendor:
        return jsonify({'success': False, 'error': t['vendor_required']}), 400
    if not model:
        return jsonify({'success': False, 'error': t['model_required']}), 400

    # Generate ID and filename
    radio_id = slugify(vendor + '-' + model)
    has_bluetooth = bool(data.get('bluetooth'))

    if has_bluetooth:
        filename = radio_id + '.bt.json'
    else:
        filename = radio_id + '.json'

    # If editing, check if filename changed and clean up old file
    original_filename = data.get('_original_filename')
    if original_filename and original_filename != filename:
        delete_radio(original_filename)

    # Build clean radio data
    radio_data = {
        'id': radio_id,
        'vendor': vendor,
        'model': model,
        'bands': data.get('bands', []),
    }

    # Optional sections — only include if present and non-empty
    if data.get('rigctrl'):
        rigctrl = data['rigctrl']
        if not rigctrl.get('id'):
            return jsonify({'success': False, 'error': t['rigctrl_id_required']}), 400
        rigctrl_clean = {
            'id': rigctrl['id'],
            'ptt': rigctrl.get('ptt', 'RIG'),
        }
        if rigctrl.get('baud'):
            rigctrl_clean['baud'] = rigctrl['baud']
        if rigctrl.get('conf'):
            rigctrl_clean['conf'] = rigctrl['conf']
        if rigctrl.get('pttOnly'):
            rigctrl_clean['pttOnly'] = rigctrl['pttOnly']
        if rigctrl.get('primeRig'):
            rigctrl_clean['primeRig'] = True
        radio_data['rigctrl'] = rigctrl_clean
    else:
        # No rigctrl block means "this radio has no CAT". QtDashboard reads a
        # top-level "cat" field: true → expect rigctld, red X if missing; false
        # → show "CAT: N/A" (gray) and don't block mode starts. Set it so a
        # no-CAT setup (HT + DigiRig Lite + VOX or serial-PTT) doesn't get
        # stuck at the CAT precondition check.
        radio_data['cat'] = False

    if data.get('varafm'):
        varafm = data['varafm']
        varafm_clean = {}
        for key in ('pttPort', 'pttVia', 'baud', 'dtr', 'rts'):
            if varafm.get(key):
                varafm_clean[key] = varafm[key]
        if varafm_clean:
            radio_data['varafm'] = varafm_clean

    if data.get('bluetooth'):
        bt = data['bluetooth']
        bt_clean = {}
        for key in ('deviceName', 'channel', 'mac'):
            bt_clean[key] = bt.get(key, '')
        radio_data['bluetooth'] = bt_clean

    if data.get('audio'):
        audio = data['audio']
        if audio.get('script'):
            radio_data['audio'] = {'script': audio['script']}

    # Notes — split textarea lines into array
    notes = data.get('notes', [])
    if isinstance(notes, str):
        notes = [line.strip() for line in notes.split('\n') if line.strip()]
    radio_data['notes'] = notes

    field_notes = data.get('fieldNotes', [])
    if isinstance(field_notes, str):
        field_notes = [line.strip() for line in field_notes.split('\n') if line.strip()]
    if field_notes:
        radio_data['fieldNotes'] = field_notes

    save_radio(radio_data, filename)

    return jsonify({'success': True, 'message': t['saved'], 'filename': filename})


@app.route('/api/delete', methods=['POST'])
def api_delete():
    """Delete a radio configuration."""
    t, _ = get_translations()
    data = request.get_json()
    filename = data.get('filename')

    if not filename:
        return jsonify({'success': False, 'error': 'No filename specified'}), 400

    # Can't delete active radio
    active = get_active_radio_filename()
    if filename == active:
        return jsonify({'success': False, 'error': t['cannot_delete_active']}), 400

    delete_radio(filename)
    return jsonify({'success': True, 'message': t['deleted']})


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
    """Run Flask server in background thread (silently)."""
    cli = sys.modules.get('flask.cli')
    if cli:
        cli.show_server_banner = lambda *args, **kwargs: None
    with open(os.devnull, 'w') as devnull:
        old_stdout, old_stderr = sys.stdout, sys.stderr
        sys.stdout, sys.stderr = devnull, devnull
        try:
            app.run(host='127.0.0.1', port=port, debug=False, use_reloader=False)
        finally:
            sys.stdout, sys.stderr = old_stdout, old_stderr


def open_browser(port):
    """Open browser after short delay."""
    time.sleep(1)
    webbrowser.open(f'http://127.0.0.1:{port}')


if __name__ == '__main__':
    port = 5057

    if '--no-browser' in sys.argv:
        app.run(host='127.0.0.1', port=port, debug=False)
    elif '--browser' in sys.argv:
        threading.Thread(target=open_browser, args=(port,), daemon=True).start()
        app.run(host='127.0.0.1', port=port, debug=False)
    elif '--help' in sys.argv:
        print("Usage: et-radio-config [OPTIONS]")
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

            try:
                import gi
                gi.require_version('Gdk', '3.0')
                from gi.repository import Gdk

                screen = Gdk.Screen.get_default()
                screen_width = screen.get_width()
                screen_height = screen.get_height()

                panel_height = 60
                if screen_height <= 800:
                    win_width = min(560, screen_width - 40)
                    win_height = screen_height - panel_height - 40
                else:
                    win_width = 560
                    win_height = min(750, screen_height - panel_height - 60)

                x = (screen_width - win_width) // 2
                y = 30
            except Exception:
                win_width = 560
                win_height = 750
                x = None
                y = None

            flask_thread = threading.Thread(target=run_flask, args=(port,), daemon=True)
            flask_thread.start()
            time.sleep(1)

            window = webview.create_window(
                'LiaisonOS',
                f'http://127.0.0.1:{port}',
                width=win_width, height=win_height,
                resizable=True, min_size=(450, 500),
                x=x, y=y, frameless=False
            )
            webview.start()

        except ImportError:
            threading.Thread(target=open_browser, args=(port,), daemon=True).start()
            app.run(host='127.0.0.1', port=port, debug=False)
