"""
ModeEngine: Reads JSON mode configs and orchestrates process chains.

Responsibilities:
- Load mode definitions from /opt/emcomm-tools/conf/modes.d/
- Run prechecks (device, audio, service, callsign)
- Apply config templates
- Execute pre_start actions (audio-config, kill-all, rfcomm-bind)
- Start process chain in order, waiting for health between steps
- Execute post_start actions (open-browser)
- Block new mode start if one is already running (safety: EmComm traffic)
"""

import json
import logging
import os
import subprocess
import threading
import time

from . import config_templater
from . import device_checker
from .health_monitor import wait_for_port
from .process_manager import ProcessInfo, ProcessManager
from .rig_client import rig

log = logging.getLogger("et-supervisor.mode")

MODES_DIR = "/opt/emcomm-tools/conf/modes.d"
ET_HOME = "/opt/emcomm-tools"

# Bilingual strings (EN/FR)
_TR = {
    "mode_not_found": {
        "en": "Mode '{}' not found",
        "fr": "Mode '{}' introuvable",
    },
    "mode_still_running": {
        "en": "Mode '{}' is still running. Stop it first.",
        "fr": "Le mode '{}' est en cours. Arrêtez-le d'abord.",
    },
    "prechecks_failed": {
        "en": "Prechecks failed: {}",
        "fr": "Vérifications échouées : {}",
    },
    "config_failed": {
        "en": "Config template application failed",
        "fr": "Échec de l'application du modèle de configuration",
    },
    "pre_start_failed": {
        "en": "Pre-start action '{}' failed",
        "fr": "Action de pré-démarrage '{}' échouée",
    },
    "start_failed": {
        "en": "Failed to start {}",
        "fr": "Échec du démarrage de {}",
    },
    "not_healthy": {
        "en": "{} did not become healthy (port {})",
        "fr": "{} n'est pas devenu fonctionnel (port {})",
    },
    "mode_started": {
        "en": "Mode {} started",
        "fr": "Mode {} démarré",
    },
    "no_mode_running": {
        "en": "No mode running",
        "fr": "Aucun mode en cours",
    },
    "mode_stopped": {
        "en": "Mode {} stopped",
        "fr": "Mode {} arrêté",
    },
    "crashed_stopped": {
        "en": "{} crashed. Mode stopped. Check logs and restart manually.",
        "fr": "{} a planté. Mode arrêté. Vérifiez les journaux et redémarrez.",
    },
    "crashed_after_retries": {
        "en": "{} crashed after {} restart(s). Mode stopped.",
        "fr": "{} a planté après {} redémarrage(s). Mode arrêté.",
    },
    "restarting": {
        "en": "Restarting {} (attempt {}/{})...",
        "fr": "Redémarrage de {} (tentative {}/{})...",
    },
    "restart_failed": {
        "en": "{} failed to restart. Mode stopped.",
        "fr": "{} n'a pas pu redémarrer. Mode arrêté.",
    },
    "restart_not_healthy": {
        "en": "{} restarted but not healthy. Mode stopped.",
        "fr": "{} redémarré mais non fonctionnel. Mode arrêté.",
    },
    "restart_success": {
        "en": "{} restarted successfully.",
        "fr": "{} redémarré avec succès.",
    },
    "callsign_not_set": {
        "en": "Callsign not set. Run et-user first.",
        "fr": "Indicatif non défini. Lancez et-user d'abord.",
    },
    "varac_license_missing": {
        "en": "VarAC license file not found",
        "fr": "Fichier de licence VarAC introuvable",
    },
    "varac_zenity_missing": {
        "en": "VarAC requires zenity for license dialog. "
              "Install with: sudo apt install zenity",
        "fr": "VarAC nécessite zenity pour la licence. "
              "Installez avec : sudo apt install zenity",
    },
    "varac_license_declined": {
        "en": "VarAC license not accepted. Cannot start VarAC.",
        "fr": "Licence VarAC non acceptée. Impossible de démarrer VarAC.",
    },
    "rfcomm_no_radio": {
        "en": "No paired Bluetooth radio found. "
              "Pair your radio in Blueman first.",
        "fr": "Aucune radio Bluetooth appariée trouvée. "
              "Appariez votre radio dans Blueman d'abord.",
    },
    "rfcomm_bind_failed": {
        "en": "Failed to bind /dev/rfcomm0: {}",
        "fr": "Échec de la liaison /dev/rfcomm0 : {}",
    },
    "band_mismatch": {
        "en": "Mode requires {} but radio supports {}",
        "fr": "Le mode nécessite {} mais la radio supporte {}",
    },
    "wrong_frequency": {
        "en": "Radio is on {} — switch to {} frequency first",
        "fr": "La radio est sur {} — changez pour une fréquence {} d'abord",
    },
}


def _get_language():
    """Read preferred language from user.json (cached)."""
    if not hasattr(_get_language, "_lang"):
        _get_language._lang = "en"
        try:
            cfg = config_templater.load_user_config()
            _get_language._lang = cfg.get("language", "en")
        except Exception:
            pass
    return _get_language._lang


def _t(key, *args):
    """Translate a key with optional format arguments."""
    lang = _get_language()
    entry = _TR.get(key, {})
    msg = entry.get(lang, entry.get("en", key))
    if args:
        msg = msg.format(*args)
    return msg


class ModeEngine:
    """Orchestrates mode lifecycle: prechecks -> config -> start chain."""

    def __init__(self, process_manager):
        self._pm = process_manager
        self._current_mode = None
        self._mode_config = None
        self._modes_cache = {}
        self._ipc = None  # Set by main after IPCServer is created

    def set_ipc(self, ipc_server):
        """Register the IPC server so we can broadcast status updates."""
        self._ipc = ipc_server

    def _broadcast_status(self):
        if self._ipc:
            self._ipc.broadcast_status()

    @property
    def current_mode(self):
        return self._current_mode

    @property
    def mode_config(self):
        return self._mode_config

    def load_mode(self, mode_id):
        """Load a mode definition from JSON.

        Returns:
            dict with mode config, or None on failure.
        """
        if mode_id in self._modes_cache:
            return self._modes_cache[mode_id]

        path = os.path.join(MODES_DIR, f"{mode_id}.json")
        if not os.path.isfile(path):
            log.error("Mode config not found: %s", path)
            return None

        try:
            with open(path) as f:
                config = json.load(f)
            self._modes_cache[mode_id] = config
            return config
        except json.JSONDecodeError as e:
            log.error("Invalid JSON in %s: %s", path, e)
            return None

    def list_modes(self):
        """List all available mode IDs.

        Returns:
            list of mode_id strings.
        """
        modes = []
        if not os.path.isdir(MODES_DIR):
            return modes
        for filename in sorted(os.listdir(MODES_DIR)):
            if filename.endswith(".json"):
                modes.append(filename[:-5])
        return modes

    def get_active_radio_bands(self):
        """Read bands from active radio config.

        Returns:
            list of band strings (e.g. ["HF", "VHF", "UHF"]), or []
            if no active radio or no bands field.
        """
        radio_path = os.path.join(ET_HOME, "conf/radios.d/active-radio.json")
        try:
            with open(radio_path) as f:
                radio = json.load(f)
            return radio.get("bands", [])
        except (FileNotFoundError, json.JSONDecodeError):
            return []

    def start_mode(self, mode_id, params=None):
        """Start a mode: prechecks, config, chain start.

        Args:
            mode_id: Mode identifier string.
            params: Optional dict of runtime parameters (e.g. modem selection).

        Returns:
            (success: bool, message: str)
        """
        config = self.load_mode(mode_id)
        if config is None:
            return False, _t("mode_not_found", mode_id)

        # Check band compatibility (safety net — dashboard already filters)
        required_bands = config.get("requires_bands", [])
        if required_bands:
            radio_bands = self.get_active_radio_bands()
            if radio_bands and not set(required_bands) & set(radio_bands):
                return False, _t("band_mismatch", required_bands, radio_bands)

        # Check current frequency matches required band — prevents starting
        # a VHF mode on HF frequency or vice versa
        if required_bands:
            current_band = self._get_band_from_radio()
            if current_band:
                band_category = self._band_to_category(current_band)
                if band_category and band_category not in required_bands:
                    required_str = "/".join(required_bands)
                    return False, _t("wrong_frequency",
                                     current_band, required_str)

        mode_name = config.get("name", {}).get("en", mode_id)
        log.info("Starting mode: %s (%s)", mode_id, mode_name)

        # Block if a mode is already running — user must stop it first
        # (EmComm safety: a critical message may be in transit)
        if self._current_mode:
            current_name = ""
            if self._mode_config:
                current_name = self._mode_config.get("name", {}).get(
                    "en", self._current_mode)
            return False, _t("mode_still_running",
                              current_name or self._current_mode)

        # Apply modem override (BBS client/server modes)
        if params and params.get("modem"):
            config = self._apply_modem_override(config, params["modem"])

        # Kill all first if requested
        if config.get("kill_all_first", False):
            wine_was_running = self._run_kill_all()
            # Only restart rigctld if Wine was running (e.g. VARA FM
            # disrupted the serial port) and the new mode needs it
            if wine_was_running:
                required_services = config.get("requires", {}).get("services", [])
                if "rigctld" in required_services:
                    self._restart_rigctld()

        # Run prechecks
        checks = config.get("prechecks", [])
        if checks:
            ok, failures = device_checker.run_prechecks(checks)
            if not ok:
                msgs = "; ".join(f"{t}: {m}" for t, m in failures)
                return False, _t("prechecks_failed", msgs)

        # Apply config templates
        configs = config.get("config", [])
        if configs:
            user_config = config_templater.load_user_config()
            if not config_templater.apply_configs(configs, user_config):
                return False, _t("config_failed")

        # Run pre_start actions
        for action in config.get("pre_start", []):
            if self._run_action(action) is False:
                return False, _t("pre_start_failed", action.get("action", ""))

        # Start process chain
        chain = config.get("chain", [])
        for step in chain:
            name = step["name"]
            cmd = self._resolve_command(step["command"], config)

            health_spec = step.get("health", {})
            health_port = health_spec.get("port")
            health_timeout = health_spec.get("timeout", 15)

            # Build environment and working directory from step config
            step_env = step.get("env")
            if step_env:
                step_env = {k: os.path.expanduser(v)
                            for k, v in step_env.items()}
            step_cwd = step.get("cwd")
            if step_cwd:
                step_cwd = os.path.expanduser(step_cwd)

            proc_info = ProcessInfo(
                name=name,
                command=cmd,
                restart_policy=step.get("restart", "never"),
                health_port=health_port,
                health_timeout=health_timeout,
                env=step_env,
                cwd=step_cwd,
                ignore_exit=step.get("ignore_exit", False),
            )

            if not self._pm.start_process(proc_info):
                self._pm.stop_all()
                return False, _t("start_failed", name)

            # Wait for health check if configured
            if health_spec.get("type") == "tcp-port" and health_port:
                if not wait_for_port(health_port, timeout=health_timeout):
                    self._pm.stop_all()
                    return False, _t("not_healthy", name, health_port)
                # Disable ongoing port monitoring if requested
                # (some apps like ardopcf treat health probes as sessions)
                if not health_spec.get("monitor", True):
                    proc_info.health_port = None

            log.info("Chain step %s started and healthy", name)

        # Run post_start actions
        for action in config.get("post_start", []):
            self._run_action(action)

        self._current_mode = mode_id
        self._mode_config = config
        self._broadcast_status()
        return True, _t("mode_started", mode_id)

    def _apply_modem_override(self, config, modem):
        """Override config template and chain for modem selection.

        For packet modems (1200/9600/300), swaps the direwolf template.
        For VARA modems, replaces the direwolf chain step with VARA
        and updates QtTermTCP.ini flags.

        Returns modified config (deep copy).
        """
        import copy
        config = copy.deepcopy(config)

        if modem in ("1200", "9600", "300"):
            # Swap direwolf template
            template_map = {
                "1200": "conf/template.d/packet/direwolf.simple.conf",
                "9600": "conf/template.d/packet/direwolf.9600.conf",
                "300": "conf/template.d/packet/direwolf.300.conf",
            }
            for cfg in config.get("config", []):
                if "direwolf" in cfg.get("template", ""):
                    cfg["template"] = template_map[modem]
            log.info("Modem override: direwolf template → %s", modem)

        elif modem in ("vara-fm", "vara-hf"):
            # Remove direwolf config template (VARA doesn't use direwolf)
            config["config"] = [
                c for c in config.get("config", [])
                if "direwolf" not in c.get("template", "")
            ]

            # Replace direwolf chain step with VARA
            new_chain = []
            for step in config.get("chain", []):
                if step["name"] == "direwolf":
                    if modem == "vara-fm":
                        new_chain.append({
                            "name": "vara-fm",
                            "command": ["wine", "C:\\VARA FM\\VARAFM.exe"],
                            "env": {
                                "WINEPREFIX": "~/.wine32",
                                "WINEARCH": "win32",
                            },
                            "cwd": "~/.wine32/drive_c/VARA FM",
                            "health": {"type": "tcp-port", "port": 8300,
                                       "timeout": 60, "monitor": False},
                            "restart": "never",
                        })
                    else:
                        new_chain.append({
                            "name": "vara-hf",
                            "command": ["wine", "C:\\VARA\\VARA.exe"],
                            "env": {
                                "WINEPREFIX": "~/.wine32",
                                "WINEARCH": "win32",
                            },
                            "cwd": "~/.wine32/drive_c/VARA",
                            "health": {"type": "tcp-port", "port": 8300,
                                       "timeout": 60, "monitor": False},
                            "restart": "never",
                        })
                else:
                    # Update depends_on to point to VARA step instead of direwolf
                    if step.get("depends_on") == "direwolf":
                        vara_name = "vara-fm" if modem == "vara-fm" else "vara-hf"
                        step["depends_on"] = vara_name
                    new_chain.append(step)
            config["chain"] = new_chain

            # Swap BBS config template for VARA modes
            bbs_template_map = {
                "vara-fm": "bpq32.vara-fm.cfg",
                "vara-hf": "bpq32.vara.cfg",
            }
            for action in config.get("pre_start", []):
                if action.get("action") == "bbs-config":
                    action["template"] = bbs_template_map[modem]

            # Update QtTermTCP.ini VARAFM/VARAHF flags and ensure VARA is enabled
            conf_file = os.path.expanduser("~/.config/QtTermTCP.ini")
            if os.path.isfile(conf_file):
                if modem == "vara-fm":
                    updates = {"VARAEnable": "1", "VARAFM": "1", "VARAHF": "0", "MERCURYEnable": "0"}
                else:
                    updates = {"VARAEnable": "1", "VARAFM": "0", "VARAHF": "1", "MERCURYEnable": "0"}
                self._update_ini_keys(conf_file, updates)

            # Add vara-fm-ptt-config pre_start action for VARA FM
            if modem == "vara-fm":
                config.setdefault("pre_start", []).insert(
                    0, {"action": "vara-fm-ptt-config"})

            log.info("Modem override: VARA chain → %s", modem)

        elif modem == "mercury":
            # Remove direwolf config template
            config["config"] = [
                c for c in config.get("config", [])
                if "direwolf" not in c.get("template", "")
            ]

            # Replace direwolf chain step with Mercury
            new_chain = []
            for step in config.get("chain", []):
                if step["name"] == "direwolf":
                    new_chain.append({
                        "name": "mercury",
                        "command": [
                            "/usr/local/bin/mercury",
                            "-C", "{mercury-ini}",
                            "-p", "8300",
                            "-b", "8100",
                            "-L", "/tmp/mercury.log"
                        ],
                        "health": {
                            "type": "tcp-port",
                            "port": 8300,
                            "timeout": 30,
                            "monitor": False
                        },
                        "restart": "never",
                    })
                else:
                    if step.get("depends_on") == "direwolf":
                        step["depends_on"] = "mercury"
                    new_chain.append(step)
            config["chain"] = new_chain

            # Swap BBS config template for Mercury (uses same port as VARA HF)
            for action in config.get("pre_start", []):
                if action.get("action") == "bbs-config":
                    action["template"] = "bpq32.vara.cfg"

            # Add mercury-config to pre_start
            pre_start = config.setdefault("pre_start", [])
            pre_start.append({"action": "mercury-config"})

            # Add mercury-tnc-init, txlevel restore, and QtMercury launch to post_start
            config.setdefault("post_start", []).insert(
                0, {"action": "mercury-tnc-init"})
            config["post_start"].append({"action": "mercury-txlevel-restore"})
            config["post_start"].append({"action": "qtmercury-start"})

            # Update QtTermTCP.ini for Mercury (VARA HF compatible)
            conf_file = os.path.expanduser("~/.config/QtTermTCP.ini")
            if os.path.isfile(conf_file):
                self._update_ini_keys(conf_file, {"VARAFM": "0", "VARAHF": "1"})

            log.info("Modem override: Mercury chain → bbs-client-qttermtcp")

        return config

    def stop(self):
        """Stop the current mode and all its processes."""
        if not self._current_mode:
            return True, _t("no_mode_running")

        mode_id = self._current_mode
        log.info("Stopping mode: %s", mode_id)

        # Check for Wine processes — need wineserver cleanup
        config = self._mode_config
        if config:
            chain = config.get("chain", [])
            has_wine = any("wine" in " ".join(s.get("command", [])).lower()
                           for s in chain)
            if has_wine:
                self._kill_wineserver()

        self._pm.stop_all()

        # Kill QtMercury if Mercury modem was used
        if config:
            chain = config.get("chain", [])
            has_mercury = any(s.get("name") == "mercury" for s in chain)
            if has_mercury:
                subprocess.call(["pkill", "-f", "QtMercury"],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                log.info("QtMercury stopped")

        # Restart rigctld after Wine modes — Wine shares the serial port
        # and corrupts rigctld's connection. Same as unplugging/replugging.
        if config:
            chain = config.get("chain", [])
            has_wine = any("wine" in " ".join(s.get("command", [])).lower()
                           for s in chain)
            if has_wine:
                self._restart_rigctld()

        self._current_mode = None
        self._mode_config = None
        self._broadcast_status()
        return True, _t("mode_stopped", mode_id)

    def handle_process_death(self, process_name, state):
        """Handle a process that has exited.

        Called by HealthMonitor's callback. Distinguishes normal exit
        (STOPPED, exit code 0) from crash (CRASHED, non-zero exit code).

        Normal exit: cascade-stop dependents, clean up mode — no crash
        notification. This is the expected path when a user closes an app
        (e.g. closes VARA, JS8Call window).

        Crash: follow restart policy, notify user.
        """
        proc_info = self._pm.processes.get(process_name)
        if proc_info and proc_info.ignore_exit:
            log.info("Process %s exited — ignore_exit set, no cascade stop", process_name)
            self._broadcast_status()
            return
        if state == "STOPPED":
            self._cascade_stop(process_name)
        else:
            self._handle_crash(process_name)

    def _cascade_stop(self, process_name):
        """Cascade-stop all processes after a normal exit.

        When a user closes an app (exit code 0), stop all remaining
        processes in the chain and clear the mode. No crash notification.
        """
        log.info("Process %s exited normally — cascade stopping mode %s",
                 process_name, self._current_mode)

        # Check for Wine processes — need wineserver cleanup
        if self._mode_config:
            chain = self._mode_config.get("chain", [])
            has_wine = any("wine" in " ".join(s.get("command", [])).lower()
                           for s in chain)
            if has_wine:
                self._kill_wineserver()

        self._pm.stop_all()

        # Restart rigctld after Wine modes — Wine shares the serial port
        # and corrupts rigctld's connection. Same as unplugging/replugging.
        if self._mode_config:
            chain = self._mode_config.get("chain", [])
            has_wine = any("wine" in " ".join(s.get("command", [])).lower()
                           for s in chain)
            if has_wine:
                self._restart_rigctld()

        self._current_mode = None
        self._mode_config = None
        self._broadcast_status()

    def _handle_crash(self, process_name):
        """Handle a crashed process based on its restart policy.

        Policies:
          "never"       — stop chain, notify user (default for field deployment)
          "once"        — retry once after 3s, then stop chain if it fails again
          "audio-retry" — up to 5 retries, 3s delay (Direwolf PulseAudio on USB)
        """
        proc_info = self._pm.processes.get(process_name)
        if not proc_info:
            return

        policy = proc_info.restart_policy
        count = proc_info.restart_count
        log.warning("Crash handler: %s (policy=%s, restarts=%d)",
                    process_name, policy, count)

        if policy == "never":
            self._notify_user(_t("crashed_stopped", process_name))
            self._stop_chain_on_crash(process_name)
            return

        if policy == "once":
            max_retries = 1
        elif policy == "audio-retry":
            max_retries = 5
        else:
            max_retries = 0

        if count >= max_retries:
            self._notify_user(
                _t("crashed_after_retries", process_name, count)
            )
            self._stop_chain_on_crash(process_name)
            return

        # Attempt restart in a separate thread to avoid blocking health monitor
        threading.Thread(
            target=self._restart_process,
            args=(process_name,),
            daemon=True,
        ).start()

    def _restart_process(self, process_name):
        """Restart a crashed process after delay."""
        proc_info = self._pm.processes.get(process_name)
        if not proc_info:
            return

        delay = 3
        proc_info.restart_count += 1
        attempt = proc_info.restart_count
        policy = proc_info.restart_policy
        max_retries = 1 if policy == "once" else 5 if policy == "audio-retry" else 0

        log.info("Restarting %s in %ds (attempt %d/%d)",
                 process_name, delay, attempt, max_retries)
        self._notify_user(
            _t("restarting", process_name, attempt, max_retries)
        )
        time.sleep(delay)

        # Re-start the process with the same command
        cmd = proc_info.command
        proc_info.state = "IDLE"
        proc_info.pid = None

        if not self._pm.start_process(proc_info):
            log.error("Restart failed for %s", process_name)
            self._notify_user(_t("restart_failed", process_name))
            self._stop_chain_on_crash(process_name)
            return

        # Wait for health if port configured
        if proc_info.health_port:
            if not wait_for_port(proc_info.health_port,
                                 timeout=proc_info.health_timeout):
                log.error("%s restarted but port %d not ready",
                          process_name, proc_info.health_port)
                self._notify_user(
                    _t("restart_not_healthy", process_name)
                )
                self._stop_chain_on_crash(process_name)
                return

        log.info("Process %s restarted successfully", process_name)
        self._notify_user(_t("restart_success", process_name))

    def _stop_chain_on_crash(self, crashed_name):
        """Stop the entire mode chain after an unrecoverable crash."""
        log.warning("Stopping mode %s due to %s crash",
                    self._current_mode, crashed_name)
        self._pm.stop_all()
        self._current_mode = None
        self._mode_config = None

    def _notify_user(self, message):
        """Send a desktop notification."""
        try:
            subprocess.Popen(
                ["notify-send", "-t", "10000",
                 "--app-name=LiaisonOS", message],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except FileNotFoundError:
            pass

    def _resolve_command(self, cmd_template, config):
        """Resolve {placeholders} in command arrays."""
        resolved = []
        for part in cmd_template:
            if "{direwolf_conf}" in part:
                part = part.replace("{direwolf_conf}",
                                    os.path.join(ET_HOME, "conf/packet/direwolf.conf"))
            if "{log_dir}" in part:
                log_dir = os.path.expanduser("~/.local/share/emcomm-tools")
                os.makedirs(log_dir, exist_ok=True)
                part = part.replace("{log_dir}", log_dir)
            if "{audio_device}" in part:
                audio = config_templater._detect_audio_device("plughw:{card},{device}")
                part = part.replace("{audio_device}", audio or "plughw:0,0")
            if "{pat_conf}" in part:
                pat_conf = os.path.expanduser("~/.config/pat/config.json")
                part = part.replace("{pat_conf}", pat_conf)
            if "{bbs_client_dir}" in part:
                bbs_dir = os.path.expanduser(
                    "~/.local/share/emcomm-tools/bbs-client")
                part = part.replace("{bbs_client_dir}", bbs_dir)
            if "{bbs_server_dir}" in part:
                bbs_dir = os.path.expanduser(
                    "~/.local/share/emcomm-tools/bbs-server")
                part = part.replace("{bbs_server_dir}", bbs_dir)
            if "{mercury-ini}" in part:
                mercury_ini = os.path.expanduser("~/.config/mercury/mercury.ini")
                part = part.replace("{mercury-ini}", mercury_ini)
            resolved.append(part)
        return resolved

    def _run_action(self, action):
        """Execute a pre_start or post_start action.

        Returns:
            None for most actions (best-effort), False if a blocking
            action fails (e.g. license-check declined).
        """
        action_type = action.get("action", "")

        if action_type == "kill-all":
            self._run_kill_all()
        elif action_type == "audio-config":
            self._run_audio_config()
        elif action_type == "open-browser":
            self._open_browser(action.get("url", ""))
        elif action_type == "prime-rigctld":
            self._prime_rigctld()
        elif action_type == "stop-rigctld":
            self._stop_rigctld()
        elif action_type == "set-radio-width":
            self._set_radio_width(action.get("width", 2750))
        elif action_type == "pat-config":
            self._apply_pat_config(action.get("template", ""))
        elif action_type == "vara-fm-ptt-config":
            self._apply_vara_fm_ptt_config()
        elif action_type == "varac-config":
            self._apply_varac_config()
        elif action_type == "license-check":
            if not self._check_varac_license():
                return False
        elif action_type == "yaac-config":
            self._apply_yaac_config(bluetooth=action.get("bluetooth", False))
        elif action_type == "chattervox-config":
            self._apply_chattervox_config(
                bluetooth=action.get("bluetooth", False))
        elif action_type == "paracon-config":
            self._apply_paracon_config()
        elif action_type == "qttermtcp-config":
            self._apply_qttermtcp_config()
        elif action_type == "js8spotter-setup":
            self._setup_js8spotter()
        elif action_type == "launch-js8spotter":
            self._launch_js8spotter()
        elif action_type == "bbs-inetd-config":
            self._configure_bbs_inetd()
        elif action_type == "bbs-config":
            self._apply_bbs_config(action.get("template", ""))
        elif action_type == "rfcomm-bind":
            if not self._bind_rfcomm():
                return False
        elif action_type == "qsy-band":
            self._qsy_to_band_freq(action.get("frequencies", {}))
        elif action_type == "wait-audio":
            self._wait_for_audio(action.get("seconds", 3))
        elif action_type == "mercury-config":
            self._apply_mercury_config()
        elif action_type == "mercury-tnc-init":
            self._apply_mercury_tnc_init()
        elif action_type == "mercury-txlevel-restore":
            self._apply_mercury_txlevel_restore()
        elif action_type == "qtmercury-start":
            self._launch_qtmercury()
        else:
            log.warning("Unknown action: %s", action_type)

    def _run_kill_all(self):
        """Stop all running mode processes.

        Returns True if Wine was running (serial port may be disrupted).
        """
        self._pm.stop_all()
        # Always kill wineserver — ensures VARA starts clean even on second run
        wine_was_running = self._is_wineserver_running()
        self._kill_wineserver()
        # Also kill any leftover processes from v1 scripts
        try:
            subprocess.run(
                [os.path.join(ET_HOME, "bin/et-kill-all")],
                timeout=10, capture_output=True
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
        if wine_was_running:
            # Wait for serial port to be free after Wine releases it
            self._wait_for_serial_port_free()
        return wine_was_running

    def _run_audio_config(self):
        """Configure ALSA mixer settings."""
        try:
            subprocess.run(
                [os.path.join(ET_HOME, "bin/et-audio"), "update-config"],
                timeout=10, capture_output=True
            )
        except (FileNotFoundError, subprocess.TimeoutExpired) as e:
            log.warning("Audio config failed: %s", e)

    def _apply_mercury_tnc_init(self):
        """Send ARQ tuning commands to Mercury TNC after startup."""
        import socket as _socket
        commands = [b"RETRIES 15\r", b"CALLINT 5\r"]
        try:
            s = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
            s.settimeout(5)
            s.connect(("localhost", 8300))
            for cmd in commands:
                s.sendall(cmd)
            s.close()
            log.info("Mercury TNC init sent (RETRIES 15, CALLINT 5)")
        except Exception as e:
            log.warning("Mercury TNC init failed: %s", e)

    def _apply_mercury_txlevel_restore(self):
        """Restore last QtMercury TX level via Unix control socket."""
        import socket as _socket
        import configparser

        ini_path = os.path.expanduser("~/.config/emcomm-tools/qtmercury.ini")
        txlevel = 100
        retries = 15
        callint = 5

        if os.path.isfile(ini_path):
            cp = configparser.ConfigParser()
            cp.read(ini_path)
            section = "emcomm-tools\\qtmercury" if "emcomm-tools\\qtmercury" in cp else None
            if section is None:
                for s in cp.sections():
                    if "qtmercury" in s.lower():
                        section = s
                        break
            if section:
                txlevel = int(cp[section].get("txlevel", 100))
                retries = int(cp[section].get("retries", 15))
                callint = int(cp[section].get("callint", 5))

        sock_path = "/tmp/mercury-ctl.sock"
        commands = [
            f"TXLEVEL {txlevel}\r",
            f"RETRIES {retries}\r",
            f"CALLINT {callint}\r",
        ]
        try:
            for cmd in commands:
                s = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
                s.settimeout(3)
                s.connect(sock_path)
                s.sendall(cmd.encode())
                s.close()
            log.info("Mercury TX level restored: TXLEVEL=%d RETRIES=%d CALLINT=%d",
                     txlevel, retries, callint)
        except Exception as e:
            log.warning("Mercury TX level restore failed: %s", e)

    def _launch_qtmercury(self):
        """Launch QtMercury companion app in the background."""
        try:
            subprocess.Popen(
                ["/usr/local/bin/QtMercury"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            log.info("QtMercury launched")
        except Exception as e:
            log.warning("Failed to launch QtMercury: %s", e)

    def _apply_mercury_config(self):
        """Write ~/.config/mercury/mercury.ini from current user/radio config."""
        user_config = config_templater.load_user_config()
        callsign = user_config.get("callsign", "N0CALL")

        audio_device = config_templater._detect_audio_device("plughw:{card},{device}") or "plughw:0,0"

        # Write softvol PCM to ~/.asoundrc so Mercury output can be
        # fine-tuned (256 steps, -30dB to 0dB) via QtMercury.
        asoundrc_path = os.path.expanduser("~/.asoundrc")
        softvol_marker = "# mercury-softvol-begin"
        softvol_block = (
            "\n" + softvol_marker + "\n"
            "pcm.mercury_out {\n"
            "    type softvol\n"
            f"    slave.pcm \"{audio_device}\"\n"
            "    control {\n"
            "        name \"Mercury TX Level\"\n"
            "        card \"ET_AUDIO\"\n"
            "    }\n"
            "    min_dB -30.0\n"
            "    max_dB 0.0\n"
            "    resolution 256\n"
            "}\n"
            "# mercury-softvol-end\n"
        )
        existing = ""
        if os.path.isfile(asoundrc_path):
            with open(asoundrc_path) as f:
                existing = f.read()
        # Replace existing block or append
        if softvol_marker in existing:
            import re as _re
            existing = _re.sub(
                r"\n# mercury-softvol-begin.*?# mercury-softvol-end\n",
                softvol_block,
                existing,
                flags=_re.DOTALL
            )
        else:
            existing += softvol_block
        with open(asoundrc_path, "w") as f:
            f.write(existing)
        log.info("Mercury softvol PCM written to %s", asoundrc_path)

        ini_lines = [
            "; Mercury init configuration — auto-generated by et-supervisor",
            "",
            "[main]",
            f"input_device = {audio_device}",
            "output_device = mercury_out",
            "sound_system = alsa",
            "capture_channel = left",
            "arq_tcp_base_port = 8300",
            "broadcast_tcp_port = 8100",
            "radio_model = 2",
            "radio_device = localhost:4532",
            "ui_enabled = false",
            "verbose = false",
        ]

        target_dir = os.path.expanduser("~/.config/mercury")
        target_path = os.path.join(target_dir, "mercury.ini")
        os.makedirs(target_dir, exist_ok=True)
        with open(target_path, "w") as f:
            f.write("\n".join(ini_lines) + "\n")

        log.info("Mercury config written: %s (audio=%s, callsign=%s)",
                 target_path, audio_device, callsign)

        # Enable Mercury in QtTermTCP.ini — adds keys if missing
        qttermtcp_ini = os.path.expanduser("~/.config/QtTermTCP.ini")
        if os.path.isfile(qttermtcp_ini):
            with open(qttermtcp_ini, encoding="latin-1") as f:
                lines = f.readlines()
            lines = self._ini_set_key(lines, "MERCURYEnable",   "1",        "General")
            lines = self._ini_set_key(lines, "MERCURYTermCall",  callsign,   "General")
            lines = self._ini_set_key(lines, "MERCURY500",       "1",        "General")
            lines = self._ini_set_key(lines, "MERCURY2300",      "0",        "General")
            lines = self._ini_set_key(lines, "MERCURY2750",      "0",        "General")
            lines = self._ini_set_key(lines, "VARAEnable",       "0",        "General")
            with open(qttermtcp_ini, "w", encoding="latin-1") as f:
                f.writelines(lines)
            log.info("QtTermTCP.ini: MERCURYEnable=1, callsign=%s", callsign)

    def _bind_rfcomm(self):
        """Bind /dev/rfcomm0 to a paired Bluetooth TNC radio.

        Scans conf/radios.d/*.bt.json for known radio deviceNames and
        channels, matches against paired Bluetooth devices, and runs
        'sudo rfcomm bind 0 <MAC> <channel>'.

        Returns:
            True if rfcomm0 already exists or was successfully bound,
            False if no paired radio found or bind failed.
        """
        if os.path.exists("/dev/rfcomm0"):
            log.info("rfcomm0 already exists, skipping bind")
            return True

        # Build map of known radio deviceNames -> channel from *.bt.json
        radios_dir = os.path.join(ET_HOME, "conf/radios.d")
        known_radios = {}  # {deviceName_lower: channel}
        if os.path.isdir(radios_dir):
            for fname in os.listdir(radios_dir):
                if not fname.endswith(".bt.json"):
                    continue
                try:
                    with open(os.path.join(radios_dir, fname)) as f:
                        radio = json.load(f)
                    bt = radio.get("bluetooth", {})
                    name = bt.get("deviceName", "")
                    channel = bt.get("channel", "")
                    if name and channel:
                        known_radios[name.lower()] = channel
                except (json.JSONDecodeError, OSError) as e:
                    log.warning("Could not read %s: %s", fname, e)

        if not known_radios:
            log.error("No radio configs with deviceName/channel found")
            self._notify_user(_t("rfcomm_no_radio"))
            return False

        log.info("Known BT radios: %s", known_radios)

        # Get paired devices via bluetoothctl
        try:
            result = subprocess.run(
                ["bluetoothctl", "devices", "Paired"],
                capture_output=True, text=True, timeout=10
            )
            if result.returncode != 0:
                log.error("bluetoothctl failed: %s", result.stderr.strip())
                self._notify_user(_t("rfcomm_no_radio"))
                return False
        except (FileNotFoundError, subprocess.TimeoutExpired) as e:
            log.error("bluetoothctl not available: %s", e)
            self._notify_user(_t("rfcomm_no_radio"))
            return False

        # Parse output: "Device AA:BB:CC:DD:EE:FF DeviceName"
        matched_mac = None
        matched_channel = None
        matched_name = None
        for line in result.stdout.strip().splitlines():
            parts = line.strip().split(None, 2)
            if len(parts) < 3 or parts[0] != "Device":
                continue
            mac = parts[1]
            dev_name = parts[2]
            if dev_name.lower() in known_radios:
                matched_mac = mac
                matched_channel = known_radios[dev_name.lower()]
                matched_name = dev_name
                break

        if not matched_mac:
            log.warning("No paired device matches known BT radios")
            self._notify_user(_t("rfcomm_no_radio"))
            return False

        log.info("Matched BT radio: %s (%s) channel %s",
                 matched_name, matched_mac, matched_channel)

        # Bind rfcomm0
        try:
            result = subprocess.run(
                ["sudo", "rfcomm", "bind", "0",
                 matched_mac, matched_channel],
                capture_output=True, text=True, timeout=10
            )
            if result.returncode != 0:
                err = result.stderr.strip()
                log.error("rfcomm bind failed: %s", err)
                self._notify_user(_t("rfcomm_bind_failed", err))
                return False
        except (FileNotFoundError, subprocess.TimeoutExpired) as e:
            log.error("rfcomm bind error: %s", e)
            self._notify_user(_t("rfcomm_bind_failed", str(e)))
            return False

        # Verify device was created (may take a moment)
        for _ in range(5):
            if os.path.exists("/dev/rfcomm0"):
                log.info("rfcomm0 bound to %s (%s) channel %s",
                         matched_name, matched_mac, matched_channel)
                return True
            time.sleep(0.5)

        log.error("rfcomm0 not created after bind command")
        self._notify_user(_t("rfcomm_bind_failed",
                              "device not created after bind"))
        return False

    def _apply_pat_config(self, template_name):
        """Apply a Pat Winlink config template.

        Templates Pat config.json with callsign, grid, and Winlink password
        from user.json, then copies to ~/.config/pat/config.json.
        """
        if not template_name:
            log.warning("pat-config action missing template name")
            return

        template_path = os.path.join(
            ET_HOME, "conf/template.d/winlink", template_name)
        target_path = os.path.expanduser("~/.config/pat/config.json")

        if not os.path.isfile(template_path):
            log.error("Pat config template not found: %s", template_path)
            return

        user_config = config_templater.load_user_config()
        callsign = user_config.get("callsign", "N0CALL")
        grid = user_config.get("grid", "")
        winlink_passwd = user_config.get("winlinkPasswd", "")

        if callsign == "N0CALL":
            log.error("Callsign not set, cannot configure Pat")
            self._notify_user(_t("callsign_not_set"))
            return

        os.makedirs(os.path.dirname(target_path), exist_ok=True)
        import shutil
        shutil.copy2(template_path, target_path)

        with open(target_path) as f:
            content = f.read()

        content = content.replace("{{ET_CALLSIGN}}", callsign)
        content = content.replace("{{ET_WINLINK_PASSWD}}", winlink_passwd)
        content = content.replace("{{ET_GRID}}", grid)

        with open(target_path, "w") as f:
            f.write(content)

        log.info("Pat config applied: %s -> %s", template_name, target_path)

    def _open_browser(self, url):
        """Open a URL in the preferred browser.

        Resolves {auto-band} placeholder by querying rigctld for the
        current radio frequency and converting to band name.
        """
        if not url:
            return

        # Resolve auto-band if present
        if "{auto-band}" in url:
            band = self._get_band_from_radio()
            url = url.replace("{auto-band}", band)

        log.info("Opening browser to %s", url)

        # Prefer Min browser (lightweight), then firefox-esr, then xdg-open
        for browser in ["min", "firefox-esr", "firefox", "xdg-open"]:
            try:
                subprocess.Popen(
                    [browser, url],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                return
            except FileNotFoundError:
                continue
        log.warning("No browser found to open %s", url)

    def _get_band_from_radio(self):
        """Query rigctld for current frequency and return band name."""
        if not rig.refresh():
            return ""
        return rig.band or ""

    def _wait_for_audio(self, seconds=3):
        """Wait for audio device to be released by PulseAudio."""
        import time
        log.info("wait-audio: waiting %d seconds for audio device", seconds)
        time.sleep(seconds)

    def _qsy_to_band_freq(self, frequencies):
        """QSY radio to the standard frequency for the detected band.

        Args:
            frequencies: dict mapping band names to dial frequencies in Hz,
                         e.g. {"20m": 14078000, "40m": 7078000}
        """
        if not frequencies:
            log.warning("qsy-band: no frequencies defined")
            return

        band = self._get_band_from_radio()
        if not band:
            log.warning("qsy-band: could not detect current band")
            return

        freq_hz = frequencies.get(band)
        if not freq_hz:
            log.info("qsy-band: no frequency defined for band %s", band)
            return

        log.info("qsy-band: detected %s, setting frequency to %d Hz", band, freq_hz)
        if rig.set_freq(freq_hz):
            log.info("qsy-band: QSY to %d Hz successful", freq_hz)
        else:
            log.warning("qsy-band: QSY failed")

    @staticmethod
    def _band_to_category(band):
        """Map a specific band name to HF/VHF/UHF category.

        Returns:
            "HF", "VHF", "UHF", or "" if unknown.
        """
        hf = {"160m", "80m", "60m", "40m", "30m", "20m", "17m",
              "15m", "12m", "10m"}
        vhf = {"6m", "2m"}
        uhf = {"70cm"}
        if band in hf:
            return "HF"
        if band in vhf:
            return "VHF"
        if band in uhf:
            return "UHF"
        return ""

    def _set_radio_width(self, width):
        """Set radio filter bandwidth via rigctld.

        Some radios (FT-891, FT-991A) default to 500Hz filter width which
        limits VARA HF performance. This sets the passband width to the
        specified value (e.g. 2750 for full VARA HF support).
        """
        rig.refresh()
        if not rig.mode_raw:
            log.warning("Could not read current mode from rigctld")
            return
        log.info("Setting radio width: mode=%s width=%s", rig.mode_raw, width)
        rig.set_mode(rig.mode_raw, int(width))

    def _prime_rigctld(self):
        """Prime rig control connection (for radios that need warmup).

        After kill-all restarts rigctld, the serial port may not be ready.
        Close any stale connection and retry until we get a valid frequency.
        """
        rig.close()
        for attempt in range(5):
            time.sleep(1)
            if rig.refresh() and rig.freq and rig.freq > 100000:
                log.info("prime-rigctld: radio ready (attempt %d)", attempt + 1)
                return
            log.debug("prime-rigctld: not ready, retrying (%d/5)", attempt + 1)
        log.warning("prime-rigctld: radio did not respond after 5 attempts")

    def _is_wineserver_running(self):
        """Check if Wine server is currently running."""
        try:
            result = subprocess.run(
                ["pgrep", "-x", "wineserver"],
                timeout=5, capture_output=True
            )
            return result.returncode == 0
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return False

    def _kill_wineserver(self):
        """Kill Wine server for clean shutdown."""
        try:
            subprocess.run(
                ["wineserver", "-k"],
                timeout=10, capture_output=True
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass

    def _wait_for_serial_port_free(self):
        """Wait for /dev/et-cat serial port to be released after Wine exits.

        After wineserver -k, the serial port may still be held briefly by
        the kernel driver. Poll with fuser until no process owns the port,
        so the next mode (e.g. rigctld) can grab it cleanly.
        """
        cat_device = "/dev/et-cat"
        if not os.path.exists(cat_device):
            return
        real_dev = os.path.realpath(cat_device)
        max_attempts = 10
        delay = 0.5
        for i in range(max_attempts):
            try:
                result = subprocess.run(
                    ["fuser", real_dev],
                    timeout=5, capture_output=True
                )
                if result.returncode != 0:
                    # No process using the port — it's free
                    log.info("Serial port %s is free (attempt %d/%d)",
                             real_dev, i + 1, max_attempts)
                    return
                log.info("Serial port %s still in use (attempt %d/%d)",
                         real_dev, i + 1, max_attempts)
            except (FileNotFoundError, subprocess.TimeoutExpired):
                return  # fuser not available, skip
            time.sleep(delay)
        log.warning("Serial port %s still in use after %d attempts",
                    real_dev, max_attempts)

    def _stop_rigctld(self):
        """Stop rigctld by killing the process directly (not systemctl)."""
        log.info("Stopping rigctld process...")
        rig.close()
        try:
            subprocess.run(
                ["sudo", "killall", "rigctld"],
                timeout=5, capture_output=True
            )
            time.sleep(0.5)
        except (subprocess.TimeoutExpired, FileNotFoundError) as e:
            log.warning("Failed to stop rigctld: %s", e)

    def _restart_rigctld(self):
        """Restart rigctld: kill existing process, then start via systemd."""
        log.info("Restarting rigctld...")
        # Drop rig client's stale TCP connection first
        rig.close()
        # Kill existing process directly (systemctl restart hangs
        # when rigctld won't release the serial port gracefully)
        try:
            subprocess.run(
                ["sudo", "killall", "rigctld"],
                timeout=5, capture_output=True
            )
            time.sleep(1)
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass
        # Start fresh via systemd
        try:
            subprocess.run(
                ["sudo", "systemctl", "start", "rigctld"],
                timeout=15, capture_output=True
            )
        except (subprocess.TimeoutExpired, FileNotFoundError) as e:
            log.warning("Failed to start rigctld: %s", e)

    # -----------------------------------------------------------------
    # Wine/VARA action implementations
    # -----------------------------------------------------------------

    def _apply_vara_fm_ptt_config(self):
        """Update VARAFM.ini with PTT settings from active-radio.json.

        Reads varafm.pttPort, varafm.pttVia, varafm.rts, varafm.dtr
        from active radio config and writes them plus a computed Pin
        value into VARAFM.ini.
        """
        vara_ini = os.path.expanduser("~/.wine32/drive_c/VARA FM/VARAFM.ini")
        radio_conf = os.path.join(ET_HOME, "conf/radios.d/active-radio.json")

        if not os.path.isfile(vara_ini):
            log.warning("VARAFM.ini not found, skipping PTT config")
            return

        # Defaults from et-vara-fm v1
        defaults = {
            "rig": None,        # VARA FM rig number (radio-specific)
            "pttPort": "COM5",
            "catPort": None,    # CAT port (defaults to pttPort if not set)
            "pttVia": "2",      # 0=VOX, 1=CAT, 2=COM (RTS/DTR), 3=CM108
            "baud": "38400",
            "rts": "1",
            "dtr": "0",
        }
        settings = dict(defaults)

        try:
            with open(radio_conf) as f:
                radio = json.load(f)
            varafm = radio.get("varafm", {})
            for key in defaults:
                val = varafm.get(key)
                if val is not None:
                    settings[key] = str(val)
        except (FileNotFoundError, json.JSONDecodeError) as e:
            log.warning("No active radio config, using PTT defaults: %s", e)

        # CATPort defaults to PTTPort if not specified
        if not settings["catPort"]:
            settings["catPort"] = settings["pttPort"]

        # Calculate Pin from RTS/DTR
        rts = settings["rts"] == "1"
        dtr = settings["dtr"] == "1"
        if rts and not dtr:
            pin = "1"      # RTS only
        elif not rts and dtr:
            pin = "2"      # DTR only
        elif rts and dtr:
            pin = "3"      # RTS + DTR
        else:
            pin = "1"      # Default to RTS

        updates = {
            "PTTPort": settings["pttPort"],
            "CATPort": settings["catPort"],
            "Baud": settings["baud"],
            "Via": settings["pttVia"],
            "RTS": settings["rts"],
            "DTR": settings["dtr"],
            "Pin": pin,
        }
        # Only write Rig if the radio config specifies one
        if settings["rig"]:
            updates["Rig"] = settings["rig"]

        self._update_ini_keys(vara_ini, updates)
        log.info("VARA FM PTT config: Rig=%s Port=%s CAT=%s Baud=%s Via=%s RTS=%s DTR=%s Pin=%s",
                 settings["rig"] or "unchanged", settings["pttPort"],
                 settings["catPort"], settings["baud"],
                 settings["pttVia"], settings["rts"], settings["dtr"], pin)

    def _apply_varac_config(self):
        """Update VarAC.ini with callsign, grid, and radio model.

        Reads callsign and grid from user.json, radio model from
        active-radio.json, and updates VarAC.ini [MY_INFO] section.
        """
        import re
        import shutil

        varac_ini = os.path.expanduser(
            "~/.wine32/drive_c/VarAC/VarAC.ini")

        if not os.path.isfile(varac_ini):
            log.warning("VarAC.ini not found, will be created on first run")
            return

        user_config = config_templater.load_user_config()
        callsign = user_config.get("callsign", "N0CALL")
        grid = user_config.get("grid", "")

        if callsign == "N0CALL" or not callsign:
            log.error("Callsign not set, cannot configure VarAC")
            self._notify_user(_t("callsign_not_set"))
            return

        # Get radio model from active-radio.json
        radio_conf = os.path.join(ET_HOME, "conf/radios.d/active-radio.json")
        myrig = ""
        try:
            with open(radio_conf) as f:
                radio = json.load(f)
            model = radio.get("model", "")
            if model:
                # Remove parenthetical notes like (DigiRig), spaces to underscores
                myrig = re.sub(r'\s*\([^)]*\)', '', model).replace(' ', '_')
        except (FileNotFoundError, json.JSONDecodeError):
            pass

        # Backup INI before modifying
        shutil.copy2(varac_ini, varac_ini + ".bak")

        with open(varac_ini, encoding="latin-1") as f:
            lines = f.readlines()

        lines = self._ini_set_key(lines, "Mycall", callsign, section="MY_INFO")
        lines = self._ini_set_key(lines, "MyLocator", grid, section="MY_INFO")
        if myrig:
            lines = self._ini_update_rig(lines, myrig)

        with open(varac_ini, "w", encoding="latin-1") as f:
            f.writelines(lines)

        log.info("VarAC config: callsign=%s grid=%s rig=%s",
                 callsign, grid, myrig)

    def _check_varac_license(self):
        """Check VarAC license agreement via zenity dialog.

        Uses a simple flag file with ACCEPTED_DATE. If the flag exists
        and has a valid date, the license is considered accepted.
        Otherwise shows the license text in a zenity dialog.

        Returns:
            True if license accepted, False if declined or error.
        """
        import datetime

        wineprefix = os.path.expanduser("~/.wine32")
        license_file = os.path.join(wineprefix, "drive_c/VarAC/License.txt")
        config_dir = os.path.expanduser("~/.config/emcomm-tools/varac")
        flag_file = os.path.join(config_dir, "license.flag")
        audit_log = os.path.join(config_dir, "license-audit.log")

        def log_audit(action, details):
            os.makedirs(config_dir, mode=0o700, exist_ok=True)
            ts = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
            machine_id = "unknown"
            try:
                with open("/etc/machine-id") as f:
                    machine_id = f.read().strip()[:8]
            except FileNotFoundError:
                pass
            with open(audit_log, "a") as f:
                f.write(f"{ts}|{action}|{machine_id}|{details}\n")

        # Check if license file exists (needed for zenity display)
        if not os.path.isfile(license_file):
            log.error("VarAC license file not found: %s", license_file)
            log_audit("ERROR", f"License file not found: {license_file}")
            self._notify_user(_t("varac_license_missing"))
            return False

        # Check if already accepted — flag file with valid ACCEPTED_DATE
        if os.path.isfile(flag_file):
            try:
                with open(flag_file) as f:
                    for line in f:
                        if line.startswith("ACCEPTED_DATE="):
                            date_str = line.split("=", 1)[1].strip()
                            datetime.datetime.fromisoformat(
                                date_str.replace("Z", "+00:00"))
                            log.info("VarAC license previously accepted")
                            return True
            except (OSError, ValueError):
                pass

        # Show license dialog via zenity
        try:
            with open(license_file) as f:
                license_content = f.read()
            proc = subprocess.Popen(
                ["zenity", "--text-info",
                 "--title=VarAC - License Agreement",
                 "--width=750", "--height=440",
                 "--checkbox=I have read and accept the license agreement"],
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            proc.communicate(input=license_content.encode(), timeout=300)
            accepted = proc.returncode == 0
        except FileNotFoundError:
            log.error("zenity not available for license dialog")
            self._notify_user(_t("varac_zenity_missing"))
            return False
        except subprocess.TimeoutExpired:
            proc.kill()
            log.error("License dialog timed out")
            return False

        if accepted:
            os.makedirs(config_dir, mode=0o700, exist_ok=True)
            ts = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
            with open(flag_file, "w") as f:
                f.write("# VarAC License Acceptance Flag\n")
                f.write("# Generated by LiaisonOS Debian Edition\n")
                f.write(f"ACCEPTED_DATE={ts}\n")
            os.chmod(flag_file, 0o600)
            log_audit("ACCEPTED", "License agreement accepted by user")
            log.info("VarAC license accepted")
            return True
        else:
            log_audit("DECLINED", "License agreement declined by user")
            log.warning("VarAC license declined")
            self._notify_user(_t("varac_license_declined"))
            return False

    # -----------------------------------------------------------------
    # INI file helpers
    # -----------------------------------------------------------------

    @staticmethod
    def _update_ini_keys(ini_path, updates):
        """Update key=value pairs in an INI file (case-sensitive match).

        Simple line-by-line replacement for INI files like VARAFM.ini
        where keys are known to exist. Uses latin-1 encoding for
        Windows-generated INI files.
        """
        with open(ini_path, encoding="latin-1") as f:
            lines = f.readlines()

        for i, line in enumerate(lines):
            stripped = line.rstrip('\r\n')
            for key, value in updates.items():
                if stripped.startswith(key + "="):
                    lines[i] = f"{key}={value}\n"
                    break

        with open(ini_path, "w", encoding="latin-1") as f:
            f.writelines(lines)

    @staticmethod
    def _ini_set_key(lines, key, value, section=None):
        """Set a key in INI lines with case-insensitive matching.

        If the key exists, replace its value. If not, add it after the
        section header. If the section doesn't exist, append both.

        Returns:
            Modified list of lines.
        """
        key_lower = key.lower()
        found = False

        for i, line in enumerate(lines):
            if line.strip().lower().startswith(key_lower + "="):
                lines[i] = f"{key}={value}\n"
                found = True
                break

        if not found and section:
            section_header = f"[{section}]"
            section_lower = section_header.lower()
            for i, line in enumerate(lines):
                if line.strip().lower() == section_lower:
                    lines.insert(i + 1, f"{key}={value}\n")
                    found = True
                    break

            if not found:
                lines.append(f"\n{section_header}\n{key}={value}\n")

        return lines

    @staticmethod
    def _ini_update_rig(lines, myrig):
        """Update MyRIG in VarAC.ini — replace first word, keep rest.

        Preserves any trailing description like 'liaisonos.com' that
        the user may have added.

        Returns:
            Modified list of lines.
        """
        key_lower = "myrig="
        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped.lower().startswith(key_lower):
                eq_pos = stripped.index("=")
                current_val = stripped[eq_pos + 1:]
                parts = current_val.split(None, 1)
                if len(parts) > 1:
                    lines[i] = f"MyRIG={myrig} {parts[1]}\n"
                else:
                    lines[i] = f"MyRIG={myrig}\n"
                return lines

        # Not found — add to [MY_INFO] section
        for i, line in enumerate(lines):
            if line.strip().lower() == "[my_info]":
                lines.insert(i + 1, f"MyRIG={myrig} liaisonos.com\n")
                return lines

        return lines

    # -----------------------------------------------------------------
    # Phase 5 action implementations
    # -----------------------------------------------------------------

    def _apply_yaac_config(self, bluetooth=False):
        """Update YAAC Ports/prefs.xml from template with callsign.

        Copies the Ports/prefs.xml template from /etc/skel, replaces
        {{ET_CALLSIGN}}, and removes ports for services that aren't running
        (GPS, Bluetooth TNC, Direwolf).

        Args:
            bluetooth: If True, use rfcomm0 only (no Direwolf KISS-over-TCP).
        """
        import shutil

        user_config = config_templater.load_user_config()
        callsign = user_config.get("callsign", "N0CALL")

        template = ("/etc/skel/.java/.userPrefs/org/ka2ddo/yaac"
                     "/Ports/prefs.xml")
        target_dir = os.path.expanduser(
            "~/.java/.userPrefs/org/ka2ddo/yaac/Ports")
        target = os.path.join(target_dir, "prefs.xml")

        if not os.path.isfile(template):
            log.warning("YAAC ports template not found: %s", template)
            return

        os.makedirs(target_dir, exist_ok=True)
        shutil.copy2(template, target)

        with open(target) as f:
            content = f.read()

        content = content.replace("{{ET_CALLSIGN}}", callsign)

        # Bluetooth mode: remove Direwolf KISS-over-TCP port (no Direwolf)
        if bluetooth:
            content = "\n".join(
                line for line in content.splitlines()
                if "KISS-over-TCP" not in line)

        # Remove ports for services not available
        # Check if gpsd is running
        try:
            result = subprocess.run(
                ["systemctl", "status", "gpsd", "--no-pager"],
                capture_output=True, timeout=5)
            if result.returncode != 0:
                # Remove GPSD port entry
                content = "\n".join(
                    line for line in content.splitlines()
                    if "GPSD" not in line)
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass

        # Remove Bluetooth TNC port if rfcomm0 not available
        if not os.path.exists("/dev/rfcomm0"):
            content = "\n".join(
                line for line in content.splitlines()
                if "rfcomm0" not in line)

        with open(target, "w") as f:
            f.write(content)

        log.info("YAAC ports config applied: callsign=%s, bluetooth=%s",
                 callsign, bluetooth)

    def _apply_chattervox_config(self, bluetooth=False):
        """Generate chattervox config.json and ensure signing key exists."""
        user_config = config_templater.load_user_config()
        callsign = user_config.get("callsign", "N0CALL")

        if callsign == "N0CALL" or not callsign:
            log.error("Callsign not set, cannot configure chattervox")
            self._notify_user(_t("callsign_not_set"))
            return

        conf_dir = os.path.expanduser("~/.chattervox")
        conf_file = os.path.join(conf_dir, "config.json")
        keystore = os.path.join(conf_dir, "keystore.json")
        os.makedirs(conf_dir, exist_ok=True)

        # Bluetooth TNC: serial KISS directly on rfcomm0 at 1200 baud
        # USB/soundcard: TCP KISS via Direwolf (baud irrelevant for TCP)
        if bluetooth:
            kiss_port = "/dev/rfcomm0"
            kiss_baud = 1200
            log.info("Chattervox: using Bluetooth TNC on %s @ %d",
                     kiss_port, kiss_baud)
        else:
            kiss_port = "kiss://localhost:8001"
            kiss_baud = 9600

        # Write initial config (needed before genkey — chattervox reads it)
        config = {
            "version": 3,
            "callsign": callsign,
            "ssid": 0,
            "keystoreFile": keystore,
            "kissPort": kiss_port,
            "kissBaud": kiss_baud,
            "feedbackDebounce": 20000,
        }
        with open(conf_file, "w") as f:
            json.dump(config, f, indent=2)

        # Generate signing key if no key exists for this callsign
        need_genkey = True
        if os.path.isfile(keystore):
            try:
                with open(keystore) as f:
                    ks = json.load(f)
                if ks.get(callsign, []):
                    need_genkey = False
            except (json.JSONDecodeError, FileNotFoundError):
                pass

        if need_genkey:
            try:
                log.info("Generating chattervox signing key for %s", callsign)
                subprocess.run(["chattervox", "genkey"],
                               timeout=10, capture_output=True)
            except (FileNotFoundError, subprocess.TimeoutExpired) as e:
                log.error("Failed to generate chattervox key: %s", e)

        # Read public key from keystore
        signing_key = ""
        if os.path.isfile(keystore):
            try:
                with open(keystore) as f:
                    ks = json.load(f)
                keys = ks.get(callsign, [])
                if keys:
                    signing_key = keys[0].get("public", "")
            except (json.JSONDecodeError, FileNotFoundError):
                pass

        # Update config with signing key
        if signing_key:
            config["signingKey"] = signing_key
            with open(conf_file, "w") as f:
                json.dump(config, f, indent=2)

            # Copy public key to clipboard and notify the user
            try:
                subprocess.run(["xclip", "-selection", "clipboard", "-i"],
                               input=signing_key.encode(),
                               timeout=5)
                self._notify_user(
                    f"Chattervox: Your public key is in the clipboard.\n"
                    f"Paste it anywhere to share with other operators.\n"
                    f"Key: {signing_key[:16]}...")
                log.info("Chattervox public key copied to clipboard")
            except (FileNotFoundError, subprocess.TimeoutExpired):
                log.warning("Could not copy public key to clipboard (xclip)")

        log.info("Chattervox config generated: %s", conf_file)

    def _apply_paracon_config(self):
        """Update paracon.cfg with callsign and host."""
        user_config = config_templater.load_user_config()
        callsign = user_config.get("callsign", "N0CALL")

        conf_file = os.path.expanduser(
            "~/.local/share/emcomm-tools/bbs-client/paracon.cfg")

        if not os.path.isfile(conf_file):
            log.warning("paracon.cfg not found: %s", conf_file)
            return

        with open(conf_file) as f:
            lines = f.readlines()

        for i, line in enumerate(lines):
            stripped = line.rstrip('\r\n')
            if stripped.startswith("host"):
                lines[i] = "host = localhost\n"
            elif stripped.startswith("callsign"):
                lines[i] = f"callsign = {callsign}\n"

        with open(conf_file, "w") as f:
            f.writelines(lines)

        log.info("Paracon config updated: callsign=%s", callsign)

    def _apply_qttermtcp_config(self):
        """Update QtTermTCP.ini with callsign."""
        user_config = config_templater.load_user_config()
        callsign = user_config.get("callsign", "N0CALL")

        conf_file = os.path.expanduser("~/.config/QtTermTCP.ini")

        if not os.path.isfile(conf_file):
            log.warning("QtTermTCP.ini not found: %s", conf_file)
            return

        with open(conf_file, encoding="latin-1") as f:
            lines = f.readlines()
        for key, value in [
            ("AGWTermCall",    callsign),
            ("MYCALL",         callsign),
            ("VARATermCall",   callsign),
            ("MERCURYTermCall", callsign),
            ("MERCURYEnable",  "0"),
            ("YAPPPath",       os.path.expanduser("~/Downloads")),
        ]:
            lines = self._ini_set_key(lines, key, value, "General")
        with open(conf_file, "w", encoding="latin-1") as f:
            f.writelines(lines)
        log.info("QtTermTCP config updated: callsign=%s", callsign)

    def _setup_js8spotter(self):
        """Set up JS8Spotter user directory with symlinks and config."""
        install_dir = "/opt/js8spotter"
        home_dir = os.path.expanduser(
            "~/.local/share/emcomm-tools/js8spotter")
        os.makedirs(home_dir, exist_ok=True)

        # Copy database if not exists
        db_src = os.path.join(install_dir, "js8spotter.db")
        db_dst = os.path.join(home_dir, "js8spotter.db")
        if not os.path.isfile(db_dst) and os.path.isfile(db_src):
            import shutil
            shutil.copy2(db_src, db_dst)
            # Disable sounds by default
            try:
                subprocess.run(
                    ["sqlite3", db_dst,
                     "UPDATE setting SET value='1' "
                     "WHERE name='disable_sounds';"],
                    timeout=5, capture_output=True)
            except (FileNotFoundError, subprocess.TimeoutExpired):
                pass

        # Create symlinks for all resources
        if os.path.isdir(install_dir):
            for name in os.listdir(install_dir):
                if name in ("js8spotter.db", "js8spotter.db.blank"):
                    continue
                link = os.path.join(home_dir, name)
                if not os.path.exists(link):
                    os.symlink(os.path.join(install_dir, name), link)

        # Update callsign and grid in database
        user_config = config_templater.load_user_config()
        callsign = user_config.get("callsign", "")
        grid = user_config.get("grid", "")

        if callsign and callsign != "N0CALL" and os.path.isfile(db_dst):
            try:
                subprocess.run(
                    ["sqlite3", db_dst,
                     f"UPDATE setting SET value='{callsign}' "
                     f"WHERE name='callsign';"],
                    timeout=5, capture_output=True)
            except (FileNotFoundError, subprocess.TimeoutExpired):
                pass

        if grid and os.path.isfile(db_dst):
            try:
                subprocess.run(
                    ["sqlite3", db_dst,
                     f"UPDATE setting SET value='{grid}' "
                     f"WHERE name='grid';"],
                    timeout=5, capture_output=True)
            except (FileNotFoundError, subprocess.TimeoutExpired):
                pass

        log.info("JS8Spotter setup complete: %s", home_dir)

    def _launch_js8spotter(self):
        """Launch JS8Spotter as a detached process (not tracked by supervisor).

        The user can close JS8Spotter independently without stopping JS8Call.
        When the mode stops, et-kill-all will clean up any remaining processes.
        """
        home_dir = os.path.expanduser(
            "~/.local/share/emcomm-tools/js8spotter")
        script = os.path.join(home_dir, "js8spotter.py")

        if not os.path.exists(script):
            log.warning("JS8Spotter not found: %s", script)
            return

        try:
            subprocess.Popen(
                ["python3", "js8spotter.py"],
                cwd=home_dir,
                start_new_session=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            log.info("JS8Spotter launched (detached)")
        except OSError as e:
            log.warning("Failed to launch JS8Spotter: %s", e)


    def _apply_bbs_config(self, template_name):
        """Template BBS LinBPQ config with callsign, grid, password."""
        if not template_name:
            log.warning("bbs-config action missing template name")
            return

        template_path = os.path.join(
            ET_HOME, "conf/template.d/bbs", template_name)
        target_dir = os.path.expanduser(
            "~/.local/share/emcomm-tools/bbs-server")
        target_path = os.path.join(target_dir, "bpq32.cfg")

        if not os.path.isfile(template_path):
            log.error("BBS config template not found: %s", template_path)
            return

        user_config = config_templater.load_user_config()
        callsign = user_config.get("callsign", "N0CALL")
        grid = user_config.get("grid", "")
        winlink_passwd = user_config.get("winlinkPasswd", "")

        if callsign == "N0CALL":
            log.error("Callsign not set, cannot configure BBS")
            self._notify_user(_t("callsign_not_set"))
            return

        os.makedirs(target_dir, exist_ok=True)
        import shutil
        shutil.copy2(template_path, target_path)

        with open(target_path) as f:
            content = f.read()

        content = content.replace("{{ET_CALLSIGN}}", callsign)
        content = content.replace("{{ET_WINLINK_PASSWD}}", winlink_passwd)
        content = content.replace("{{ET_GRID}}", grid)

        with open(target_path, "w") as f:
            f.write(content)

        log.info("BBS config applied: %s -> %s", template_name, target_path)

    def _configure_bbs_inetd(self):
        """Configure /etc/inetd.conf with BBS service entries for current user.

        Calls the et-configure-bbs-inetd helper (via sudo) which writes
        inetd.conf entries with correct user paths and restarts inetd.
        """
        import subprocess
        username = os.environ.get("USER", "user")
        try:
            result = subprocess.run(
                ["sudo", "/opt/emcomm-tools/bin/et-configure-bbs-inetd",
                 username],
                capture_output=True, text=True, timeout=10
            )
            if result.returncode != 0:
                log.error("Failed to configure BBS inetd: %s",
                          result.stderr.strip())
            else:
                log.info("BBS inetd configured for user: %s", username)
        except Exception as e:
            log.error("Error configuring BBS inetd: %s", e)
