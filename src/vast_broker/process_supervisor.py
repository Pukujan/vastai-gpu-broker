"""Request-scoped subprocess launcher for the durable lease supervisor."""
from __future__ import annotations

import os
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any


class ProcessLeaseSupervisor:
    """Launch a separate Python process that creates its own provider client.

    The child inherits the configured credential environment; credential values are
    never included in command arguments, journal records, or supervisor output.
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
        self._children: dict[str, Any] = {}

    def start(self, request_id: str) -> None:
        existing = self._children.get(request_id)
        if existing is not None and existing.poll() is None:
            return
        env = os.environ.copy()
        env["VAST_BROKER_JOURNAL_DIR"] = self.journal_dir
        env["VAST_BROKER_PROVIDER_FACTORY"] = self.provider_factory
        ready_file = str(Path(self.journal_dir) / (".supervisor-ready-" + uuid.uuid4().hex))
        cmd = [sys.executable, "-m", "vast_broker.supervisor", "--request-id", request_id,
               "--poll-seconds", str(self.poll_seconds), "--ready-file", ready_file]
        child = self._popen(cmd, env=env, stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self._children[request_id] = child
        deadline = time.monotonic() + self.startup_timeout_seconds
        try:
            while time.monotonic() < deadline:
                if Path(ready_file).exists():
                    return
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

    def stop(self) -> None:
        """No-op by design: the independent child exits after verified destruction."""
        return None
