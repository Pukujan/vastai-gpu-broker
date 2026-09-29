"""Bounded lease ownership and cleanup transitions for temporary Vast instances.

This module never reports a host/network failure as a provider-side deletion guarantee.
"""
from __future__ import annotations

import math
import hashlib
import json
import os
import time
import uuid
from dataclasses import asdict
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Mapping, Protocol

from .guardians import GuardianAck, GuardianCapabilities, GuardianReadinessReceipt, LeaseCreateIntent
from .journal import JournalError, LeaseJournal


class LeaseProvider(Protocol):
    def list_instances(self) -> list[dict[str, Any]]: ...
    def list_volumes(self) -> list[dict[str, Any]]: ...
    def get_instance(self, instance_id: str) -> dict[str, Any] | None: ...
    def create_instance(self, offer_id: int, params: dict[str, Any]) -> dict[str, Any]: ...
    def stop_instance(self, instance_id: str) -> Any: ...
    def destroy_instance(self, instance_id: str) -> Any: ...
    def destroy_volume(self, volume_id: str) -> Any: ...
    def change_bid(self, instance_id: str, price_usd_per_machine_hour: Decimal | str) -> Any: ...


class LeaseError(RuntimeError):
    def __init__(self, message: str, result: dict[str, Any] | None = None):
        super().__init__(message)
        self.result = result or {}


def _utc(ts: float | None = None) -> str:
    return datetime.fromtimestamp(ts if ts is not None else time.time(), timezone.utc).isoformat()


def _duration(plan: Mapping[str, Any], name: str, *, required: bool = False) -> float | None:
    value = plan.get(name)
    if value is None:
        if required:
            raise LeaseError(f"missing required lifecycle limit: {name}")
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise LeaseError(f"invalid lifecycle limit: {name}")
    return float(value)


def _instances(value: Any) -> list[dict[str, Any]]:
    # A malformed or partial provider listing must never be treated as proof of absence.
    if isinstance(value, dict):
        value = value.get("instances")
    if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
        raise ValueError("provider instance listing is malformed")
    return value


def _instance_inventory_receipt(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Fingerprint the IDs from a successfully completed provider inventory."""
    ids = [_instance_id(row) for row in rows]
    if any(value is None for value in ids) or len(set(ids)) != len(ids):
        raise ValueError("provider instance inventory has missing or duplicate IDs")
    canonical_ids = sorted(ids)
    return {
        "complete": True,
        "instance_count": len(rows),
        "instance_ids_sha256": hashlib.sha256(
            json.dumps(canonical_ids, separators=(",", ":")).encode("utf-8")
        ).hexdigest(),
    }


def _instance_id(instance: Mapping[str, Any]) -> str | None:
    value = instance.get("id", instance.get("instance_id", instance.get("contract_id", instance.get("new_contract"))))
    return str(value) if value is not None and str(value) else None


def _volume_ids(instance: Mapping[str, Any]) -> tuple[str, ...]:
    """Read explicit volume IDs without treating malformed provider data as empty."""
    found: list[str] = []
    for field in ("volume_ids", "volumes", "volume_id", "disk_id"):
        if field not in instance or instance[field] is None:
            continue
        values = instance[field]
        if isinstance(values, (str, int)) and not isinstance(values, bool):
            values = [values]
        elif isinstance(values, Mapping):
            values = [values]
        elif not isinstance(values, (list, tuple)):
            raise LeaseError(f"provider returned malformed {field} ownership data")
        for item in values:
            if isinstance(item, Mapping):
                value = item.get("id", item.get("volume_id", item.get("disk_id")))
            else:
                value = item
            if isinstance(value, bool) or not isinstance(value, (str, int)) or not str(value).strip():
                raise LeaseError(f"provider returned malformed {field} ownership data")
            raw = str(value).strip()
            try:
                if int(raw) < 1 or str(int(raw)) != raw:
                    raise ValueError
            except ValueError:
                raise LeaseError(f"provider returned malformed {field} ownership data") from None
            found.append(raw)
    return tuple(sorted(set(found)))


def _volume_instance_ids(volume: Mapping[str, Any]) -> tuple[str, ...]:
    """Parse the documented volume-to-instance relationship conservatively."""
    attached = volume.get("instances")
    if not isinstance(attached, list):
        raise ValueError("provider volume listing omitted its instance relationships")
    found: list[str] = []
    for item in attached:
        if isinstance(item, Mapping):
            value = item.get("id", item.get("instance_id", item.get("contract_id")))
        else:
            value = item
        if isinstance(value, bool) or not isinstance(value, (str, int)) or not str(value).strip():
            raise ValueError("provider volume listing returned a malformed instance relationship")
        found.append(str(value))
    return tuple(sorted(set(found)))


def _volume_snapshot(value: Any) -> tuple[dict[str, tuple[str, ...]], dict[str, dict[str, Any]]]:
    if isinstance(value, Mapping):
        value = value.get("volumes")
    if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
        raise ValueError("provider volume listing is malformed")
    attachments: dict[str, tuple[str, ...]] = {}
    rows: dict[str, dict[str, Any]] = {}
    for row in value:
        raw_id = row.get("id")
        if isinstance(raw_id, bool) or not isinstance(raw_id, (str, int)) or not str(raw_id).strip():
            raise ValueError("provider volume listing omitted a valid ID")
        volume_id = str(raw_id)
        try:
            if int(volume_id) < 1 or str(int(volume_id)) != volume_id:
                raise ValueError
        except ValueError:
            raise ValueError("provider volume listing returned an invalid ID") from None
        if volume_id in rows:
            raise ValueError("provider volume listing returned duplicate IDs")
        attachments[volume_id] = _volume_instance_ids(row)
        rows[volume_id] = row
    return attachments, rows


def _readiness_matches(receipt: Any, intent: LeaseCreateIntent) -> bool:
    if not isinstance(receipt, GuardianReadinessReceipt):
        return False
    acks = receipt.guardian_acks
    if not isinstance(acks, tuple) or len(acks) < 2 or any(not isinstance(ack, GuardianAck) for ack in acks):
        return False
    guardian_ids = {ack.guardian_id for ack in acks}
    control_hosts = {ack.control_host_id for ack in acks}
    failure_domains = {ack.failure_domain_id for ack in acks}
    declared_domains = receipt.declared_failure_domains
    return bool(
        isinstance(receipt.registry_id, str) and receipt.registry_id.strip()
        and receipt.request_id == intent.request_id
        and receipt.operation_id == intent.operation_id
        and receipt.attempt_id == intent.attempt_id
        and receipt.operation_fence == intent.operation_fence
        and receipt.intent_digest == intent.digest
        and receipt.deadline_epoch == intent.deadline_epoch
        and receipt.physical_separation_proven is False
        and len(guardian_ids) == len(acks)
        and len(control_hosts) == len(acks)
        and len(failure_domains) == len(acks)
        and intent.initiating_host_id not in control_hosts
        and isinstance(declared_domains, tuple)
        and len(declared_domains) == len(acks)
        and set(declared_domains) == failure_domains
        and all(
            isinstance(ack.guardian_id, str) and ack.guardian_id.strip()
            and isinstance(ack.control_host_id, str) and ack.control_host_id.strip()
            and isinstance(ack.failure_domain_id, str) and ack.failure_domain_id.strip()
            and ack.registry_id == receipt.registry_id
            and ack.request_id == intent.request_id
            and ack.operation_fence == intent.operation_fence
            and ack.intent_digest == intent.digest
            and isinstance(ack.capabilities, GuardianCapabilities)
            and ack.capabilities.recovery_complete
            and isinstance(ack.read_revision, int) and not isinstance(ack.read_revision, bool)
            and ack.read_revision >= 0
            for ack in acks
        )
    )


class LeaseSupervisor:
    """One polling pass for the separately launched supervisor process.

    This object is intentionally stateless; its process reloads the durable journal on
    every pass and builds its own provider client. It cannot force cleanup while the
    host, network, or provider is unavailable.
    """

    def __init__(self, provider: LeaseProvider, journal: LeaseJournal, *, poll_seconds: float = 1.0,
                 clock: Callable[[], float] = time.time):
        if poll_seconds <= 0:
            raise ValueError("poll_seconds must be positive")
        self.provider, self.journal = provider, journal
        self.poll_seconds, self.clock = poll_seconds, clock

    def tick(self, request_id: str) -> dict[str, Any] | None:
        controller = LeaseController(self.provider, self.journal, supervisor=object(), clock=self.clock)
        record = controller.status(request_id)
        if not record or record.get("state") == "DESTROYED":
            return record
        now = self.clock()
        state = record.get("state")
        if state in {"CREATE_IN_PROGRESS", "CREATE_UNCERTAIN"}:
            record = controller.reconcile_create_uncertain(request_id) or record
            state = record.get("state")
        elif state == "CREATE_INTENT" and now >= record.get("start_deadline_epoch", float("inf")):
            with self.journal.locked(request_id):
                current = self.journal.load(request_id)
                if current and current.get("state") == "CREATE_INTENT":
                    current["state"] = "CREATE_UNCERTAIN"
                    current["failure"] = "create_intent_expired_before_outcome_was_known"
                    self.journal.save(current)
            return controller.status(request_id)
        if state in {"DESTROY_REQUIRED", "VERIFYING_DESTROY", "CLEANUP_PENDING"}:
            return controller._cleanup_request(request_id)
        if state not in {"LEASE_OWNED", "STARTING", "INSTALLING", "PROBING", "READY", "STOPPING", "STOPPED_RETAINING"}:
            return record

        policy = record.get("policy", {})
        created = record.get("created_epoch", now)
        ready = record.get("ready_epoch")
        last_work = record.get("last_inference_epoch", ready or created)
        active_since = record.get("active_request_epoch")
        action: str | None = None
        if now >= record.get("hard_deadline_epoch", float("inf")):
            action = "destroy"
        elif active_since is not None and now - active_since >= policy.get("hung_request_timeout_seconds", float("inf")):
            action = "destroy"
        elif ready is None and now - created >= policy.get("cold_start_timeout_seconds", float("inf")):
            action = "destroy"
        elif state == "STOPPED_RETAINING":
            if now >= record.get("retention_deadline_epoch", now):
                action = "destroy"
        elif state == "STOPPING":
            action = "stop"
        elif ready is not None and now - last_work >= min(
            policy.get("idle_timeout_seconds", float("inf")),
            policy.get("stop_after_idle_seconds", float("inf")),
        ):
            stop_after = policy.get("stop_after_idle_seconds")
            retention = policy.get("retention_seconds")
            if stop_after is not None and retention is not None and now - last_work >= stop_after:
                action = "stop"
            else:
                action = "destroy"
        if action == "stop":
            with self.journal.locked(request_id):
                current = self.journal.load(request_id)
                if not current or current.get("state") in {"DESTROYED", "DESTROY_REQUIRED", "VERIFYING_DESTROY"}:
                    return current
                current["state"] = "STOPPING"
                current.setdefault("retention_deadline_epoch", now + policy.get("retention_seconds", 0))
                self.journal.save(current)
                ids = list(current.get("owned_instance_ids", []))
            errors = []
            for instance_id in ids:
                try:
                    self.provider.stop_instance(instance_id)
                except Exception as exc:
                    errors.append(type(exc).__name__)
            with self.journal.locked(request_id):
                current = self.journal.load(request_id)
                if current and current.get("state") == "STOPPING":
                    current["state"] = "STOPPED_RETAINING" if not errors else "STOPPING"
                    current.setdefault("events", []).append({"at_utc": _utc(now), "event": "stop_requested" if not errors else "stop_retry_pending"})
                    self.journal.save(current)
                return current
        if action == "destroy":
            with self.journal.locked(request_id):
                current = self.journal.load(request_id)
                if current and current.get("state") not in {"DESTROYED", "FAILED_CLEAN"}:
                    current["state"] = "DESTROY_REQUIRED"
                    self.journal.save(current)
            return controller._cleanup_request(request_id)
        return record


class LeaseController:
    """Owns create intent, performs work under a bounded lease, and verifies cleanup."""

    def __init__(self, provider: LeaseProvider, journal: LeaseJournal, supervisor: Any | None = None,
                 *, guardian_gate: Any | None = None,
                 clock: Callable[[], float] = time.time, sleep_fn: Callable[[float], None] = time.sleep,
                 startup_poll_seconds: float = 1.0, cleanup_attempts: int = 3):
        if cleanup_attempts < 1:
            raise ValueError("cleanup_attempts must be at least one")
        self.provider, self.journal, self.clock = provider, journal, clock
        if startup_poll_seconds <= 0:
            raise ValueError("startup_poll_seconds must be positive")
        self.sleep_fn, self.startup_poll_seconds = sleep_fn, startup_poll_seconds
        self.cleanup_attempts = cleanup_attempts
        self.supervisor = supervisor
        self.guardian_gate = guardian_gate

    def run(self, request_id: str, plan: Mapping[str, Any], operation: Callable[[dict[str, Any]], Any]) -> dict[str, Any]:
        """Create one owned lease, run caller work without holding the journal lock, and verify teardown."""
        create_spec = plan.get("create_params")
        declared_types = [plan.get("rental_type"), plan.get("type")]
        if isinstance(create_spec, Mapping):
            declared_types.extend(
                create_spec.get(field)
                for field in ("rental_type", "type", "_broker_rental_type")
            )
        if any(isinstance(value, str) and value.strip().casefold() == "reserved" for value in declared_types):
            raise LeaseError(
                "reserved pricing requires a separately authorized prepaid conversion; "
                "it cannot run through the direct-create lease path"
            )
        hard_timeout = _duration(plan, "max_runtime_seconds", required=True)
        start_timeout = _duration(plan, "start_deadline_seconds", required=True)
        _duration(plan, "cold_start_timeout_seconds", required=True)
        _duration(plan, "idle_timeout_seconds", required=True)
        _duration(plan, "hung_request_timeout_seconds", required=True)
        if "offer_id" not in plan or not isinstance(plan.get("create_params"), Mapping):
            raise LeaseError("plan must include offer_id and an explicit create_params object")
        if self.guardian_gate is None:
            raise LeaseError("two remote cleanup paths and a shared registry must be configured before create")
        initiating_host_id = os.environ.get("VAST_BROKER_CONTROL_HOST_ID", "").strip()
        if not initiating_host_id:
            raise LeaseError("VAST_BROKER_CONTROL_HOST_ID must identify the initiating control host before create")
        plan_type = str(plan.get("rental_type", plan.get("type", ""))).lower()
        create_params = dict(plan["create_params"])
        if plan_type in {"bid", "interruptible"}:
            try:
                bid_cap = Decimal(str(plan["max_bid_usd_per_machine_hour"]))
                bid_step = Decimal(str(plan["bid_increment_usd"]))
                starting_bid = Decimal(str(plan["starting_bid_usd_per_machine_hour"]))
                attempts = plan["max_bid_attempts"]
                if (not all(x.is_finite() for x in (bid_cap, bid_step, starting_bid))
                        or bid_cap < Decimal("0.001") or bid_cap > Decimal("128")
                        or starting_bid < Decimal("0.001") or starting_bid > bid_cap
                        or bid_step <= 0 or isinstance(attempts, bool)
                        or not isinstance(attempts, int) or attempts < 0):
                    raise ValueError
            except (KeyError, InvalidOperation, ValueError, TypeError):
                raise LeaseError("bid lease requires finite machine-hour prices in Vast's documented range") from None
            if create_params.get("price") is not None:
                try:
                    request_bid = Decimal(str(create_params["price"]))
                except (InvalidOperation, ValueError, TypeError):
                    raise LeaseError("create price must match the authorized starting bid") from None
                if request_bid != starting_bid:
                    raise LeaseError("create price must match the authorized starting bid")
            create_params["price"] = str(starting_bid)
        elif create_params.get("price") is not None:
            raise LeaseError("on-demand create cannot include an interruptible bid price")
        policy: dict[str, float] = {"max_runtime_seconds": hard_timeout}
        for field in ("cold_start_timeout_seconds", "idle_timeout_seconds", "hung_request_timeout_seconds", "stop_after_idle_seconds", "retention_seconds", "start_deadline_seconds"):
            val = _duration(plan, field)
            if val is not None:
                policy[field] = val
        if ("stop_after_idle_seconds" in policy) != ("retention_seconds" in policy):
            raise LeaseError("stop_after_idle_seconds and retention_seconds must be configured together")
        attempt_id = uuid.uuid4().hex
        operation_fence = time.time_ns()
        label = f"vbr-{self.journal._key(request_id)[:16]}-{attempt_id[:12]}"
        intent: LeaseCreateIntent | None = None
        readiness: GuardianReadinessReceipt | None = None
        with self.journal.locked(request_id):
            prior = self.journal.load(request_id)
            if prior:
                detail = "an unresolved lease already exists for this request" if prior.get("state") not in {"DESTROYED", "FAILED_CLEAN"} else "this request already has a completed lease attempt"
                raise LeaseError(detail, prior)
            pre = self._list_required()
            pre_ids = {_instance_id(x) for x in pre}
            pre_volume_attachments, _ = _volume_snapshot(self._list_volumes_required())
            pre_volume_ids = set(pre_volume_attachments)
            now = self.clock()
            deadline_epoch = now + hard_timeout
            intent = LeaseCreateIntent(
                request_id=request_id,
                operation_id=request_id,
                attempt_id=attempt_id,
                attempt_label=label,
                operation_fence=operation_fence,
                deadline_epoch=deadline_epoch,
                initiating_host_id=initiating_host_id,
                preexisting_instance_ids=tuple(sorted(x for x in pre_ids if x)),
                preexisting_volume_ids=tuple(sorted(pre_volume_ids)),
            )
            record = {"schema_version": 1, "request_id": request_id, "label": label,
                      "attempt_id": attempt_id, "operation_fence": operation_fence,
                      "initiating_host_id": initiating_host_id,
                      "state": "CREATE_INTENT", "created_at_utc": _utc(now), "created_epoch": now,
                      "hard_deadline_epoch": deadline_epoch, "start_deadline_epoch": now + start_timeout, "policy": policy,
                      "pre_create_instance_ids": sorted(x for x in pre_ids if x),
                      "pre_create_volume_ids": sorted(pre_volume_ids),
                      "owned_instance_ids": [], "events": [{"at_utc": _utc(now), "event": "create_intent_persisted"}],
                      "plan": dict(plan)}
            self.journal.save(record)

        try:
            self._supervisor().start(request_id)
        except Exception as exc:
            with self.journal.locked(request_id):
                current = self.journal.load(request_id) or record
                if current.get("state") in {"CREATE_INTENT", "CREATE_IN_PROGRESS"} and not current.get("owned_instance_ids"):
                    current["state"] = "FAILED_CLEAN"
                    current["failure"] = "supervisor_start_failed"
                    self.journal.save(current)
            raise LeaseError("supervisor failed to start; no create was attempted", current) from exc

        try:
            readiness = self.guardian_gate.arm_before_create(intent)
            if not _readiness_matches(readiness, intent):
                raise ValueError("guardian readiness receipt did not match the durable intent")
        except Exception as exc:
            with self.journal.locked(request_id):
                current = self.journal.load(request_id) or record
                if current.get("state") == "CREATE_INTENT" and not current.get("owned_instance_ids"):
                    current["state"] = "FAILED_CLEAN"
                    current["failure"] = "guardian_readiness_failed"
                    current["guardian_readiness_error"] = getattr(exc, "reason_code", type(exc).__name__)
                    self.journal.save(current)
            raise LeaseError("the shared lease record and two remote cleanup paths could not be verified; no create was attempted", current) from None

        with self.journal.locked(request_id):
            current = self.journal.load(request_id)
            if not current or current.get("state") != "CREATE_INTENT":
                raise LeaseError("lease state changed before create; create was blocked", current or {})
            current["guardian_readiness_receipt"] = asdict(readiness)
            current["guardian_ready_at_utc"] = _utc(self.clock())
            self.journal.save(current)

        with self.journal.locked(request_id):
            current = self.journal.load(request_id)
            if not current or current.get("state") != "CREATE_INTENT":
                raise LeaseError("lease state changed before create; create was blocked", current or {})
            current["state"] = "CREATE_IN_PROGRESS"
            self.journal.save(current)

        create_params["label"] = label
        try:
            instance = self.provider.create_instance(plan["offer_id"], create_params)
        except Exception as exc:
            with self.journal.locked(request_id):
                current = self.journal.load(request_id)
                if current and current.get("state") not in {"DESTROYED", "FAILED_CLEAN"}:
                    current["state"] = "CREATE_UNCERTAIN"
                    current["failure"] = "create_response_ambiguous"
                    self.journal.save(current)
            reconciled = self.reconcile_create_uncertain(request_id) or record
            raise LeaseError("create outcome was ambiguous; no retry is allowed", reconciled) from exc

        iid = _instance_id(instance) if isinstance(instance, dict) else None
        if not iid:
            with self.journal.locked(request_id):
                current = self.journal.load(request_id) or record
                current["state"] = "CREATE_UNCERTAIN"
                current["failure"] = "create_response_missing_instance_id"
                self.journal.save(current)
            reconciled = self.reconcile_create_uncertain(request_id) or record
            raise LeaseError("create response did not identify its instance", reconciled)

        try:
            with self.journal.locked(request_id):
                current = self.journal.load(request_id)
                if current is None:
                    current = record
                if current.get("state") not in {"CREATE_IN_PROGRESS", "LEASE_OWNED", "STARTING"}:
                    owned = set(current.get("owned_instance_ids", []))
                    if iid not in owned:
                        current.setdefault("owned_instance_ids", []).append(iid)
                    current["state"] = "CLEANUP_PENDING"
                    self.journal.save(current)
                    handoff_allowed = False
                else:
                    ids = list(current.get("owned_instance_ids", []))
                    if iid not in ids:
                        ids.append(iid)
                    current["owned_instance_ids"] = ids
                    current["instance"] = dict(instance)
                    current["ownership_persisted_at_utc"] = _utc(self.clock())
                    current["state"] = "STARTING"
                    self.journal.save(current)
                    handoff_allowed = True
        except JournalError:
            # Intent and label were already durable; find the remote lease and clean it.
            reconciled = self.reconcile_create_uncertain(request_id)
            if reconciled and reconciled.get("owned_instance_ids"):
                with self.journal.locked(request_id):
                    latest = self.journal.load(request_id)
                    if latest and latest.get("state") not in {"DESTROYED", "FAILED_CLEAN"}:
                        latest["state"] = "DESTROY_REQUIRED"
                        self.journal.save(latest)
            self._cleanup_request(request_id)
            raise

        try:
            direct_volume_ids = set(_volume_ids(instance))
            preexisting_volume_ids = set(intent.preexisting_volume_ids)
            known_owned_volume_ids = direct_volume_ids - preexisting_volume_ids
            borrowed_volume_ids = direct_volume_ids & preexisting_volume_ids
            with self.journal.locked(request_id):
                current = self.journal.load(request_id)
                if not current or iid not in current.get("owned_instance_ids", []):
                    raise JournalError("local ownership record changed before access")
                current["owned_volume_ids"] = sorted(set(current.get("owned_volume_ids", [])) | known_owned_volume_ids)
                current["borrowed_volume_ids"] = sorted(set(current.get("borrowed_volume_ids", [])) | borrowed_volume_ids)
                self.journal.save(current)

            volume_attachments, volume_rows = _volume_snapshot(self._list_volumes_required())
            related_volume_ids = {
                volume_id for volume_id, attached_instances in volume_attachments.items()
                if iid in attached_instances
            }
            observed_volume_ids = direct_volume_ids | related_volume_ids
            known_owned_volume_ids |= observed_volume_ids - preexisting_volume_ids
            borrowed_volume_ids |= observed_volume_ids & preexisting_volume_ids
            missing_volume_rows = direct_volume_ids - set(volume_rows)
            with self.journal.locked(request_id):
                current = self.journal.load(request_id)
                if not current or iid not in current.get("owned_instance_ids", []):
                    raise JournalError("local ownership record changed before access")
                current["owned_volume_ids"] = sorted(set(current.get("owned_volume_ids", [])) | known_owned_volume_ids)
                current["borrowed_volume_ids"] = sorted(set(current.get("borrowed_volume_ids", [])) | borrowed_volume_ids)
                current["unresolved_volume_ids"] = sorted(set(current.get("unresolved_volume_ids", [])) | missing_volume_rows)
                self.journal.save(current)
            if borrowed_volume_ids:
                raise LeaseError("attaching a pre-existing separately billed volume is not supported")
            if missing_volume_rows:
                raise LeaseError("provider volume listing did not confirm every volume attached to the new instance")

            shared_record = self.guardian_gate.publish_owned_resources(
                intent,
                readiness,
                instance_ids=(iid,),
                volume_ids=tuple(sorted(known_owned_volume_ids)),
            )
            with self.journal.locked(request_id):
                current = self.journal.load(request_id)
                if not current or iid not in current.get("owned_instance_ids", []):
                    raise JournalError("local ownership record changed before access")
                current["guardian_registry_revision"] = shared_record.revision
                self.journal.save(current)
        except Exception as exc:
            with self.journal.locked(request_id):
                current = self.journal.load(request_id)
                if current and current.get("owned_instance_ids"):
                    current["state"] = "DESTROY_REQUIRED"
                    current["failure"] = "resource_ownership_handoff_failed"
                    current["resource_ownership_error"] = getattr(exc, "reason_code", type(exc).__name__)
                    try:
                        self.journal.save(current)
                    except JournalError:
                        pass
            cleaned = self._cleanup_request(request_id)
            self._supervisor().stop()
            raise LeaseError("provider resource ownership and shared lease registration could not be verified; access was blocked and cleanup was attempted", cleaned or {}) from None

        if not handoff_allowed:
            self._cleanup_request(request_id)
            raise LeaseError("lease was reclaimed by its supervisor before access could be handed over", self.status(request_id))

        failure: BaseException | None = None
        result: Any = None
        active_instance: dict[str, Any] = dict(instance)
        try:
            active_instance = self._await_started(request_id)
            bind_activity = getattr(operation, "bind_activity", None)
            if callable(bind_activity):
                bind_activity(lambda: self.record_activity(request_id, inference_completed=True))
            result = operation(dict(active_instance))
            self._persist_operation_result(request_id, result)
        except BaseException as exc:
            failure = exc
            with self.journal.locked(request_id):
                current = self.journal.load(request_id)
                if current and current.get("state") not in {"DESTROYED", "VERIFYING_DESTROY"}:
                    current["failure"] = type(exc).__name__
                    self.journal.save(current)
        finally:
            with self.journal.locked(request_id):
                current = self.journal.load(request_id)
                if current and current.get("state") not in {"DESTROYED", "FAILED_CLEAN"}:
                    current["state"] = "DESTROY_REQUIRED"
                    current.setdefault("events", []).append({"at_utc": _utc(self.clock()), "event": "cleanup_required"})
                    self.journal.save(current)
            cleaned = self._cleanup_request(request_id)
            self._supervisor().stop()

        out = {"request_id": request_id, "state": (cleaned or {}).get("state"),
               "instance": active_instance, "operation_result": result}
        if failure is not None:
            raise LeaseError("lease operation failed; cleanup was attempted", out) from failure
        if out["state"] != "DESTROYED":
            raise LeaseError("lease operation ended with unresolved cleanup", out)
        return out

    def _persist_operation_result(self, request_id: str, result: Any) -> None:
        """Durably record a bounded JSON result before the cleanup transition."""
        try:
            # Canonical key ordering lets the final receipt verifier recompute
            # this digest after the sorted journal has been written and read.
            encoded = json.dumps(result, ensure_ascii=False, allow_nan=False,
                                 separators=(",", ":"), sort_keys=True)
            if len(encoded.encode("utf-8")) > 1_000_000:
                raise ValueError("operation result exceeds the durable receipt limit")
            receipt = json.loads(encoded)
            digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        except (TypeError, ValueError, UnicodeEncodeError):
            raise LeaseError("operation result could not be durably recorded as bounded JSON") from None
        with self.journal.locked(request_id):
            current = self.journal.load(request_id)
            if (not current or not current.get("owned_instance_ids")
                    or current.get("state") in {"DESTROY_REQUIRED", "VERIFYING_DESTROY",
                                                  "CLEANUP_PENDING", "DESTROYED", "FAILED_CLEAN"}):
                raise LeaseError("lease ended before the operation result could be recorded", current or {})
            current["operation_result"] = receipt
            current["operation_result_sha256"] = digest
            current["operation_completed_epoch"] = self.clock()
            current["operation_completed_at_utc"] = _utc(current["operation_completed_epoch"])
            current.setdefault("events", []).append({
                "at_utc": current["operation_completed_at_utc"],
                "event": "operation_result_persisted",
            })
            self.journal.save(current)

    def status(self, request_id: str) -> dict[str, Any] | None:
        with self.journal.locked(request_id):
            return self.journal.load(request_id)

    def _supervisor(self) -> Any:
        if self.supervisor is None:
            from .process_supervisor import ProcessLeaseSupervisor
            self.supervisor = ProcessLeaseSupervisor(self.journal.directory)
        return self.supervisor

    def reconcile(self, request_id: str | None = None) -> list[dict[str, Any]]:
        records = [self.status(request_id)] if request_id is not None else self.journal.list_records()
        results: list[dict[str, Any]] = []
        for item in records:
            if item is None:
                continue
            rid = item["request_id"]
            if item.get("state") in {"CREATE_INTENT", "CREATE_IN_PROGRESS", "CREATE_UNCERTAIN"}:
                record = self.reconcile_create_uncertain(rid)
            else:
                record = self.status(rid)
                if record and (
                    record.get("owned_instance_ids")
                    or record.get("owned_volume_ids")
                    or record.get("unresolved_volume_ids")
                    or record.get("volume_discovery_pending")
                ) and record.get("state") not in {"DESTROYED", "FAILED_CLEAN"}:
                    record = self._cleanup_request(rid)
            if record:
                results.append(record)
        return results

    def reconcile_create_uncertain(self, request_id: str) -> dict[str, Any] | None:
        """Find labeled instances without holding the request lock across provider I/O."""
        with self.journal.locked(request_id):
            record = self.journal.load(request_id)
            if not record or record.get("state") not in {"CREATE_INTENT", "CREATE_IN_PROGRESS", "CREATE_UNCERTAIN"}:
                return record
            snapshot = dict(record)
        try:
            rows = self._list_required()
        except LeaseError:
            return snapshot
        prior = set(snapshot.get("pre_create_instance_ids", []))
        matches = [row for row in rows if row.get("label") == snapshot.get("label") and _instance_id(row) not in prior]
        ids = sorted({_instance_id(row) for row in matches if _instance_id(row)})
        now = self.clock()
        with self.journal.locked(request_id):
            record = self.journal.load(request_id)
            if not record or record.get("state") not in {"CREATE_INTENT", "CREATE_IN_PROGRESS", "CREATE_UNCERTAIN"}:
                return record
            if not ids:
                if record.get("state") == "CREATE_UNCERTAIN" or now >= record.get("start_deadline_epoch", float("inf")):
                    record["state"] = "CREATE_UNCERTAIN"
                    record["failure"] = "no_unambiguous_labeled_instance_found"
                    self.journal.save(record)
                return record
            current_ids = list(record.get("owned_instance_ids", []))
            for iid in ids:
                if iid not in current_ids:
                    current_ids.append(iid)
            record["owned_instance_ids"] = current_ids
            record["instance"] = next((row for row in matches if _instance_id(row) == ids[0]), {})
            if record.get("state") == "CREATE_UNCERTAIN" or len(ids) > 1:
                record["state"] = "DESTROY_REQUIRED"
                record.setdefault("events", []).append({"at_utc": _utc(now), "event": "ambiguous_create_owned_by_label"})
            else:
                record["state"] = "LEASE_OWNED"
                record["ownership_persisted_at_utc"] = _utc(now)
            self.journal.save(record)
        if record.get("state") == "DESTROY_REQUIRED":
            return self._cleanup_request(request_id)
        return record

    def reconcile_create_uncertain_worker(self, request_id: str) -> dict[str, Any] | None:
        """Worker entry point alias used by the independent supervisor process."""
        return self.reconcile_create_uncertain(request_id)

    def record_activity(self, request_id: str, *, inference_started: bool = False, inference_completed: bool = False) -> dict[str, Any]:
        """Persist authenticated model-work boundaries; polling/heartbeats are ignored."""
        with self.journal.locked(request_id):
            record = self.journal.load(request_id)
            if not record or record.get("state") in {"DESTROYED", "DESTROY_REQUIRED", "VERIFYING_DESTROY", "CLEANUP_PENDING", "STOPPING", "STOPPED_RETAINING"}:
                raise LeaseError("lease is not active", record or {})
            now = self.clock()
            if inference_started:
                record["active_request_epoch"] = now
            if inference_completed:
                record["active_request_epoch"] = None
                record["last_inference_epoch"] = now
                record["ready_epoch"] = record.get("ready_epoch") or now
                record["state"] = "READY"
            self.journal.save(record)
            return record

    def cancel(self, request_id: str) -> dict[str, Any] | None:
        with self.journal.locked(request_id):
            record = self.journal.load(request_id)
            if not record or record.get("state") in {"DESTROYED", "FAILED_CLEAN"}:
                return record
            if record.get("state") in {"CREATE_INTENT", "CREATE_IN_PROGRESS", "CREATE_UNCERTAIN"} and not record.get("owned_instance_ids"):
                record["state"] = "CREATE_UNCERTAIN"
                record["failure"] = "cancelled_with_ambiguous_create_outcome"
                self.journal.save(record)
                unresolved = True
            else:
                record["state"] = "DESTROY_REQUIRED"
                self.journal.save(record)
                unresolved = False
        if unresolved:
            record = self.reconcile_create_uncertain(request_id) or record
            if record.get("owned_instance_ids"):
                return self._cleanup_request(request_id)
            return record
        return self._cleanup_request(request_id)

    def _list_required(self) -> list[dict[str, Any]]:
        try:
            rows = _instances(self.provider.list_instances())
            _instance_inventory_receipt(rows)
            return rows
        except Exception as exc:
            raise LeaseError("provider listing is unavailable or malformed; absence is unverified") from exc

    def _list_volumes_required(self) -> list[dict[str, Any]]:
        try:
            rows = self.provider.list_volumes()
            _volume_snapshot(rows)
            return rows
        except Exception as exc:
            raise LeaseError("provider volume listing is unavailable or malformed; volume absence is unverified") from exc

    def _await_started(self, request_id: str) -> dict[str, Any]:
        record = self.status(request_id)
        if not record:
            raise LeaseError("lease journal disappeared before startup")
        iid = record["owned_instance_ids"][0]
        plan = record["plan"]
        is_bid = str(plan.get("rental_type", plan.get("type", ""))).lower() in {"bid", "interruptible"}
        bid_limit: Decimal | None = None
        bid_increment: Decimal | None = None
        bid_price: Decimal | None = None
        bid_attempts = 0
        max_attempts = 0
        if is_bid:
            try:
                bid_limit = Decimal(str(plan["max_bid_usd_per_machine_hour"]))
                bid_increment = Decimal(str(plan["bid_increment_usd"]))
                bid_price = Decimal(str(plan["starting_bid_usd_per_machine_hour"]))
                max_attempts = plan["max_bid_attempts"]
                if (not bid_limit.is_finite() or not bid_increment.is_finite() or not bid_price.is_finite()
                        or bid_limit <= 0 or bid_increment <= 0 or bid_price <= 0
                        or bid_price > bid_limit or isinstance(max_attempts, bool)
                        or not isinstance(max_attempts, int) or max_attempts < 0):
                    raise ValueError
            except (KeyError, InvalidOperation, ValueError, TypeError) as exc:
                raise LeaseError("bid lease is missing finite per-machine bid limits", record) from exc
        while self.clock() < record["start_deadline_epoch"]:
            latest = self.status(request_id)
            if not latest or latest.get("state") in {"DESTROY_REQUIRED", "VERIFYING_DESTROY", "CLEANUP_PENDING", "DESTROYED", "FAILED_CLEAN"}:
                raise LeaseError("supervisor ended this lease before startup", latest or {})
            try:
                instance = self.provider.get_instance(iid)
            except Exception:
                instance = None
            if isinstance(instance, dict):
                state = str(instance.get("actual_status", instance.get("status", instance.get("state", "")))).lower()
                if state in {"running", "started", "active"}:
                    with self.journal.locked(request_id):
                        current = self.journal.load(request_id)
                        if not current or current.get("state") in {"DESTROY_REQUIRED", "VERIFYING_DESTROY", "CLEANUP_PENDING", "DESTROYED"}:
                            raise LeaseError("supervisor ended this lease before startup", current or {})
                        current["instance"] = dict(instance)
                        current["state"] = "INSTALLING"
                        current["started_at_utc"] = _utc(self.clock())
                        self.journal.save(current)
                    return dict(instance)
                if state in {"exited", "deleted", "destroyed", "error", "failed"}:
                    raise LeaseError("provider reported a failed start", latest)
            if is_bid and bid_attempts < max_attempts and bid_price is not None and bid_increment is not None and bid_limit is not None:
                next_bid = min(bid_price + bid_increment, bid_limit)
                if next_bid > bid_price:
                    bid_attempts += 1
                    with self.journal.locked(request_id):
                        current = self.journal.load(request_id)
                        if not current or current.get("state") in {"DESTROY_REQUIRED", "VERIFYING_DESTROY", "CLEANUP_PENDING", "DESTROYED"}:
                            raise LeaseError("supervisor ended this lease before bid update", current or {})
                        current.setdefault("bid_history", []).append({"attempt": bid_attempts, "price_usd_per_machine_hour": str(next_bid), "at_utc": _utc(self.clock())})
                        self.journal.save(current)
                    try:
                        self.provider.change_bid(iid, str(next_bid))
                        bid_price = next_bid
                    except Exception as exc:
                        with self.journal.locked(request_id):
                            current = self.journal.load(request_id)
                            if current:
                                current["last_bid_change_error"] = type(exc).__name__
                                self.journal.save(current)
            self.sleep_fn(min(self.startup_poll_seconds, max(0.0, record["start_deadline_epoch"] - self.clock())))
        with self.journal.locked(request_id):
            current = self.journal.load(request_id)
            if current and current.get("state") not in {"DESTROYED", "VERIFYING_DESTROY"}:
                current["failure"] = "start_deadline_expired"
                current["state"] = "DESTROY_REQUIRED"
                self.journal.save(current)
        raise LeaseError("instance did not start before its bounded deadline", self.status(request_id) or {})

    def _cleanup_request(self, request_id: str) -> dict[str, Any] | None:
        # Tell the independent recovery paths before local cleanup starts. If the
        # shared registry is down, local cleanup still proceeds and the bounded
        # lease deadline remains the guardians' fallback trigger.
        operation_fence: int | None = None
        with self.journal.locked(request_id):
            initial = self.journal.load(request_id)
            if not initial:
                return None
            if initial.get("state") in {"DESTROYED", "FAILED_CLEAN"}:
                return initial
            value = initial.get("operation_fence")
            if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                operation_fence = value
        request_cleanup = getattr(self.guardian_gate, "request_cleanup", None)
        if operation_fence is not None and callable(request_cleanup):
            try:
                request_cleanup(request_id, operation_fence)
            except Exception as exc:
                with self.journal.locked(request_id):
                    current = self.journal.load(request_id)
                    if current and current.get("state") not in {"DESTROYED", "FAILED_CLEAN"}:
                        current["guardian_cleanup_signal_error_type"] = type(exc).__name__
                        self.journal.save(current)

        with self.journal.locked(request_id):
            record = self.journal.load(request_id)
            if not record:
                return None
            if record.get("state") in {"DESTROYED", "FAILED_CLEAN"}:
                return record
            ids = list(dict.fromkeys(record.get("owned_instance_ids", [])))
            volume_ids = list(dict.fromkeys(record.get("owned_volume_ids", [])))
            unresolved_volume_ids = list(dict.fromkeys(record.get("unresolved_volume_ids", [])))
            if (not ids and not volume_ids and not unresolved_volume_ids
                    and not record.get("volume_discovery_pending")):
                if record.get("state") in {"CREATE_INTENT", "CREATE_IN_PROGRESS", "CREATE_UNCERTAIN"}:
                    return record
                record["state"] = "CLEANUP_PENDING"
                self.journal.save(record)
                return record
            record["state"] = "VERIFYING_DESTROY"
            self.journal.save(record)
        for attempt in range(1, self.cleanup_attempts + 1):
            with self.journal.locked(request_id):
                current = self.journal.load(request_id)
                if not current:
                    return None
                ids = list(dict.fromkeys(current.get("owned_instance_ids", [])))
                volume_ids = list(dict.fromkeys(current.get("owned_volume_ids", [])))
                unresolved_volume_ids = list(dict.fromkeys(current.get("unresolved_volume_ids", [])))
                has_volume_baseline = "pre_create_volume_ids" in current
                preexisting_volume_ids = set(current.get("pre_create_volume_ids", []))
                direct_volume_ids = set()
                if isinstance(current.get("instance"), Mapping):
                    try:
                        direct_volume_ids.update(_volume_ids(current["instance"]))
                    except LeaseError:
                        current.setdefault("cleanup_warnings", []).append("malformed_volume_ids_in_instance_record")
                newly_owned = direct_volume_ids - preexisting_volume_ids if has_volume_baseline else set()
                if not has_volume_baseline:
                    current["unresolved_volume_ids"] = sorted(
                        set(current.get("unresolved_volume_ids", [])) | (direct_volume_ids - set(volume_ids))
                    )
                volume_ids = list(dict.fromkeys(volume_ids + sorted(newly_owned)))
                current["owned_volume_ids"] = volume_ids
                known_volume_references = (
                    direct_volume_ids
                    | set(volume_ids)
                    | set(current.get("borrowed_volume_ids", []))
                    | set(current.get("unresolved_volume_ids", []))
                )
                volume_discovery_anchor_required = (
                    bool(current.get("volume_discovery_pending"))
                    and bool(ids)
                    and not known_volume_references
                )
                self.journal.save(current)

            # Learn every newly attached volume while the instance-to-volume relation is
            # still visible. Never delete a volume that existed before this attempt.
            volume_listing_error = None
            pre_destroy_volume_listing_failed = False
            discovery_anchor_error = None
            if volume_discovery_anchor_required:
                try:
                    anchor_rows = self._list_required()
                    present_anchor_ids = {_instance_id(row) for row in anchor_rows}
                    if not set(ids).issubset(present_anchor_ids):
                        discovery_anchor_error = "DiscoveryAnchorMissing"
                except Exception as exc:
                    discovery_anchor_error = type(exc).__name__

            if discovery_anchor_error:
                # An earlier inventory outage made the instance the only ownership
                # anchor. If it has since disappeared, do not infer that its volumes
                # were also removed; preserve the unresolved obligation for recovery.
                before_attachments, before_rows = {}, {}
            else:
                try:
                    before_attachments, before_rows = _volume_snapshot(self._list_volumes_required())
                    attached_now = {
                        volume_id for volume_id, attached in before_attachments.items()
                        if set(attached) & set(ids)
                    }
                    direct_rows = direct_volume_ids & set(before_rows)
                    if has_volume_baseline:
                        discovered_owned = (attached_now | direct_rows) - preexisting_volume_ids
                        discovered_borrowed = (attached_now | direct_rows) & preexisting_volume_ids
                        unidentified_attached = set()
                    else:
                        discovered_owned = set()
                        discovered_borrowed = set()
                        unidentified_attached = attached_now - set(volume_ids)
                    with self.journal.locked(request_id):
                        current = self.journal.load(request_id)
                        if not current:
                            return None
                        current["owned_volume_ids"] = sorted(set(current.get("owned_volume_ids", [])) | discovered_owned)
                        current["borrowed_volume_ids"] = sorted(set(current.get("borrowed_volume_ids", [])) | discovered_borrowed)
                        unresolved = set(current.get("unresolved_volume_ids", [])) & set(before_rows)
                        unresolved -= discovered_owned | discovered_borrowed
                        current["unresolved_volume_ids"] = sorted(unresolved | unidentified_attached)
                        current["volume_discovery_pending"] = False
                        self.journal.save(current)
                        volume_ids = list(current["owned_volume_ids"])
                        unresolved_volume_ids = list(current.get("unresolved_volume_ids", []))
                except Exception as exc:
                    before_attachments, before_rows = {}, {}
                    volume_listing_error = type(exc).__name__
                    pre_destroy_volume_listing_failed = True
                    if ids and not known_volume_references:
                        # Keep the instance alive until a complete volume listing can reveal
                        # its attachments; destroying it now can erase the only association.
                        with self.journal.locked(request_id):
                            current = self.journal.load(request_id)
                            if current:
                                current["volume_discovery_pending"] = True
                                self.journal.save(current)

            hold_instance_for_volume_discovery = (
                discovery_anchor_error is not None
                or (
                    pre_destroy_volume_listing_failed
                    and bool(ids)
                    and not known_volume_references
                )
            )
            if not hold_instance_for_volume_discovery:
                for iid in ids:
                    try:
                        self.provider.destroy_instance(iid)
                    except Exception:
                        pass
            try:
                rows = self._list_required()
                present_instances = {_instance_id(row) for row in rows}
                instance_inventory = _instance_inventory_receipt(rows)
                list_error = None
            except Exception as exc:
                rows = None
                present_instances = None
                instance_inventory = None
                list_error = type(exc).__name__
            remaining_instances = [iid for iid in ids if present_instances is None or iid in present_instances]

            # A volume can be deleted only after every instance using it is gone.
            if not remaining_instances:
                try:
                    current_volume_attachments, current_volume_rows = _volume_snapshot(self._list_volumes_required())
                    current_volume_ids = set(current_volume_rows)
                    for volume_id in volume_ids:
                        if volume_id not in current_volume_ids:
                            continue
                        attached = current_volume_attachments[volume_id]
                        if attached:
                            continue
                        try:
                            self.provider.destroy_volume(volume_id)
                        except Exception:
                            pass
                    # The delete acknowledgement is not proof; a fresh complete listing is.
                    _, verified_volume_rows = _volume_snapshot(self._list_volumes_required())
                    present_volume_ids = set(verified_volume_rows)
                    volume_listing_error = None
                    if pre_destroy_volume_listing_failed and has_volume_baseline:
                        unresolved_candidates = present_volume_ids - preexisting_volume_ids - set(volume_ids)
                        if unresolved_candidates:
                            with self.journal.locked(request_id):
                                current = self.journal.load(request_id)
                                if current:
                                    current["unresolved_volume_ids"] = sorted(
                                        set(current.get("unresolved_volume_ids", [])) | unresolved_candidates
                                    )
                                    self.journal.save(current)
                except Exception as exc:
                    present_volume_ids = None
                    volume_listing_error = type(exc).__name__
            else:
                present_volume_ids = None

            with self.journal.locked(request_id):
                current = self.journal.load(request_id)
                if not current:
                    return None
                all_owned = list(dict.fromkeys(current.get("owned_instance_ids", [])))
                all_owned_volumes = list(dict.fromkeys(current.get("owned_volume_ids", [])))
                unresolved_volume_ids = list(dict.fromkeys(current.get("unresolved_volume_ids", [])))
                remaining = [iid for iid in all_owned if present_instances is None or iid in present_instances]
                remaining_volumes = [vid for vid in all_owned_volumes if present_volume_ids is None or vid in present_volume_ids]
                unresolved_present = [vid for vid in unresolved_volume_ids if present_volume_ids is None or vid in present_volume_ids]
                verification = {
                    "attempt": attempt,
                    "checked_at_utc": _utc(self.clock()),
                    "remaining_instance_ids": remaining,
                    "remaining_volume_ids": sorted(set(remaining_volumes) | set(unresolved_present)),
                }
                if instance_inventory is not None:
                    verification["instance_inventory"] = instance_inventory
                if list_error:
                    verification["instance_list_error_type"] = list_error
                if volume_listing_error:
                    verification["volume_list_error_type"] = volume_listing_error
                if discovery_anchor_error:
                    verification["volume_discovery_anchor_error_type"] = discovery_anchor_error
                current["verification"] = verification
                keep_instance_as_discovery_anchor = bool(current.get("volume_discovery_pending"))
                current["owned_instance_ids"] = all_owned if remaining or keep_instance_as_discovery_anchor else []
                current["owned_volume_ids"] = all_owned_volumes if remaining_volumes else []
                current["unresolved_volume_ids"] = unresolved_present
                if (
                    not remaining
                    and not remaining_volumes
                    and not unresolved_present
                    and volume_listing_error is None
                    and not current.get("volume_discovery_pending")
                ):
                    current["state"] = "DESTROYED"
                    current["confirmed_absent_at_utc"] = _utc(self.clock())
                    current["absence_evidence"] = {
                        "checked_at_utc": verification["checked_at_utc"],
                        "instance_inventory": instance_inventory,
                        "owned_instance_ids": sorted(all_owned),
                        "owned_instance_ids_absent": True,
                        "owned_volume_ids": sorted(set(all_owned_volumes) | set(unresolved_volume_ids)),
                        "owned_volume_ids_absent": True,
                    }
                    current.setdefault("events", []).append({"at_utc": _utc(self.clock()), "event": "provider_instance_and_volume_absence_confirmed"})
                    self.journal.save(current)
                    return current
                current["state"] = "CLEANUP_PENDING"
                self.journal.save(current)
        return self.status(request_id)
