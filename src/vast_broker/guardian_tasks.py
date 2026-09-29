"""One-shot entry point for scheduled or dispatched hosted recovery jobs."""
from __future__ import annotations

import os
import re
from typing import Any

from .guardian_http import worker_from_environment


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ValueError(f"{name} is required for this recovery action")
    return value


def run_from_environment() -> dict[str, Any]:
    action = os.environ.get("VAST_BROKER_GUARDIAN_ACTION", "sweep").strip().casefold()
    worker = worker_from_environment()
    if action == "sweep":
        receipts = worker.run_once()
        return {"action": action, "receipt_count": len(receipts)}
    if action == "acknowledge":
        request_id = _required("VAST_BROKER_GUARDIAN_REQUEST_ID")
        registry_id = _required("VAST_BROKER_GUARDIAN_REGISTRY_ID")
        intent_digest = _required("VAST_BROKER_GUARDIAN_INTENT_DIGEST")
        if re.fullmatch(r"[0-9a-f]{64}", intent_digest) is None:
            raise ValueError("lease intent digest is malformed")
        fence = int(_required("VAST_BROKER_GUARDIAN_OPERATION_FENCE"))
        ack = worker.acknowledge_intent(
            registry_id=registry_id, request_id=request_id,
            operation_fence=fence, intent_digest=intent_digest,
        )
        return {"action": action, "acknowledged": bool(ack.guardian_id)}
    if action == "reconcile":
        request_id = _required("VAST_BROKER_GUARDIAN_REQUEST_ID")
        fence = int(_required("VAST_BROKER_GUARDIAN_OPERATION_FENCE"))
        receipt = worker.reconcile(request_id=request_id, operation_fence=fence)
        return {"action": action, "state": receipt.state.value}
    raise ValueError("recovery action must be sweep, acknowledge, or reconcile")


def main() -> int:
    try:
        result = run_from_environment()
    except Exception as exc:
        print(f"guardian_task_failed={type(exc).__name__}")
        return 1
    print(" ".join(f"{key}={value}" for key, value in sorted(result.items())))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
