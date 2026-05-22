#!/usr/bin/env python3
"""
et-menu-editor - LiaisonOS Dashboard Menu Editor
Author: Sylvain Deguire (VA2OPS)
Date: May 2026

Flask-based web UI for editing dashboard-menu.overrides.json. MVP scope:
toggle visibility (hide/show) of existing groups and modes only. No reorder,
no relabel, no custom-group creation — those land in later iterations.

Reads:
  /opt/emcomm-tools/conf/dashboard-menu.json   (system menu, read-only)
  ~/.config/liaisonos/dashboard-menu.overrides.json   (user overrides)

Writes:
  ~/.config/liaisonos/dashboard-menu.overrides.json   (only this)
"""

import warnings
warnings.filterwarnings("ignore", category=DeprecationWarning)
warnings.filterwarnings("ignore", message=".*pkg_resources.*")

import json
import logging
import os
import sys
import threading
import time
import webbrowser
from pathlib import Path

from flask import Flask, render_template, request, jsonify

log = logging.getLogger("werkzeug")
log.setLevel(logging.ERROR)

app = Flask(__name__)
app.secret_key = "liaisonos-menu-editor-2026"

SYSTEM_MENU_PATH = Path("/opt/emcomm-tools/conf/dashboard-menu.json")
OVERRIDES_PATH   = Path.home() / ".config" / "liaisonos" / "dashboard-menu.overrides.json"
USER_CONFIG_PATH = Path.home() / ".config" / "liaisonos" / "user.json"


def get_language():
    """Read 'language' from the LiaisonOS user.json; default 'en'."""
    if USER_CONFIG_PATH.exists():
        try:
            with open(USER_CONFIG_PATH, "r") as f:
                return json.load(f).get("language", "en") or "en"
        except Exception:
            pass
    return "en"


def load_system_menu():
    """Load /opt/emcomm-tools/conf/dashboard-menu.json. Returns {} on error."""
    if not SYSTEM_MENU_PATH.exists():
        return {}
    try:
        with open(SYSTEM_MENU_PATH, "r") as f:
            return json.load(f)
    except Exception as e:
        print(f"et-menu-editor: failed to read {SYSTEM_MENU_PATH}: {e}",
              file=sys.stderr)
        return {}


def load_overrides():
    """Load user overrides. Returns {} if file is missing or unreadable."""
    if not OVERRIDES_PATH.exists():
        return {}
    try:
        with open(OVERRIDES_PATH, "r") as f:
            return json.load(f)
    except Exception as e:
        print(f"et-menu-editor: failed to read {OVERRIDES_PATH}: {e}",
              file=sys.stderr)
        return {}


def save_overrides(payload):
    """Write user overrides atomically (write tmp then rename)."""
    OVERRIDES_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = OVERRIDES_PATH.with_suffix(".json.tmp")
    with open(tmp, "w") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
        f.write("\n")
    tmp.replace(OVERRIDES_PATH)


def build_view_model():
    """
    Compose the data the template iterates over. Each group/mode carries:
      - the system "default" label/title (used as placeholder)
      - the operator's current "override" (or empty if none)
      - a "hidden" boolean derived from hide_groups / hide_modes
    Items that the MenuLoader can't hide (multi-as-a-whole) are flagged
    so the template can disable just that checkbox.
    """
    system = load_system_menu()
    overrides = load_overrides()
    hide_groups   = set(overrides.get("hide_groups", []) or [])
    hide_modes    = set(overrides.get("hide_modes",  []) or [])
    relabel       = overrides.get("relabel",        {}) or {}
    relabel_touch = overrides.get("relabel_touch",  {}) or {}
    relabel_grp   = overrides.get("relabel_groups", {}) or {}

    lang = get_language()
    groups_view = []
    for g in system.get("groups", []) or []:
        gid = g.get("id", "")
        default_title = (g.get("title_fr") if lang == "fr" and g.get("title_fr")
                         else g.get("title_en") or g.get("title") or gid)
        gview = {
            "id": gid,
            "default_title": default_title,
            "title_override": relabel_grp.get(gid, "") or "",
            "icon": g.get("icon", ""),
            "hidden": gid in hide_groups,
            "items": [],
        }
        for it in g.get("items", []) or []:
            t = it.get("type")
            if t in ("mode", "param"):
                mid = it.get("id", "")
                gview["items"].append({
                    "kind": t,
                    "id": mid,
                    "default_label":        it.get("label") or mid,
                    "label_override":       relabel.get(mid, "") or "",
                    "default_label_touch":  it.get("label_touch") or "",
                    "label_touch_override": relabel_touch.get(mid, "") or "",
                    "hidden": mid in hide_modes,
                    "hideable": True,
                })
            elif t == "multi":
                children = []
                for m in it.get("modes", []) or []:
                    mid = m.get("id", "")
                    children.append({
                        "id": mid,
                        "default_label":        m.get("label") or mid,
                        "label_override":       relabel.get(mid, "") or "",
                        "default_label_touch":  m.get("label_touch") or "",
                        "label_touch_override": relabel_touch.get(mid, "") or "",
                        "hidden": mid in hide_modes,
                    })
                gview["items"].append({
                    "kind": "multi",
                    "label": it.get("label") or "(multi)",
                    "children": children,
                    "hideable": False,   # whole-multi hiding not supported
                })
            else:
                gview["items"].append({
                    "kind": "unknown",
                    "label": str(it.get("label") or it.get("id") or "?"),
                    "hideable": False,
                })
        groups_view.append(gview)

    return {
        "lang": lang,
        "groups": groups_view,
        "overrides_path": str(OVERRIDES_PATH),
        "system_path": str(SYSTEM_MENU_PATH),
    }


# ── Translations (en + fr; keys mirror the et-radio-config style) ──────────────
TRANSLATIONS = {
    "en": {
        "title":          "Menu Editor",
        "subtitle":       "Hide or rename dashboard groups and modes",
        "group":          "Group",
        "show_group":     "Show this group",
        "show_mode":      "Show",
        "save":           "Save",
        "reset":          "Reset to defaults",
        "reset_confirm":  "Remove the override file and restore the default menu?",
        "saved":          "Saved.",
        "reset_done":     "Override file removed.",
        "cannot_hide":    "(can't hide)",
        "system_path":    "System menu",
        "your_overrides": "Your overrides",
        "ph_group_title": "Custom group title",
        "ph_label":       "Custom label",
        "ph_label_touch": "Custom touch label",
        "col_desktop":    "Desktop label",
        "col_touch":      "Touch label",
        "col_id":         "ID",
    },
    "fr": {
        "title":          "Éditeur de menu",
        "subtitle":       "Masquer ou renommer les groupes et modes du menu",
        "group":          "Groupe",
        "show_group":     "Afficher ce groupe",
        "show_mode":      "Afficher",
        "save":           "Enregistrer",
        "reset":          "Restaurer le menu par défaut",
        "reset_confirm":  "Supprimer le fichier d'overrides et restaurer le menu par défaut ?",
        "saved":          "Enregistré.",
        "reset_done":     "Fichier d'overrides supprimé.",
        "cannot_hide":    "(ne peut être masqué)",
        "system_path":    "Menu système",
        "your_overrides": "Vos overrides",
        "ph_group_title": "Titre personnalisé",
        "ph_label":       "Étiquette personnalisée",
        "ph_label_touch": "Étiquette tactile",
        "col_desktop":    "Étiquette desktop",
        "col_touch":      "Étiquette tactile",
        "col_id":         "ID",
    },
}


@app.route("/")
def index():
    vm = build_view_model()
    return render_template(
        "index.html",
        t=TRANSLATIONS.get(vm["lang"], TRANSLATIONS["en"]),
        lang=vm["lang"],
        groups=vm["groups"],
        overrides_path=vm["overrides_path"],
        system_path=vm["system_path"],
    )


@app.route("/api/save", methods=["POST"])
def api_save():
    """
    Expects:
      { "hide_groups":    [...],
        "hide_modes":     [...],
        "relabel":        { id: "..." },
        "relabel_touch":  { id: "..." },
        "relabel_groups": { id: "..." } }
    Empty values are stripped so the file stays tidy. Preserves any other
    top-level keys already in the file (add_to_group, etc.) for future
    iterations.
    """
    try:
        incoming = request.get_json(force=True, silent=False) or {}
    except Exception as e:
        return jsonify({"ok": False, "error": f"bad JSON: {e}"}), 400

    def _clean_map(src):
        """Drop empty / whitespace-only values; trim whatever's left."""
        if not isinstance(src, dict):
            return {}
        out = {}
        for k, v in src.items():
            if not isinstance(v, str):
                continue
            v = v.strip()
            if v:
                out[k] = v
        return out

    hide_groups   = list(dict.fromkeys(incoming.get("hide_groups", []) or []))
    hide_modes    = list(dict.fromkeys(incoming.get("hide_modes",  []) or []))
    relabel       = _clean_map(incoming.get("relabel"))
    relabel_touch = _clean_map(incoming.get("relabel_touch"))
    relabel_grp   = _clean_map(incoming.get("relabel_groups"))

    payload = load_overrides() or {}
    payload["hide_groups"]    = hide_groups
    payload["hide_modes"]     = hide_modes
    payload["relabel"]        = relabel
    payload["relabel_touch"]  = relabel_touch
    payload["relabel_groups"] = relabel_grp

    # Drop empty keys to keep the file readable
    for k in ("hide_groups", "hide_modes",
              "relabel", "relabel_touch", "relabel_groups"):
        if not payload.get(k):
            payload.pop(k, None)

    try:
        save_overrides(payload)
    except Exception as e:
        return jsonify({"ok": False, "error": f"write failed: {e}"}), 500
    return jsonify({"ok": True})


@app.route("/api/reset", methods=["POST"])
def api_reset():
    """Delete the overrides file entirely."""
    try:
        if OVERRIDES_PATH.exists():
            OVERRIDES_PATH.unlink()
    except Exception as e:
        return jsonify({"ok": False, "error": f"delete failed: {e}"}), 500
    return jsonify({"ok": True})


def run_flask(port):
    """Run Flask server in background thread, silenced."""
    cli = sys.modules.get("flask.cli")
    if cli:
        cli.show_server_banner = lambda *args, **kwargs: None
    with open(os.devnull, "w") as devnull:
        old_stdout, old_stderr = sys.stdout, sys.stderr
        sys.stdout, sys.stderr = devnull, devnull
        try:
            app.run(host="127.0.0.1", port=port, debug=False, use_reloader=False)
        finally:
            sys.stdout, sys.stderr = old_stdout, old_stderr


def open_browser(port):
    """Delay then open the default browser (fallback when pywebview missing)."""
    time.sleep(1)
    webbrowser.open(f"http://127.0.0.1:{port}")


if __name__ == "__main__":
    PORT = 5058

    if "--no-browser" in sys.argv:
        app.run(host="127.0.0.1", port=PORT, debug=False)
    elif "--browser" in sys.argv:
        threading.Thread(target=open_browser, args=(PORT,), daemon=True).start()
        app.run(host="127.0.0.1", port=PORT, debug=False)
    elif "--help" in sys.argv:
        print("Usage: et-menu-editor [OPTIONS]")
        print("")
        print("Options:")
        print("  --no-browser    Start server only (no window)")
        print("  --browser       Open in default web browser")
        print("  --help          Show this help message")
        print("")
        print("Default: Opens in native PyWebView window")
        sys.exit(0)
    else:
        # Default: PyWebView native window — same pattern as et-radio-config.
        try:
            import webview

            try:
                import gi
                gi.require_version("Gdk", "3.0")
                from gi.repository import Gdk

                screen = Gdk.Screen.get_default()
                screen_width = screen.get_width()
                screen_height = screen.get_height()

                panel_height = 60
                if screen_height <= 800:
                    win_width = min(620, screen_width - 40)
                    win_height = screen_height - panel_height - 40
                else:
                    win_width = 620
                    win_height = min(800, screen_height - panel_height - 60)

                x = (screen_width - win_width) // 2
                y = 30
            except Exception:
                win_width = 620
                win_height = 800
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
                resizable=True, min_size=(480, 500),
                x=x, y=y, frameless=False,
            )
            webview.start()

        except ImportError:
            # pywebview not installed — fall back to a system browser.
            threading.Thread(target=open_browser, args=(PORT,),
                             daemon=True).start()
            app.run(host="127.0.0.1", port=PORT, debug=False)
