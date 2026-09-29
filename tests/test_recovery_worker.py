from __future__ import annotations

import json
from hashlib import md5
from io import BytesIO
import threading
from http.server import ThreadingHTTPServer

import pytest

from vast_broker.guardians import (
    GuardianCapabilities,
    GuardianDescriptor,
    GuardianKind,
    GuardianReadinessGate,
    LeaseCreateIntent,
    RecoveryState,
)
from vast_broker.guardian_http import HTTPRecoveryGuardian, create_handler
from vast_broker.github_guardian import GitHubActionsGuardian
from vast_broker.recovery_worker import GuardianRecoveryWorker, RecoveryNotDue
from vast_broker.r2_registry import R2SharedLeaseRegistry


class S3Error(Exception):
    def __init__(self, code: str, status: int):
        self.response = {"Error": {"Code": code}, "ResponseMetadata": {"HTTPStatusCode": status}}


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
        page = keys[offset:offset + 100]
        truncated = offset + len(page) < len(keys)
        return {
            "Contents": [{"Key": key} for key in page],
            "IsTruncated": truncated,
            **({"NextContinuationToken": str(offset + len(page))} if truncated else {}),
        }


class FakeAlertSink:
    def __init__(self):
        self.sent = []

    def send(self, payload, *, idempotency_key):
        self.sent.append((payload, idempotency_key))
        return "alert-receipt-1"


class FakeProvider:
    def __init__(self):
        self.instances = {
            "99": {"id": 99, "label": "pre-existing"},
            "42": {"id": 42, "label": "vbr-attempt", "volume_ids": [142]},
        }
        self.volumes = {
            "199": {"id": 199, "instances": [{"id": 99}]},
            "142": {"id": 142, "instances": [{"id": 42}]},
        }
        self.destroyed_instances: list[str] = []
        self.destroyed_volumes: list[str] = []

    def list_instances(self):
        return [dict(row) for row in self.instances.values()]

    def list_volumes(self):
        return [dict(row) for row in self.volumes.values()]

    def destroy_instance(self, instance_id):
        resource_id = str(instance_id)
        self.destroyed_instances.append(resource_id)
        self.instances.pop(resource_id, None)
        for volume in self.volumes.values():
            volume["instances"] = [item for item in volume["instances"] if str(item["id"]) != resource_id]
        return {"success": True}

    def destroy_volume(self, volume_id):
        resource_id = str(volume_id)
        self.destroyed_volumes.append(resource_id)
        self.volumes.pop(resource_id, None)
        return {"success": True}


def setup_workers(*, now=1000, register=True):
    memory = MemoryR2()
    registry = R2SharedLeaseRegistry(
        bucket="test-bucket", endpoint_url="https://r2.example",
        access_key_id="not-a-secret", secret_access_key="not-a-secret",
        account_id="test-account", prefix="broker/test/", client=memory,
    )
    intent = LeaseCreateIntent(
        request_id="openjev/test", operation_id="openjev/test", attempt_id="attempt-1",
        attempt_label="vbr-attempt", operation_fence=7, deadline_epoch=now + 300,
        initiating_host_id="owner-host", preexisting_instance_ids=("99",),
        preexisting_volume_ids=("199",),
    )
    registry.reserve_fence(
        request_id=intent.request_id, attempt_id=intent.attempt_id,
        operation_fence=intent.operation_fence,
    )
    registry.publish_intent(intent)
    alert = FakeAlertSink()
    caps = GuardianCapabilities(True, True, True, True, True)
    workers = tuple(
        GuardianRecoveryWorker(
            registry=registry,
            provider=FakeProvider(),
            descriptor=GuardianDescriptor(
                guardian_id=f"guardian-{suffix}", control_host_id=f"host-{suffix}",
                failure_domain_id=f"domain-{suffix}", kind=GuardianKind.INDEPENDENT_SERVICE,
                trusted=True, capabilities=caps,
            ),
            alert_sink=alert,
            clock=lambda: now,
        )
        for suffix in ("a", "b")
    )
    if register:
        for worker in workers:
            worker.acknowledge_intent(
                registry_id=registry.registry_id,
                request_id=intent.request_id,
                operation_fence=intent.operation_fence,
                intent_digest=intent.digest,
            )
        registry.publish_owned_resources(
            request_id=intent.request_id, operation_fence=intent.operation_fence,
            intent_digest=intent.digest, instance_ids=("42",), volume_ids=("142",),
        )
    return registry, intent, workers


def test_worker_deletes_only_registry_owned_instance_and_volume_then_records_two_receipts():
    registry, intent, workers = setup_workers()
    provider = FakeProvider()
    for worker in workers:
        worker.provider = provider
    registry.request_cleanup(request_id=intent.request_id, operation_fence=intent.operation_fence)

    first = workers[0].reconcile(request_id=intent.request_id, operation_fence=intent.operation_fence)
    assert first.state == RecoveryState.VERIFIED_ABSENT
    assert first.destroy_requests_instance_ids == ("42",)
    assert first.destroy_requests_volume_ids == ("142",)
    assert provider.destroyed_instances == ["42"]
    assert provider.destroyed_volumes == ["142"]
    assert set(provider.instances) == {"99"}
    assert set(provider.volumes) == {"199"}

    second = workers[1].reconcile(request_id=intent.request_id, operation_fence=intent.operation_fence)
    assert second.state == RecoveryState.VERIFIED_ABSENT
    snapshot = registry.read(intent.request_id)
    assert snapshot is not None
    assert snapshot.state == RecoveryState.VERIFIED_ABSENT.value
    assert snapshot.cleanup_requested is True


def test_worker_does_not_destroy_an_active_lease_before_deadline_or_cleanup_signal():
    registry, intent, workers = setup_workers()
    provider = workers[0].provider

    with pytest.raises(RecoveryNotDue):
        workers[0].reconcile(request_id=intent.request_id, operation_fence=intent.operation_fence)

    assert provider.destroyed_instances == []
    assert provider.destroyed_volumes == []
    assert workers[0].run_once() == []
    assert registry.read(intent.request_id).state == "LEASE_OWNED"


def test_authenticated_http_service_acknowledges_the_shared_intent():
    registry, intent, workers = setup_workers(register=False)
    worker = workers[0]
    token = "x" * 40
    server = ThreadingHTTPServer(("127.0.0.1", 0), create_handler(worker, token))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        client = HTTPRecoveryGuardian(
            base_url=f"http://127.0.0.1:{server.server_port}",
            bearer_token=token,
            descriptor=worker.descriptor,
        )
        ack = client.acknowledge_intent(
            registry_id=registry.registry_id,
            request_id=intent.request_id,
            operation_fence=intent.operation_fence,
            intent_digest=intent.digest,
        )
        assert ack.guardian_id == "guardian-a"
        assert ack in registry.read(intent.request_id).guardian_acks
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_authenticated_http_reconcile_returns_fresh_cleanup_receipt():
    registry, intent, workers = setup_workers()
    provider = FakeProvider()
    for worker in workers:
        worker.provider = provider
    registry.request_cleanup(request_id=intent.request_id, operation_fence=intent.operation_fence)
    worker = workers[0]
    token = "y" * 40
    server = ThreadingHTTPServer(("127.0.0.1", 0), create_handler(worker, token))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        client = HTTPRecoveryGuardian(
            base_url=f"http://127.0.0.1:{server.server_port}",
            bearer_token=token,
            descriptor=worker.descriptor,
        )
        receipt = client.reconcile(request_id=intent.request_id, operation_fence=intent.operation_fence)
        assert receipt.state == RecoveryState.VERIFIED_ABSENT
        assert receipt.provider_absence_confirmed is True
        assert receipt.destroy_requests_instance_ids == ("42",)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_github_guardian_dispatches_and_waits_for_durable_ack():
    registry, intent, workers = setup_workers(register=False)
    remote = workers[1]

    def dispatch(fields):
        assert fields["action"] == "acknowledge"
        remote.acknowledge_intent(
            registry_id=fields["registry_id"], request_id=fields["request_id"],
            operation_fence=fields["operation_fence"], intent_digest=fields["intent_digest"],
        )

    guardian = GitHubActionsGuardian(
        registry=registry, repository="owner/repo", workflow="guardian.yml",
        descriptor=remote.descriptor, dispatch=dispatch,
    )
    ack = guardian.acknowledge_intent(
        registry_id=registry.registry_id, request_id=intent.request_id,
        operation_fence=intent.operation_fence, intent_digest=intent.digest,
    )
    assert ack.guardian_id == remote.descriptor.guardian_id


def test_github_guardian_waits_for_new_recovery_revision():
    registry, intent, workers = setup_workers()
    provider = FakeProvider()
    for worker in workers:
        worker.provider = provider
    registry.request_cleanup(request_id=intent.request_id, operation_fence=intent.operation_fence)
    remote = workers[1]

    def dispatch(fields):
        assert fields["action"] == "reconcile"
        remote.reconcile(request_id=fields["request_id"], operation_fence=fields["operation_fence"])

    guardian = GitHubActionsGuardian(
        registry=registry, repository="owner/repo", workflow="guardian.yml",
        descriptor=remote.descriptor, dispatch=dispatch,
    )
    receipt = guardian.reconcile(request_id=intent.request_id, operation_fence=intent.operation_fence)
    assert receipt.state == RecoveryState.VERIFIED_ABSENT
    assert receipt.observed_at_epoch is not None


def test_readiness_gate_arms_http_and_github_guardians_and_persists_early_cleanup():
    registry, intent, workers = setup_workers(register=False)
    token = "z" * 40
    server = ThreadingHTTPServer(("127.0.0.1", 0), create_handler(workers[0], token))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    dispatched = []

    def dispatch(fields):
        dispatched.append(dict(fields))
        if fields["action"] == "acknowledge":
            workers[1].acknowledge_intent(
                registry_id=fields["registry_id"], request_id=fields["request_id"],
                operation_fence=fields["operation_fence"], intent_digest=fields["intent_digest"],
            )

    try:
        http = HTTPRecoveryGuardian(
            base_url=f"http://127.0.0.1:{server.server_port}", bearer_token=token,
            descriptor=workers[0].descriptor,
        )
        github = GitHubActionsGuardian(
            registry=registry, repository="owner/repo", workflow="guardian.yml",
            descriptor=workers[1].descriptor, dispatch=dispatch,
        )
        gate = GuardianReadinessGate(registry, [http, github], clock=lambda: 1000)
        readiness = gate.arm_before_create(intent)
        assert {ack.guardian_id for ack in readiness.guardian_acks} == {"guardian-a", "guardian-b"}
        requested = gate.request_cleanup(intent.request_id, intent.operation_fence)
        assert requested.cleanup_requested is True
        assert dispatched[-1] == {
            "action": "reconcile",
            "request_id": intent.request_id,
            "operation_fence": intent.operation_fence,
        }
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
