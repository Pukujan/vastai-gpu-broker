"""First-class search and guarded Vast rental lifecycle operations."""
from __future__ import annotations

import math
from typing import Any, Callable, Mapping

from .journal import LeaseJournal
from .lease import LeaseController, LeaseError
from .market import search_offers
from .router import authorize_run


def _owned_ids(record: Mapping[str, Any], field: str) -> list[str]:
    values = record.get(field)
    absence = record.get("absence_evidence")
    if not isinstance(values, list) or not values:
        values = absence.get(field) if isinstance(absence, Mapping) else []
    return sorted({str(value) for value in values if isinstance(value, (str, int)) and str(value)})


def _safe_provider_row(row: Mapping[str, Any]) -> dict[str, Any]:
    """Keep connection details and provider payload secrets out of lifecycle output."""
    safe: dict[str, Any] = {}
    for key in ("id", "actual_status", "status", "state", "label", "gpu_name", "dph_total", "start_date"):
        value = row.get(key)
        if isinstance(value, (str, int, bool)) or isinstance(value, float) and math.isfinite(value):
            safe[key] = value
    return safe


def _status_payload(
    record: Mapping[str, Any],
    instances: list[dict[str, Any]] | None,
    volumes: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    instance_ids = _owned_ids(record, "owned_instance_ids")
    volume_ids = _owned_ids(record, "owned_volume_ids")
    present_instances = (
        [_safe_provider_row(row) for row in instances if str(row.get("id")) in instance_ids]
        if instances is not None else []
    )
    present_volumes = (
        [_safe_provider_row(row) for row in volumes if str(row.get("id")) in volume_ids]
        if volumes is not None else []
    )
    instance_absence = not present_instances if instances is not None and instance_ids else None
    volume_absence = not present_volumes if volumes is not None else None
    cleanup_verified = (
        record.get("state") in {"DESTROYED", "FAILED_CLEAN"}
        and instance_absence is True
        and volume_absence is True
    )
    return {
        "request_id": record.get("request_id"),
        "state": record.get("state"),
        "owned_instance_ids": instance_ids,
        "owned_volume_ids": volume_ids,
        "instance_inventory_complete": instances is not None,
        "volume_inventory_complete": volumes is not None,
        "owned_instances_absent": instance_absence,
        "owned_volumes_absent": volume_absence,
        "cleanup_verified": cleanup_verified,
        "instances": present_instances,
        "volumes": present_volumes,
        "operation_result_state": (
            record.get("operation_result", {}).get("state")
            if isinstance(record.get("operation_result"), Mapping) else None
        ),
        "last_verified_absent_at_utc": (
            record.get("confirmed_absent_at_utc")
            if isinstance(record.get("absence_evidence"), Mapping) or record.get("confirmed_absent_at_utc") else None
        ),
    }


class VastRentaiService:
    """SDK facade for marketplace search and broker-owned rental operations.

    ``create`` accepts only a proposal freshly rebuilt by the router and a plan
    bound to that proposal. The lease controller still enforces the shared
    registry and two-guardian gate before it calls Vast's create endpoint.
    """

    def __init__(
        self,
        *,
        provider: Any | None = None,
        journal: LeaseJournal | None = None,
        search_client: Any | None = None,
        guardian_gate: Any | None = None,
        controller: LeaseController | None = None,
    ) -> None:
        if (provider is None) != (journal is None):
            raise ValueError("lifecycle operations require both a Vast provider and private lease journal")
        if provider is None and search_client is None:
            raise ValueError("configure a Vast provider or search client")
        self.provider = provider
        self.search_client = search_client if search_client is not None else provider
        self.journal = journal
        self.controller = controller or (
            LeaseController(provider, journal, guardian_gate=guardian_gate)
            if provider is not None and journal is not None else None
        )

    def search(
        self,
        filters: dict[str, Any] | None = None,
        *,
        page_size: int = 100,
        max_pages: int = 20,
        disk_gb: float | int | None = None,
        rental_types: tuple[str, ...] = ("ondemand", "bid", "reserved"),
    ) -> dict[str, Any]:
        """Return normalized, paginated marketplace observations."""
        return search_offers(
            filters, client=self.search_client, page_size=page_size,
            max_pages=max_pages, disk_gb=disk_gb, rental_types=rental_types,
        )

    def create(
        self,
        proposal: Mapping[str, Any],
        confirmation_digest: str,
        *,
        current_proposal: Mapping[str, Any],
        plan: Mapping[str, Any],
        operation: Callable[[dict[str, Any]], Any],
    ) -> dict[str, Any]:
        """Run a confirmed, freshly quoted proposal through guarded creation."""
        if self.controller is None:
            raise ValueError("create requires a Vast provider and private lease journal")
        return authorize_run(
            proposal, confirmation_digest, current_proposal=current_proposal,
            lease_controller=self.controller, plan=plan, operation=operation,
        )

    def status(self, request_id: str) -> dict[str, Any]:
        """Refresh provider inventories and report only resources owned by a request."""
        if self.provider is None or self.journal is None:
            raise ValueError("status requires a Vast provider and private lease journal")
        record = self.journal.load(request_id)
        if record is None:
            return {"request_id": request_id, "state": "NOT_FOUND"}
        instances: list[dict[str, Any]] | None
        volumes: list[dict[str, Any]] | None
        try:
            instances = self.provider.list_instances()
            if not isinstance(instances, list) or any(not isinstance(row, dict) for row in instances):
                instances = None
        except Exception:
            instances = None
        try:
            volumes = self.provider.list_volumes()
            if not isinstance(volumes, list) or any(not isinstance(row, dict) for row in volumes):
                volumes = None
        except Exception:
            volumes = None
        return _status_payload(record, instances, volumes)

    def destroy(self, request_id: str) -> dict[str, Any]:
        """Cancel one journal-owned lease and verify both provider inventories."""
        if self.provider is None or self.journal is None or self.controller is None:
            raise ValueError("destroy requires a Vast provider and private lease journal")
        record = self.journal.load(request_id)
        if record is None:
            return {"request_id": request_id, "state": "NOT_FOUND", "destroyed": False}
        failure_type: str | None = None
        try:
            self.controller.cancel(request_id)
        except LeaseError:
            failure_type = "LeaseError"
        except Exception as exc:
            failure_type = type(exc).__name__
        result = self.status(request_id)
        result["destroyed"] = result["cleanup_verified"]
        if failure_type is not None:
            result["destroy_error_type"] = failure_type
        return result
