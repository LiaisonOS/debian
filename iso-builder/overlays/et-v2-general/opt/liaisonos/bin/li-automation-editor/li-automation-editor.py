#!/usr/bin/env python3
#
# Author  : Sylvain Deguire (VA2OPS)
# Date    : June 2026
# Purpose : Flask-based web UI for editing ~/.config/liaisonos/automation.json.
#           Loads the existing schedule, presents Presence + Mission editors,
#           validates via `li-automation validate` before save, and optionally
#           restarts the daemon. Same pattern as et-radio-config — pywebview
#           native window if available, fallback to default browser.
#
# Usage   : /opt/liaisonos/bin/li-automation-editor/li-automation-editor.py
#

import warnings
warnings.filterwarnings("ignore", category=DeprecationWarning)
warnings.filterwarnings("ignore", message=".*pkg_resources.*")

import os
import sys
import json
import subprocess
import threading
import time
import webbrowser
import logging
from datetime import datetime
from pathlib import Path
from flask import Flask, render_template, request, jsonify

# Quiet werkzeug
logging.getLogger('werkzeug').setLevel(logging.ERROR)

app = Flask(__name__)
app.secret_key = 'liaisonos-automation-editor-2026'

# ── Paths ─────────────────────────────────────────────────────────────────────
SCHEDULE_PATH = Path.home() / ".config" / "liaisonos" / "automation.json"
PAUSE_FLAG    = Path.home() / ".cache" / "liaisonos" / "automation-paused"
USER_CONFIG   = Path.home() / ".config" / "emcomm-tools" / "user.json"
BACKUP_DIR    = Path.home() / ".cache" / "liaisonos" / "automation-backups"
LI_AUTOMATION = "/opt/liaisonos/bin/li-automation"
# Where QtTermTCP's BulletinStore lives — single source of truth for the
# bulletin picker UI in the bbs_publish mission card.
BULLETIN_PENDING = Path.home() / ".config" / "liaisonos" / "bulletins" / "pending"
import shutil
PAT_BIN       = shutil.which("pat") or "/usr/bin/pat"

# ── Mode catalog — drives the editor dropdowns ────────────────────────────────
# Hard-coded for editor UX. Adding a new mode here means adding it in the
# editor without touching the JSON validator (which is type-loose).
PRESENCE_MODES = [
    {"id": "js8call",    "label": "JS8Call",            "modems": []},
    {"id": "varac",      "label": "VarAC",              "modems": []},
    # Standalone modem-only presences — no BBS behind them. Useful when the
    # operator wants the modem parked and listening (P2P chat, third-party
    # AGWPE apps, manual TX from QtTermTCP, etc.) without launching the
    # full BBS-server stack. Same mode-id names as the modem sub-options
    # under bbs-server, but exposed here as top-level standalone presences.
    {"id": "vara-hf",    "label": "VARA HF (standalone)", "modems": []},
    {"id": "vara-fm",    "label": "VARA FM (standalone)", "modems": []},
    {"id": "mercury",    "label": "Mercury (standalone)", "modems": []},
    {"id": "direwolf-tnc",     "label": "Direwolf KISS TNC",  "modems": []},
    {"id": "packet-digipeater","label": "Packet Digipeater",  "modems": []},
    {"id": "bbs-server", "label": "BBS Server (LinBPQ)",
     "modems": [
        {"value": "vara-hf", "label": "VARA HF"},
        {"value": "vara-fm", "label": "VARA FM"},
        {"value": "mercury", "label": "Mercury"},
        {"value": "300",     "label": "HF 300"},
        {"value": "1200",    "label": "Pkt 1200"},
        {"value": "9600",    "label": "Pkt 9600"},
     ]},
]
MISSION_MODES = [
    {"id": "winlink-vara-hf", "label": "Winlink — VARA HF", "scheme": "varahf"},
    {"id": "winlink-vara-fm", "label": "Winlink — VARA FM", "scheme": "varafm"},
    {"id": "winlink-packet",  "label": "Winlink — Packet",  "scheme": "ax25"},
    {"id": "winlink-ardop",   "label": "Winlink — ARDOP",   "scheme": "ardop"},
    {"id": "winlink-mercury", "label": "Winlink — Mercury", "scheme": "mercury"},
]
# Mission type catalog. The default for missions without an explicit "type"
# field is "winlink" — backward-compat with 2.3.x schedules. Phase 4 adds
# bbs_publish for scheduled BBS bulletin posts.
MISSION_TYPES = [
    {"id": "winlink",     "label": "Winlink"},
    {"id": "bbs_publish", "label": "BBS Publish"},
]
# Transport options for bbs_publish missions. telnet is v1; vara_hf is
# planned but not yet exercised end-to-end through the daemon.
BBS_PUBLISH_MODES = [
    {"id": "telnet",  "label": "Telnet (LAN to LinBPQ)"},
    {"id": "vara_hf", "label": "VARA HF (RF, requires rig)"},
]
WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


# ── Helpers ───────────────────────────────────────────────────────────────────
def get_language() -> str:
    """Read language preference from user.json (en/fr). Defaults to en."""
    if USER_CONFIG.exists():
        try:
            with open(USER_CONFIG) as f:
                return json.load(f).get("language", "en") or "en"
        except (OSError, json.JSONDecodeError):
            pass
    return "en"


def load_schedule() -> dict:
    """Load the automation schedule. Returns a sensible default skeleton if
    the file is absent — so a fresh operator sees the editor populated with
    something to start from, not an empty page."""
    if SCHEDULE_PATH.exists():
        try:
            with open(SCHEDULE_PATH) as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            pass
    return {
        "timezone": "UTC",
        "station": {
            "role": "Autonomous emcomm station",
            "radio": "yaesu-ft897d",
            "callsign_must_be_set": True,
        },
        "presence": [],
        "missions": [],
        "boot": {"start_current_presence": True, "delay_seconds": 30},
        "override_flag": "~/.cache/liaisonos/automation-paused",
    }


def backup_schedule() -> Path | None:
    """Copy the existing schedule to BACKUP_DIR with a timestamp. Returns
    the backup path on success. Skips silently if nothing to back up."""
    if not SCHEDULE_PATH.exists():
        return None
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%dT%H%M%S")
    dst = BACKUP_DIR / f"automation.{ts}.json.bak"
    try:
        dst.write_bytes(SCHEDULE_PATH.read_bytes())
        return dst
    except OSError:
        return None


def save_schedule(doc: dict) -> tuple[bool, str]:
    """Write the schedule. Backs up the prior version first."""
    backup_schedule()
    try:
        SCHEDULE_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(SCHEDULE_PATH, "w") as f:
            json.dump(doc, f, indent=2)
        return True, f"Saved to {SCHEDULE_PATH}"
    except OSError as e:
        return False, f"Save failed: {e}"


def validate_schedule_file(path: Path) -> tuple[bool, str]:
    """Invoke `li-automation validate <path>` and return (ok, output)."""
    if not Path(LI_AUTOMATION).exists():
        return False, f"{LI_AUTOMATION} not installed"
    try:
        r = subprocess.run(
            [LI_AUTOMATION, "validate", str(path)],
            capture_output=True, text=True, timeout=15)
        return (r.returncode == 0, r.stdout + r.stderr)
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        return False, f"Validator error: {e}"


def daemon_status() -> dict:
    """Quick status of the systemd user service for the daemon."""
    out = {"active": False, "enabled": False, "paused": PAUSE_FLAG.exists()}
    try:
        r = subprocess.run(
            ["systemctl", "--user", "is-active", "liaisonos-automation"],
            capture_output=True, text=True, timeout=3)
        out["active"] = (r.stdout.strip() == "active")
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    try:
        r = subprocess.run(
            ["systemctl", "--user", "is-enabled", "liaisonos-automation"],
            capture_output=True, text=True, timeout=3)
        out["enabled"] = (r.stdout.strip() == "enabled")
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return out


def restart_daemon() -> tuple[bool, str]:
    try:
        r = subprocess.run(
            ["systemctl", "--user", "restart", "liaisonos-automation"],
            capture_output=True, text=True, timeout=10)
        return (r.returncode == 0,
                r.stdout + r.stderr or "Daemon restarted.")
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        return False, f"Restart failed: {e}"


import re as _re   # local alias so we don't shadow module-level import

# Connect URL pattern in pat rmslist output: <scheme>:///<callsign>?freq=...&bw=...
# pat prints one URL per RMS line. We parse this out instead of assuming any
# column layout — column widths vary between pat versions but the URL format
# is part of Winlink's spec and stable.
_PAT_URL_RE = _re.compile(
    r'\b(?P<scheme>varahf|varafm|ardop|ax25|packet|mercury):///'
    r'(?P<callsign>[A-Z0-9\-]+)'
    r'(?:\?(?P<query>[^\s]*))?',
    _re.IGNORECASE)


def _parse_pat_query(q: str) -> dict:
    """Pull freq= and bw= out of a connect-URL query string."""
    out = {}
    if not q:
        return out
    for pair in q.split("&"):
        if "=" in pair:
            k, v = pair.split("=", 1)
            out[k.lower()] = v
    return out


def _band_from_freq_khz(freq_khz):
    """Map a frequency (kHz) to the canonical amateur band label, or '' if
    out of any band we care about for Winlink. Used because pat's rmslist
    text output doesn't print the band as a column."""
    if not freq_khz:
        return ""
    f = float(freq_khz)
    # ITU R1/R2 amateur HF + VHF/UHF; widened slightly for sub-band edges.
    for lo, hi, label in (
        ( 1800,  2000, "160m"),
        ( 3500,  4000, "80m"),
        ( 5330,  5410, "60m"),
        ( 7000,  7300, "40m"),
        (10100, 10150, "30m"),
        (14000, 14350, "20m"),
        (18068, 18168, "17m"),
        (21000, 21450, "15m"),
        (24890, 24990, "12m"),
        (28000, 29700, "10m"),
        (50000, 54000, "6m"),
        (144000, 148000, "2m"),
        (222000, 225000, "1.25m"),
        (420000, 450000, "70cm"),
    ):
        if lo <= f <= hi:
            return label
    return ""


def fetch_rms_list(modem_scheme: str = "", band: str = "") -> dict:
    """Call `pat rmslist` (no --json — that flag doesn't exist in upstream pat)
    and parse the standard text output. Each line printed by pat includes the
    Winlink connect URL; we regex those out, decode freq/bw, and surface them
    to the picker UI.

    Returns: { ok, rms: [{callsign, scheme, freq_khz, bw, distance, raw}],
              error, command }

    Returning errors instead of silent [] so the picker UI can SHOW WHY it's
    empty — most common causes (Pat not configured, no internet, wrong
    --mode value) all need operator action.

    Pat's CLI does NOT need pat-http running for rmslist — it queries
    Winlink CMS directly over HTTPS."""
    if not PAT_BIN or not Path(PAT_BIN).exists():
        return {"ok": False, "rms": [],
                "error": "pat binary not found on PATH",
                "command": ""}
    # pat uses -m / --mode (NOT --json which doesn't exist).
    args = [PAT_BIN, "rmslist"]
    if modem_scheme:
        args += ["-m", modem_scheme]
    if band:
        args += ["-b", band]
    cmd_str = " ".join(args)
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=30)
    except FileNotFoundError:
        return {"ok": False, "rms": [],
                "error": f"pat not executable: {PAT_BIN}",
                "command": cmd_str}
    except subprocess.TimeoutExpired:
        return {"ok": False, "rms": [],
                "error": "pat rmslist timed out (30s) — check internet "
                         "connectivity to Winlink CMS",
                "command": cmd_str}
    if r.returncode != 0:
        return {"ok": False, "rms": [],
                "error": (f"pat exit {r.returncode}.\n"
                          f"stderr: {r.stderr.strip()[:500] or '(empty)'}\n"
                          f"stdout: {r.stdout.strip()[:200] or '(empty)'}"),
                "command": cmd_str}
    # Parse each line: extract the connect URL, decode the rest of the line
    # as best-effort distance text. Skip header lines.
    rms_list = []
    for line in r.stdout.splitlines():
        m = _PAT_URL_RE.search(line)
        if not m:
            continue
        q = _parse_pat_query(m.group("query") or "")
        freq_khz = None
        try:
            if "freq" in q:
                # pat sometimes reports kHz, sometimes Hz — heuristically
                # decide: a value > 100000 is Hz, else kHz.
                v = float(q["freq"])
                freq_khz = v / 1000.0 if v > 100000 else v
        except ValueError:
            pass
        bw = q.get("bw", "")
        # Distance: pat output varies between versions.
        #   - Some emit "1234km" with a unit suffix.
        #   - Some emit a bare integer column (unit only in the header).
        # Try suffix-form first; fall back to scanning the tokens BEFORE the
        # URL for the largest plausible integer that isn't the freq or bw
        # value (and prefer values > 360 to avoid grabbing the azimuth field).
        distance_str = ""
        dm = _re.search(r'(\d+(?:\.\d+)?)\s*(km|mi|miles|kilometers)\b',
                        line, _re.IGNORECASE)
        if dm:
            distance_str = f"{dm.group(1)} {dm.group(2).lower()[:2]}"
        else:
            url_pos = line.find(m.group(0))
            pre_url = line[:url_pos] if url_pos > 0 else line
            freq_str = str(q.get("freq", "")).split(".")[0]
            bw_str   = str(q.get("bw", "")).split(".")[0]
            plausible = []
            for tok in pre_url.split():
                clean = _re.sub(r'(?:km|mi|miles|°)$', '', tok,
                                flags=_re.IGNORECASE)
                if _re.fullmatch(r'\d+', clean):
                    n = int(clean)
                    if 0 < n <= 30000 and clean != freq_str and clean != bw_str:
                        plausible.append(n)
            if plausible:
                far = [n for n in plausible if n > 360]
                chosen = (far[-1] if far else plausible[-1])
                distance_str = f"{chosen} km"
        # Band: pat's text rmslist doesn't print a "band" column — derive it
        # from the freq, then fall back to the band filter the user picked.
        band_str = _band_from_freq_khz(freq_khz) or (band or "")
        rms_list.append({
            "callsign": m.group("callsign").upper(),
            "scheme":   m.group("scheme").lower(),
            "mode":     m.group("scheme").lower(),
            "band":     band_str,
            "Band":     band_str,
            "freq":     int(freq_khz * 1000) if freq_khz else None,
            "freq_khz": freq_khz,
            "bw":       bw,
            "Bandwidth": bw,
            "distance": distance_str,
            "Distance": distance_str,
            "raw":      line.strip(),
            "url":      m.group(0),
        })
    return {"ok": True, "rms": rms_list, "error": "", "command": cmd_str,
            "raw_lines": len(r.stdout.splitlines()),
            "raw_first": r.stdout.splitlines()[:3]}


# ── Routes ────────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return render_template(
        "index.html",
        language=get_language(),
        schedule=load_schedule(),
        status=daemon_status(),
        schedule_path=str(SCHEDULE_PATH),
    )


@app.route("/editor")
def editor():
    return render_template(
        "editor.html",
        language=get_language(),
        schedule=load_schedule(),
        presence_modes=PRESENCE_MODES,
        mission_modes=MISSION_MODES,
        mission_types=MISSION_TYPES,
        bbs_publish_modes=BBS_PUBLISH_MODES,
        weekdays=WEEKDAYS,
        status=daemon_status(),
        schedule_path=str(SCHEDULE_PATH),
    )


def list_pending_bulletins() -> list:
    """Scan ~/.config/liaisonos/bulletins/pending/ and return one summary
    dict per bulletin for the mission card's picker dropdown. Includes
    MD5-derived freshness state so the card can render the right pill
    without a second round-trip.

    Newest first (by file mtime) so the operator's most recently composed
    bulletin is at the top of the list."""
    out = []
    if not BULLETIN_PENDING.exists():
        return out
    for jsn in sorted(BULLETIN_PENDING.glob("*.json"),
                      key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            with jsn.open(encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        bid = data.get("id", jsn.stem)
        txt = BULLETIN_PENDING / f"{bid}.txt"
        current_md5 = _md5_file(txt) if txt.is_file() else ""
        last_md5 = data.get("last_sent_md5", "")
        if not txt.is_file():
            state = "broken"
        elif last_md5 and last_md5 == current_md5:
            state = "already_sent"
        else:
            state = "ready"
        out.append({
            "id":            bid,
            "title":         data.get("title", ""),
            "to":            data.get("to", ""),
            "at_bbs":        data.get("at_bbs", ""),
            "type":          data.get("type", "B"),
            "status":        data.get("status", "pending"),
            "created":       data.get("created", ""),
            "current_md5":   current_md5,
            "last_sent_md5": last_md5,
            "last_sent_at":  data.get("last_sent_at", ""),
            "last_sent_bid": data.get("last_sent_bid", ""),
            "send_count":    data.get("send_count", 0),
            "state":         state,
        })
    return out


@app.route("/api/bulletins/pending")
def api_bulletins_pending():
    return jsonify({"ok": True, "bulletins": list_pending_bulletins()})


# State files written by the daemon (li-automation). Editor only reads them
# here for rendering; the Re-arm endpoint below is the one place the editor
# writes back, and it touches a single key in disabled-state.
DISABLED_STATE_PATH = Path.home() / ".cache" / "liaisonos" / "automation-disabled.json"
RETRY_STATE_PATH    = Path.home() / ".cache" / "liaisonos" / "automation-retry-state.json"


def _md5_file(path: Path) -> str:
    """Hex MD5 of a file, or '' if it doesn't exist."""
    import hashlib as _hashlib
    try:
        h = _hashlib.md5()
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return ""


def _safe_json_load(path: Path) -> dict:
    try:
        with path.open() as f:
            d = json.load(f)
            return d if isinstance(d, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


@app.route("/api/bulletins/info")
def api_bulletins_info():
    """Per-bulletin freshness + last-send state. Renders the mission card's
    status pill: green (new content ready), amber (already sent unchanged),
    red (missing files / broken ref)."""
    bid = request.args.get("id", "").strip()
    if not bid:
        return jsonify({"ok": False, "error": "id required"}), 400
    txt = BULLETIN_PENDING / f"{bid}.txt"
    jsn = BULLETIN_PENDING / f"{bid}.json"
    info = {
        "id":            bid,
        "txt_exists":    txt.is_file(),
        "json_exists":   jsn.is_file(),
        "current_md5":   _md5_file(txt),
        "title":         "",
        "last_sent_md5": "",
        "last_sent_at":  "",
        "last_sent_bid": "",
        "send_count":    0,
    }
    if jsn.is_file():
        meta = _safe_json_load(jsn)
        info["title"]         = meta.get("title", "")
        info["last_sent_md5"] = meta.get("last_sent_md5", "")
        info["last_sent_at"]  = meta.get("last_sent_at", "")
        info["last_sent_bid"] = meta.get("last_sent_bid", "")
        info["send_count"]    = meta.get("send_count", 0)
    # Derived state for the UI — easier than recomputing in JS.
    if not info["txt_exists"] or not info["json_exists"]:
        info["state"] = "broken"
    elif info["last_sent_md5"] and info["last_sent_md5"] == info["current_md5"]:
        info["state"] = "already_sent"
    else:
        info["state"] = "ready"
    return jsonify({"ok": True, "info": info})


@app.route("/api/missions/status")
def api_mission_status():
    """Surface daemon-side mission state (disabled + retry) for the editor's
    mission-card top banner. Returns the lot in one call so the editor can
    decorate every card from a single fetch."""
    return jsonify({
        "ok":       True,
        "disabled": _safe_json_load(DISABLED_STATE_PATH),
        "retry":    _safe_json_load(RETRY_STATE_PATH),
    })


@app.route("/api/missions/rearm", methods=["POST"])
def api_mission_rearm():
    """Operator hit the Re-arm button on a disabled bbs_publish mission.
    Clears the entry from automation-disabled.json so the daemon resumes
    firing it on its scheduled slots."""
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"ok": False, "error": "name required"}), 400
    state = _safe_json_load(DISABLED_STATE_PATH)
    if state.pop(name, None) is None:
        return jsonify({"ok": True, "rearmed": False,
                        "message": "mission was not disabled"})
    DISABLED_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = DISABLED_STATE_PATH.with_suffix(".json.tmp")
    with tmp.open("w") as f:
        json.dump(state, f, indent=2)
    tmp.replace(DISABLED_STATE_PATH)
    return jsonify({"ok": True, "rearmed": True, "name": name})


@app.route("/api/bulletins/clear-last-sent", methods=["POST"])
def api_bulletin_clear_last_sent():
    """Wipe the MD5 / last_sent_* fields from a bulletin so the next mission
    fire treats the content as fresh. Useful when the operator wants to
    re-send the same body without editing the TXT."""
    data = request.get_json(silent=True) or {}
    bid = (data.get("id") or "").strip()
    if not bid:
        return jsonify({"ok": False, "error": "id required"}), 400
    jsn = BULLETIN_PENDING / f"{bid}.json"
    if not jsn.is_file():
        return jsonify({"ok": False, "error": "bulletin not in pending/"}), 404
    meta = _safe_json_load(jsn)
    for k in ("last_sent_md5", "last_sent_at", "last_sent_bid", "send_count"):
        meta.pop(k, None)
    tmp = jsn.with_suffix(".json.tmp")
    with tmp.open("w") as f:
        json.dump(meta, f, indent=2)
    tmp.replace(jsn)
    return jsonify({"ok": True, "cleared": True, "id": bid})


@app.route("/api/save", methods=["POST"])
def api_save():
    """Receive a full schedule, validate, save, optional restart."""
    body = request.get_json(silent=True) or {}
    doc = body.get("schedule")
    do_restart = bool(body.get("restart"))
    if not isinstance(doc, dict):
        return jsonify({"ok": False, "error": "schedule must be an object"}), 400

    # Write to a temp file for validation first, so we never overwrite a
    # known-good schedule with broken JSON.
    tmp = SCHEDULE_PATH.with_suffix(".tmp")
    try:
        tmp.parent.mkdir(parents=True, exist_ok=True)
        with open(tmp, "w") as f:
            json.dump(doc, f, indent=2)
    except OSError as e:
        return jsonify({"ok": False, "error": f"tmp write failed: {e}"}), 500

    ok, output = validate_schedule_file(tmp)
    if not ok:
        try: tmp.unlink()
        except OSError: pass
        return jsonify({"ok": False, "error": output,
                        "stage": "validation"}), 422

    saved, msg = save_schedule(doc)
    try: tmp.unlink()
    except OSError: pass
    if not saved:
        return jsonify({"ok": False, "error": msg}), 500

    restart_msg = ""
    if do_restart:
        rok, restart_msg = restart_daemon()

    return jsonify({"ok": True, "message": msg,
                    "validator_output": output,
                    "restarted": do_restart,
                    "restart_output": restart_msg})


@app.route("/api/validate", methods=["POST"])
def api_validate():
    """Validate a posted schedule without saving."""
    body = request.get_json(silent=True) or {}
    doc = body.get("schedule")
    if not isinstance(doc, dict):
        return jsonify({"ok": False, "error": "schedule must be an object"}), 400
    tmp = SCHEDULE_PATH.with_suffix(".tmp")
    try:
        tmp.parent.mkdir(parents=True, exist_ok=True)
        with open(tmp, "w") as f:
            json.dump(doc, f, indent=2)
        ok, output = validate_schedule_file(tmp)
    finally:
        try: tmp.unlink()
        except OSError: pass
    return jsonify({"ok": ok, "output": output})


@app.route("/api/pause", methods=["POST"])
def api_pause():
    """Toggle the pause flag (same as QtDashboard AUTOMATION tile)."""
    body = request.get_json(silent=True) or {}
    want_paused = bool(body.get("paused"))
    PAUSE_FLAG.parent.mkdir(parents=True, exist_ok=True)
    if want_paused:
        try:
            PAUSE_FLAG.write_text(
                f"paused-by-editor {datetime.now():%Y-%m-%d %H:%M:%S}\n")
        except OSError as e:
            return jsonify({"ok": False, "error": str(e)}), 500
    else:
        try:
            PAUSE_FLAG.unlink(missing_ok=True)
        except OSError as e:
            return jsonify({"ok": False, "error": str(e)}), 500
    return jsonify({"ok": True, "paused": PAUSE_FLAG.exists()})


@app.route("/api/rms-list")
def api_rms_list():
    """Proxy `pat rmslist --json` for the mission RMS picker. Step 5 fully
    wires this into the editor UI. Right now it's just here so the endpoint
    is testable independently."""
    modem = request.args.get("modem", "")
    band  = request.args.get("band", "")
    return jsonify(fetch_rms_list(modem, band))


@app.route("/api/status")
def api_status():
    return jsonify(daemon_status())


@app.route("/api/restart-daemon", methods=["POST"])
def api_restart_daemon():
    ok, msg = restart_daemon()
    return jsonify({"ok": ok, "message": msg})


# ── Bootstrap ─────────────────────────────────────────────────────────────────
# Pattern follows et-radio-config / et-menu-editor exactly so behaviour matches
# the other LiaisonOS Flask apps. Module-level main block, GTK screen-sizing
# via gi (so the window fits the operator's panel), Flask stdout/stderr
# redirected to /dev/null so the dev-server banner doesn't pollute terminals.
PORT = 5055  # Browsers block 5060 (SIP), 5061 (SIPS); 5055 is unrestricted


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
    if '--no-browser' in sys.argv:
        app.run(host='127.0.0.1', port=PORT, debug=False)
    elif '--browser' in sys.argv:
        threading.Thread(target=open_browser, args=(PORT,), daemon=True).start()
        app.run(host='127.0.0.1', port=PORT, debug=False)
    elif '--help' in sys.argv:
        print("Usage: li-automation-editor [OPTIONS]")
        print("")
        print("Options:")
        print("  --no-browser    Start server only (no window)")
        print("  --browser       Open in default web browser")
        print("  --help          Show this help message")
        print("")
        print("Default: Opens in native PyWebView window")
        sys.exit(0)
    else:
        # Default: PyWebView native window — same pattern as et-menu-editor.
        try:
            import webview

            try:
                import gi
                gi.require_version("Gdk", "3.0")
                from gi.repository import Gdk

                screen = Gdk.Screen.get_default()
                screen_width  = screen.get_width()
                screen_height = screen.get_height()

                panel_height = 60
                if screen_height <= 800:
                    win_width  = min(900, screen_width - 40)
                    win_height = screen_height - panel_height - 40
                else:
                    win_width  = 900
                    win_height = min(820, screen_height - panel_height - 60)

                x = (screen_width - win_width) // 2
                y = 30
            except Exception:
                win_width  = 900
                win_height = 820
                x = None
                y = None

            flask_thread = threading.Thread(target=run_flask, args=(PORT,),
                                            daemon=True)
            flask_thread.start()
            time.sleep(1)

            window = webview.create_window(
                "LiaisonOS",
                f"http://127.0.0.1:{PORT}",
                width=win_width, height=win_height,
                resizable=True, min_size=(640, 480),
                x=x, y=y, frameless=False,
            )
            webview.start()

        except ImportError:
            # pywebview not installed — fall back to a system browser.
            threading.Thread(target=open_browser, args=(PORT,),
                             daemon=True).start()
            app.run(host='127.0.0.1', port=PORT, debug=False)
