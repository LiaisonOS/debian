"""
IPCServer: Unix domain socket server for dashboard/CLI communication.

Listens on /run/user/$UID/et-supervisor.sock
Protocol: newline-delimited JSON request/response. Connections are persistent —
clients may send multiple commands on one socket, and the server pushes status
broadcasts to all connected clients whenever the mode/process state changes.
Broadcasts carry an "_event": "status" marker so request/response code paths
can ignore unsolicited pushes.

Commands:
  {"cmd": "status"}                          -> current mode + process states
  {"cmd": "start-mode", "mode": "<mode-id>"} -> start a mode
  {"cmd": "stop"}                            -> stop current mode
  {"cmd": "health"}                          -> detailed health check results
  {"cmd": "list-modes"}                      -> available mode configs
"""

import json
import logging
import os
import socket
import threading

log = logging.getLogger("et-supervisor.ipc")

SOCKET_DIR = f"/run/user/{os.getuid()}"
SOCKET_PATH = os.path.join(SOCKET_DIR, "et-supervisor.sock")
BUFFER_SIZE = 8192


class IPCServer:
    """Unix domain socket server for et-supervisor IPC."""

    def __init__(self, mode_engine, health_monitor):
        self._engine = mode_engine
        self._health = health_monitor
        self._socket = None
        self._thread = None
        self._stop_event = threading.Event()
        # Active connections: {conn_id: {"sock": socket, "lock": Lock}}
        self._conns = {}
        self._conns_lock = threading.Lock()
        self._next_conn_id = 0

    @property
    def socket_path(self):
        return SOCKET_PATH

    def start(self):
        """Start the IPC server thread."""
        if os.path.exists(SOCKET_PATH):
            os.unlink(SOCKET_PATH)

        self._socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._socket.bind(SOCKET_PATH)
        os.chmod(SOCKET_PATH, 0o600)
        self._socket.listen(8)
        self._socket.settimeout(1.0)

        self._stop_event.clear()
        self._thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._thread.start()
        log.info("IPC server listening on %s", SOCKET_PATH)

    def stop(self):
        self._stop_event.set()
        if self._socket:
            try:
                self._socket.close()
            except OSError:
                pass
        # Close active connections so handler threads exit
        with self._conns_lock:
            entries = list(self._conns.values())
            self._conns.clear()
        for entry in entries:
            try:
                entry["sock"].close()
            except OSError:
                pass
        if self._thread:
            self._thread.join(timeout=3)
        if os.path.exists(SOCKET_PATH):
            os.unlink(SOCKET_PATH)
        log.info("IPC server stopped")

    def broadcast_status(self):
        """Push current status JSON to all connected clients.

        Each write is guarded by the connection's own lock so a broadcast
        can't interleave with a command response on the same socket.
        """
        try:
            payload = self._cmd_status()
            payload["_event"] = "status"
        except Exception:
            log.exception("broadcast_status: failed to build status")
            return
        line = json.dumps(payload).encode() + b"\n"
        with self._conns_lock:
            entries = list(self._conns.items())
        dead = []
        for conn_id, entry in entries:
            try:
                with entry["lock"]:
                    entry["sock"].sendall(line)
            except (BrokenPipeError, ConnectionResetError, OSError):
                dead.append(conn_id)
        if dead:
            with self._conns_lock:
                for conn_id in dead:
                    entry = self._conns.pop(conn_id, None)
                    if entry:
                        try:
                            entry["sock"].close()
                        except OSError:
                            pass

    def _accept_loop(self):
        while not self._stop_event.is_set():
            try:
                conn, _ = self._socket.accept()
                t = threading.Thread(target=self._handle_conn, args=(conn,),
                                     daemon=True)
                t.start()
            except socket.timeout:
                continue
            except OSError:
                if not self._stop_event.is_set():
                    log.error("IPC accept error")
                break

    def _register_conn(self, conn):
        with self._conns_lock:
            conn_id = self._next_conn_id
            self._next_conn_id += 1
            self._conns[conn_id] = {"sock": conn, "lock": threading.Lock()}
        return conn_id

    def _unregister_conn(self, conn_id):
        with self._conns_lock:
            entry = self._conns.pop(conn_id, None)
        if entry:
            try:
                entry["sock"].close()
            except OSError:
                pass

    def _handle_conn(self, conn):
        """Persistent per-connection loop: read newline-delimited JSON
        commands until the client disconnects."""
        conn_id = self._register_conn(conn)
        buf = b""
        try:
            conn.settimeout(None)
            while not self._stop_event.is_set():
                try:
                    chunk = conn.recv(BUFFER_SIZE)
                except (ConnectionResetError, OSError):
                    break
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        request = json.loads(line.decode("utf-8"))
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        self._send_line(conn_id,
                                        {"status": "error",
                                         "message": "Invalid JSON"})
                        continue
                    try:
                        response = self._dispatch(request)
                    except Exception:
                        log.exception("Unhandled error in command: %s",
                                      request.get("cmd", "?"))
                        response = {"status": "error",
                                    "message": "Internal supervisor error "
                                               "(check log)"}
                    self._send_line(conn_id, response)
        finally:
            self._unregister_conn(conn_id)

    def _send_line(self, conn_id, payload):
        with self._conns_lock:
            entry = self._conns.get(conn_id)
        if not entry:
            return
        data = json.dumps(payload).encode() + b"\n"
        try:
            with entry["lock"]:
                entry["sock"].sendall(data)
        except (BrokenPipeError, ConnectionResetError, OSError):
            self._unregister_conn(conn_id)

    def _dispatch(self, request):
        cmd = request.get("cmd", "")
        if cmd == "status":
            return self._cmd_status()
        elif cmd == "start-mode":
            return self._cmd_start_mode(request)
        elif cmd == "stop":
            return self._cmd_stop()
        elif cmd == "health":
            return self._cmd_health()
        elif cmd == "list-modes":
            return self._cmd_list_modes()
        elif cmd == "ignore-exit":
            return self._cmd_ignore_exit(request)
        else:
            return {"status": "error", "message": f"Unknown command: {cmd}"}

    def _cmd_ignore_exit(self, request):
        """Flip ignore_exit=True on a running process so its next exit
        is treated as 'expected' and does NOT cascade-stop the mode.

        Used by client apps that want to gracefully hand off (e.g.
        QtPatWinlink's 'Open in Browser' safety-net — close itself but
        keep pat-http and the modem running).
        """
        name = request.get("process", "")
        if not name:
            return {"status": "error", "message": "No process specified"}
        proc = self._engine._pm.processes.get(name)
        if not proc:
            return {"status": "error",
                    "message": f"Process '{name}' not found"}
        proc.ignore_exit = True
        log.info("Process %s marked ignore_exit (next exit will not cascade)",
                 name)
        return {"status": "ok",
                "message": f"{name} ignore_exit set"}

    def _cmd_status(self):
        pm = self._engine._pm
        processes = [p.to_dict() for p in pm.processes.values()]
        mode_name = None
        config = self._engine.mode_config
        if config:
            names = config.get("name", {})
            mode_name = names.get("en", self._engine.current_mode)
        return {
            "status": "ok",
            "mode": self._engine.current_mode,
            "mode_name": mode_name,
            # Internal-state flags so callers can wait for a TRUE idle
            # (mode cleared AND teardown / start truly finished). Without
            # these, a caller that races stop's Phase-2 sees mode=None
            # while _stopping is still True and gets "Mode '?'" on the
            # next start-mode.
            "stopping":          self._engine._stopping,
            "start_in_progress": self._engine._start_in_progress,
            "processes": processes,
        }

    def _cmd_start_mode(self, request):
        mode_id = request.get("mode", "")
        if not mode_id:
            return {"status": "error", "message": "No mode specified"}
        params = request.get("params", {})
        ok, msg = self._engine.start_mode(mode_id, params=params)
        return {
            "status": "ok" if ok else "error",
            "message": msg,
            "mode": self._engine.current_mode,
        }

    def _cmd_stop(self):
        ok, msg = self._engine.stop()
        return {"status": "ok" if ok else "error", "message": msg}

    def _cmd_health(self):
        results = self._health.check_now()
        return {
            "status": "ok",
            "mode": self._engine.current_mode,
            "health": results,
        }

    def _cmd_list_modes(self):
        modes = self._engine.list_modes()
        radio_bands = self._engine.get_active_radio_bands()
        details = []
        for mode_id in modes:
            config = self._engine.load_mode(mode_id)
            if config:
                required = config.get("requires_bands", [])
                if required and radio_bands and not set(required) & set(radio_bands):
                    continue
                details.append({
                    "id": mode_id,
                    "name": config.get("name", {}),
                    "category": config.get("category", ""),
                })
        return {"status": "ok", "modes": details}
