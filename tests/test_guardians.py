from __future__ import annotations

from dataclasses import replace

import pytest

from vast_broker.guardians import (
    GuardianAck,
    GuardianCapabilities,
    GuardianDescriptor,
    GuardianKind,
    GuardianReadinessError,
    GuardianReadinessGate,
    LeaseCreateIntent,
    LeaseRegistrySnapshot,
    GuardianRecoveryReceipt,
    RecoveryState,
    validate_guardian_recovery_receipt,
)


class FakeRegistry:
    registry_id = "shared-registry"

    def __init__(self):
        self.snapshot: LeaseRegistrySnapshot | None = None
        self.last_fence_by_request: dict[str, tuple[str, int]] = {}
        self.publish_calls = 0
        self.fence_calls = 0
        self.ack_calls = 0
        self.resource_calls = 0
        self.fail_publish = False
        self.fail_read = False
        self.drop_ack = False
        self.fail_resource_publish = False
        self.mismatch_readback = False

    def reserve_fence(self, *, request_id, attempt_id, operation_fence):
        from vast_broker.guardians import FenceReservation

        self.fence_calls += 1
        previous = self.last_fence_by_request.get(request_id)
        if previous:
            previous_attempt, previous_fence = previous
            if previous_attempt == attempt_id and previous_fence == operation_fence:
                return FenceReservation(self.registry_id, request_id, attempt_id, operation_fence, True)
            if operation_fence <= previous_fence:
                raise RuntimeError("stale operation fence")
        self.last_fence_by_request[request_id] = (attempt_id, operation_fence)
        return FenceReservation(self.registry_id, request_id, attempt_id, operation_fence, True)

    def publish_intent(self, intent: LeaseCreateIntent) -> LeaseRegistrySnapshot:
        self.publish_calls += 1
        if self.fail_publish:
            raise TimeoutError("registry write unavailable")
        self.snapshot = LeaseRegistrySnapshot(
            registry_id=self.registry_id,
            request_id=intent.request_id,
            operation_fence=intent.operation_fence,
            intent_digest=intent.digest,
            state="CREATE_INTENT",
            deadline_epoch=intent.deadline_epoch,
            durable=True,
        )
        return self.snapshot

    def read(self, request_id: str) -> LeaseRegistrySnapshot | None:
        if self.fail_read:
            raise TimeoutError("registry read unavailable")
        if not self.snapshot or self.snapshot.request_id != request_id:
            return None
        if self.mismatch_readback:
            return replace(self.snapshot, operation_fence=self.snapshot.operation_fence + 1)
        return self.snapshot

    def acknowledge_guardian(self, ack: GuardianAck) -> LeaseRegistrySnapshot:
        self.ack_calls += 1
        if not self.snapshot:
            raise RuntimeError("intent is not present")
        if not self.drop_ack:
            acks = {item.guardian_id: item for item in self.snapshot.guardian_acks}
            acks[ack.guardian_id] = ack
            self.snapshot = replace(self.snapshot, guardian_acks=tuple(acks.values()))
        return self.snapshot

    def publish_owned_resources(
        self,
        *,
        request_id: str,
        operation_fence: int,
        intent_digest: str,
        instance_ids: tuple[str, ...],
        volume_ids: tuple[str, ...],
    ) -> LeaseRegistrySnapshot:
        self.resource_calls += 1
        if self.fail_resource_publish:
            raise TimeoutError("registry resource update unavailable")
        current = self.snapshot
        if (not current or current.request_id != request_id
                or current.operation_fence != operation_fence
                or current.intent_digest != intent_digest):
            raise RuntimeError("fenced registry record changed")
        self.snapshot = replace(
            current,
            owned_instance_ids=tuple(sorted(set(current.owned_instance_ids) | set(instance_ids))),
            owned_volume_ids=tuple(sorted(set(current.owned_volume_ids) | set(volume_ids))),
            revision=current.revision + 1,
        )
        return self.snapshot


class FakeGuardian:
    def __init__(self, guardian_id: str, host: str, domain: str, registry: FakeRegistry,
                 *, kind=GuardianKind.INDEPENDENT_SERVICE, readable=True, ack_changes=None):
        self.descriptor = GuardianDescriptor(
            guardian_id=guardian_id,
            control_host_id=host,
            failure_domain_id=domain,
            kind=kind,
            trusted=True,
            capabilities=GuardianCapabilities(
                reconcile_ambiguous_create=True,
                destroy_owned_instances=True,
                destroy_owned_volumes=True,
                verify_provider_absence=True,
                emit_actionable_alert=True,
            ),
        )
        self.registry = registry
        self.readable = readable
        self.ack_changes = ack_changes or {}
        self.calls = 0

    def acknowledge_intent(self, *, registry_id, request_id, operation_fence, intent_digest):
        self.calls += 1
        if not self.readable:
            raise TimeoutError("guardian cannot read shared registry")
        assert registry_id == self.registry.registry_id
        record = self.registry.read(request_id)
        if record is None:
            raise RuntimeError("shared lease intent is missing")
        values = {
            "guardian_id": self.descriptor.guardian_id,
            "request_id": record.request_id,
            "operation_fence": record.operation_fence,
            "intent_digest": record.intent_digest,
            "registry_id": record.registry_id,
            "control_host_id": self.descriptor.control_host_id,
            "failure_domain_id": self.descriptor.failure_domain_id,
            "capabilities": self.descriptor.capabilities,
            "read_revision": record.revision,
        }
        values.update(self.ack_changes)
        return GuardianAck(**values)


def intent(**changes) -> LeaseCreateIntent:
    values = {
        "request_id": "request-1",
        "operation_id": "operation-1",
        "attempt_id": "attempt-1",
        "attempt_label": "vbr-request-attempt",
        "operation_fence": 7,
        "deadline_epoch": 500.0,
        "initiating_host_id": "agent-host",
        "preexisting_instance_ids": ("unrelated",),
        "preexisting_volume_ids": (),
    }
    values.update(changes)
    return LeaseCreateIntent(**values)


def two_guardians(registry: FakeRegistry, **kwargs):
    return [
        FakeGuardian("guardian-a", "host-a", "domain-a", registry, **kwargs),
        FakeGuardian("guardian-b", "host-b", "domain-b", registry, **kwargs),
    ]


def test_arm_requires_durable_intent_and_two_guardian_readbacks_then_publishes_resources():
    registry = FakeRegistry()
    guardians = two_guardians(registry)
    gate = GuardianReadinessGate(registry, guardians, clock=lambda: 100.0)

    ready = gate.arm_before_create(intent())

    assert registry.publish_calls == 1
    assert registry.ack_calls == 2
    assert {ack.guardian_id for ack in ready.guardian_acks} == {"guardian-a", "guardian-b"}
    assert ready.operation_fence == 7
    assert ready.intent_digest == intent().digest
    assert ready.physical_separation_proven is False
    assert {ack.guardian_id for ack in registry.read("request-1").guardian_acks} == {
        "guardian-a", "guardian-b"
    }

    published = gate.publish_owned_resources(
        intent(), ready, instance_ids=("instance-1",), volume_ids=("volume-1",)
    )
    assert published.owned_instance_ids == ("instance-1",)
    assert published.owned_volume_ids == ("volume-1",)
    assert registry.read("request-1").owned_instance_ids == ("instance-1",)


@pytest.mark.parametrize(
    "guardian_factory,reason",
    [
        (lambda registry: [], "two_guardians_required"),
        (lambda registry: [FakeGuardian("only", "one", "one", registry)], "two_guardians_required"),
        (
            lambda registry: [
                FakeGuardian("a", "host-a", "same-domain", registry),
                FakeGuardian("b", "host-b", "same-domain", registry),
            ],
            "failure_domains_not_distinct",
        ),
        (
            lambda registry: [
                FakeGuardian("a", "same-host", "domain-a", registry),
                FakeGuardian("b", "same-host", "domain-b", registry),
            ],
            "control_hosts_not_distinct",
        ),
        (
            lambda registry: [
                FakeGuardian("a", "agent-host", "domain-a", registry),
                FakeGuardian("b", "host-b", "domain-b", registry),
            ],
            "guardian_shares_initiating_host",
        ),
    ],
)
def test_missing_or_duplicate_guardian_domains_fail_before_registry_write(guardian_factory, reason):
    registry = FakeRegistry()
    gate = GuardianReadinessGate(registry, guardian_factory(registry), clock=lambda: 100.0)

    with pytest.raises(GuardianReadinessError) as error:
        gate.arm_before_create(intent())

    assert error.value.reason_code == reason
    assert registry.publish_calls == 0
    assert registry.fence_calls == 0


@pytest.mark.parametrize(
    "kind",
    [GuardianKind.LOCAL_BROWSER_SESSION, GuardianKind.SCHEDULED_WORKFLOW, GuardianKind.SAME_HOST_PROCESS],
)
def test_browser_actions_and_same_host_process_cannot_be_the_only_guardians(kind):
    registry = FakeRegistry()
    guardians = [
        FakeGuardian("a", "browser-host-a", "domain-a", registry, kind=kind),
        FakeGuardian("b", "browser-host-b", "domain-b", registry, kind=kind),
    ]
    gate = GuardianReadinessGate(registry, guardians, clock=lambda: 100.0)

    with pytest.raises(GuardianReadinessError, match="independent recovery guardians"):
        gate.arm_before_create(intent())
    assert registry.publish_calls == 0


def test_missing_registry_fails_closed_without_contacting_guardians():
    registry = FakeRegistry()
    guardians = two_guardians(registry)
    gate = GuardianReadinessGate(None, guardians, clock=lambda: 100.0)

    with pytest.raises(GuardianReadinessError) as error:
        gate.arm_before_create(intent())

    assert error.value.reason_code == "shared_registry_unavailable"
    assert all(guardian.calls == 0 for guardian in guardians)


def test_registry_write_or_read_outage_fails_closed():
    registry = FakeRegistry()
    registry.fail_publish = True
    guardians = two_guardians(registry)
    gate = GuardianReadinessGate(registry, guardians, clock=lambda: 100.0)
    with pytest.raises(GuardianReadinessError, match="registry"):
        gate.arm_before_create(intent())
    assert all(guardian.calls == 0 for guardian in guardians)

    registry = FakeRegistry()
    registry.fail_read = True
    guardians = two_guardians(registry)
    gate = GuardianReadinessGate(registry, guardians, clock=lambda: 100.0)
    with pytest.raises(GuardianReadinessError):
        gate.arm_before_create(intent())
    assert registry.ack_calls == 0


@pytest.mark.parametrize("ack_changes", [{"intent_digest": "wrong"}, {"operation_fence": 8}])
def test_guardian_ack_must_match_exact_fence_and_intent_digest(ack_changes):
    registry = FakeRegistry()
    guardians = [
        FakeGuardian("guardian-a", "host-a", "domain-a", registry, ack_changes=ack_changes),
        FakeGuardian("guardian-b", "host-b", "domain-b", registry),
    ]
    gate = GuardianReadinessGate(registry, guardians, clock=lambda: 100.0)

    with pytest.raises(GuardianReadinessError, match="acknowledgement"):
        gate.arm_before_create(intent())


def test_registry_must_persist_and_read_back_both_acknowledgements():
    registry = FakeRegistry()
    registry.drop_ack = True
    gate = GuardianReadinessGate(registry, two_guardians(registry), clock=lambda: 100.0)

    with pytest.raises(GuardianReadinessError, match="acknowledgement"):
        gate.arm_before_create(intent())


def test_shared_record_digest_and_fence_are_checked_on_readback():
    registry = FakeRegistry()
    registry.mismatch_readback = True
    gate = GuardianReadinessGate(registry, two_guardians(registry), clock=lambda: 100.0)

    with pytest.raises(GuardianReadinessError, match="fence"):
        gate.arm_before_create(intent())


def test_stale_fence_is_rejected_before_intent_publication():
    registry = FakeRegistry()
    registry.last_fence_by_request["request-1"] = ("prior-attempt", 7)
    gate = GuardianReadinessGate(registry, two_guardians(registry), clock=lambda: 100.0)

    with pytest.raises(GuardianReadinessError) as error:
        gate.arm_before_create(intent(operation_fence=6))

    assert error.value.reason_code == "fence_reservation_failed"
    assert registry.publish_calls == 0


def test_guardian_registry_read_failure_blocks_create_even_after_intent_was_written():
    registry = FakeRegistry()
    guardians = two_guardians(registry)
    guardians[1].readable = False
    gate = GuardianReadinessGate(registry, guardians, clock=lambda: 100.0)

    with pytest.raises(GuardianReadinessError) as error:
        gate.arm_before_create(intent())

    assert error.value.reason_code == "guardian_acknowledgement_failed"
    assert registry.snapshot.state == "CREATE_INTENT"
    assert [ack.guardian_id for ack in registry.snapshot.guardian_acks] == ["guardian-a"]
    assert error.value.hard_deletion_guaranteed is False


def test_precreate_failure_never_calls_provider_create():
    registry = FakeRegistry()
    registry.drop_ack = True
    gate = GuardianReadinessGate(registry, two_guardians(registry), clock=lambda: 100.0)
    create_calls = []

    from vast_broker.guardians import run_after_guardian_arm

    with pytest.raises(GuardianReadinessError):
        run_after_guardian_arm(gate, intent(), lambda _: create_calls.append("create"))

    assert create_calls == []


def test_post_create_owned_resource_publish_is_fenced_verified_and_fails_closed():
    registry = FakeRegistry()
    gate = GuardianReadinessGate(registry, two_guardians(registry), clock=lambda: 100.0)
    ready = gate.arm_before_create(intent())
    registry.fail_resource_publish = True

    with pytest.raises(GuardianReadinessError) as error:
        gate.publish_owned_resources(intent(), ready, instance_ids=("instance-1",), volume_ids=())

    assert error.value.reason_code == "owned_resource_publication_failed"
    assert error.value.cleanup_required is True
    assert error.value.hard_deletion_guaranteed is False


def test_post_create_publication_rejects_preexisting_or_unfenced_resource_ids():
    registry = FakeRegistry()
    gate = GuardianReadinessGate(registry, two_guardians(registry), clock=lambda: 100.0)
    ready = gate.arm_before_create(intent())

    with pytest.raises(GuardianReadinessError, match="pre-existing"):
        gate.publish_owned_resources(intent(), ready, instance_ids=("unrelated",), volume_ids=())
    with pytest.raises(GuardianReadinessError, match="readiness receipt"):
        gate.publish_owned_resources(
            intent(operation_fence=8), ready, instance_ids=("instance-1",), volume_ids=()
        )


def test_cleanup_pending_recovery_receipt_preserves_outage_and_cost_uncertainty():
    guardian = FakeGuardian("guardian-a", "host-a", "domain-a", FakeRegistry())
    receipt = GuardianRecoveryReceipt(
        request_id="request-1",
        operation_fence=7,
        guardian_id="guardian-a",
        failure_domain_id="domain-a",
        state=RecoveryState.CLEANUP_PENDING,
        unresolved_instance_ids=("instance-1",),
        alert_receipt_id="alert-1",
        provider_absence_confirmed=False,
        hard_deletion_guaranteed=False,
    )

    validated = validate_guardian_recovery_receipt(
        receipt,
        descriptor=guardian.descriptor,
        request_id="request-1",
        operation_fence=7,
        owned_instance_ids=("instance-1",),
        owned_volume_ids=(),
    )

    assert validated.state == RecoveryState.CLEANUP_PENDING
    assert validated.hard_deletion_guaranteed is False


def test_recovery_receipt_cannot_claim_hard_deletion_or_name_unowned_resources():
    guardian = FakeGuardian("guardian-a", "host-a", "domain-a", FakeRegistry())
    receipt = GuardianRecoveryReceipt(
        request_id="request-1",
        operation_fence=7,
        guardian_id="guardian-a",
        failure_domain_id="domain-a",
        state=RecoveryState.CLEANUP_PENDING,
        destroy_requests_instance_ids=("somebody-elses-instance",),
        alert_receipt_id="alert-1",
        hard_deletion_guaranteed=False,
    )
    common = {
        "descriptor": guardian.descriptor,
        "request_id": "request-1",
        "operation_fence": 7,
        "owned_instance_ids": ("instance-1",),
        "owned_volume_ids": (),
    }
    with pytest.raises(GuardianReadinessError) as error:
        validate_guardian_recovery_receipt(receipt, **common)
    assert error.value.reason_code == "recovery_unowned_resource_rejected"

    with pytest.raises(GuardianReadinessError) as error:
        validate_guardian_recovery_receipt(
            replace(receipt, destroy_requests_instance_ids=(), hard_deletion_guaranteed=True),
            **common,
        )
    assert error.value.reason_code == "hard_deletion_claim_rejected"


def test_verified_absence_requires_fresh_evidence_and_no_unresolved_resources():
    guardian = FakeGuardian("guardian-a", "host-a", "domain-a", FakeRegistry())
    receipt = GuardianRecoveryReceipt(
        request_id="request-1",
        operation_fence=7,
        guardian_id="guardian-a",
        failure_domain_id="domain-a",
        state=RecoveryState.VERIFIED_ABSENT,
        alert_receipt_id="receipt-1",
        provider_absence_confirmed=False,
    )

    with pytest.raises(GuardianReadinessError) as error:
        validate_guardian_recovery_receipt(
            receipt,
            descriptor=guardian.descriptor,
            request_id="request-1",
            operation_fence=7,
            owned_instance_ids=(),
            owned_volume_ids=(),
        )

    assert error.value.reason_code == "recovery_state_mismatch"
