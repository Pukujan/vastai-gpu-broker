from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from vast_broker.guardians import (
    GuardianAck,
    GuardianCapabilities,
    GuardianReadinessReceipt,
)
from vast_broker.journal import JournalError, LeaseJournal
from vast_broker.lease import LeaseController, LeaseError, LeaseSupervisor


class FakeSupervisor:
    def __init__(self, fail: bool = False):
        self.fail = fail
        self.started: list[str] = []

    def start(self, request_id: str) -> None:
        if self.fail:
            raise RuntimeError("start failed")
        self.started.append(request_id)

    def stop(self) -> None:
        pass


class FakeGuardianGate:
    def __init__(self, fail_arm: bool = False, fail_publish: bool = False, fail_cleanup_signal: bool = False):
        self.fail_arm = fail_arm
        self.fail_publish = fail_publish
        self.fail_cleanup_signal = fail_cleanup_signal
        self.armed: list[Any] = []
        self.published: list[tuple[tuple[str, ...], tuple[str, ...]]] = []
        self.cleanup_requests: list[tuple[str, int]] = []

    def request_cleanup(self, request_id, operation_fence):
        self.cleanup_requests.append((request_id, operation_fence))
        if self.fail_cleanup_signal:
            raise RuntimeError("remote cleanup dispatch failed")

    def arm_before_create(self, intent):
        if self.fail_arm:
            raise RuntimeError("guardian readiness is unavailable")
        self.armed.append(intent)
        capabilities = GuardianCapabilities(True, True, True, True, True)
        acks = tuple(
            GuardianAck(
                guardian_id=f"guardian-{suffix}",
                request_id=intent.request_id,
                operation_fence=intent.operation_fence,
                intent_digest=intent.digest,
                registry_id="shared-registry",
                control_host_id=f"host-{suffix}",
                failure_domain_id=f"domain-{suffix}",
                capabilities=capabilities,
                read_revision=2,
            )
            for suffix in ("a", "b")
        )
        return GuardianReadinessReceipt(
            registry_id="shared-registry",
            request_id=intent.request_id,
            operation_id=intent.operation_id,
            attempt_id=intent.attempt_id,
            operation_fence=intent.operation_fence,
            intent_digest=intent.digest,
            deadline_epoch=intent.deadline_epoch,
            guardian_acks=acks,
            declared_failure_domains=("domain-a", "domain-b"),
            physical_separation_proven=False,
        )

    def publish_owned_resources(self, intent, readiness, *, instance_ids, volume_ids):
        if self.fail_publish:
            raise RuntimeError("shared registry update is unavailable")
        assert readiness.request_id == intent.request_id
        self.published.append((instance_ids, volume_ids))
        return type("Snapshot", (), {"revision": 3})()


class FakeProvider:
    def __init__(self):
        self.instances: dict[str, dict[str, Any]] = {}
        self.creates = 0
        self.destroy_calls: list[str] = []
        self.stop_calls: list[str] = []
        self.bid_changes: list[tuple[str, str]] = []
        self.lose_create_response = False
        self.delete_ack_but_present = False
        self.list_error = False
        self.list_volume_error = False
        self.list_volume_error_after_create = False
        self.delete_volume_error = False
        self.created_volume_ids: list[str] = []
        self.omit_volume_ids_in_instance = False
        self.volumes: dict[str, dict[str, Any]] = {}
        self.cleanup_events: list[tuple[str, str]] = []
        self.lock = threading.Lock()

    def list_instances(self):
        if self.list_error:
            raise TimeoutError("listing unavailable")
        return [dict(x) for x in self.instances.values()]

    def list_volumes(self):
        if self.list_volume_error or (self.list_volume_error_after_create and self.creates):
            raise TimeoutError("volume listing unavailable")
        return [
            {"id": volume_id, "instances": [dict(item) for item in row["instances"]]}
            for volume_id, row in self.volumes.items()
        ]

    def get_instance(self, iid):
        return dict(self.instances[iid]) if iid in self.instances else None

    def create_instance(self, offer_id, params):
        with self.lock:
            self.creates += 1
            iid = str(self.creates)
            row = {"id": iid, "label": params["label"], "status": "running", "offer_id": offer_id}
            if self.created_volume_ids and not self.omit_volume_ids_in_instance:
                row["volume_ids"] = list(self.created_volume_ids)
            self.instances[iid] = row
            for volume_id in self.created_volume_ids:
                self.volumes.setdefault(volume_id, {"instances": []})["instances"] = [{"id": iid}]
        if self.lose_create_response:
            raise TimeoutError("response was lost after remote creation")
        return dict(row)

    def stop_instance(self, iid):
        self.stop_calls.append(iid)
        if iid in self.instances:
            self.instances[iid]["status"] = "stopped"

    def destroy_instance(self, iid):
        self.destroy_calls.append(iid)
        self.cleanup_events.append(("instance", iid))
        if self.delete_ack_but_present:
            return {"success": True}
        self.instances.pop(iid, None)
        for volume in self.volumes.values():
            volume["instances"] = [item for item in volume["instances"] if item.get("id") != iid]
        return {"success": True}

    def destroy_volume(self, volume_id):
        self.cleanup_events.append(("volume", volume_id))
        if self.delete_volume_error:
            raise TimeoutError("volume deletion unavailable")
        volume = self.volumes.get(volume_id)
        if volume and volume["instances"]:
            raise RuntimeError("volume still attached")
        self.volumes.pop(volume_id, None)
        return {"success": True}

    def change_bid(self, iid, price):
        self.bid_changes.append((iid, price))


def plan(**updates):
    result = {"offer_id": 77, "max_runtime_seconds": 100, "start_deadline_seconds": 10,
              "cold_start_timeout_seconds": 50, "idle_timeout_seconds": 30,
              "hung_request_timeout_seconds": 20,
              "create_params": {"image": "fixture"}}
    result.update(updates)
    return result


def controller(tmp_path, provider=None, supervisor=None, **kwargs):
    kwargs.setdefault("guardian_gate", FakeGuardianGate())
    return LeaseController(provider or FakeProvider(), LeaseJournal(tmp_path), supervisor or FakeSupervisor(), **kwargs)


@pytest.fixture(autouse=True)
def initiating_control_host(monkeypatch):
    monkeypatch.setenv("VAST_BROKER_CONTROL_HOST_ID", "agent-host")


def test_create_intent_is_durable_before_remote_create_and_success_is_verified(tmp_path):
    provider = FakeProvider()
    provider.created_volume_ids = ["901"]
    journal = LeaseJournal(tmp_path)
    guardian_gate = FakeGuardianGate()
    observed = []
    original_create = provider.create_instance

    def create(offer, params):
        record = journal.load("request-1")
        observed.append((record["state"], list(record["owned_instance_ids"])))
        return original_create(offer, params)

    provider.create_instance = create
    observed_at_destroy = []
    original_destroy = provider.destroy_instance

    def destroy(iid):
        saved = journal.load("request-1")
        observed_at_destroy.append((saved.get("operation_result"), saved.get("operation_result_sha256")))
        return original_destroy(iid)

    provider.destroy_instance = destroy
    def operation(inst):
        record = journal.load("request-1")
        assert guardian_gate.published == [(("1",), ("901",))]
        assert record["guardian_registry_revision"] == 3
        assert record["owned_volume_ids"] == ["901"]
        return "ok"

    result = LeaseController(provider, journal, FakeSupervisor(), guardian_gate=guardian_gate).run("request-1", plan(), operation)
    assert observed == [("CREATE_IN_PROGRESS", [])]
    assert guardian_gate.armed[0].deadline_epoch == journal.load("request-1")["hard_deadline_epoch"]
    assert result["state"] == "DESTROYED"
    assert guardian_gate.cleanup_requests == [("request-1", guardian_gate.armed[0].operation_fence)]
    assert provider.instances == {}
    assert provider.volumes == {}
    assert provider.cleanup_events == [("instance", "1"), ("volume", "901")]
    saved = journal.load("request-1")
    assert saved["confirmed_absent_at_utc"]
    assert saved["operation_result"] == "ok"
    assert saved["operation_result_sha256"]
    assert saved["absence_evidence"]["owned_instance_ids"] == ["1"]
    assert saved["absence_evidence"]["owned_instance_ids_absent"] is True
    assert saved["absence_evidence"]["owned_volume_ids"] == ["901"]
    assert saved["absence_evidence"]["owned_volume_ids_absent"] is True
    assert saved["absence_evidence"]["instance_inventory"]["complete"] is True
    assert saved["absence_evidence"]["instance_inventory"]["instance_count"] == 0
    assert len(saved["absence_evidence"]["instance_inventory"]["instance_ids_sha256"]) == 64
    assert observed_at_destroy == [("ok", saved["operation_result_sha256"])]


def test_remote_cleanup_signal_failure_does_not_block_local_teardown(tmp_path):
    provider = FakeProvider()
    journal = LeaseJournal(tmp_path)
    gate = FakeGuardianGate(fail_cleanup_signal=True)
    result = LeaseController(
        provider, journal, FakeSupervisor(), guardian_gate=gate,
    ).run("remote-signal-outage", plan(), lambda _: "done")

    assert result["state"] == "DESTROYED"
    assert provider.instances == {}
    saved = journal.load("remote-signal-outage")
    assert saved["guardian_cleanup_signal_error_type"] == "RuntimeError"


def test_unrecordable_operation_result_fails_the_trial_but_still_destroys(tmp_path):
    provider = FakeProvider()
    journal = LeaseJournal(tmp_path)
    ctl = LeaseController(provider, journal, FakeSupervisor(), guardian_gate=FakeGuardianGate())

    with pytest.raises(LeaseError, match="operation failed; cleanup was attempted"):
        ctl.run("unrecordable-result", plan(), lambda _: {"non_finite": float("nan")})

    record = journal.load("unrecordable-result")
    assert record["state"] == "DESTROYED"
    assert "operation_result" not in record
    assert provider.instances == {}
    assert provider.destroy_calls == ["1"]


def test_volume_delete_failure_keeps_cleanup_pending_and_retries(tmp_path):
    provider = FakeProvider()
    provider.created_volume_ids = ["902"]
    provider.delete_volume_error = True
    journal = LeaseJournal(tmp_path)
    ctl = LeaseController(provider, journal, FakeSupervisor(), guardian_gate=FakeGuardianGate())
    with pytest.raises(LeaseError, match="unresolved cleanup") as error:
        ctl.run("volume-delete-retry", plan(), lambda _: "done")
    assert error.value.result["state"] == "CLEANUP_PENDING"
    assert provider.instances == {}
    assert "902" in provider.volumes
    assert journal.load("volume-delete-retry")["owned_volume_ids"] == ["902"]

    provider.delete_volume_error = False
    assert ctl.reconcile("volume-delete-retry")[0]["state"] == "DESTROYED"
    assert provider.volumes == {}


def test_volume_listing_outage_never_claims_clean_and_recovers_by_retry(tmp_path):
    provider = FakeProvider()
    provider.created_volume_ids = ["903"]
    provider.list_volume_error_after_create = True
    journal = LeaseJournal(tmp_path)
    ctl = LeaseController(provider, journal, FakeSupervisor(), guardian_gate=FakeGuardianGate())
    with pytest.raises(LeaseError, match="access was blocked") as error:
        ctl.run("volume-list-outage", plan(), lambda _: pytest.fail("operation must not run"))
    assert error.value.result["state"] == "CLEANUP_PENDING"
    assert provider.instances == {}
    assert "903" in provider.volumes

    provider.list_volume_error_after_create = False
    assert ctl.reconcile("volume-list-outage")[0]["state"] == "DESTROYED"
    assert provider.volumes == {}


def test_volume_inventory_outage_preserves_instance_until_attachment_is_discovered(tmp_path):
    provider = FakeProvider()
    provider.created_volume_ids = ["906"]
    provider.omit_volume_ids_in_instance = True
    provider.list_volume_error_after_create = True
    journal = LeaseJournal(tmp_path)
    ctl = LeaseController(provider, journal, FakeSupervisor(), guardian_gate=FakeGuardianGate())

    with pytest.raises(LeaseError, match="access was blocked") as error:
        ctl.run("volume-discovery-retry", plan(), lambda _: pytest.fail("operation must not run"))

    assert error.value.result["state"] == "CLEANUP_PENDING"
    assert "1" in provider.instances
    assert provider.instances["1"]["status"] == "running"
    assert provider.volumes["906"]["instances"] == [{"id": "1"}]
    assert provider.destroy_calls == []
    assert journal.load("volume-discovery-retry")["volume_discovery_pending"] is True

    provider.list_volume_error_after_create = False
    recovered = ctl.reconcile("volume-discovery-retry")[0]

    assert recovered["state"] == "DESTROYED"
    assert provider.instances == {}
    assert provider.volumes == {}
    assert provider.cleanup_events == [("instance", "1"), ("volume", "906")]


def test_reconcile_retries_unresolved_volume_without_deleting_uncertain_ownership(tmp_path):
    provider = FakeProvider()
    provider.volumes["907"] = {"instances": []}
    journal = LeaseJournal(tmp_path)
    journal.save({
        "request_id": "unresolved-volume-restart",
        "state": "CLEANUP_PENDING",
        "owned_instance_ids": [],
        "owned_volume_ids": [],
        "unresolved_volume_ids": ["907"],
        "pre_create_volume_ids": [],
        "events": [],
    })
    ctl = controller(tmp_path, provider)

    pending = ctl.reconcile()[0]

    assert pending["state"] == "CLEANUP_PENDING"
    assert pending["verification"]["remaining_volume_ids"] == ["907"]
    assert provider.volumes == {"907": {"instances": []}}
    assert provider.cleanup_events == []
    assert provider.destroy_calls == []

    # A fresh provider read can close the obligation after independent deletion;
    # the broker must not issue a delete for a volume whose ownership was uncertain.
    provider.volumes.pop("907")
    recovered = ctl.reconcile("unresolved-volume-restart")[0]

    assert recovered["state"] == "DESTROYED"
    assert provider.cleanup_events == []


def test_missing_volume_discovery_anchor_keeps_orphan_volume_obligation_pending(tmp_path):
    provider = FakeProvider()
    provider.volumes["908"] = {"instances": []}
    journal = LeaseJournal(tmp_path)
    journal.save({
        "request_id": "missing-discovery-anchor",
        "state": "CLEANUP_PENDING",
        "owned_instance_ids": ["1"],
        "owned_volume_ids": [],
        "unresolved_volume_ids": [],
        "volume_discovery_pending": True,
        "pre_create_volume_ids": [],
        "events": [],
    })
    ctl = controller(tmp_path, provider)

    pending = ctl.reconcile()[0]

    assert pending["state"] == "CLEANUP_PENDING"
    assert pending["volume_discovery_pending"] is True
    assert pending["owned_instance_ids"] == ["1"]
    assert pending["verification"]["volume_discovery_anchor_error_type"] == "DiscoveryAnchorMissing"
    assert provider.volumes == {"908": {"instances": []}}
    assert provider.cleanup_events == []
    assert provider.destroy_calls == []


def test_preexisting_attached_volume_is_preserved_and_access_is_blocked(tmp_path):
    provider = FakeProvider()
    provider.created_volume_ids = ["904"]
    provider.volumes["904"] = {"instances": []}
    journal = LeaseJournal(tmp_path)
    ctl = LeaseController(provider, journal, FakeSupervisor(), guardian_gate=FakeGuardianGate())
    with pytest.raises(LeaseError, match="access was blocked"):
        ctl.run("borrowed-volume", plan(), lambda _: pytest.fail("operation must not run"))
    assert provider.instances == {}
    assert provider.volumes == {"904": {"instances": []}}
    assert provider.cleanup_events == [("instance", "1")]
    record = journal.load("borrowed-volume")
    assert record["state"] == "DESTROYED"
    assert record["borrowed_volume_ids"] == ["904"]


def test_legacy_journal_without_volume_baseline_does_not_delete_unknown_volume(tmp_path):
    provider = FakeProvider()
    provider.instances["legacy-instance"] = {"id": "legacy-instance", "status": "running"}
    provider.volumes["905"] = {"instances": [{"id": "legacy-instance"}]}
    journal = LeaseJournal(tmp_path)
    journal.save({
        "request_id": "legacy-volume",
        "state": "DESTROY_REQUIRED",
        "owned_instance_ids": ["legacy-instance"],
        "instance": {"id": "legacy-instance", "volume_ids": ["905"]},
        "events": [],
    })

    result = LeaseController(provider, journal, FakeSupervisor())._cleanup_request("legacy-volume")

    assert result["state"] == "CLEANUP_PENDING"
    assert provider.instances == {}
    assert provider.volumes == {"905": {"instances": []}}
    assert provider.cleanup_events == [("instance", "legacy-instance")]
    assert result["unresolved_volume_ids"] == ["905"]


def test_create_is_blocked_without_a_guardian_gate_or_control_host_identity(tmp_path, monkeypatch):
    provider = FakeProvider()
    supervisor = FakeSupervisor()
    with pytest.raises(LeaseError, match="two remote cleanup paths"):
        LeaseController(provider, LeaseJournal(tmp_path / "no-gate"), supervisor).run(
            "no-guardian-gate", plan(), lambda _: pytest.fail("operation must not run")
        )
    assert provider.creates == 0
    assert supervisor.started == []
    assert list((tmp_path / "no-gate").iterdir()) == []

    monkeypatch.delenv("VAST_BROKER_CONTROL_HOST_ID")
    gate = FakeGuardianGate()
    with pytest.raises(LeaseError, match="VAST_BROKER_CONTROL_HOST_ID"):
        LeaseController(provider, LeaseJournal(tmp_path / "no-host"), supervisor,
                        guardian_gate=gate).run(
            "no-host-identity", plan(), lambda _: pytest.fail("operation must not run")
        )
    assert provider.creates == 0
    assert gate.armed == []
    assert list((tmp_path / "no-host").iterdir()) == []


def test_missing_guardian_acknowledgement_blocks_create(tmp_path):
    provider = FakeProvider()
    supervisor = FakeSupervisor()
    gate = FakeGuardianGate(fail_arm=True)
    journal = LeaseJournal(tmp_path)
    with pytest.raises(LeaseError, match="two remote cleanup paths") as error:
        LeaseController(provider, journal, supervisor, guardian_gate=gate).run(
            "guardian-ack-failed", plan(), lambda _: pytest.fail("operation must not run")
        )
    assert provider.creates == 0
    assert supervisor.started == ["guardian-ack-failed"]
    assert error.value.result["state"] == "FAILED_CLEAN"
    assert error.value.result["failure"] == "guardian_readiness_failed"


def test_registry_resource_failure_triggers_cleanup_before_access(tmp_path):
    provider = FakeProvider()
    gate = FakeGuardianGate(fail_publish=True)
    journal = LeaseJournal(tmp_path)
    with pytest.raises(LeaseError, match="access was blocked") as error:
        LeaseController(provider, journal, FakeSupervisor(), guardian_gate=gate).run(
            "registry-resource-failed", plan(), lambda _: pytest.fail("operation must not run")
        )
    assert provider.creates == 1
    assert provider.instances == {}
    assert gate.published == []
    assert error.value.result["state"] == "DESTROYED"


def test_ambiguous_create_is_reconciled_by_label_and_cleaned_before_return(tmp_path):
    provider = FakeProvider()
    provider.lose_create_response = True
    ctl = controller(tmp_path, provider)
    with pytest.raises(LeaseError, match="ambiguous") as error:
        ctl.run("request-ambiguous", plan(), lambda _: pytest.fail("must not hand out access"))
    assert error.value.result["state"] == "DESTROYED"
    assert provider.creates == 1
    assert provider.instances == {}


def test_ambiguous_create_without_visible_match_remains_blocking(tmp_path):
    provider = FakeProvider()
    provider.lose_create_response = True
    provider.list_error = False
    # Simulate an API that did not create an instance and still lost its response.
    def no_create(offer, params):
        provider.creates += 1
        raise TimeoutError("unknown outcome")
    provider.create_instance = no_create
    ctl = controller(tmp_path, provider)
    with pytest.raises(LeaseError):
        ctl.run("request-unknown", plan(), lambda _: None)
    assert ctl.status("request-unknown")["state"] == "CREATE_UNCERTAIN"
    with pytest.raises(LeaseError, match="unresolved lease"):
        ctl.run("request-unknown", plan(), lambda _: None)
    assert provider.creates == 1


def test_precreate_journal_failure_and_supervisor_failure_make_zero_creates(tmp_path, monkeypatch):
    provider = FakeProvider()
    journal = LeaseJournal(tmp_path / "journal-fail")
    monkeypatch.setattr(journal, "save", lambda _: (_ for _ in ()).throw(JournalError("disk full")))
    with pytest.raises(JournalError):
        LeaseController(provider, journal, FakeSupervisor(), guardian_gate=FakeGuardianGate()).run("persist-before", plan(), lambda _: None)
    assert provider.creates == 0

    provider2 = FakeProvider()
    with pytest.raises(LeaseError, match="no create"):
        controller(tmp_path / "supervisor-fail", provider2, FakeSupervisor(fail=True)).run("supervisor-fail", plan(), lambda _: None)
    assert provider2.creates == 0


def test_reserved_plan_is_rejected_before_provider_journal_or_supervisor_calls(tmp_path):
    class CountingProvider(FakeProvider):
        def __init__(self):
            super().__init__()
            self.calls: list[str] = []

        def list_instances(self):
            self.calls.append("list")
            return super().list_instances()

        def create_instance(self, offer_id, params):
            self.calls.append("create")
            return super().create_instance(offer_id, params)

    invalid_plans = [
        plan(rental_type="reserved"),
        plan(type="reserved"),
        plan(create_params={"image": "fixture", "rental_type": "reserved"}),
        plan(create_params={"image": "fixture", "type": "reserved"}),
        plan(create_params={"image": "fixture", "_broker_rental_type": "reserved"}),
    ]
    for index, invalid_plan in enumerate(invalid_plans):
        provider = CountingProvider()
        supervisor = FakeSupervisor()
        journal_dir = tmp_path / str(index)
        with pytest.raises(LeaseError, match="reserved pricing requires"):
            controller(journal_dir, provider, supervisor).run(
                f"reserved-{index}", invalid_plan, lambda _: pytest.fail("operation must not run")
            )
        assert provider.calls == []
        assert provider.creates == 0
        assert supervisor.started == []
        assert list(journal_dir.iterdir()) == []


def test_postcreate_ownership_persist_failure_attempts_cleanup(tmp_path, monkeypatch):
    provider = FakeProvider()
    journal = LeaseJournal(tmp_path)
    original = journal.save
    seen_owned = False

    def save(record):
        nonlocal seen_owned
        if record.get("state") == "STARTING" and record.get("owned_instance_ids") and not seen_owned:
            seen_owned = True
            raise JournalError("simulated post-create disk error")
        return original(record)

    monkeypatch.setattr(journal, "save", save)
    with pytest.raises(JournalError):
        LeaseController(provider, journal, FakeSupervisor(), guardian_gate=FakeGuardianGate()).run("persist-after", plan(), lambda _: None)
    assert provider.creates == 1
    assert provider.instances == {}
    assert provider.destroy_calls == ["1"]


def test_delete_acknowledgement_is_not_absence_and_reconcile_retries(tmp_path):
    provider = FakeProvider()
    provider.delete_ack_but_present = True
    ctl = controller(tmp_path, provider)
    with pytest.raises(LeaseError, match="unresolved cleanup") as error:
        ctl.run("delete-unverified", plan(), lambda _: None)
    assert error.value.result["state"] == "CLEANUP_PENDING"
    assert provider.instances
    provider.delete_ack_but_present = False
    assert ctl.reconcile("delete-unverified")[0]["state"] == "DESTROYED"
    assert not provider.instances


def test_callback_failure_cancel_and_replayed_cleanup_converge_without_a_second_create(tmp_path):
    provider = FakeProvider()
    ctl = controller(tmp_path, provider)
    with pytest.raises(LeaseError, match="operation failed"):
        ctl.run("operation-error", plan(), lambda _: (_ for _ in ()).throw(RuntimeError("install failed")))
    assert ctl.status("operation-error")["state"] == "DESTROYED"
    assert provider.creates == 1
    assert ctl.cancel("operation-error")["state"] == "DESTROYED"
    assert ctl.reconcile("operation-error")[0]["state"] == "DESTROYED"
    assert provider.creates == 1
    assert not provider.instances


def test_deadline_supervisor_destroys_while_user_callback_is_blocked(tmp_path):
    provider = FakeProvider()
    journal = LeaseJournal(tmp_path)
    clock = [0.0]
    ctl = LeaseController(provider, journal, FakeSupervisor(), guardian_gate=FakeGuardianGate(), clock=lambda: clock[0])
    entered = threading.Event()
    release = threading.Event()
    output = []

    def blocked_work(_instance):
        entered.set()
        assert release.wait(3)
        return "completed after cleanup was requested"

    def run():
        try:
            output.append(ctl.run("blocked-callback", plan(cold_start_timeout_seconds=200), blocked_work))
        except Exception as exc:
            output.append(exc)

    thread = threading.Thread(target=run)
    thread.start()
    assert entered.wait(2)
    clock[0] = 100.0  # hard deadline; the callback is still waiting on its event
    result = LeaseSupervisor(provider, journal, clock=lambda: clock[0]).tick("blocked-callback")
    assert result["state"] == "DESTROYED"
    assert not provider.instances
    assert thread.is_alive()  # cleanup did not wait for arbitrary callback work
    release.set()
    thread.join(3)
    assert not thread.is_alive()
    # The callback finished only after the hard deadline had already destroyed
    # its lease, so its late result cannot count as a successful paid run.
    assert output and isinstance(output[0], LeaseError)
    assert output[0].result["state"] == "DESTROYED"
    assert "operation_result" not in journal.load("blocked-callback")


def test_malformed_or_unavailable_listing_never_confirms_absence(tmp_path):
    provider = FakeProvider()
    ctl = controller(tmp_path, provider)
    provider.list_error = True
    with pytest.raises(LeaseError, match="listing is unavailable"):
        ctl.run("list-down", plan(), lambda _: None)
    assert provider.creates == 0


def test_two_concurrent_callers_do_not_create_two_instances(tmp_path):
    provider = FakeProvider()
    ctl = controller(tmp_path, provider)
    barrier = threading.Barrier(2)
    outcomes = []

    def call():
        barrier.wait()
        try:
            outcomes.append(ctl.run("same-request", plan(), lambda _: time.sleep(0.05)))
        except LeaseError as exc:
            outcomes.append(exc)

    threads = [threading.Thread(target=call), threading.Thread(target=call)]
    for thread in threads: thread.start()
    for thread in threads: thread.join()
    assert provider.creates == 1
    assert len(outcomes) == 2
    assert len(provider.instances) == 0


def test_bid_escalation_is_finite_and_never_exceeds_machine_cap(tmp_path):
    provider = FakeProvider()
    clock = [100.0]
    provider.instances = {}

    def create(offer, params):
        provider.creates += 1
        row = {"id": "bid-1", "label": params["label"], "status": "loading"}
        provider.instances["bid-1"] = row
        return dict(row)

    def get(iid):
        if len(provider.bid_changes) >= 2:
            provider.instances[iid]["status"] = "running"
        return dict(provider.instances[iid])

    provider.create_instance = create
    provider.get_instance = get
    ctl = LeaseController(provider, LeaseJournal(tmp_path), FakeSupervisor(), guardian_gate=FakeGuardianGate(), clock=lambda: clock[0],
                          sleep_fn=lambda delay: clock.__setitem__(0, clock[0] + delay), startup_poll_seconds=1)
    bidplan = plan(rental_type="bid", starting_bid_usd_per_machine_hour="0.10",
                   bid_increment_usd="0.05", max_bid_usd_per_machine_hour="0.20", max_bid_attempts=2)
    result = ctl.run("bid-bounded", bidplan, lambda _: "started")
    assert result["state"] == "DESTROYED"
    assert [price for _, price in provider.bid_changes] == ["0.15", "0.20"]
    assert all(float(price) <= 0.20 for _, price in provider.bid_changes)
    assert not provider.instances


def test_bid_never_starts_and_is_destroyed_at_start_deadline(tmp_path):
    provider = FakeProvider()
    clock = [0.0]

    def create(offer, params):
        provider.creates += 1
        row = {"id": "bid-never", "label": params["label"], "status": "loading"}
        provider.instances[row["id"]] = row
        return dict(row)

    provider.create_instance = create
    ctl = LeaseController(provider, LeaseJournal(tmp_path), FakeSupervisor(), guardian_gate=FakeGuardianGate(), clock=lambda: clock[0],
                          sleep_fn=lambda delay: clock.__setitem__(0, clock[0] + delay), startup_poll_seconds=1)
    with pytest.raises(LeaseError, match="cleanup") as error:
        ctl.run("bid-timeout", plan(rental_type="bid", starting_bid_usd_per_machine_hour="0.1",
                                   bid_increment_usd="0.1", max_bid_usd_per_machine_hour="0.2", max_bid_attempts=1), lambda _: None)
    assert error.value.result["state"] == "DESTROYED"
    assert not provider.instances


def test_supervisor_idle_activity_is_independent_of_polling(tmp_path):
    provider = FakeProvider()
    journal = LeaseJournal(tmp_path)
    clock = [10.0]
    journal.save({"request_id": "supervise", "state": "READY", "created_epoch": 0,
                  "hard_deadline_epoch": 100, "ready_epoch": 0, "last_inference_epoch": 0,
                  "policy": {"idle_timeout_seconds": 20}, "owned_instance_ids": ["owned"], "events": []})
    provider.instances["owned"] = {"id": "owned", "label": "owned", "status": "running"}
    ctl = LeaseController(provider, journal, FakeSupervisor(), guardian_gate=FakeGuardianGate(), clock=lambda: clock[0])
    ctl.record_activity("supervise")  # heartbeat/polling cannot reset model-use idle time
    worker = LeaseSupervisor(provider, journal, clock=lambda: clock[0])
    clock[0] = 19
    worker.tick("supervise")
    assert provider.instances
    clock[0] = 20
    rec = worker.tick("supervise")
    assert rec["state"] == "DESTROYED"
    assert rec["last_inference_epoch"] == 0  # polling did not count as model activity
    assert not provider.instances


def test_stop_retains_instance_then_bounded_retention_destroys(tmp_path):
    provider = FakeProvider()
    journal = LeaseJournal(tmp_path)
    clock = [1.0]
    provider.instances["owned"] = {"id": "owned", "label": "owned", "status": "running"}
    journal.save({"request_id": "retention", "state": "READY", "created_epoch": 0,
                  "hard_deadline_epoch": 100, "ready_epoch": 0, "last_inference_epoch": 0,
                  "policy": {"stop_after_idle_seconds": 5, "retention_seconds": 3},
                  "owned_instance_ids": ["owned"], "events": []})
    worker = LeaseSupervisor(provider, journal, clock=lambda: clock[0])
    clock[0] = 5
    assert worker.tick("retention")["state"] == "STOPPED_RETAINING"
    assert provider.instances  # stopped storage is still retained
    clock[0] = 8
    assert worker.tick("retention")["state"] == "DESTROYED"
    assert not provider.instances


def test_restart_reconcile_cleans_known_owned_id_and_never_touches_unrelated(tmp_path):
    provider = FakeProvider()
    provider.instances = {"mine": {"id": "mine", "label": "mine"}, "other": {"id": "other", "label": "user"}}
    journal = LeaseJournal(tmp_path)
    journal.save({"request_id": "restart", "state": "CLEANUP_PENDING", "owned_instance_ids": ["mine"],
                  "events": [], "policy": {}, "created_epoch": 0, "hard_deadline_epoch": 1})
    ctl = controller(tmp_path, provider)
    assert ctl.reconcile("restart")[0]["state"] == "DESTROYED"
    assert set(provider.instances) == {"other"}
    assert provider.destroy_calls == ["mine"]
