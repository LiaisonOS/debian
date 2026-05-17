"""
ProcessManager: fork/exec processes, track PIDs, capture exit codes.

Handles the lifecycle of individual processes within a mode chain:
IDLE -> STARTING -> RUNNING -> STOPPING -> IDLE
                      |
                   CRASHED / FAILED
"""

import os
import signal
import subprocess
import threading
import time
import logging

log = logging.getLogger("et-supervisor.process")


class ProcessInfo:
    """Tracks state of a single managed process."""

    __slots__ = (
        "name", "command", "pid", "state", "start_time",
        "exit_code", "restart_count", "restart_policy",
        "health_port", "health_timeout", "process",
        "env", "cwd", "log_file", "ignore_exit",
    )

    def __init__(self, name, command, restart_policy="never",
                 health_port=None, health_timeout=15,
                 env=None, cwd=None, ignore_exit=False):
        self.name = name
        self.command = command
        self.pid = None
        self.state = "IDLE"
        self.start_time = None
        self.exit_code = None
        self.restart_count = 0
        self.restart_policy = restart_policy
        self.health_port = health_port
        self.health_timeout = health_timeout
        self.process = None  # subprocess.Popen object
        self.env = env        # extra environment variables (merged with os.environ)
        self.cwd = cwd        # working directory for the process
        self.log_file = None  # file handle for process log
        self.ignore_exit = ignore_exit

    @property
    def uptime(self):
        if self.start_time and self.state == "RUNNING":
            return int(time.time() - self.start_time)
        return 0

    def to_dict(self):
        return {
            "name": self.name,
            "pid": self.pid,
            "state": self.state,
            "uptime": self.uptime,
            "exit_code": self.exit_code,
            "restart_count": self.restart_count,
        }


class ProcessManager:
    """Manages fork/exec of processes with PID tracking."""

    def __init__(self):
        self._processes = {}  # name -> ProcessInfo
        self._waitpid_lock = threading.Lock()

    @property
    def processes(self):
        return dict(self._processes)

    def start_process(self, proc_info):
        """Start a process and track it.

        Args:
            proc_info: ProcessInfo with command to execute.

        Returns:
            True if process started successfully.
        """
        name = proc_info.name
        cmd = proc_info.command

        if name in self._processes and self._processes[name].state == "RUNNING":
            log.warning("Process %s already running (PID %d)",
                        name, self._processes[name].pid)
            return True

        log.info("Starting process %s: %s", name, " ".join(cmd))
        proc_info.state = "STARTING"

        try:
            # Merge extra env vars with current environment
            proc_env = None
            if proc_info.env:
                proc_env = os.environ.copy()
                proc_env.update(proc_info.env)

            # Redirect stdout/stderr to per-process log files for debugging
            log_dir = os.path.expanduser(
                "~/.local/share/emcomm-tools/logs")
            os.makedirs(log_dir, exist_ok=True)
            log_path = os.path.join(log_dir, f"{name}.log")
            log_file = open(log_path, "a")
            log_file.write(f"\n--- {time.strftime('%Y-%m-%d %H:%M:%S')} "
                           f"Starting: {' '.join(cmd)} ---\n")
            log_file.flush()

            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=log_file,
                stderr=log_file,
                start_new_session=True,
                env=proc_env,
                cwd=proc_info.cwd,
            )
            proc_info.process = proc
            proc_info.log_file = log_file
            proc_info.pid = proc.pid
            proc_info.start_time = time.time()
            proc_info.state = "RUNNING"
            proc_info.exit_code = None
            self._processes[name] = proc_info
            log.info("Process %s started with PID %d (log: %s)",
                     name, proc.pid, log_path)
            return True
        except FileNotFoundError:
            log.error("Command not found: %s", cmd[0])
            proc_info.state = "FAILED"
            self._processes[name] = proc_info
            if log_file:
                log_file.close()
            return False
        except OSError as e:
            log.error("Failed to start %s: %s", name, e)
            proc_info.state = "FAILED"
            self._processes[name] = proc_info
            if log_file:
                log_file.close()
            return False

    def _close_log(self, proc_info):
        """Close the process log file if open."""
        if proc_info.log_file:
            try:
                proc_info.log_file.close()
            except OSError:
                pass
            proc_info.log_file = None

    def stop_process(self, name, timeout=5):
        """Stop a process by name. SIGTERM then SIGKILL after timeout.

        Returns:
            True if process was stopped or already stopped.
        """
        proc_info = self._processes.get(name)
        if not proc_info or proc_info.state not in ("RUNNING", "STARTING"):
            return True

        proc_info.state = "STOPPING"
        pid = proc_info.pid
        log.info("Stopping %s (PID %d)", name, pid)

        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            proc_info.state = "IDLE"
            proc_info.pid = None
            self._close_log(proc_info)
            return True

        # Wait for graceful exit
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                result = os.waitpid(pid, os.WNOHANG)
                if result[0] != 0:
                    proc_info.exit_code = os.WEXITSTATUS(result[1]) if os.WIFEXITED(result[1]) else -1
                    proc_info.state = "IDLE"
                    proc_info.pid = None
                    self._close_log(proc_info)
                    log.info("Process %s exited (code %d)", name, proc_info.exit_code)
                    return True
            except ChildProcessError:
                proc_info.state = "IDLE"
                proc_info.pid = None
                self._close_log(proc_info)
                return True
            time.sleep(0.2)

        # Force kill
        log.warning("Process %s did not exit, sending SIGKILL", name)
        try:
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
        except (ProcessLookupError, ChildProcessError):
            pass

        proc_info.state = "IDLE"
        proc_info.pid = None
        self._close_log(proc_info)
        return True

    def stop_all(self, timeout=5):
        """Stop all managed processes in reverse order."""
        names = list(reversed(list(self._processes.keys())))
        for name in names:
            self.stop_process(name, timeout)
        self._processes.clear()

    def check_process(self, name):
        """Check if a process is still alive. Updates state if crashed.

        Returns:
            True if process is running, False if crashed/stopped.
        """
        proc_info = self._processes.get(name)
        if not proc_info or proc_info.state != "RUNNING":
            return False

        pid = proc_info.pid
        try:
            os.kill(pid, 0)  # Signal 0 = just check if alive, don't send anything
            return True  # Still running
        except ProcessLookupError:
            # Process is gone — check exit code via Popen if available
            proc = proc_info.process
            exit_code = proc.poll() if proc else None
            proc_info.exit_code = exit_code if exit_code is not None else 0
            proc_info.pid = None
            if proc_info.exit_code == 0:
                proc_info.state = "STOPPED"
                log.info("Process %s exited normally (code 0)", name)
            else:
                proc_info.state = "CRASHED"
                log.warning("Process %s crashed (exit code %d)", name, proc_info.exit_code)
            return False
        except PermissionError:
            return True  # Process exists but we can't signal it — still alive

    def get_status(self):
        """Return status dict for all tracked processes."""
        return {name: p.to_dict() for name, p in self._processes.items()}
