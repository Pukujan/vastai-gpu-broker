"""Request-scoped subprocess launcher for the durable lease supervisor."""
from __future__ import annotations

import os
import re
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from .journal import LeaseJournal


_TERMINAL_STATES = {"DESTROYED", "FAILED_CLEAN"}


def _safe_exception_type(exc: BaseException) -> str:
    """Keep diagnostics useful without storing exception messages or secrets."""
    name = type(exc).__name__
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", name)[:80]
    return safe or "Exception"


class ProcessLeaseSupervisor:
    """Launch and locally restart an independent process for each active lease.

    The child inherits the configured credential environment; credential values are
    never included in command arguments or journal diagnostics.
    """

    def __init__(self, journal_dir: str | os.PathLike[str], *, provider_factory: str | None = None,
                 poll_seconds: float = 2.0, startup_timeout_seconds: float = 3.0,
                 popen: Any = subprocess.Popen):
        self.journal_dir = str(Path(journal_dir).resolve())
        self.provider_factory = provider_factory or os.environ.get("VAST_BROKER_PROVIDER_FACTORY")
        if not self.provider_factory or ":" not in self.provider_factory:
            raise ValueError("provider_factory must be module:callable and construct an independent client")
        if poll_seconds <= 0 or startup_timeout_seconds <= 0:
            raise ValueError("supervisor intervals must be positive")
        self.poll_seconds = poll_seconds
        self.startup_timeout_seconds = startup_timeout_seconds
        self._popen = popen
        self._journal = LeaseJournal(self.journal_dir)
        self._children: dict[str, Any] = {}
        self._monitors: dict[str, threading.Thread] = {}
        self._lock = threading.RLock()

    def start(self, request_id: str) -> None:
        with self._lock:
            existing = self._children.get(request_id)
            monitor = self._monitors.get(request_id)
            if existing is not None and existing.poll() is None:
                return
            if monitor is not None and monitor.is_alive():
                return

            record = self._journal.load(request_id)
            if record is not None and record.get("state") in _TERMINAL_STATES:
                return

            child = self._spawn_and_wait(request_id)
            self._children[request_id] = child
            monitor = threading.Thread(
                target=self._watch_child,
                args=(request_id, child),
                name="vast-lease-supervisor-" + uuid.uuid4().hex[:8],
                daemon=True,
            )
            self._monitors[request_id] = monitor
            monitor.start()

    def _spawn_and_wait(self, request_id: str) -> Any:
        env = os.environ.copy()
        env["VAST_BROKER_JOURNAL_DIR"] = self.journal_dir
        env["VAST_BROKER_PROVIDER_FACTORY"] = self.provider_factory
        ready_file = str(Path(self.journal_dir) / (".supervisor-ready-" + uuid.uuid4().hex))
        cmd = [sys.executable, "-m", "vast_broker.supervisor", "--request-id", request_id,
               "--poll-seconds", str(self.poll_seconds), "--ready-file", ready_file]
        child = self._popen(cmd, env=env, stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + self.startup_timeout_seconds
        try:
            while time.monotonic() < deadline:
                if Path(ready_file).exists():
                    return child
                if child.poll() is not None:
                    raise RuntimeError("independent supervisor process exited during startup")
                time.sleep(min(0.025, max(0.0, deadline - time.monotonic())))
            raise RuntimeError("independent supervisor process did not become ready")
        except Exception:
            if child.poll() is None:
                child.terminate()
            raise
        finally:
            try:
                Path(ready_file).unlink()
            except OSError:
                pass

    def _watch_child(self, request_id: str, child: Any) -> None:
        while True:
            time.sleep(self.poll_seconds)
            if child.poll() is None:
                continue

            record = self._journal.load(request_id)
            if not record or record.get("state") in _TERMINAL_STATES:
                self._record_terminal_exit(request_id)
                with self._lock:
                    if self._children.get(request_id) is child:
                        self._children.pop(request_id, None)
                return

            self._record_unexpected_exit(request_id, child.returncode)
            while True:
                record = self._journal.load(request_id)
                if not record or record.get("state") in _TERMINAL_STATES:
                    self._record_terminal_exit(request_id)
                    with self._lock:
                        if self._children.get(request_id) is child:
                            self._children.pop(request_id, None)
                    return
                try:
                    replacement = self._spawn_and_wait(request_id)
                except Exception as exc:
                    self._record_restart_failure(request_id, exc)
                    time.sleep(self.poll_seconds)
                    continue

                with self._lock:
                    self._children[request_id] = replacement
                self._record_child_running(request_id)
                child = replacement
                break

    def _update_record(self, request_id: str, update: Any) -> None:
        with self._journal.locked(request_id):
            current = self._journal.load(request_id)
            if current is None:
                return
            update(current)
            self._journal.save(current)

    def _record_unexpected_exit(self, request_id: str, exit_code: int | None) -> None:
        now = time.time()

        def apply(record: dict[str, Any]) -> None:
            if record.get("state") in _TERMINAL_STATES:
                return
            record["supervisor_child_status"] = "exited_unexpectedly"
            record["supervisor_child_exit_epoch"] = now
            record["supervisor_child_exit_code"] = exit_code
            record["supervisor_child_restart_count"] = int(record.get("supervisor_child_restart_count", 0)) + 1
            record["supervisor_child_last_error"] = {"code": "child_exited_while_lease_nonterminal"}

        self._update_record(request_id, apply)

    def _record_restart_failure(self, request_id: str, exc: BaseException) -> None:
        now = time.time()
        safe_type = _safe_exception_type(exc)

        def apply(record: dict[str, Any]) -> None:
            if record.get("state") in _TERMINAL_STATES:
                return
            record["supervisor_child_status"] = "restart_failed"
            record["supervisor_child_restart_attempt_epoch"] = now
            record["supervisor_child_last_error"] = {
                "code": "child_restart_failed",
                "exception_type": safe_type,
            }

        self._update_record(request_id, apply)

    def _record_child_running(self, request_id: str) -> None:
        now = time.time()

        def apply(record: dict[str, Any]) -> None:
            if record.get("state") in _TERMINAL_STATES:
                return
            record["supervisor_child_status"] = "running"
            record["supervisor_child_started_epoch"] = now

        self._update_record(request_id, apply)

    def _record_terminal_exit(self, request_id: str) -> None:
        def apply(record: dict[str, Any]) -> None:
            if record.get("state") in _TERMINAL_STATES:
                record["supervisor_child_status"] = "stopped_terminal"

        self._update_record(request_id, apply)

    def stop(self) -> None:
        """No-op by design: the independent child exits after verified cleanup."""
        return None
