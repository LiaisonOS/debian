"""
ConfigTemplater: Replace sed-based config templating with Python.

Handles:
- template: Copy template file, replace {{VAR}} placeholders with values
- ini-update: Modify INI-style config files (JS8Call.ini, WSJT-X.ini)
- xml-update: Modify XML config files (YAAC prefs.xml, fldigi_def.xml)
- station-position: Grid-to-latlon for YAAC prefs.xml
"""

import json
import logging
import os
import re
import shutil
import subprocess

log = logging.getLogger("et-supervisor.config")

ET_HOME = "/opt/emcomm-tools"
USER_CONFIG_PATH = os.path.expanduser("~/.config/liaisonos/user.json")


def load_user_config():
    """Load user.json config file.

    Returns:
        dict with user config or empty dict on failure.
    """
    try:
        with open(USER_CONFIG_PATH) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        log.error("Failed to load user config: %s", e)
        return {}


def resolve_var(var_spec, user_config=None):
    """Resolve a variable value from its source specification.

    Args:
        var_spec: dict with "source" and "key" or other source-specific fields.
        user_config: pre-loaded user config dict (optional, loaded if needed).

    Returns:
        Resolved string value, or None if resolution failed.
    """
    source = var_spec.get("source")
    if user_config is None:
        user_config = load_user_config()

    if source == "user.json":
        key = var_spec.get("key", "")
        value = user_config.get(key)
        if value is None:
            log.warning("Key '%s' not found in user.json", key)
        return str(value) if value is not None else None

    if source == "detect-audio":
        return _detect_audio_device(var_spec.get("format", "plughw:{card},{device}"))

    if source == "active-radio":
        return _read_active_radio(var_spec.get("key", ""))

    if source == "literal":
        return var_spec.get("value", "")

    if source == "detect-grid":
        return _detect_grid(user_config)

    if source == "radio-model-safe":
        return _get_radio_model_safe()

    if source == "detect-portaudio":
        return _detect_portaudio(var_spec.get("field", "device"))

    if source == "detect-position":
        field = var_spec.get("field")  # "latitude" or "longitude"
        value = user_config.get(field, "")
        if value and isinstance(value, (int, float)):
            return str(value)
        # Fallback: convert grid square
        grid = user_config.get("grid", "")
        if grid:
            from et_supervisor.grid_utils import grid_to_latlon
            coords = grid_to_latlon(grid)
            if coords:
                return str(coords[0] if field == "latitude" else coords[1])
        log.warning("No %s available (no coordinates or grid in user.json)", field)
        return None

    if source == "detect-savedir":
        # Build save directory path from $HOME + app-specific suffix
        # Usage: {"source": "detect-savedir", "field": "JS8Call/save"}
        suffix = var_spec.get("field", var_spec.get("key", ""))
        home = os.path.expanduser("~")
        path = os.path.join(home, ".local/share", suffix)
        os.makedirs(path, exist_ok=True)
        return path

    log.warning("Unknown variable source: %s", source)
    return None


def _detect_audio_device(fmt):
    """Detect the ET_AUDIO tagged sound device.

    Returns:
        Audio device string like "plughw:2,0" or None.
    """
    import subprocess
    try:
        result = subprocess.run(
            ["arecord", "-l"],
            capture_output=True, text=True, timeout=5
        )
        for line in result.stdout.splitlines():
            if "ET_AUDIO" in line:
                # Parse "card X: ... device Y: ..."
                card_match = re.search(r"card (\d+)", line)
                device_match = re.search(r"device (\d+)", line)
                if card_match and device_match:
                    card = card_match.group(1)
                    device = device_match.group(1)
                    return fmt.format(card=card, device=device)
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        log.error("Audio detection failed: %s", e)
    return None


def _read_active_radio(key):
    """Read a value from the active radio config."""
    radio_conf = os.path.join(ET_HOME, "conf/radios.d/active-radio.json")
    try:
        with open(radio_conf) as f:
            data = json.load(f)
        # Support dotted keys like "rigctrl.primeRig"
        for part in key.split("."):
            data = data[part]
        return str(data)
    except (FileNotFoundError, KeyError, json.JSONDecodeError) as e:
        log.warning("Failed to read active-radio key '%s': %s", key, e)
        return None


def apply_template(config_entry, user_config=None):
    """Process a template config entry from a mode JSON.

    Args:
        config_entry: dict with "template", "target", "vars" keys.
        user_config: pre-loaded user config (optional).

    Returns:
        True if template was applied successfully.
    """
    if user_config is None:
        user_config = load_user_config()

    template_path = os.path.join(ET_HOME, config_entry["template"])
    target_path = os.path.join(ET_HOME, config_entry["target"])

    if not os.path.isfile(template_path):
        log.error("Template not found: %s", template_path)
        return False

    # Copy template to target
    os.makedirs(os.path.dirname(target_path), exist_ok=True)
    shutil.copy2(template_path, target_path)

    # Read template content
    with open(target_path) as f:
        content = f.read()

    # Replace all variables
    vars_spec = config_entry.get("vars", {})
    for var_name, var_spec in vars_spec.items():
        value = resolve_var(var_spec, user_config)
        if value is None:
            log.error("Cannot resolve variable %s", var_name)
            return False
        placeholder = "{{" + var_name + "}}"
        content = content.replace(placeholder, value)

    # Write resolved content
    with open(target_path, "w") as f:
        f.write(content)

    log.info("Applied template %s -> %s", template_path, target_path)
    return True


def apply_configs(config_entries, user_config=None):
    """Apply all config entries from a mode definition.

    Returns:
        True if all configs applied successfully.
    """
    if user_config is None:
        user_config = load_user_config()

    for entry in config_entries:
        config_type = entry.get("type", "template")

        if config_type == "template":
            if not apply_template(entry, user_config):
                return False
        elif config_type == "ini-update":
            if not ini_update(entry, user_config):
                return False
        elif config_type == "xml-update":
            if not xml_update(entry, user_config):
                return False
        elif config_type == "station-position":
            if not station_position(entry, user_config):
                return False
        else:
            log.warning("Unknown config type: %s", config_type)

    return True


# -------------------------------------------------------------------------
# New source type helpers
# -------------------------------------------------------------------------

def _detect_grid(user_config=None):
    """Detect grid square from user config.

    Returns:
        Grid string or empty string.
    """
    if user_config is None:
        user_config = load_user_config()
    return user_config.get("grid", "")


def _get_radio_model_safe():
    """Get active radio model, cleaned for use as identifier.

    Removes parenthetical notes and replaces spaces with underscores.
    Example: "IC-705 (DigiRig)" -> "IC-705"
    """
    radio_conf = os.path.join(ET_HOME, "conf/radios.d/active-radio.json")
    try:
        with open(radio_conf) as f:
            data = json.load(f)
        model = data.get("model", "")
        if model:
            return re.sub(r'\s*\([^)]*\)', '', model).replace(' ', '_')
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return ""


def _detect_portaudio(field):
    """Detect PortAudio device for ET_AUDIO using et-portaudio.

    Args:
        field: "device" for device name, "index" for device index.

    Returns:
        String value or None.
    """
    try:
        result = subprocess.run(
            ["et-portaudio"],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode != 0:
            log.error("et-portaudio failed")
            return None
        data = json.loads(result.stdout)
        return str(data.get(field, ""))
    except (FileNotFoundError, subprocess.TimeoutExpired,
            json.JSONDecodeError) as e:
        log.error("PortAudio detection failed: %s", e)
        return None


# -------------------------------------------------------------------------
# Resolve update value (for ini-update and xml-update entries)
# -------------------------------------------------------------------------

def _resolve_update_value(update_spec, user_config=None):
    """Resolve the value for an ini-update or xml-update entry.

    Supports two forms:
    - {"value": "literal"} — use the literal value directly
    - {"source": "...", "field": "...", ...} — resolve via resolve_var()
    """
    if "value" in update_spec:
        return update_spec["value"]

    # Build a var_spec compatible with resolve_var
    var_spec = {}
    source = update_spec.get("source", "")
    var_spec["source"] = source

    if source in ("user.json", "active-radio"):
        var_spec["key"] = update_spec.get("field", "")
    elif source == "detect-audio":
        var_spec["format"] = update_spec.get("format",
                                             "plughw:{card},{device}")
    elif source == "detect-portaudio":
        var_spec["field"] = update_spec.get("field", "device")
    elif source == "detect-savedir":
        var_spec["field"] = update_spec.get("field", "")

    return resolve_var(var_spec, user_config)


# -------------------------------------------------------------------------
# ini-update: Modify INI-style config files
# -------------------------------------------------------------------------

def ini_update(config_entry, user_config=None):
    """Process an ini-update config entry.

    Updates key=value pairs in INI files. Supports:
    - mode "replace" (default): replace entire value
    - mode "replace-first-word": replace only the first word, keep rest

    Args:
        config_entry: dict with "target" and "updates" list.
        user_config: pre-loaded user config (optional).

    Returns:
        True if update succeeded.
    """
    if user_config is None:
        user_config = load_user_config()

    target = os.path.expanduser(config_entry["target"])
    updates = config_entry.get("updates", [])

    if not os.path.isfile(target):
        log.warning("INI file not found: %s (will be created on first run)",
                    target)
        return True  # Not fatal

    with open(target) as f:
        lines = f.readlines()

    for update in updates:
        key = update["key"]
        value = _resolve_update_value(update, user_config)
        if value is None:
            log.warning("Could not resolve value for INI key '%s'", key)
            continue

        mode = update.get("mode", "replace")
        section = update.get("section")

        if mode == "replace-first-word":
            _ini_replace_first_word(lines, key, value)
        else:
            _ini_replace_or_add(lines, key, value, section)

    with open(target, "w") as f:
        f.writelines(lines)

    log.info("INI update applied: %s (%d keys)", target, len(updates))
    return True


def _ini_replace_or_add(lines, key, value, section=None):
    """Replace key=value in INI lines, or add it if missing.

    Case-sensitive key matching (standard for most INI files).
    If key not found and section specified, add after section header.
    """
    for i, line in enumerate(lines):
        stripped = line.rstrip('\r\n')
        if stripped.startswith(key + "="):
            lines[i] = f"{key}={value}\n"
            return

    # Key not found — add it
    if section:
        section_header = f"[{section}]"
        for i, line in enumerate(lines):
            if line.strip() == section_header:
                lines.insert(i + 1, f"{key}={value}\n")
                return

    # Fallback: append
    lines.append(f"{key}={value}\n")


def _ini_replace_first_word(lines, key, value):
    """Replace only the first word of a key's value, keep the rest.

    Example: MyInfo=FT-857D liaisonos.com -> MyInfo=IC-705 liaisonos.com
    """
    for i, line in enumerate(lines):
        stripped = line.rstrip('\r\n')
        if stripped.startswith(key + "="):
            eq_pos = stripped.index("=")
            current_val = stripped[eq_pos + 1:]
            parts = current_val.split(None, 1)
            if len(parts) > 1:
                lines[i] = f"{key}={value} {parts[1]}\n"
            else:
                lines[i] = f"{key}={value}\n"
            return

    # Key not found — add it
    lines.append(f"{key}={value}\n")


# -------------------------------------------------------------------------
# xml-update: Modify XML config files
# -------------------------------------------------------------------------

def xml_update(config_entry, user_config=None):
    """Process an xml-update config entry.

    Updates <TAG>value</TAG> patterns in XML files. Uses simple regex
    line-by-line replacement (matching fldigi_def.xml format where each
    setting is on its own line).

    Args:
        config_entry: dict with "target" and "updates" list.
        user_config: pre-loaded user config (optional).

    Returns:
        True if update succeeded.
    """
    if user_config is None:
        user_config = load_user_config()

    target = os.path.expanduser(config_entry["target"])
    updates = config_entry.get("updates", [])

    if not os.path.isfile(target):
        log.warning("XML file not found: %s", target)
        return True  # Not fatal

    with open(target) as f:
        content = f.read()

    for update in updates:
        tag = update["tag"]
        value = _resolve_update_value(update, user_config)
        if value is None:
            log.warning("Could not resolve value for XML tag '%s'", tag)
            continue

        pattern = rf"<{re.escape(tag)}>.*?</{re.escape(tag)}>"
        replacement = f"<{tag}>{value}</{tag}>"
        content = re.sub(pattern, replacement, content)

    with open(target, "w") as f:
        f.write(content)

    log.info("XML update applied: %s (%d tags)", target, len(updates))
    return True


# -------------------------------------------------------------------------
# station-position: YAAC prefs.xml with grid-to-latlon
# -------------------------------------------------------------------------

def station_position(config_entry, user_config=None):
    """Update YAAC prefs.xml with station lat/lon from grid square.

    Uses grid_utils.grid_to_latlon() for conversion. Creates the file
    if it doesn't exist; updates existing entries if it does.

    Args:
        config_entry: dict with "target" path.
        user_config: pre-loaded user config (optional).

    Returns:
        True if update succeeded.
    """
    from . import grid_utils

    if user_config is None:
        user_config = load_user_config()

    target = os.path.expanduser(config_entry["target"])
    grid = user_config.get("grid", "")
    callsign = user_config.get("callsign", "N0CALL")

    # Prefer precise lat/lon from user.json (written by QtDashboard's
    # updateGpsPosition after a GPS sync). A 4-char grid like "FN35"
    # resolves to the center of a ~110x220km square — easily 50km off
    # from the operator's actual position. Fall back to grid-to-latlon
    # only if lat/lon aren't stored.
    lat_pref = user_config.get("lat")
    lon_pref = user_config.get("lon")
    if isinstance(lat_pref, (int, float)) and isinstance(lon_pref, (int, float)):
        lat = round(float(lat_pref), 6)
        lon = round(float(lon_pref), 6)
        log.info("Station position from user.json lat/lon: lat=%s, lon=%s", lat, lon)
    else:
        if not grid:
            log.warning("No grid square or lat/lon set, skipping station position")
            return True
        coords = grid_utils.grid_to_latlon(grid)
        if coords is None:
            log.error("Failed to convert grid square: %s", grid)
            return True  # Non-fatal
        lat, lon = coords
        log.info("Station position from grid %s (no lat/lon available): lat=%s, lon=%s",
                 grid, lat, lon)

    os.makedirs(os.path.dirname(target), exist_ok=True)

    if os.path.isfile(target):
        # Update existing prefs.xml
        with open(target) as f:
            content = f.read()

        # Remove old entries
        for entry_key in ("Latitude", "Longitude", "StationCallsign"):
            content = re.sub(
                rf'\s*<entry key="{entry_key}" value="[^"]*"/>\n?',
                '', content)

        # Add new entries before </map>
        new_entries = (
            f'  <entry key="Latitude" value="{lat}"/>\n'
            f'  <entry key="Longitude" value="{lon}"/>\n'
            f'  <entry key="StationCallsign" value="{callsign}"/>\n'
        )
        content = content.replace("</map>", new_entries + "</map>")

        with open(target, "w") as f:
            f.write(content)
    else:
        # Create new prefs.xml
        with open(target, "w") as f:
            f.write('<?xml version="1.0" encoding="UTF-8" '
                    'standalone="no"?>\n')
            f.write('<!DOCTYPE map SYSTEM '
                    '"http://java.sun.com/dtd/preferences.dtd">\n')
            f.write('<map MAP_XML_VERSION="1.0">\n')
            f.write(f'  <entry key="Latitude" value="{lat}"/>\n')
            f.write(f'  <entry key="Longitude" value="{lon}"/>\n')
            f.write(f'  <entry key="StationCallsign" '
                    f'value="{callsign}"/>\n')
            f.write('</map>\n')

    log.info("Station position updated: %s", target)
    return True
