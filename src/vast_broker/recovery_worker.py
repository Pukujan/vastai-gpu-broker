"""Independent worker for reconciling fenced Vast leases from shared R2 state."""
from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from typing import Any, Callable, Protocol
from urllib.parse import urlsplit

from .guardians import (
    GuardianAck,
    GuardianCapabilities,
    GuardianDescriptor,
    GuardianKind,
    GuardianReadinessError,
    GuardianRecoveryReceipt,
    LeaseRegistrySnapshot,
    RecoveryState,
)
from .journal import LeaseJournal
from .lease import (
    LeaseController,
    _instance_id,
    _instances,
    _volume_ids,
    _volume_snapshot,
)


class AlertSink(Protocol):
    def send(self, payload: dict[str, Any], *, idempotency_key: str) -> str: ...


class WebhookAlertSink:
    """Send bounded, sanitized cleanup alerts to an owner-configured HTTPS hook."""

    def __init__(self, url: str, *, bearer_token: str | None = None, timeout: float = 10.0):
        parsed = urlsplit(url) if isinstance(url, str) else None
        if (
            parsed is None or parsed.scheme != "https" or not parsed.hostname
            or parsed.username or parsed.password or parsed.fragment
        ):
            raise ValueError("recovery alert webhook must use HTTPS")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
            raise ValueError("alert timeout must be positive")
        self.url = url
        self._bearer_token = bearer_token
        self.timeout = float(timeout)

    def send(self, payload: dict[str, Any], *, idempotency_key: str) -> str:
        body = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
        if len(body) > 16_384:
            raise ValueError("recovery alert payload exceeds its limit")
        headers = {
            "Content-Type": "application/json",
            "Idempotency-Key": idempotency_key,
        }
        if self._bearer_token:
            headers["Authorization"] = f"Bearer {self._bearer_token}"
        request = urllib.request.Request(self.url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                if not 200 <= response.status < 300:
                    raise RuntimeError("recovery alert webhook did not accept the alert")
                receipt = response.headers.get("X-Receipt-Id")
                return receipt if receipt and len(receipt) <= 256 else f"alert-{uuid.uuid4().hex}"
        except (urllib.error.URLError, TimeoutError, OSError):
            raise RuntimeError("recovery alert webhook is unavailable") from None


class _TrackedProvider:
    def __init__(self, provider: Any):
        self._provider = provider
        self.destroyed_instances: list[str] = []
        self.destroyed_volumes: list[str] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(self._provider, name)

    def destroy_instance(self, instance_id: str) -> Any:
        self.destroyed_instances.append(str(instance_id))
        return self._provider.destroy_instance(instance_id)

    def destroy_volume(self, volume_id: str) -> Any:
        self.destroyed_volumes.append(str(volume_id))
        return self._provider.destroy_volume(volume_id)


class RecoveryNotDue(RuntimeError):
    """The lease is still within its authorized runtime and has no cleanup signal."""


class GuardianRecoveryWorker:
    """A stateless worker pass driven by the durable shared lease registry.

    The registry is authoritative across process restarts. A short-lived local
    journal is reconstructed for each cleanup pass and discarded afterward.
    """

    def __init__(
        self,
        *,
        registry: Any,
        provider: Any,
        descriptor: GuardianDescriptor,
        alert_sink: AlertSink | None,
        clock: Callable[[], float] = time.time,
        cleanup_attempts: int = 3,
    ):
        self.registry = registry
        self.provider = provider
        self.descriptor = descriptor
        self.alert_sink = alert_sink
        self.clock = clock
        self.cleanup_attempts = cleanup_attempts
        if not isinstance(descriptor, GuardianDescriptor):
            raise TypeError("guardian descriptor is required")
        if descriptor.kind != GuardianKind.INDEPENDENT_SERVICE:
            raise ValueError("recovery worker must identify as an independent service")
        if descriptor.capabilities != self.capabilities:
            raise ValueError("guardian descriptor capabilities do not match configured recovery services")

    @property
    def capabilities(self) -> GuardianCapabilities:
        has_alert = self.alert_sink is not None
        return GuardianCapabilities(
            reconcile_ambiguous_create=True,
            destroy_owned_instances=True,
            destroy_owned_volumes=True,
            verify_provider_absence=True,
            emit_actionable_alert=has_alert,
        )

    def acknowledge_intent(
        self,
        *,
        registry_id: str,
        request_id: str,
        operation_fence: int,
        intent_digest: str,
    ) -> GuardianAck:
        if not self.capabilities.recovery_complete:
            raise GuardianReadinessError(
                "guardian_alert_sink_unavailable",
                "recovery guardian cannot acknowledge a lease without an actionable alert sink",
            )
        snapshot = self.registry.read(request_id)
        if (
            not isinstance(snapshot, LeaseRegistrySnapshot)
            or snapshot.registry_id != registry_id
            or registry_id != self.registry.registry_id
            or snapshot.request_id != request_id
            or snapshot.operation_fence != operation_fence
            or snapshot.intent_digest != intent_digest
            or snapshot.state != "CREATE_INTENT"
            or isinstance(snapshot.deadline_epoch, bool)
            or not isinstance(snapshot.deadline_epoch, (int, float))
            or not math.isfinite(snapshot.deadline_epoch)
            or snapshot.deadline_epoch <= self.clock()
        ):
            raise GuardianReadinessError(
                "guardian_intent_readback_failed",
                "recovery service could not read the live fenced lease intent",
            )
        ack = GuardianAck(
            guardian_id=self.descriptor.guardian_id,
            request_id=request_id,
            operation_fence=operation_fence,
            intent_digest=intent_digest,
            registry_id=registry_id,
            control_host_id=self.descriptor.control_host_id,
            failure_domain_id=self.descriptor.failure_domain_id,
            capabilities=self.capabilities,
            read_revision=snapshot.revision,
        )
        try:
            written = self.registry.acknowledge_guardian(ack)
            readback = self.registry.read(request_id)
        except Exception:
            raise GuardianReadinessError(
                "guardian_ack_persist_failed",
                "recovery service could not persist and read back its lease acknowledgement",
            ) from None
        if (
            not isinstance(written, LeaseRegistrySnapshot)
            or not isinstance(readback, LeaseRegistrySnapshot)
            or ack not in written.guardian_acks
            or ack not in readback.guardian_acks
        ):
            raise GuardianReadinessError(
                "guardian_ack_readback_failed",
                "recovery service acknowledgement was not durable",
            )
        return ack

    def run_once(self) -> list[GuardianRecoveryReceipt]:
        """Reconcile every published lease whose cleanup flag or deadline is due."""
        records = self.registry.list_records()
        receipts: list[GuardianRecoveryReceipt] = []
        for row in records:
            request_id = row.get("request_id") if isinstance(row, dict) else None
            fence = row.get("operation_fence") if isinstance(row, dict) else None
            if not isinstance(request_id, str) or isinstance(fence, bool) or not isinstance(fence, int):
                continue
            snapshot = self.registry.read(request_id)
            if not isinstance(snapshot, LeaseRegistrySnapshot):
                continue
            if snapshot.cleanup_requested or snapshot.deadline_epoch <= self.clock():
                receipts.append(self.reconcile(request_id=request_id, operation_fence=fence))
        return receipts

    def reconcile(self, *, request_id: str, operation_fence: int) -> GuardianRecoveryReceipt:
        snapshot = self.registry.read(request_id)
        if (
            not isinstance(snapshot, LeaseRegistrySnapshot)
            or snapshot.request_id != request_id
            or snapshot.operation_fence != operation_fence
            or snapshot.registry_id != self.registry.registry_id
            or isinstance(snapshot.deadline_epoch, bool)
            or not isinstance(snapshot.deadline_epoch, (int, float))
            or not math.isfinite(snapshot.deadline_epoch)
        ):
            raise GuardianReadinessError("recovery_fence_mismatch", "recovery request does not match the shared lease fence", cleanup_required=True)
        ack = next((item for item in snapshot.guardian_acks if item.guardian_id == self.descriptor.guardian_id), None)
        if (
            ack is None
            or ack.control_host_id != self.descriptor.control_host_id
            or ack.failure_domain_id != self.descriptor.failure_domain_id
            or ack.operation_fence != operation_fence
            or ack.intent_digest != snapshot.intent_digest
            or not ack.capabilities.recovery_complete
        ):
            raise GuardianReadinessError("recovery_guardian_not_acknowledged", "guardian has no matching durable lease acknowledgement", cleanup_required=True)
        cleanup_due = snapshot.cleanup_requested or snapshot.deadline_epoch <= self.clock()
        if not cleanup_due:
            raise RecoveryNotDue("lease deadline has not arrived and cleanup was not requested")

        try:
            instance_rows = _instances(self.provider.list_instances())
            volume_rows = self.provider.list_volumes()
            volume_attachments, _ = _volume_snapshot(volume_rows)
        except Exception as exc:
            return self._pending_receipt(
                snapshot,
                unresolved_instances=snapshot.owned_instance_ids,
                unresolved_volumes=snapshot.owned_volume_ids,
                error_type=type(exc).__name__,
            )

        preexisting_instances = set(snapshot.preexisting_instance_ids)
        preexisting_volumes = set(snapshot.preexisting_volume_ids)
        matching_instances = [
            row for row in instance_rows
            if row.get("label") == snapshot.attempt_label
            and _instance_id(row) is not None
            and _instance_id(row) not in preexisting_instances
        ]
        owned_instances = set(snapshot.owned_instance_ids)
        owned_instances.update(
            resource_id for row in matching_instances
            if (resource_id := _instance_id(row)) is not None
        )
        direct_volume_ids: set[str] = set()
        try:
            for row in instance_rows:
                resource_id = _instance_id(row)
                if resource_id in owned_instances:
                    direct_volume_ids.update(_volume_ids(row))
        except Exception as exc:
            return self._pending_receipt(
                snapshot,
                unresolved_instances=tuple(sorted(owned_instances)),
                unresolved_volumes=snapshot.owned_volume_ids,
                error_type=type(exc).__name__,
            )
        attached_volume_ids = {
            volume_id for volume_id, attached in volume_attachments.items()
            if set(attached) & owned_instances
        }
        owned_volumes = set(snapshot.owned_volume_ids)
        owned_volumes.update((direct_volume_ids | attached_volume_ids) - preexisting_volumes)
        if owned_instances & preexisting_instances or owned_volumes & preexisting_volumes:
            raise GuardianReadinessError("recovery_ownership_conflict", "shared lease record overlaps the pre-create account baseline", cleanup_required=True)
        if any(not self._positive_id(item) for item in owned_instances):
            raise GuardianReadinessError("recovery_instance_id_invalid", "shared lease record contains an invalid owned instance ID", cleanup_required=True)
        if any(not self._positive_id(item) for item in owned_volumes):
            raise GuardianReadinessError("recovery_volume_id_invalid", "shared lease record contains an invalid owned volume ID", cleanup_required=True)

        try:
            published = self.registry.publish_owned_resources(
                request_id=request_id,
                operation_fence=operation_fence,
                intent_digest=snapshot.intent_digest,
                instance_ids=tuple(sorted(owned_instances)),
                volume_ids=tuple(sorted(owned_volumes)),
            )
            snapshot = self.registry.read(request_id)
        except Exception:
            raise GuardianReadinessError(
                "recovery_resource_publication_failed",
                "recovery worker could not publish discovered owned resources before cleanup",
                cleanup_required=True,
            ) from None
        if (
            not isinstance(published, LeaseRegistrySnapshot)
            or not isinstance(snapshot, LeaseRegistrySnapshot)
            or not owned_instances.issubset(snapshot.owned_instance_ids)
            or not owned_volumes.issubset(snapshot.owned_volume_ids)
        ):
            raise GuardianReadinessError("recovery_resource_readback_failed", "shared registry omitted discovered owned resources", cleanup_required=True)

        # A cancellation during an ambiguous create may precede the provider's
        # instance row. Keep the obligation open until its hard runtime window.
        if not owned_instances and not owned_volumes and self.clock() < snapshot.deadline_epoch:
            return self._pending_receipt(
                snapshot, unresolved_instances=(), unresolved_volumes=(),
                error_type="CreateOutcomeStillAmbiguous",
            )

        tracked_provider = _TrackedProvider(self.provider)
        with tempfile.TemporaryDirectory(prefix="vast-broker-recovery-") as journal_dir:
            journal = LeaseJournal(journal_dir)
            instance = next(
                (row for row in instance_rows if _instance_id(row) in owned_instances),
                {},
            )
            journal.save({
                "schema_version": LeaseJournal.SCHEMA_VERSION,
                "request_id": request_id,
                "label": snapshot.attempt_label,
                "attempt_id": f"recovery-{operation_fence}",
                "operation_fence": operation_fence,
                "state": "DESTROY_REQUIRED",
                "owned_instance_ids": sorted(owned_instances),
                "owned_volume_ids": sorted(owned_volumes),
                "unresolved_volume_ids": [],
                "pre_create_instance_ids": list(snapshot.preexisting_instance_ids),
                "pre_create_volume_ids": list(snapshot.preexisting_volume_ids),
                "volume_discovery_pending": False,
                "instance": instance,
            })
            controller = LeaseController(
                tracked_provider, journal, supervisor=object(), clock=self.clock,
                sleep_fn=lambda _seconds: None, cleanup_attempts=self.cleanup_attempts,
            )
            cleaned = controller._cleanup_request(request_id)
            final = journal.load(request_id) or cleaned or {}
            # The local controller may discover more attached volumes while it
            # deletes. Publish that union before recording any recovery receipt.
            final_instances = tuple(sorted(set(final.get("owned_instance_ids", []))))
            final_volumes = tuple(sorted(set(final.get("owned_volume_ids", []))))
            try:
                self.registry.publish_owned_resources(
                    request_id=request_id, operation_fence=operation_fence,
                    intent_digest=snapshot.intent_digest,
                    instance_ids=final_instances, volume_ids=final_volumes,
                )
                snapshot = self.registry.read(request_id)
            except Exception:
                raise GuardianReadinessError(
                    "recovery_cleanup_publication_failed",
                    "cleanup result could not be durably published; recovery remains pending",
                    cleanup_required=True,
                ) from None
            if not isinstance(snapshot, LeaseRegistrySnapshot):
                raise GuardianReadinessError("recovery_cleanup_readback_failed", "shared lease record disappeared during cleanup", cleanup_required=True)

        unresolved_instances = tuple(sorted(set(final.get("verification", {}).get("remaining_instance_ids", []))))
        unresolved_volumes = tuple(sorted(
            set(final.get("verification", {}).get("remaining_volume_ids", []))
            | set(final.get("unresolved_volume_ids", []))
        ))
        absent = (
            final.get("state") in {"DESTROYED", "FAILED_CLEAN"}
            and not unresolved_instances
            and not unresolved_volumes
            and isinstance(final.get("absence_evidence"), dict)
            and final["absence_evidence"].get("owned_instance_ids_absent") is True
            and final["absence_evidence"].get("owned_volume_ids_absent") is True
        )
        if absent:
            receipt = GuardianRecoveryReceipt(
                request_id=request_id, operation_fence=operation_fence,
                guardian_id=self.descriptor.guardian_id,
                failure_domain_id=self.descriptor.failure_domain_id,
                state=RecoveryState.VERIFIED_ABSENT,
                destroy_requests_instance_ids=tuple(sorted(set(tracked_provider.destroyed_instances))),
                destroy_requests_volume_ids=tuple(sorted(set(tracked_provider.destroyed_volumes))),
                provider_absence_confirmed=True,
                alert_receipt_id=f"completion-{uuid.uuid4().hex}",
                observed_at_epoch=self.clock(),
            )
        else:
            receipt = self._pending_receipt(
                snapshot,
                unresolved_instances=unresolved_instances or snapshot.owned_instance_ids,
                unresolved_volumes=unresolved_volumes or snapshot.owned_volume_ids,
                error_type="CleanupStillPresent",
                destroy_instances=tracked_provider.destroyed_instances,
                destroy_volumes=tracked_provider.destroyed_volumes,
                persist=False,
            )
        try:
            self.registry.record_recovery_receipt(receipt)
        except Exception:
            raise GuardianReadinessError(
                "recovery_receipt_persist_failed",
                "recovery receipt could not be durably recorded",
                cleanup_required=True,
            ) from None
        return receipt

    @staticmethod
    def _positive_id(value: Any) -> bool:
        if isinstance(value, bool) or not isinstance(value, (str, int)):
            return False
        raw = str(value).strip()
        try:
            number = int(raw)
        except ValueError:
            return False
        return number > 0 and str(number) == raw

    def _pending_receipt(
        self,
        snapshot: LeaseRegistrySnapshot,
        *,
        unresolved_instances: tuple[str, ...],
        unresolved_volumes: tuple[str, ...],
        error_type: str,
        destroy_instances: tuple[str, ...] | list[str] = (),
        destroy_volumes: tuple[str, ...] | list[str] = (),
        persist: bool = True,
    ) -> GuardianRecoveryReceipt:
        unresolved_instances = tuple(sorted(set(unresolved_instances)))
        unresolved_volumes = tuple(sorted(set(unresolved_volumes)))
        prior: GuardianRecoveryReceipt | None = None
        try:
            prior = self.registry.latest_recovery_receipt(
                snapshot.request_id, self.descriptor.guardian_id,
            )
        except Exception:
            prior = None
        reuse_alert = bool(
            isinstance(prior, GuardianRecoveryReceipt)
            and prior.state == RecoveryState.CLEANUP_PENDING
            and prior.unresolved_instance_ids == unresolved_instances
            and prior.unresolved_volume_ids == unresolved_volumes
            and isinstance(prior.alert_receipt_id, str)
            and prior.alert_receipt_id.strip()
        )
        if reuse_alert:
            alert_id = prior.alert_receipt_id
        else:
            if self.alert_sink is None:
                raise GuardianReadinessError(
                    "recovery_alert_unavailable",
                    "cleanup is unresolved and no actionable alert sink is configured",
                    cleanup_required=True,
                )
            identity = f"{self.descriptor.guardian_id}:{snapshot.request_id}:{snapshot.operation_fence}:cleanup-pending"
            alert_id = self.alert_sink.send({
                "event": "vast_lease_cleanup_pending",
                "guardian_id": self.descriptor.guardian_id,
                "request_id": snapshot.request_id,
                "operation_fence": snapshot.operation_fence,
                "unresolved_instance_ids": list(unresolved_instances),
                "unresolved_volume_ids": list(unresolved_volumes),
                "error_type": error_type,
            }, idempotency_key=hashlib.sha256(identity.encode()).hexdigest())
        receipt = GuardianRecoveryReceipt(
            request_id=snapshot.request_id,
            operation_fence=snapshot.operation_fence,
            guardian_id=self.descriptor.guardian_id,
            failure_domain_id=self.descriptor.failure_domain_id,
            state=RecoveryState.CLEANUP_PENDING,
            destroy_requests_instance_ids=tuple(sorted(set(destroy_instances))),
            destroy_requests_volume_ids=tuple(sorted(set(destroy_volumes))),
            unresolved_instance_ids=unresolved_instances,
            unresolved_volume_ids=unresolved_volumes,
            provider_absence_confirmed=False,
            alert_receipt_id=alert_id,
            observed_at_epoch=self.clock(),
        )
        if persist:
            try:
                self.registry.record_recovery_receipt(receipt)
            except Exception:
                raise GuardianReadinessError(
                    "recovery_receipt_persist_failed",
                    "cleanup alert was sent but its receipt could not be durably recorded",
                    cleanup_required=True,
                ) from None
        return receipt


def guardian_descriptor_from_environment(*, alert_sink: AlertSink | None) -> GuardianDescriptor:
    """Build one service identity from deployment-specific host metadata."""
    names = ("VAST_BROKER_GUARDIAN_ID", "VAST_BROKER_CONTROL_HOST_ID", "VAST_BROKER_FAILURE_DOMAIN_ID")
    values = {name: os.environ.get(name, "").strip() for name in names}
    if any(not value for value in values.values()):
        raise ValueError("recovery guardian identity is incomplete")
    return GuardianDescriptor(
        guardian_id=values[names[0]],
        control_host_id=values[names[1]],
        failure_domain_id=values[names[2]],
        kind=GuardianKind.INDEPENDENT_SERVICE,
        trusted=True,
        capabilities=GuardianCapabilities(True, True, True, True, alert_sink is not None),
    )


def alert_sink_from_environment() -> WebhookAlertSink | None:
    url = os.environ.get("VAST_BROKER_ALERT_WEBHOOK_URL", "").strip()
    if not url:
        return None
    return WebhookAlertSink(
        url,
        bearer_token=os.environ.get("VAST_BROKER_ALERT_WEBHOOK_TOKEN") or None,
    )
