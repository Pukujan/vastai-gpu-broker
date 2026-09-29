"""Worker entry point for an independent, credential-isolated lease supervisor.

Set VAST_BROKER_JOURNAL_DIR and VAST_BROKER_PROVIDER_FACTORY=module:callable in
its environment. The factory should read its scoped credential from an environment
variable and return a provider implementing the local lifecycle Protocol.
"""
from __future__ import annotations

import argparse
import importlib
import os
from pathlib import Path
import re
import threading
import time
from typing import Any

from .journal import LeaseJournal
from .lease import LeaseSupervisor


_TERMINAL_STATES = {"DESTROYED", "FAILED_CLEAN"}


def _factory(spec: str) -> Any:
    module_name, separator, callable_name = spec.partition(":")
    if not separator or not module_name or not callable_name:
        raise ValueError("provider factory must be module:callable")
    result = getattr(importlib.import_module(module_name), callable_name)()
    return result


def _safe_exception_type(exc: BaseException) -> str:
    """Record the error class without storing its message or possible credentials."""
    name = type(exc).__name__
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", name)[:80]
    return safe or "Exception"


def _begin_attempt(journal: LeaseJournal, request_id: str) -> int | None:
    with journal.locked(request_id):
        record = journal.load(request_id)
        if record is None:
            return None
        attempt = int(record.get("supervisor_attempt_count", 0)) + 1
        now = time.time()
        record["supervisor_attempt_count"] = attempt
        record["supervisor_last_attempt_epoch"] = now
        record["supervisor_last_attempt_state"] = "running"
        record["supervisor_heartbeat_epoch"] = now
        journal.save(record)
        return attempt


def _finish_attempt(journal: LeaseJournal, request_id: str, attempt: int, *,
                    error: dict[str, str] | None = None) -> dict[str, Any] | None:
    with journal.locked(request_id):
        record = journal.load(request_id)
        if record is None:
            return None
        now = time.time()
        record["supervisor_attempt_count"] = attempt
        record["supervisor_last_attempt_epoch"] = now
        record["supervisor_last_attempt_state"] = "failed" if error else "succeeded"
        record["supervisor_heartbeat_epoch"] = now
        if error:
            record["supervisor_last_error"] = {**error, "at_epoch": now}
        journal.save(record)
        return record


def _heartbeat_loop(journal: LeaseJournal, request_id: str, stop: threading.Event,
                    interval_seconds: float) -> None:
    """Keep worker liveness fresh even while a bounded provider call is in progress."""
    while not stop.wait(interval_seconds):
        with journal.locked(request_id):
            record = journal.load(request_id)
            if record is None or record.get("state") in _TERMINAL_STATES:
                return
            record["supervisor_heartbeat_epoch"] = time.time()
            journal.save(record)


def serve(request_id: str, *, poll_seconds: float = 2.0, ready_file: str | None = None) -> None:
    journal_dir = os.environ["VAST_BROKER_JOURNAL_DIR"]
    provider = _factory(os.environ["VAST_BROKER_PROVIDER_FACTORY"])
    journal = LeaseJournal(journal_dir)
    supervisor = LeaseSupervisor(provider, journal)
    if ready_file:
        Path(ready_file).write_text("ready", encoding="utf-8")
    heartbeat_stop = threading.Event()
    heartbeat_interval = min(poll_seconds, 5.0)
    heartbeat_thread = threading.Thread(
        target=_heartbeat_loop,
        args=(journal, request_id, heartbeat_stop, heartbeat_interval),
        name="vast-lease-worker-heartbeat",
        daemon=True,
    )
    heartbeat_thread.start()
    try:
        while True:
            attempt = _begin_attempt(journal, request_id)
            if attempt is None:
                return
            try:
                record = supervisor.tick(request_id)
            except Exception as exc:
                _finish_attempt(journal, request_id, attempt, error={
                    "code": "tick_failed",
                    "exception_type": _safe_exception_type(exc),
                })
                time.sleep(poll_seconds)
                continue

            current = _finish_attempt(journal, request_id, attempt)
            if record is None or record.get("state") in _TERMINAL_STATES or (
                current and current.get("state") in _TERMINAL_STATES
            ):
                return
            # Unresolved create outcomes stay journaled and are reconciled on each tick.
            time.sleep(poll_seconds)
    finally:
        heartbeat_stop.set()
        heartbeat_thread.join(timeout=max(0.1, heartbeat_interval * 2))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--request-id", required=True)
    parser.add_argument("--poll-seconds", type=float, default=2.0)
    parser.add_argument("--ready-file")
    args = parser.parse_args()
    if args.poll_seconds <= 0:
        raise SystemExit("poll-seconds must be positive")
    serve(args.request_id, poll_seconds=args.poll_seconds, ready_file=args.ready_file)


if __name__ == "__main__":
    main()
