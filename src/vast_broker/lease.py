"""Bounded lease ownership and cleanup transitions for temporary Vast instances.

This module never reports a host/network failure as a provider-side deletion guarantee.
"""
from __future__ import annotations

import math
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Mapping, Protocol

from .journal import JournalError, LeaseJournal


class LeaseProvider(Protocol):
    def list_instances(self) -> list[dict[str, Any]]: ...
    def get_instance(self, instance_id: str) -> dict[str, Any] | None: ...
    def create_instance(self, offer_id: int, params: dict[str, Any]) -> dict[str, Any]: ...
    def stop_instance(self, instance_id: str) -> Any: ...
    def destroy_instance(self, instance_id: str) -> Any: ...
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


def _instance_id(instance: Mapping[str, Any]) -> str | None:
    value = instance.get("id", instance.get("instance_id", instance.get("contract_id", instance.get("new_contract"))))
    return str(value) if value is not None and str(value) else None


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
                 *, clock: Callable[[], float] = time.time, sleep_fn: Callable[[float], None] = time.sleep,
                 startup_poll_seconds: float = 1.0, cleanup_attempts: int = 3):
        if cleanup_attempts < 1:
            raise ValueError("cleanup_attempts must be at least one")
        self.provider, self.journal, self.clock = provider, journal, clock
        if startup_poll_seconds <= 0:
            raise ValueError("startup_poll_seconds must be positive")
        self.sleep_fn, self.startup_poll_seconds = sleep_fn, startup_poll_seconds
        self.cleanup_attempts = cleanup_attempts
        self.supervisor = supervisor

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
        now = self.clock()
        label = f"vbr-{self.journal._key(request_id)[:16]}-{uuid.uuid4().hex[:8]}"
        policy: dict[str, float] = {"max_runtime_seconds": hard_timeout}
        for field in ("cold_start_timeout_seconds", "idle_timeout_seconds", "hung_request_timeout_seconds", "stop_after_idle_seconds", "retention_seconds", "start_deadline_seconds"):
            val = _duration(plan, field)
            if val is not None:
                policy[field] = val
        if ("stop_after_idle_seconds" in policy) != ("retention_seconds" in policy):
            raise LeaseError("stop_after_idle_seconds and retention_seconds must be configured together")
        with self.journal.locked(request_id):
            prior = self.journal.load(request_id)
            if prior:
                detail = "an unresolved lease already exists for this request" if prior.get("state") not in {"DESTROYED", "FAILED_CLEAN"} else "this request already has a completed lease attempt"
                raise LeaseError(detail, prior)
            pre = self._list_required()
            pre_ids = {_instance_id(x) for x in pre}
            record = {"schema_version": 1, "request_id": request_id, "label": label,
                      "state": "CREATE_INTENT", "created_at_utc": _utc(now), "created_epoch": now,
                      "hard_deadline_epoch": now + hard_timeout, "start_deadline_epoch": now + start_timeout, "policy": policy,
                      "pre_create_instance_ids": sorted(x for x in pre_ids if x),
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
        if not handoff_allowed:
            self._cleanup_request(request_id)
            raise LeaseError("lease was reclaimed by its supervisor before access could be handed over", self.status(request_id))

        failure: BaseException | None = None
        result: Any = None
        active_instance: dict[str, Any] = dict(instance)
        try:
            active_instance = self._await_started(request_id)
            result = operation(dict(active_instance))
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
                if record and record.get("owned_instance_ids") and record.get("state") not in {"DESTROYED", "FAILED_CLEAN"}:
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
            return _instances(self.provider.list_instances())
        except Exception as exc:
            raise LeaseError("provider listing is unavailable or malformed; absence is unverified") from exc

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
        with self.journal.locked(request_id):
            record = self.journal.load(request_id)
            if not record:
                return None
            if record.get("state") in {"DESTROYED", "FAILED_CLEAN"}:
                return record
            ids = list(dict.fromkeys(record.get("owned_instance_ids", [])))
            if not ids:
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
            for iid in ids:
                try:
                    self.provider.destroy_instance(iid)
                except Exception:
                    pass
            try:
                rows = self._list_required()
                present = {_instance_id(row) for row in rows}
                list_error = None
            except Exception as exc:
                present = None
                list_error = type(exc).__name__
            with self.journal.locked(request_id):
                current = self.journal.load(request_id)
                if not current:
                    return None
                all_owned = list(dict.fromkeys(current.get("owned_instance_ids", [])))
                remaining = [iid for iid in all_owned if present is None or iid in present]
                verification = {"attempt": attempt, "checked_at_utc": _utc(self.clock()), "remaining_instance_ids": remaining}
                if list_error:
                    verification["error_type"] = list_error
                current["verification"] = verification
                if not remaining:
                    current["state"] = "DESTROYED"
                    current["confirmed_absent_at_utc"] = _utc(self.clock())
                    current["owned_instance_ids"] = []
                    current.setdefault("events", []).append({"at_utc": _utc(self.clock()), "event": "provider_absence_confirmed"})
                    self.journal.save(current)
                    return current
                current["state"] = "CLEANUP_PENDING"
                current["owned_instance_ids"] = all_owned
                self.journal.save(current)
        return self.status(request_id)
