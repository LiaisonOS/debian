"""
HealthMonitor: Event-driven health checks on running process chains.

Reacts to two signals:
1. SIGCHLD from the kernel — any tracked child exiting wakes the monitor
   thread within microseconds (no polling latency in the common case).
2. A 1-second polling fallback — safety net for edge cases (signal
   coalescing when many children die at once, TCP-port health checks,
   misc. bookkeeping).

Checks per RUNNING process:
1. PID alive via subprocess.Popen.poll() (reaps zombies)
2. TCP port probe (connect+disconnect) for services like direwolf:8001
"""

import signal
import socket
import logging
import threading
import time

log = logging.getLogger("et-supervisor.health")


def check_tcp_port(port, host="127.0.0.1", timeout=2):
    """Test if a TCP port is accepting connections.

    Returns:
        True if connection succeeded.
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            s.connect((host, port))
            return True
    except (ConnectionRefusedError, TimeoutError, OSError):
        return False


def wait_for_port(port, timeout=15, interval=1, host="127.0.0.1",
                  cancel_event=None):
    """Wait for a TCP port to become available.

    Args:
        port: TCP port number to probe.
        timeout: seconds to keep trying.
        interval: poll interval in seconds.
        host: target host.
        cancel_event: optional threading.Event — if set during the wait,
            the function returns False immediately (used by ModeEngine.stop
            to abort an in-progress start without waiting the full timeout).

    Returns:
        True if port became available within timeout, False on timeout
        or cancellation.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cancel_event is not None and cancel_event.is_set():
            log.info("wait_for_port(%d): cancelled", port)
            return False
        if check_tcp_port(port, host):
            log.info("Port %d is ready", port)
            return True
        # Cancel-aware sleep so a stop request doesn't wait the full
        # `interval` before being noticed.
        if cancel_event is not None:
            if cancel_event.wait(interval):
                log.info("wait_for_port(%d): cancelled", port)
                return False
        else:
            time.sleep(interval)
    log.warning("Port %d not ready after %ds", port, timeout)
    return False


class HealthMonitor:
    """Periodically checks health of running processes."""

    def __init__(self, process_manager, interval=1.0):
        self._pm = process_manager
        self._interval = interval
        self._thread = None
        self._stop_event = threading.Event()
        # _wake_event lets the SIGCHLD handler (and stop()) interrupt the
        # interval sleep so the monitor reacts immediately to child exits
        # instead of waiting up to `interval` seconds.
        self._wake_event = threading.Event()
        self._crash_callback = None
        self._sigchld_installed = False

    def set_crash_callback(self, callback):
        """Set callback(process_name, state) called when a process dies.

        Args:
            callback: function(name, state) where state is "CRASHED" or "STOPPED".
        """
        self._crash_callback = callback

    def start(self):
        """Start the health monitoring thread.

        Must be called from the main thread so signal.signal() works.
        Falls back to pure polling if SIGCHLD registration fails.
        """
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._wake_event.clear()

        # SIGCHLD reactor — kernel signals us the moment any tracked child
        # exits, so detection drops from ~1s (polling) to microseconds.
        try:
            signal.signal(signal.SIGCHLD, self._sigchld_handler)
            self._sigchld_installed = True
            log.info("SIGCHLD handler installed")
        except (ValueError, OSError) as e:
            # signal.signal only works from main thread; fall back gracefully
            self._sigchld_installed = False
            log.warning("Could not install SIGCHLD handler (%s) — "
                        "falling back to polling-only mode", e)

        self._thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self._thread.start()
        log.info("Health monitor started (interval=%ds, sigchld=%s)",
                 self._interval, self._sigchld_installed)

    def stop(self):
        """Stop the health monitoring thread."""
        self._stop_event.set()
        self._wake_event.set()   # break the wait immediately
        if self._sigchld_installed:
            try:
                signal.signal(signal.SIGCHLD, signal.SIG_DFL)
            except (ValueError, OSError):
                pass
            self._sigchld_installed = False
        if self._thread:
            self._thread.join(timeout=self._interval + 1)
            self._thread = None
        log.info("Health monitor stopped")

    def wake(self):
        """Force the monitor to re-check immediately.

        Public so other code can poke it (e.g. just after a process is
        started, or from external bookkeeping).
        """
        self._wake_event.set()

    def _sigchld_handler(self, signum, frame):
        """Kernel-signalled child-exit notification.

        Must be async-signal-safe — we only set a threading.Event, which
        is GIL-serialised in Python and safe to call from signal handlers.
        The real work happens in _monitor_loop on wake-up.
        """
        self._wake_event.set()

    def _monitor_loop(self):
        while not self._stop_event.is_set():
            self._check_all()
            # Wait until poked by SIGCHLD / wake() OR the polling interval
            # elapses (safety net for missed signals + TCP-port checks).
            if self._wake_event.wait(self._interval):
                self._wake_event.clear()

    def _check_all(self):
        """Run health checks on all tracked processes."""
        for name, proc_info in self._pm.processes.items():
            if proc_info.state != "RUNNING":
                continue

            # Check PID alive
            alive = self._pm.check_process(name)
            if not alive:
                # Get the updated state after check_process
                updated = self._pm.processes.get(name)
                state = updated.state if updated else "CRASHED"
                log.warning("Health check: %s is no longer running (state=%s)",
                            name, state)
                if self._crash_callback:
                    self._crash_callback(name, state)
                continue

            # Check TCP port if configured
            if proc_info.health_port:
                port_ok = check_tcp_port(proc_info.health_port)
                if not port_ok:
                    log.warning("Health check: %s port %d not responding",
                                name, proc_info.health_port)

    def check_now(self):
        """Run health checks immediately (for CLI status queries)."""
        results = {}
        for name, proc_info in self._pm.processes.items():
            entry = {"pid_alive": False, "port_ok": None}
            if proc_info.state == "RUNNING":
                entry["pid_alive"] = self._pm.check_process(name)
                if proc_info.health_port:
                    entry["port_ok"] = check_tcp_port(proc_info.health_port)
            results[name] = entry
        return results
