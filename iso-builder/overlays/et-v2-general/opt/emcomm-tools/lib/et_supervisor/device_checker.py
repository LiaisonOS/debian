"""
DeviceChecker: Verify hardware preconditions before starting a mode.

Checks:
- device-exists: udev symlink exists (e.g., /dev/et-audio, /dev/et-cat)
- audio-tagged: arecord -l shows ET_AUDIO tag
- service-active: systemctl check (e.g., rigctld)
- callsign-set: user.json callsign is not N0CALL
"""

import json
import logging
import os
import subprocess

log = logging.getLogger("et-supervisor.device")

USER_CONFIG_PATH = os.path.expanduser("~/.config/liaisonos/user.json")
ACTIVE_RADIO_PATH = "/opt/emcomm-tools/conf/radios.d/active-radio.json"


def active_radio_has_cat():
    """Return True if the currently selected radio exposes CAT.
    Defaults to True if the radio JSON has no explicit "cat" flag, or
    if active-radio.json is missing/unreadable.
    """
    try:
        with open(ACTIVE_RADIO_PATH) as f:
            radio = json.load(f)
        return bool(radio.get("cat", True))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return True


def _is_cat_check(check):
    """A precheck is CAT-related if it targets /dev/et-cat or the rigctld service."""
    ctype = check.get("type", "")
    if ctype == "device-exists" and check.get("path") == "/dev/et-cat":
        return True
    if ctype == "service-active" and check.get("name") == "rigctld":
        return True
    return False


def run_precheck(check):
    """Run a single precheck from mode config.

    Args:
        check: dict with "type" and type-specific fields.

    Returns:
        (success: bool, message: str)
    """
    check_type = check.get("type", "")

    if check_type == "device-exists":
        return _check_device_exists(check.get("path", ""))

    if check_type == "audio-tagged":
        return _check_audio_tagged(check.get("tag", "ET_AUDIO"))

    if check_type == "service-active":
        return _check_service_active(check.get("name", ""))

    if check_type == "callsign-set":
        return _check_callsign_set()

    if check_type == "file-exists":
        return _check_file_exists(check.get("path", ""))

    return False, f"Unknown precheck type: {check_type}"


def run_prechecks(checks):
    """Run all prechecks for a mode.

    Returns:
        (all_passed: bool, failures: list of (type, message) tuples)
    """
    failures = []
    skip_cat = not active_radio_has_cat()
    for check in checks:
        if skip_cat and _is_cat_check(check):
            log.info("Skipping CAT precheck %s (active radio has cat:false)",
                     check.get("type"))
            continue
        ok, msg = run_precheck(check)
        if not ok:
            failures.append((check.get("type", "unknown"), msg))
            log.warning("Precheck failed: %s — %s", check.get("type"), msg)
    return len(failures) == 0, failures


def _short_device_name(path):
    base = os.path.basename(path) if path else ""
    if base.startswith("et-"):
        base = base[3:]
    return base.upper() if base else "device"


def _check_device_exists(path):
    if not path:
        return False, "Device path missing"
    if os.path.exists(path):
        return True, f"{path} found"
    return False, f"{_short_device_name(path)} device not connected"


def _check_audio_tagged(tag):
    try:
        result = subprocess.run(
            ["arecord", "-l"],
            capture_output=True, text=True, timeout=5
        )
        if tag in result.stdout:
            return True, f"Audio device with {tag} tag found"
        return False, "Audio device not connected"
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False, "Audio check failed"


def _check_service_active(name):
    if not name:
        return False, "Service name missing"
    try:
        result = subprocess.run(
            ["systemctl", "is-active", "--quiet", name],
            timeout=5
        )
        if result.returncode == 0:
            return True, f"{name} is active"
        return False, f"{name} not running"
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False, f"{name} check failed"


def _check_callsign_set():
    try:
        with open(USER_CONFIG_PATH) as f:
            config = json.load(f)
        callsign = config.get("callsign", "N0CALL")
        if callsign and callsign != "N0CALL":
            return True, f"Callsign: {callsign}"
        return False, "Callsign not set"
    except (FileNotFoundError, json.JSONDecodeError):
        return False, "User config missing"


def _check_file_exists(path):
    if not path:
        return False, "File path missing"
    expanded = os.path.expanduser(path)
    if os.path.isfile(expanded):
        return True, f"{path} found"
    return False, f"Missing: {os.path.basename(path)}"
