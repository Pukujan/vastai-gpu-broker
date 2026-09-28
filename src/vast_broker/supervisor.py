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
import time
from typing import Any

from .journal import LeaseJournal
from .lease import LeaseSupervisor


def _factory(spec: str) -> Any:
    module_name, separator, callable_name = spec.partition(":")
    if not separator or not module_name or not callable_name:
        raise ValueError("provider factory must be module:callable")
    result = getattr(importlib.import_module(module_name), callable_name)()
    return result


def serve(request_id: str, *, poll_seconds: float = 2.0, ready_file: str | None = None) -> None:
    journal_dir = os.environ["VAST_BROKER_JOURNAL_DIR"]
    provider = _factory(os.environ["VAST_BROKER_PROVIDER_FACTORY"])
    journal = LeaseJournal(journal_dir)
    supervisor = LeaseSupervisor(provider, journal)
    if ready_file:
        Path(ready_file).write_text("ready", encoding="utf-8")
    while True:
        try:
            record = supervisor.tick(request_id)
            if record is None or record.get("state") in {"DESTROYED", "FAILED_CLEAN"}:
                return
            # Unresolved create outcomes stay journaled and are reconciled on each tick.
        except Exception:
            # Preserve the journal obligation; next pass retries after transient faults.
            pass
        time.sleep(poll_seconds)


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
