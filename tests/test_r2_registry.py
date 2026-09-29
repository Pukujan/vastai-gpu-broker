from __future__ import annotations

from hashlib import md5
from io import BytesIO

import pytest

from vast_broker.guardians import (
    GuardianAck,
    GuardianCapabilities,
    GuardianRecoveryReceipt,
    LeaseCreateIntent,
    RecoveryState,
)
from vast_broker.r2_registry import R2RegistryError, R2SharedLeaseRegistry


class S3Error(Exception):
    def __init__(self, code: str, status: int):
        self.response = {
            "Error": {"Code": code},
            "ResponseMetadata": {"HTTPStatusCode": status},
        }


class MemoryR2:
    def __init__(self):
        self.objects: dict[str, tuple[bytes, str]] = {}

    def get_object(self, *, Bucket, Key):
        del Bucket
        if Key not in self.objects:
            raise S3Error("NoSuchKey", 404)
        body, etag = self.objects[Key]
        return {"Body": BytesIO(body), "ETag": etag}

    def put_object(self, *, Bucket, Key, Body, IfMatch=None, IfNoneMatch=None, **_kwargs):
        del Bucket
        current = self.objects.get(Key)
        if IfNoneMatch == "*" and current is not None:
            raise S3Error("PreconditionFailed", 412)
        if IfMatch is not None and (current is None or current[1] != IfMatch):
            raise S3Error("PreconditionFailed", 412)
        etag = f'"{md5(Body).hexdigest()}"'
        self.objects[Key] = (Body, etag)
        return {"ETag": etag}

    def list_objects_v2(self, *, Bucket, Prefix, ContinuationToken=None):
        del Bucket
        keys = sorted(key for key in self.objects if key.startswith(Prefix))
        offset = int(ContinuationToken or 0)
        page = keys[offset:offset + 2]
        truncated = offset + len(page) < len(keys)
        return {
            "Contents": [{"Key": key} for key in page],
            "IsTruncated": truncated,
            **({"NextContinuationToken": str(offset + len(page))} if truncated else {}),
        }


def make_registry():
    return R2SharedLeaseRegistry(
        bucket="test-bucket", endpoint_url="https://r2.example",
        access_key_id="not-a-secret", secret_access_key="not-a-secret",
        account_id="test-account", prefix="broker/test/", client=MemoryR2(),
    )


def publish_owned_lease(registry: R2SharedLeaseRegistry, request_id: str):
    intent = LeaseCreateIntent(
        request_id=request_id, operation_id=request_id, attempt_id="attempt-1",
        attempt_label="vbr-attempt-1", operation_fence=7, deadline_epoch=9999999999,
        initiating_host_id="owner-host", preexisting_instance_ids=("99",),
        preexisting_volume_ids=("199",),
    )
    reservation = registry.reserve_fence(
        request_id=request_id, attempt_id=intent.attempt_id, operation_fence=intent.operation_fence,
    )
    assert reservation.durable is True
    registry.publish_intent(intent)
    caps = GuardianCapabilities(True, True, True, True, True)
    for guardian_id, host, domain in (
        ("guardian-a", "host-a", "zone-a"),
        ("guardian-b", "host-b", "zone-b"),
    ):
        snapshot = registry.read(request_id)
        registry.acknowledge_guardian(GuardianAck(
            guardian_id=guardian_id, request_id=request_id,
            operation_fence=intent.operation_fence, intent_digest=intent.digest,
            registry_id=registry.registry_id, control_host_id=host,
            failure_domain_id=domain, capabilities=caps, read_revision=snapshot.revision,
        ))
    registry.publish_owned_resources(
        request_id=request_id, operation_fence=intent.operation_fence,
        intent_digest=intent.digest, instance_ids=("42",), volume_ids=("142",),
    )
    return intent


def absent_receipt(request_id: str, guardian_id: str, domain: str) -> GuardianRecoveryReceipt:
    return GuardianRecoveryReceipt(
        request_id=request_id, operation_fence=7, guardian_id=guardian_id,
        failure_domain_id=domain, state=RecoveryState.VERIFIED_ABSENT,
        destroy_requests_instance_ids=("42",), destroy_requests_volume_ids=("142",),
        provider_absence_confirmed=True, alert_receipt_id=f"receipt-{guardian_id}",
        hard_deletion_guaranteed=False,
    )


def test_r2_round_trips_url_sensitive_request_ids_and_lists_them():
    registry = make_registry()
    request_id = "openjev/a%2Fb"
    intent = publish_owned_lease(registry, request_id)

    snapshot = registry.read(request_id)
    assert snapshot is not None
    assert snapshot.request_id == request_id
    assert snapshot.intent_digest == intent.digest
    assert snapshot.owned_instance_ids == ("42",)
    records = registry.list_records()
    assert len(records) == 1
    assert records[0]["request_id"] == request_id


def test_cleanup_request_is_fenced_durable_and_idempotent():
    registry = make_registry()
    intent = publish_owned_lease(registry, "openjev/cleanup-signal")

    first = registry.request_cleanup(
        request_id=intent.request_id, operation_fence=intent.operation_fence,
    )
    second = registry.request_cleanup(
        request_id=intent.request_id, operation_fence=intent.operation_fence,
    )

    assert first.cleanup_requested is True
    assert first.cleanup_requested_at_epoch is not None
    assert second.cleanup_requested is True
    assert second.cleanup_requested_at_epoch == first.cleanup_requested_at_epoch
    assert second.state == "LEASE_OWNED"
    assert registry.read(intent.request_id).cleanup_requested is True
    with pytest.raises(R2RegistryError, match="matching published lease fence"):
        registry.request_cleanup(request_id=intent.request_id, operation_fence=8)


def test_one_guardian_receipt_cannot_finalize_cleanup_or_allow_a_new_fence():
    registry = make_registry()
    request_id = "openjev/one-receipt"
    publish_owned_lease(registry, request_id)

    registry.record_recovery_receipt(absent_receipt(request_id, "guardian-a", "zone-a"))
    snapshot = registry.read(request_id)
    assert snapshot is not None and snapshot.state == "CLEANUP_PENDING"
    with pytest.raises(R2RegistryError, match="reserved, active, or newer fence"):
        registry.reserve_fence(request_id=request_id, attempt_id="attempt-2", operation_fence=8)

    registry.record_recovery_receipt(absent_receipt(request_id, "guardian-b", "zone-b"))
    snapshot = registry.read(request_id)
    assert snapshot is not None and snapshot.state == RecoveryState.VERIFIED_ABSENT.value
    assert registry.list_records() == []
    assert len(registry.list_records(include_terminal=True)) == 1
    retry = registry.reserve_fence(request_id=request_id, attempt_id="attempt-1", operation_fence=7)
    assert retry.durable is True


def test_receipt_from_unacknowledged_guardian_is_rejected():
    registry = make_registry()
    request_id = "openjev/unacknowledged"
    publish_owned_lease(registry, request_id)

    with pytest.raises(R2RegistryError, match="matching acknowledged guardian"):
        registry.record_recovery_receipt(absent_receipt(request_id, "attacker", "zone-x"))


def test_new_owned_resource_invalidates_a_pending_absence_receipt():
    registry = make_registry()
    request_id = "openjev/new-resource"
    intent = publish_owned_lease(registry, request_id)
    registry.record_recovery_receipt(absent_receipt(request_id, "guardian-a", "zone-a"))

    snapshot = registry.publish_owned_resources(
        request_id=request_id, operation_fence=intent.operation_fence,
        intent_digest=intent.digest, instance_ids=(), volume_ids=("143",),
    )
    row, _ = registry._read(request_id)
    assert snapshot.owned_volume_ids == ("142", "143")
    assert row["state"] == "LEASE_OWNED"
    assert row["recovery_receipts"] == []


def test_fence_reservation_is_idempotent_but_an_active_fence_is_not_replaced():
    registry = make_registry()
    first = registry.reserve_fence(request_id="trial", attempt_id="attempt-1", operation_fence=1)
    retry = registry.reserve_fence(request_id="trial", attempt_id="attempt-1", operation_fence=1)
    assert first == retry
    with pytest.raises(R2RegistryError, match="reserved, active, or newer fence"):
        registry.reserve_fence(request_id="trial", attempt_id="attempt-2", operation_fence=2)
