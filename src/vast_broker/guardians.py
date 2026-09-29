"""Interfaces for arming independent recovery guardians before lease creation.

This module validates declared control-host placement and shared-registry
readbacks. It cannot establish that deployed hosts really occupy separate
failure domains; deployment evidence must be checked outside this interface.
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from enum import StrEnum
from typing import Callable, Protocol, TypeVar


class GuardianKind(StrEnum):
    """Deployment types; only an independently operated service counts for P45."""

    INDEPENDENT_SERVICE = "independent_service"
    SAME_HOST_PROCESS = "same_host_process"
    LOCAL_BROWSER_SESSION = "local_browser_session"
    SCHEDULED_WORKFLOW = "scheduled_workflow"


class RecoveryState(StrEnum):
    CLEANUP_PENDING = "CLEANUP_PENDING"
    VERIFIED_ABSENT = "VERIFIED_ABSENT"


@dataclass(frozen=True, slots=True)
class GuardianCapabilities:
    reconcile_ambiguous_create: bool = False
    destroy_owned_instances: bool = False
    destroy_owned_volumes: bool = False
    verify_provider_absence: bool = False
    emit_actionable_alert: bool = False

    @property
    def recovery_complete(self) -> bool:
        return all((
            self.reconcile_ambiguous_create,
            self.destroy_owned_instances,
            self.destroy_owned_volumes,
            self.verify_provider_absence,
            self.emit_actionable_alert,
        ))


@dataclass(frozen=True, slots=True)
class GuardianDescriptor:
    guardian_id: str
    control_host_id: str
    failure_domain_id: str
    kind: GuardianKind
    trusted: bool
    capabilities: GuardianCapabilities


@dataclass(frozen=True, slots=True)
class LeaseCreateIntent:
    """Immutable CREATE_INTENT identity shared before any provider create call."""

    request_id: str
    operation_id: str
    attempt_id: str
    attempt_label: str
    operation_fence: int
    deadline_epoch: float
    initiating_host_id: str
    preexisting_instance_ids: tuple[str, ...] = ()
    preexisting_volume_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in ("request_id", "operation_id", "attempt_id", "attempt_label", "initiating_host_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
        if isinstance(self.operation_fence, bool) or not isinstance(self.operation_fence, int) or self.operation_fence <= 0:
            raise ValueError("operation_fence must be a positive integer")
        if isinstance(self.deadline_epoch, bool) or not isinstance(self.deadline_epoch, (int, float)):
            raise ValueError("deadline_epoch must be a finite positive epoch")
        if not math.isfinite(self.deadline_epoch) or self.deadline_epoch <= 0:
            raise ValueError("deadline_epoch must be a finite positive epoch")
        for field in ("preexisting_instance_ids", "preexisting_volume_ids"):
            values = getattr(self, field)
            if not isinstance(values, tuple) or any(not isinstance(item, str) or not item for item in values):
                raise ValueError(f"{field} must be a tuple of non-empty strings")
            if len(set(values)) != len(values):
                raise ValueError(f"{field} cannot contain duplicate IDs")

    @property
    def state(self) -> str:
        return "CREATE_INTENT"

    @property
    def digest(self) -> str:
        payload = {
            "state": self.state,
            "request_id": self.request_id,
            "operation_id": self.operation_id,
            "attempt_id": self.attempt_id,
            "attempt_label": self.attempt_label,
            "operation_fence": self.operation_fence,
            "deadline_epoch": self.deadline_epoch,
            "initiating_host_id": self.initiating_host_id,
            "preexisting_instance_ids": sorted(self.preexisting_instance_ids),
            "preexisting_volume_ids": sorted(self.preexisting_volume_ids),
        }
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class FenceReservation:
    registry_id: str
    request_id: str
    attempt_id: str
    operation_fence: int
    durable: bool


@dataclass(frozen=True, slots=True)
class GuardianAck:
    guardian_id: str
    request_id: str
    operation_fence: int
    intent_digest: str
    registry_id: str
    control_host_id: str
    failure_domain_id: str
    capabilities: GuardianCapabilities
    read_revision: int


@dataclass(frozen=True, slots=True)
class LeaseRegistrySnapshot:
    """Durable lease view, including immutable create-discovery metadata.

    Independent guardians need the attempt label and pre-create resource baseline
    to reconcile an ambiguous provider create without using the initiating host's
    journal or claiming pre-existing resources.
    """

    registry_id: str
    request_id: str
    operation_fence: int
    intent_digest: str
    state: str
    deadline_epoch: float
    attempt_label: str
    preexisting_instance_ids: tuple[str, ...]
    preexisting_volume_ids: tuple[str, ...]
    durable: bool
    revision: int = 0
    guardian_acks: tuple[GuardianAck, ...] = ()
    owned_instance_ids: tuple[str, ...] = ()
    owned_volume_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class GuardianReadinessReceipt:
    registry_id: str
    request_id: str
    operation_id: str
    attempt_id: str
    operation_fence: int
    intent_digest: str
    deadline_epoch: float
    guardian_acks: tuple[GuardianAck, ...]
    declared_failure_domains: tuple[str, ...]
    physical_separation_proven: bool = False


@dataclass(frozen=True, slots=True)
class GuardianRecoveryReceipt:
    request_id: str
    operation_fence: int
    guardian_id: str
    failure_domain_id: str
    state: RecoveryState
    destroy_requests_instance_ids: tuple[str, ...] = ()
    destroy_requests_volume_ids: tuple[str, ...] = ()
    unresolved_instance_ids: tuple[str, ...] = ()
    unresolved_volume_ids: tuple[str, ...] = ()
    provider_absence_confirmed: bool = False
    alert_receipt_id: str | None = None
    hard_deletion_guaranteed: bool = False


class GuardianReadinessError(RuntimeError):
    """A fail-closed pre-create or post-create registry boundary error."""

    def __init__(self, reason_code: str, message: str, *, cleanup_required: bool = False):
        super().__init__(message)
        self.reason_code = reason_code
        self.cleanup_required = cleanup_required
        self.hard_deletion_guaranteed = False


class SharedLeaseRegistry(Protocol):
    """Durable CAS-backed registry reachable from both independent guardians.

    ``reserve_fence`` must atomically bind this request/attempt to the supplied
    positive monotonic fence. Repeating the same request and attempt is
    idempotent; a stale fence or a fence owned by another attempt is rejected.
    ``read`` must return the exact durable attempt label and pre-create resource
    baseline with every later snapshot. Resource publication is a monotonic union
    under the same fence.
    """

    @property
    def registry_id(self) -> str: ...

    def reserve_fence(self, *, request_id: str, attempt_id: str, operation_fence: int) -> FenceReservation: ...
    def publish_intent(self, intent: LeaseCreateIntent) -> LeaseRegistrySnapshot: ...
    def read(self, request_id: str) -> LeaseRegistrySnapshot | None: ...
    def acknowledge_guardian(self, ack: GuardianAck) -> LeaseRegistrySnapshot: ...
    def publish_owned_resources(
        self,
        *,
        request_id: str,
        operation_fence: int,
        intent_digest: str,
        instance_ids: tuple[str, ...],
        volume_ids: tuple[str, ...],
    ) -> LeaseRegistrySnapshot: ...


class RecoveryGuardian(Protocol):
    """Guardian adapter with its own registry credentials and recovery worker."""

    @property
    def descriptor(self) -> GuardianDescriptor: ...

    def acknowledge_intent(
        self,
        *,
        registry_id: str,
        request_id: str,
        operation_fence: int,
        intent_digest: str,
    ) -> GuardianAck: ...

    def reconcile(self, *, request_id: str, operation_fence: int) -> GuardianRecoveryReceipt: ...


T = TypeVar("T")


class GuardianReadinessGate:
    """Require two distinct registered recovery paths before provider create."""

    def __init__(
        self,
        registry: SharedLeaseRegistry | None,
        guardians: tuple[RecoveryGuardian, ...] | list[RecoveryGuardian],
        *,
        clock: Callable[[], float],
    ) -> None:
        self.registry = registry
        self.guardians = tuple(guardians)
        self.clock = clock
        self._armed: dict[str, GuardianReadinessReceipt] = {}

    def arm_before_create(self, intent: LeaseCreateIntent) -> GuardianReadinessReceipt:
        """Durably reserve, publish, and read back the exact intent and both acks."""
        registry = self.registry
        if registry is None:
            raise GuardianReadinessError("shared_registry_unavailable", "shared lease registry is required before create")
        if intent.state != "CREATE_INTENT" or intent.deadline_epoch <= self.clock():
            raise GuardianReadinessError("invalid_create_intent", "a future-dated CREATE_INTENT is required before create")
        descriptors = self._eligible_guardians(intent)
        registry_id = self._registry_id(registry)
        try:
            reservation = registry.reserve_fence(
                request_id=intent.request_id,
                attempt_id=intent.attempt_id,
                operation_fence=intent.operation_fence,
            )
        except Exception:
            raise GuardianReadinessError("fence_reservation_failed", "shared registry could not reserve the operation fence") from None
        if not (
            isinstance(reservation, FenceReservation)
            and reservation.durable is True
            and reservation.registry_id == registry_id
            and reservation.request_id == intent.request_id
            and reservation.attempt_id == intent.attempt_id
            and reservation.operation_fence == intent.operation_fence
        ):
            raise GuardianReadinessError("fence_reservation_invalid", "shared registry returned a mismatched or non-durable fence")
        try:
            written = registry.publish_intent(intent)
        except Exception:
            raise GuardianReadinessError("intent_publication_failed", "shared registry could not durably publish CREATE_INTENT") from None
        self._validate_snapshot(written, intent, registry_id, require_intent_state=True)
        self._readback(intent, registry_id, require_intent_state=True)

        acknowledgements: list[GuardianAck] = []
        for guardian, descriptor in descriptors:
            try:
                ack = guardian.acknowledge_intent(
                    registry_id=registry_id,
                    request_id=intent.request_id,
                    operation_fence=intent.operation_fence,
                    intent_digest=intent.digest,
                )
            except Exception:
                raise GuardianReadinessError(
                    "guardian_acknowledgement_failed",
                    "a recovery guardian could not read and acknowledge the shared CREATE_INTENT",
                ) from None
            self._validate_ack(ack, descriptor, intent, registry_id, minimum_revision=written.revision)
            try:
                acknowledged = registry.acknowledge_guardian(ack)
            except Exception:
                raise GuardianReadinessError("guardian_ack_persist_failed", "shared registry could not persist a guardian acknowledgement") from None
            self._validate_snapshot(acknowledged, intent, registry_id, require_intent_state=True)
            if not any(item.guardian_id == ack.guardian_id and item == ack for item in acknowledged.guardian_acks):
                raise GuardianReadinessError("guardian_ack_readback_failed", "shared registry did not retain the guardian acknowledgement")
            acknowledgements.append(ack)

        final = self._readback(intent, registry_id, require_intent_state=True)
        persisted = {item.guardian_id: item for item in final.guardian_acks}
        if any(persisted.get(ack.guardian_id) != ack for ack in acknowledgements):
            raise GuardianReadinessError("guardian_ack_readback_failed", "shared registry readback is missing a guardian acknowledgement")
        receipt = GuardianReadinessReceipt(
            registry_id=registry_id,
            request_id=intent.request_id,
            operation_id=intent.operation_id,
            attempt_id=intent.attempt_id,
            operation_fence=intent.operation_fence,
            intent_digest=intent.digest,
            deadline_epoch=intent.deadline_epoch,
            guardian_acks=tuple(acknowledgements),
            declared_failure_domains=tuple(descriptor.failure_domain_id for _, descriptor in descriptors),
            physical_separation_proven=False,
        )
        self._armed[intent.request_id] = receipt
        return receipt

    def publish_owned_resources(
        self,
        intent: LeaseCreateIntent,
        readiness: GuardianReadinessReceipt,
        *,
        instance_ids: tuple[str, ...],
        volume_ids: tuple[str, ...],
    ) -> LeaseRegistrySnapshot:
        """Publish returned IDs under the same fence and require registry readback."""
        registry = self.registry
        if registry is None:
            raise GuardianReadinessError(
                "shared_registry_unavailable_after_create",
                "shared lease registry is unavailable after create; cleanup remains required",
                cleanup_required=True,
            )
        if self._armed.get(intent.request_id) != readiness or not self._receipt_matches(intent, readiness):
            raise GuardianReadinessError(
                "readiness_receipt_mismatch",
                "owned resources do not match the armed readiness receipt",
                cleanup_required=True,
            )
        instance_ids = self._resource_ids(instance_ids, intent.preexisting_instance_ids, "instance")
        volume_ids = self._resource_ids(volume_ids, intent.preexisting_volume_ids, "volume")
        registry_id = self._registry_id(registry)
        try:
            current = registry.read(intent.request_id)
            self._validate_snapshot(current, intent, registry_id, require_intent_state=False)
            for ack in readiness.guardian_acks:
                if ack not in current.guardian_acks:
                    raise GuardianReadinessError(
                        "guardian_ack_readback_failed",
                        "a guardian acknowledgement is missing before resource publication",
                        cleanup_required=True,
                    )
            published = registry.publish_owned_resources(
                request_id=intent.request_id,
                operation_fence=intent.operation_fence,
                intent_digest=intent.digest,
                instance_ids=instance_ids,
                volume_ids=volume_ids,
            )
            self._validate_snapshot(published, intent, registry_id, require_intent_state=False)
            readback = registry.read(intent.request_id)
            self._validate_snapshot(readback, intent, registry_id, require_intent_state=False)
            if not set(instance_ids).issubset(readback.owned_instance_ids) or not set(volume_ids).issubset(readback.owned_volume_ids):
                raise GuardianReadinessError(
                    "owned_resource_readback_failed",
                    "shared registry readback is missing an owned resource ID",
                    cleanup_required=True,
                )
            for ack in readiness.guardian_acks:
                if ack not in readback.guardian_acks:
                    raise GuardianReadinessError(
                        "guardian_ack_readback_failed",
                        "a guardian acknowledgement is missing after resource publication",
                        cleanup_required=True,
                    )
            return readback
        except GuardianReadinessError as exc:
            if exc.cleanup_required:
                raise
            raise GuardianReadinessError(exc.reason_code, str(exc), cleanup_required=True) from None
        except Exception:
            raise GuardianReadinessError(
                "owned_resource_publication_failed",
                "shared registry did not durably publish and read back owned resources; cleanup remains required",
                cleanup_required=True,
            ) from None

    def _eligible_guardians(self, intent: LeaseCreateIntent) -> list[tuple[RecoveryGuardian, GuardianDescriptor]]:
        eligible: list[tuple[RecoveryGuardian, GuardianDescriptor]] = []
        for guardian in self.guardians:
            try:
                descriptor = guardian.descriptor
            except Exception:
                continue
            if not isinstance(descriptor, GuardianDescriptor) or descriptor.kind != GuardianKind.INDEPENDENT_SERVICE:
                continue
            eligible.append((guardian, descriptor))
        if len(eligible) < 2:
            raise GuardianReadinessError(
                "two_guardians_required",
                "two independent recovery guardians are required before create",
            )
        descriptors = [descriptor for _, descriptor in eligible]
        if any(
            not isinstance(item.guardian_id, str) or not item.guardian_id.strip()
            or not isinstance(item.control_host_id, str) or not item.control_host_id.strip()
            or not isinstance(item.failure_domain_id, str) or not item.failure_domain_id.strip()
            or item.trusted is not True
            or not isinstance(item.capabilities, GuardianCapabilities)
            or not item.capabilities.recovery_complete
            for item in descriptors
        ):
            raise GuardianReadinessError("guardian_not_trusted_or_capable", "each recovery guardian must be trusted and able to reconcile, clean, verify, and alert")
        if len({item.guardian_id for item in descriptors}) != len(descriptors):
            raise GuardianReadinessError("guardian_ids_not_distinct", "recovery guardian IDs must be distinct")
        if any(item.control_host_id == intent.initiating_host_id for item in descriptors):
            raise GuardianReadinessError("guardian_shares_initiating_host", "a guardian on the initiating control host cannot count toward P45")
        if len({item.control_host_id for item in descriptors}) != len(descriptors):
            raise GuardianReadinessError("control_hosts_not_distinct", "recovery guardians must declare distinct control hosts")
        if len({item.failure_domain_id for item in descriptors}) != len(descriptors):
            raise GuardianReadinessError("failure_domains_not_distinct", "recovery guardians must declare distinct failure domains")
        return eligible

    @staticmethod
    def _registry_id(registry: SharedLeaseRegistry) -> str:
        registry_id = getattr(registry, "registry_id", None)
        if not isinstance(registry_id, str) or not registry_id.strip():
            raise GuardianReadinessError("shared_registry_unavailable", "shared registry has no stable identity")
        return registry_id

    @staticmethod
    def _validate_snapshot(
        snapshot: LeaseRegistrySnapshot | None,
        intent: LeaseCreateIntent,
        registry_id: str,
        *,
        require_intent_state: bool,
    ) -> LeaseRegistrySnapshot:
        if not isinstance(snapshot, LeaseRegistrySnapshot):
            raise GuardianReadinessError("registry_readback_failed", "shared registry returned no valid lease record")
        if snapshot.durable is not True or snapshot.registry_id != registry_id:
            raise GuardianReadinessError("registry_readback_failed", "shared registry durability or identity could not be verified")
        if snapshot.request_id != intent.request_id:
            raise GuardianReadinessError("registry_request_mismatch", "shared registry returned a different request")
        if snapshot.operation_fence != intent.operation_fence:
            raise GuardianReadinessError("registry_fence_mismatch", "shared registry fence does not match CREATE_INTENT")
        if snapshot.intent_digest != intent.digest or snapshot.deadline_epoch != intent.deadline_epoch:
            raise GuardianReadinessError("registry_intent_mismatch", "shared registry digest or deadline does not match CREATE_INTENT")
        if (
            snapshot.attempt_label != intent.attempt_label
            or snapshot.preexisting_instance_ids != intent.preexisting_instance_ids
            or snapshot.preexisting_volume_ids != intent.preexisting_volume_ids
        ):
            raise GuardianReadinessError(
                "registry_intent_mismatch",
                "shared registry attempt label or pre-existing resource baseline does not match CREATE_INTENT",
            )
        if require_intent_state and snapshot.state != "CREATE_INTENT":
            raise GuardianReadinessError("registry_state_mismatch", "shared registry record is not in CREATE_INTENT")
        if isinstance(snapshot.revision, bool) or not isinstance(snapshot.revision, int) or snapshot.revision < 0:
            raise GuardianReadinessError("registry_revision_invalid", "shared registry revision is invalid")
        if not isinstance(snapshot.guardian_acks, tuple) or any(not isinstance(item, GuardianAck) for item in snapshot.guardian_acks):
            raise GuardianReadinessError("registry_acknowledgements_invalid", "shared registry acknowledgements are malformed")
        if (
            not isinstance(snapshot.owned_instance_ids, tuple)
            or not isinstance(snapshot.owned_volume_ids, tuple)
            or any(not isinstance(item, str) or not item for item in (*snapshot.owned_instance_ids, *snapshot.owned_volume_ids))
        ):
            raise GuardianReadinessError("registry_resources_invalid", "shared registry owned resource IDs are malformed")
        for field_name, values in (
            ("pre-existing instance IDs", snapshot.preexisting_instance_ids),
            ("pre-existing volume IDs", snapshot.preexisting_volume_ids),
        ):
            if (
                not isinstance(values, tuple)
                or any(not isinstance(item, str) or not item for item in values)
                or len(set(values)) != len(values)
            ):
                raise GuardianReadinessError(
                    "registry_intent_invalid",
                    f"shared registry {field_name} are malformed",
                )
        if not isinstance(snapshot.attempt_label, str) or not snapshot.attempt_label.strip():
            raise GuardianReadinessError("registry_intent_invalid", "shared registry attempt label is malformed")
        if set(snapshot.owned_instance_ids) & set(snapshot.preexisting_instance_ids):
            raise GuardianReadinessError("registry_resources_invalid", "shared registry marks a pre-existing instance as owned")
        if set(snapshot.owned_volume_ids) & set(snapshot.preexisting_volume_ids):
            raise GuardianReadinessError("registry_resources_invalid", "shared registry marks a pre-existing volume as owned")
        return snapshot

    def _readback(
        self,
        intent: LeaseCreateIntent,
        registry_id: str,
        *,
        require_intent_state: bool,
    ) -> LeaseRegistrySnapshot:
        try:
            snapshot = self.registry.read(intent.request_id) if self.registry else None
        except Exception:
            raise GuardianReadinessError("registry_readback_failed", "shared registry readback failed") from None
        return self._validate_snapshot(snapshot, intent, registry_id, require_intent_state=require_intent_state)

    @staticmethod
    def _validate_ack(
        ack: GuardianAck,
        descriptor: GuardianDescriptor,
        intent: LeaseCreateIntent,
        registry_id: str,
        *,
        minimum_revision: int,
    ) -> None:
        if not isinstance(ack, GuardianAck):
            raise GuardianReadinessError("guardian_ack_invalid", "guardian returned an invalid acknowledgement")
        if (
            ack.guardian_id != descriptor.guardian_id
            or ack.control_host_id != descriptor.control_host_id
            or ack.failure_domain_id != descriptor.failure_domain_id
            or ack.registry_id != registry_id
            or ack.request_id != intent.request_id
            or ack.operation_fence != intent.operation_fence
            or ack.intent_digest != intent.digest
            or ack.capabilities != descriptor.capabilities
            or not isinstance(ack.capabilities, GuardianCapabilities)
            or not ack.capabilities.recovery_complete
            or isinstance(ack.read_revision, bool)
            or not isinstance(ack.read_revision, int)
            or ack.read_revision < minimum_revision
        ):
            raise GuardianReadinessError("guardian_ack_invalid", "guardian acknowledgement does not match the fenced shared intent")

    @staticmethod
    def _resource_ids(values: tuple[str, ...], preexisting: tuple[str, ...], kind: str) -> tuple[str, ...]:
        if not isinstance(values, tuple) or any(not isinstance(value, str) or not value.strip() for value in values):
            raise GuardianReadinessError("owned_resource_ids_invalid", f"owned {kind} IDs must be non-empty strings", cleanup_required=True)
        if len(set(values)) != len(values):
            raise GuardianReadinessError("owned_resource_ids_invalid", f"owned {kind} IDs must be unique", cleanup_required=True)
        if set(values) & set(preexisting):
            raise GuardianReadinessError("owned_resource_not_owned", f"a pre-existing {kind} cannot be published as request-owned", cleanup_required=True)
        return tuple(sorted(values))

    @staticmethod
    def _receipt_matches(intent: LeaseCreateIntent, receipt: GuardianReadinessReceipt) -> bool:
        return (
            isinstance(receipt, GuardianReadinessReceipt)
            and receipt.request_id == intent.request_id
            and receipt.operation_id == intent.operation_id
            and receipt.attempt_id == intent.attempt_id
            and receipt.operation_fence == intent.operation_fence
            and receipt.intent_digest == intent.digest
            and receipt.deadline_epoch == intent.deadline_epoch
            and receipt.physical_separation_proven is False
            and len(receipt.guardian_acks) >= 2
        )


def validate_guardian_recovery_receipt(
    receipt: GuardianRecoveryReceipt,
    *,
    descriptor: GuardianDescriptor,
    request_id: str,
    operation_fence: int,
    owned_instance_ids: tuple[str, ...],
    owned_volume_ids: tuple[str, ...],
) -> GuardianRecoveryReceipt:
    """Reject recovery receipts that overstate cleanup or name unrelated IDs."""
    def fail(reason: str, message: str) -> None:
        raise GuardianReadinessError(reason, message, cleanup_required=True)

    if not isinstance(receipt, GuardianRecoveryReceipt):
        fail("recovery_receipt_invalid", "guardian returned no valid recovery receipt")
    if (
        receipt.request_id != request_id
        or receipt.operation_fence != operation_fence
        or receipt.guardian_id != descriptor.guardian_id
        or receipt.failure_domain_id != descriptor.failure_domain_id
    ):
        fail("recovery_receipt_mismatch", "guardian recovery receipt does not match the request fence and guardian")
    if receipt.hard_deletion_guaranteed is not False:
        fail("hard_deletion_claim_rejected", "a guardian cannot claim provider-enforced or hard deletion")
    if not isinstance(receipt.alert_receipt_id, str) or not receipt.alert_receipt_id.strip():
        fail("recovery_alert_missing", "guardian recovery must retain an alert or completion receipt")

    resource_groups = (
        (receipt.destroy_requests_instance_ids, owned_instance_ids, "instance"),
        (receipt.destroy_requests_volume_ids, owned_volume_ids, "volume"),
        (receipt.unresolved_instance_ids, owned_instance_ids, "instance"),
        (receipt.unresolved_volume_ids, owned_volume_ids, "volume"),
    )
    for reported, owned, kind in resource_groups:
        if (
            not isinstance(reported, tuple)
            or any(not isinstance(resource_id, str) or not resource_id for resource_id in reported)
            or len(set(reported)) != len(reported)
        ):
            fail("recovery_resource_ids_invalid", f"guardian reported invalid {kind} IDs")
        if not set(reported).issubset(owned):
            fail("recovery_unowned_resource_rejected", f"guardian receipt names a request-unowned {kind}")

    if receipt.state == RecoveryState.CLEANUP_PENDING:
        if receipt.provider_absence_confirmed is not False:
            fail("recovery_state_mismatch", "CLEANUP_PENDING cannot claim provider absence")
    elif receipt.state == RecoveryState.VERIFIED_ABSENT:
        if receipt.provider_absence_confirmed is not True or receipt.unresolved_instance_ids or receipt.unresolved_volume_ids:
            fail("recovery_state_mismatch", "VERIFIED_ABSENT requires fresh provider absence evidence and no unresolved IDs")
    else:
        fail("recovery_state_invalid", "guardian returned an unsupported recovery state")
    return receipt


def run_after_guardian_arm(
    gate: GuardianReadinessGate,
    intent: LeaseCreateIntent,
    create: Callable[[GuardianReadinessReceipt], T],
) -> tuple[GuardianReadinessReceipt, T]:
    """Call the provider only after the fail-closed guardian gate succeeds."""
    receipt = gate.arm_before_create(intent)
    return receipt, create(receipt)
