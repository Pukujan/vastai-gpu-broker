"""Fake-only recovery integration tests for the public lease and guardian APIs.

The file-backed registry/provider let separate Python objects (and a spawned
controller process) observe shared durable truth. The tests do not establish
that real deployments occupy separate physical or administrative failure
domains, and they do not exercise Vast or a production registry.
"""
from __future__ import annotations

import json
import multiprocessing
import os
import signal
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Any

import pytest

from vast_broker.guardians import (
    FenceReservation,
    GuardianAck,
    GuardianCapabilities,
    GuardianDescriptor,
    GuardianKind,
    GuardianReadinessGate,
    GuardianRecoveryReceipt,
    LeaseCreateIntent,
    LeaseRegistrySnapshot,
    RecoveryState,
    validate_guardian_recovery_receipt,
)
from vast_broker.journal import LeaseJournal
from vast_broker.lease import LeaseController, LeaseError


REGISTRY_ID = "file-backed-test-registry"
INITIATING_HOST = "initiating-test-host"
CAPABILITIES = GuardianCapabilities(True, True, True, True, True)


def _atomic_json_write(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    with temp_path.open("w", encoding="utf-8") as handle:
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp_path, path)


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


class RegistryControl:
    """Test switch shared by independently constructed registry clients."""

    def __init__(self, path: Path):
        self.path = path
        _atomic_json_write(path, {"available": True})

    def set_available(self, available: bool) -> None:
        _atomic_json_write(self.path, {"available": available})

    def is_available(self) -> bool:
        return _read_json(self.path)["available"] is True


class ProviderControl:
    """Shared fake provider/network availability, separate from provider truth."""

    def __init__(self, path: Path):
        self.path = path
        _atomic_json_write(
            path,
            {
                "network_available": True,
                "vast_available": True,
                "instance_listing_available": True,
                "volume_listing_available": True,
            },
        )

    def set_outage(
        self,
        *,
        network: bool | None = None,
        vast: bool | None = None,
        instance_listing: bool | None = None,
        volume_listing: bool | None = None,
    ) -> None:
        state = _read_json(self.path)
        if network is not None:
            state["network_available"] = network
        if vast is not None:
            state["vast_available"] = vast
        if instance_listing is not None:
            state["instance_listing_available"] = instance_listing
        if volume_listing is not None:
            state["volume_listing_available"] = volume_listing
        _atomic_json_write(self.path, state)

    def get(self) -> dict[str, bool]:
        return _read_json(self.path)


class FileBackedFencedRegistry:
    """Small durable CAS-like registry fake implementing the public protocol."""

    registry_id = REGISTRY_ID

    def __init__(self, data_path: Path, control_path: Path, *, credential_available: bool = True):
        self.data_path = data_path
        self.control_path = control_path
        self.credential_available = credential_available
        if not data_path.exists():
            _atomic_json_write(data_path, {"fences": {}, "records": {}})

    def _require_access(self) -> None:
        if not self.credential_available:
            raise PermissionError("fake registry credential is unavailable")
        if _read_json(self.control_path)["available"] is not True:
            raise TimeoutError("fake shared registry is unavailable")

    def _load(self) -> dict[str, Any]:
        self._require_access()
        return _read_json(self.data_path)

    def reserve_fence(self, *, request_id: str, attempt_id: str, operation_fence: int) -> FenceReservation:
        state = self._load()
        previous = state["fences"].get(request_id)
        if previous:
            if previous == {"attempt_id": attempt_id, "operation_fence": operation_fence}:
                return FenceReservation(self.registry_id, request_id, attempt_id, operation_fence, True)
            if operation_fence <= previous["operation_fence"]:
                raise ValueError("stale or reused operation fence")
        state["fences"][request_id] = {"attempt_id": attempt_id, "operation_fence": operation_fence}
        _atomic_json_write(self.data_path, state)
        return FenceReservation(self.registry_id, request_id, attempt_id, operation_fence, True)

    def publish_intent(self, intent: LeaseCreateIntent) -> LeaseRegistrySnapshot:
        state = self._load()
        fence = state["fences"].get(intent.request_id)
        if fence != {"attempt_id": intent.attempt_id, "operation_fence": intent.operation_fence}:
            raise ValueError("CREATE_INTENT does not own the reserved fence")
        previous = state["records"].get(intent.request_id)
        if previous:
            snapshot = self._snapshot(previous)
            if snapshot.intent_digest != intent.digest or snapshot.operation_fence != intent.operation_fence:
                raise ValueError("request already has a different fenced intent")
            return snapshot
        record = {
            "registry_id": self.registry_id,
            "request_id": intent.request_id,
            "operation_fence": intent.operation_fence,
            "intent_digest": intent.digest,
            "state": "CREATE_INTENT",
            "deadline_epoch": intent.deadline_epoch,
            "durable": True,
            "revision": 1,
            "guardian_acks": [],
            "owned_instance_ids": [],
            "owned_volume_ids": [],
            "attempt_label": intent.attempt_label,
            "preexisting_instance_ids": list(intent.preexisting_instance_ids),
            "preexisting_volume_ids": list(intent.preexisting_volume_ids),
        }
        state["records"][intent.request_id] = record
        _atomic_json_write(self.data_path, state)
        return self._snapshot(record)

    def read(self, request_id: str) -> LeaseRegistrySnapshot | None:
        state = self._load()
        record = state["records"].get(request_id)
        return self._snapshot(record) if record else None

    def acknowledge_guardian(self, ack: GuardianAck) -> LeaseRegistrySnapshot:
        state = self._load()
        record = state["records"].get(ack.request_id)
        if not record or record["operation_fence"] != ack.operation_fence:
            raise ValueError("acknowledgement has no matching intent")
        acks = {item["guardian_id"]: item for item in record["guardian_acks"]}
        old = acks.get(ack.guardian_id)
        new = asdict(ack)
        if old is not None and old != new:
            raise ValueError("guardian acknowledgement changed under the same fence")
        acks[ack.guardian_id] = new
        record["guardian_acks"] = list(acks.values())
        record["revision"] += 1
        _atomic_json_write(self.data_path, state)
        return self._snapshot(record)

    def publish_owned_resources(
        self,
        *,
        request_id: str,
        operation_fence: int,
        intent_digest: str,
        instance_ids: tuple[str, ...],
        volume_ids: tuple[str, ...],
    ) -> LeaseRegistrySnapshot:
        state = self._load()
        record = state["records"].get(request_id)
        if not record or (record["operation_fence"], record["intent_digest"]) != (operation_fence, intent_digest):
            raise ValueError("resource publication does not match the current fence")
        record["owned_instance_ids"] = sorted(set(record["owned_instance_ids"]) | set(instance_ids))
        record["owned_volume_ids"] = sorted(set(record["owned_volume_ids"]) | set(volume_ids))
        record["state"] = "LEASE_OWNED"
        record["revision"] += 1
        _atomic_json_write(self.data_path, state)
        return self._snapshot(record)

    @staticmethod
    def _snapshot(record: dict[str, Any]) -> LeaseRegistrySnapshot:
        acks = tuple(
            GuardianAck(
                guardian_id=item["guardian_id"],
                request_id=item["request_id"],
                operation_fence=item["operation_fence"],
                intent_digest=item["intent_digest"],
                registry_id=item["registry_id"],
                control_host_id=item["control_host_id"],
                failure_domain_id=item["failure_domain_id"],
                capabilities=GuardianCapabilities(**item["capabilities"]),
                read_revision=item["read_revision"],
            )
            for item in record["guardian_acks"]
        )
        return LeaseRegistrySnapshot(
            registry_id=record["registry_id"],
            request_id=record["request_id"],
            operation_fence=record["operation_fence"],
            intent_digest=record["intent_digest"],
            state=record["state"],
            deadline_epoch=record["deadline_epoch"],
            attempt_label=record["attempt_label"],
            preexisting_instance_ids=tuple(record["preexisting_instance_ids"]),
            preexisting_volume_ids=tuple(record["preexisting_volume_ids"]),
            durable=record["durable"],
            revision=record["revision"],
            guardian_acks=acks,
            owned_instance_ids=tuple(record["owned_instance_ids"]),
            owned_volume_ids=tuple(record["owned_volume_ids"]),
        )


class FileProviderTruth:
    """Provider state persists separately from all controller/guardian clients."""

    def __init__(self, path: Path):
        self.path = path
        if not path.exists():
            _atomic_json_write(
                path,
                {
                    "next_instance": 1,
                    "next_volume": 9001,
                    "instances": {
                        "unrelated-instance": {
                            "id": "unrelated-instance",
                            "label": "owner-resource",
                            "status": "running",
                            "volume_ids": ["9000"],
                        }
                    },
                    "volumes": {"9000": {"id": "9000", "instances": [{"id": "unrelated-instance"}]}},
                    "create_count": 0,
                    "events": [],
                },
            )

    def read(self) -> dict[str, Any]:
        return _read_json(self.path)

    def write(self, state: dict[str, Any]) -> None:
        _atomic_json_write(self.path, state)


class FileProviderClient:
    """Each instance is an independent client over the same provider truth file."""

    def __init__(self, truth_path: Path, control_path: Path):
        self.truth = FileProviderTruth(truth_path)
        self.control_path = control_path

    def _require_api(self) -> None:
        state = _read_json(self.control_path)
        if not state["network_available"]:
            raise TimeoutError("fake provider network route is unavailable")
        if not state["vast_available"]:
            raise ConnectionError("fake Vast API is unavailable")

    def list_instances(self) -> list[dict[str, Any]]:
        self._require_api()
        if not _read_json(self.control_path)["instance_listing_available"]:
            raise TimeoutError("fake provider instance listing is unavailable")
        return [dict(row) for row in self.truth.read()["instances"].values()]

    def list_volumes(self) -> list[dict[str, Any]]:
        self._require_api()
        if not _read_json(self.control_path)["volume_listing_available"]:
            raise TimeoutError("fake provider volume listing is unavailable")
        return [dict(row) for row in self.truth.read()["volumes"].values()]

    def get_instance(self, instance_id: str) -> dict[str, Any] | None:
        self._require_api()
        row = self.truth.read()["instances"].get(str(instance_id))
        return dict(row) if row else None

    def create_instance(self, offer_id: Any, params: dict[str, Any]) -> dict[str, Any]:
        self._require_api()
        state = self.truth.read()
        iid = str(state["next_instance"])
        vid = str(state["next_volume"])
        state["next_instance"] += 1
        state["next_volume"] += 1
        state["create_count"] += 1
        instance = {
            "id": iid,
            "label": params["label"],
            "status": "running",
            "offer_id": offer_id,
            "volume_ids": [vid],
        }
        state["instances"][iid] = instance
        state["volumes"][vid] = {"id": vid, "instances": [{"id": iid}]}
        state["events"].append(["create", iid])
        self.truth.write(state)
        return dict(instance)

    def destroy_instance(self, instance_id: str) -> dict[str, bool]:
        self._require_api()
        state = self.truth.read()
        iid = str(instance_id)
        state["events"].append(["destroy_instance", iid])
        state["instances"].pop(iid, None)
        for volume in state["volumes"].values():
            volume["instances"] = [item for item in volume["instances"] if str(item.get("id")) != iid]
        self.truth.write(state)
        return {"success": True}

    def destroy_volume(self, volume_id: str) -> dict[str, bool]:
        self._require_api()
        state = self.truth.read()
        vid = str(volume_id)
        state["events"].append(["destroy_volume", vid])
        volume = state["volumes"].get(vid)
        if volume and volume["instances"]:
            raise RuntimeError("fake provider cannot delete an attached volume")
        state["volumes"].pop(vid, None)
        self.truth.write(state)
        return {"success": True}

    def stop_instance(self, instance_id: str) -> dict[str, bool]:
        self._require_api()
        state = self.truth.read()
        row = state["instances"].get(str(instance_id))
        if row:
            row["status"] = "stopped"
            self.truth.write(state)
        return {"success": True}

    def change_bid(self, instance_id: str, price: str) -> dict[str, bool]:
        self._require_api()
        return {"success": True}


class NoopSupervisor:
    """Only the public API's start/stop contract; no separate worker is launched."""

    def start(self, request_id: str) -> None:
        self.request_id = request_id

    def stop(self) -> None:
        pass


class FileRecoveryGuardian:
    """Fake worker with its own registry and provider client objects."""

    def __init__(
        self,
        guardian_id: str,
        host: str,
        domain: str,
        registry_path: Path,
        registry_control_path: Path,
        provider_truth_path: Path,
        provider_control_path: Path,
        alert_path: Path,
        *,
        kind: GuardianKind = GuardianKind.INDEPENDENT_SERVICE,
        registry_credential_available: bool = True,
    ):
        self.descriptor = GuardianDescriptor(
            guardian_id=guardian_id,
            control_host_id=host,
            failure_domain_id=domain,
            kind=kind,
            trusted=True,
            capabilities=CAPABILITIES,
        )
        self.registry = FileBackedFencedRegistry(
            registry_path, registry_control_path,
            credential_available=registry_credential_available,
        )
        self.provider = FileProviderClient(provider_truth_path, provider_control_path)
        self.alert_path = alert_path
        self.available = True

    def acknowledge_intent(
        self,
        *,
        registry_id: str,
        request_id: str,
        operation_fence: int,
        intent_digest: str,
    ) -> GuardianAck:
        if not self.available:
            raise TimeoutError("fake guardian is stopped")
        record = self.registry.read(request_id)
        if not record or record.registry_id != registry_id:
            raise RuntimeError("fake guardian could not read the shared intent")
        if record.operation_fence != operation_fence or record.intent_digest != intent_digest:
            raise ValueError("fake guardian received a stale or different intent")
        return GuardianAck(
            guardian_id=self.descriptor.guardian_id,
            request_id=request_id,
            operation_fence=operation_fence,
            intent_digest=intent_digest,
            registry_id=registry_id,
            control_host_id=self.descriptor.control_host_id,
            failure_domain_id=self.descriptor.failure_domain_id,
            capabilities=self.descriptor.capabilities,
            read_revision=record.revision,
        )

    def reconcile(self, *, request_id: str, operation_fence: int) -> GuardianRecoveryReceipt:
        alert_id = self._write_alert(request_id, operation_fence, "recovery_attempted")
        if not self.available:
            return self._pending(request_id, operation_fence, alert_id, (), ())
        try:
            record = self.registry.read(request_id)
        except Exception:
            return self._pending(request_id, operation_fence, alert_id, (), ())
        if not record or record.operation_fence != operation_fence:
            return self._pending(request_id, operation_fence, alert_id, (), ())

        preexisting_instances = set(record.preexisting_instance_ids)
        preexisting_volumes = set(record.preexisting_volume_ids)
        if (set(record.owned_instance_ids) & preexisting_instances
                or set(record.owned_volume_ids) & preexisting_volumes):
            return self._pending(
                request_id,
                operation_fence,
                alert_id,
                record.owned_instance_ids,
                record.owned_volume_ids,
            )
        owned_instances = record.owned_instance_ids
        owned_volumes = record.owned_volume_ids
        destroy_instances: list[str] = []
        destroy_volumes: list[str] = []
        try:
            # The unique attempt label is the recovery key when create succeeded
            # but the initiating controller died before publishing returned IDs.
            rows_by_id = self._parse_instance_listing(self.provider.list_instances())

            matching_attempt_ids = {
                iid for iid, row in rows_by_id.items()
                if row.get("label") == record.attempt_label and iid not in preexisting_instances
            }
            owned_instances = tuple(sorted(set(record.owned_instance_ids) | matching_attempt_ids))
            if not owned_instances:
                # A complete listing with no label match is still inconclusive for
                # an ambiguous create: provider visibility can lag the create call.
                return self._pending(request_id, operation_fence, alert_id, (), ())

            # Persist the instance ownership before deleting it. If later volume
            # inventory fails, this record remains the durable discovery anchor.
            current = self._publish_owned_resources(
                record,
                instance_ids=owned_instances,
                volume_ids=record.owned_volume_ids,
            )
            if current is None:
                return self._pending(
                    request_id,
                    operation_fence,
                    alert_id,
                    record.owned_instance_ids,
                    record.owned_volume_ids,
                )
            record = current
            owned_instances = record.owned_instance_ids

            direct_volume_ids: set[str] = set()
            for iid in owned_instances:
                row = rows_by_id.get(iid)
                if row is None:
                    continue
                raw_volume_ids = row.get("volume_ids", [])
                if not isinstance(raw_volume_ids, (list, tuple)):
                    raise ValueError("fake provider returned malformed direct volume IDs")
                for raw_id in raw_volume_ids:
                    volume_id = self._volume_id(raw_id)
                    if volume_id is None:
                        raise ValueError("fake provider returned an invalid direct volume ID")
                    direct_volume_ids.add(volume_id)

            # Complete volume inventory is required before destroying an instance
            # that may be the only attachment-to-volume discovery anchor.
            volume_rows, attachments = self._parse_volume_listing(self.provider.list_volumes())

            if not direct_volume_ids.issubset(volume_rows):
                raise ValueError("directly reported volume is missing from complete provider inventory")
            owned_instance_set = set(owned_instances)
            for volume_id in direct_volume_ids:
                if attachments[volume_id] and not attachments[volume_id].issubset(owned_instance_set):
                    raise ValueError("direct volume is attached to an unowned instance")
            attached_volume_ids = {
                volume_id for volume_id, attached_ids in attachments.items()
                if attached_ids & owned_instance_set
            }
            for volume_id in attached_volume_ids:
                if not attachments[volume_id].issubset(owned_instance_set):
                    raise ValueError("attached volume has an unowned attachment")
            discovered_volumes = (direct_volume_ids | attached_volume_ids) - preexisting_volumes

            current = self._publish_owned_resources(
                record,
                instance_ids=owned_instances,
                volume_ids=tuple(sorted(set(record.owned_volume_ids) | discovered_volumes)),
            )
            if current is None:
                return self._pending(
                    request_id,
                    operation_fence,
                    alert_id,
                    record.owned_instance_ids,
                    record.owned_volume_ids,
                )
            record = current
            owned_instances = record.owned_instance_ids
            owned_volumes = record.owned_volume_ids

            present_instances = set(rows_by_id)
            for iid in owned_instances:
                if iid in present_instances:
                    self.provider.destroy_instance(iid)
                    destroy_instances.append(iid)

            after_instances = set(self._parse_instance_listing(self.provider.list_instances()))
            volume_rows, attachments = self._parse_volume_listing(self.provider.list_volumes())
            for vid in owned_volumes:
                row = volume_rows.get(vid)
                if row is None:
                    continue
                if attachments[vid] or after_instances.intersection(owned_instances):
                    continue
                self.provider.destroy_volume(vid)
                destroy_volumes.append(vid)

            final_instances = set(self._parse_instance_listing(self.provider.list_instances()))
            final_volumes, _ = self._parse_volume_listing(self.provider.list_volumes())
        except Exception:
            return self._pending(
                request_id,
                operation_fence,
                alert_id,
                owned_instances,
                owned_volumes,
                destroy_instances=tuple(destroy_instances),
                destroy_volumes=tuple(destroy_volumes),
            )

        unresolved_instances = tuple(iid for iid in owned_instances if iid in final_instances)
        unresolved_volumes = tuple(vid for vid in owned_volumes if vid in final_volumes)
        if unresolved_instances or unresolved_volumes:
            return self._pending(
                request_id,
                operation_fence,
                alert_id,
                unresolved_instances,
                unresolved_volumes,
                destroy_instances=tuple(destroy_instances),
                destroy_volumes=tuple(destroy_volumes),
            )
        return GuardianRecoveryReceipt(
            request_id=request_id,
            operation_fence=operation_fence,
            guardian_id=self.descriptor.guardian_id,
            failure_domain_id=self.descriptor.failure_domain_id,
            state=RecoveryState.VERIFIED_ABSENT,
            destroy_requests_instance_ids=tuple(destroy_instances),
            destroy_requests_volume_ids=tuple(destroy_volumes),
            provider_absence_confirmed=True,
            alert_receipt_id=alert_id,
            hard_deletion_guaranteed=False,
        )

    @staticmethod
    def _valid_instance_id(value: Any) -> bool:
        if isinstance(value, bool) or not isinstance(value, (str, int)):
            return False
        text = str(value)
        return bool(text.strip()) and text == text.strip()

    @classmethod
    def _parse_instance_listing(cls, value: Any) -> dict[str, dict[str, Any]]:
        if not isinstance(value, list):
            raise ValueError("fake provider returned a malformed instance listing")
        rows_by_id: dict[str, dict[str, Any]] = {}
        for row in value:
            if not isinstance(row, dict) or not cls._valid_instance_id(row.get("id")):
                raise ValueError("fake provider returned an instance without a valid ID")
            instance_id = str(row["id"])
            if instance_id in rows_by_id:
                raise ValueError("fake provider returned a duplicate instance ID")
            rows_by_id[instance_id] = row
        return rows_by_id

    @classmethod
    def _parse_volume_listing(
        cls,
        value: Any,
    ) -> tuple[dict[str, dict[str, Any]], dict[str, set[str]]]:
        if not isinstance(value, list):
            raise ValueError("fake provider returned a malformed volume listing")
        volume_rows: dict[str, dict[str, Any]] = {}
        attachments: dict[str, set[str]] = {}
        for row in value:
            if not isinstance(row, dict):
                raise ValueError("fake provider returned a malformed volume row")
            volume_id = cls._volume_id(row.get("id"))
            if volume_id is None or volume_id in volume_rows:
                raise ValueError("fake provider returned an invalid or duplicate volume ID")
            raw_attachments = row.get("instances")
            if not isinstance(raw_attachments, (list, tuple)):
                raise ValueError("fake provider returned a malformed volume attachment list")
            attached_ids: set[str] = set()
            for attached in raw_attachments:
                if not isinstance(attached, dict) or not cls._valid_instance_id(attached.get("id")):
                    raise ValueError("fake provider returned an invalid volume attachment")
                attached_ids.add(str(attached["id"]))
            volume_rows[volume_id] = row
            attachments[volume_id] = attached_ids
        return volume_rows, attachments

    @staticmethod
    def _volume_id(value: Any) -> str | None:
        if isinstance(value, bool) or not isinstance(value, (str, int)):
            return None
        text = str(value)
        try:
            parsed = int(text)
        except ValueError:
            return None
        if parsed < 1 or str(parsed) != text:
            return None
        return text

    def _publish_owned_resources(
        self,
        snapshot: LeaseRegistrySnapshot,
        *,
        instance_ids: tuple[str, ...],
        volume_ids: tuple[str, ...],
    ) -> LeaseRegistrySnapshot | None:
        if self.registry is None:
            return None
        instances = tuple(sorted(set(snapshot.owned_instance_ids) | set(instance_ids)))
        volumes = tuple(sorted(set(snapshot.owned_volume_ids) | set(volume_ids)))
        if (
            set(instances) & set(snapshot.preexisting_instance_ids)
            or set(volumes) & set(snapshot.preexisting_volume_ids)
        ):
            return None
        try:
            published = self.registry.publish_owned_resources(
                request_id=snapshot.request_id,
                operation_fence=snapshot.operation_fence,
                intent_digest=snapshot.intent_digest,
                instance_ids=instances,
                volume_ids=volumes,
            )
            readback = self.registry.read(snapshot.request_id)
        except Exception:
            return None
        if not (
            isinstance(readback, LeaseRegistrySnapshot)
            and readback.durable is True
            and readback.registry_id == snapshot.registry_id
            and readback.request_id == snapshot.request_id
            and readback.operation_fence == snapshot.operation_fence
            and readback.intent_digest == snapshot.intent_digest
            and readback.attempt_label == snapshot.attempt_label
            and readback.preexisting_instance_ids == snapshot.preexisting_instance_ids
            and readback.preexisting_volume_ids == snapshot.preexisting_volume_ids
            and set(instances).issubset(readback.owned_instance_ids)
            and set(volumes).issubset(readback.owned_volume_ids)
            and not (set(readback.owned_instance_ids) & set(readback.preexisting_instance_ids))
            and not (set(readback.owned_volume_ids) & set(readback.preexisting_volume_ids))
        ):
            return None
        return readback

    def _write_alert(self, request_id: str, fence: int, status: str) -> str:
        receipt_id = f"{self.descriptor.guardian_id}-{uuid.uuid4().hex}"
        entries = _read_json(self.alert_path)["entries"] if self.alert_path.exists() else []
        entries.append({"receipt_id": receipt_id, "request_id": request_id, "fence": fence, "status": status})
        _atomic_json_write(self.alert_path, {"entries": entries})
        return receipt_id

    def _pending(
        self,
        request_id: str,
        fence: int,
        alert_id: str,
        unresolved_instances: tuple[str, ...],
        unresolved_volumes: tuple[str, ...],
        *,
        destroy_instances: tuple[str, ...] = (),
        destroy_volumes: tuple[str, ...] = (),
    ) -> GuardianRecoveryReceipt:
        return GuardianRecoveryReceipt(
            request_id=request_id,
            operation_fence=fence,
            guardian_id=self.descriptor.guardian_id,
            failure_domain_id=self.descriptor.failure_domain_id,
            state=RecoveryState.CLEANUP_PENDING,
            destroy_requests_instance_ids=destroy_instances,
            destroy_requests_volume_ids=destroy_volumes,
            unresolved_instance_ids=unresolved_instances,
            unresolved_volume_ids=unresolved_volumes,
            provider_absence_confirmed=False,
            alert_receipt_id=alert_id,
            hard_deletion_guaranteed=False,
        )


class FileLeaseLifecycleHarness:
    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.registry_path = root / "registry.json"
        self.registry_control_path = root / "registry-control.json"
        self.provider_path = root / "provider-truth.json"
        self.provider_control_path = root / "provider-control.json"
        self.alert_path = root / "alerts.json"
        self.local_worker_pid_path = root / "local-worker.pid"
        self.journal_dir = root / "controller-journal"
        self.registry_control = RegistryControl(self.registry_control_path)
        self.provider_control = ProviderControl(self.provider_control_path)
        self.provider_truth = FileProviderTruth(self.provider_path)
        FileBackedFencedRegistry(self.registry_path, self.registry_control_path)

    def guardian(
        self,
        guardian_id: str,
        host: str,
        domain: str,
        *,
        kind: GuardianKind = GuardianKind.INDEPENDENT_SERVICE,
        registry_credential_available: bool = True,
    ) -> FileRecoveryGuardian:
        return FileRecoveryGuardian(
            guardian_id,
            host,
            domain,
            self.registry_path,
            self.registry_control_path,
            self.provider_path,
            self.provider_control_path,
            self.alert_path,
            kind=kind,
            registry_credential_available=registry_credential_available,
        )

    def workers(self) -> tuple[FileRecoveryGuardian, FileRecoveryGuardian]:
        first = self.guardian("guardian-a", "host-a", "domain-a")
        second = self.guardian("guardian-b", "host-b", "domain-b")
        assert first.provider is not second.provider
        assert first.provider.truth.path == second.provider.truth.path
        return first, second

    def registry(self) -> FileBackedFencedRegistry:
        return FileBackedFencedRegistry(self.registry_path, self.registry_control_path)

    def provider_client(self) -> FileProviderClient:
        return FileProviderClient(self.provider_path, self.provider_control_path)


def _plan() -> dict[str, Any]:
    return {
        "offer_id": 77,
        "max_runtime_seconds": 600,
        "start_deadline_seconds": 60,
        "cold_start_timeout_seconds": 300,
        "idle_timeout_seconds": 180,
        "hung_request_timeout_seconds": 120,
        "create_params": {"image": "fake-only-test-image"},
    }


def _direct_controller(harness: FileLeaseLifecycleHarness, guardians: tuple[FileRecoveryGuardian, ...]) -> LeaseController:
    gate = GuardianReadinessGate(harness.registry(), guardians, clock=time.time)
    return LeaseController(
        harness.provider_client(),
        LeaseJournal(harness.journal_dir),
        NoopSupervisor(),
        guardian_gate=gate,
    )


def _controller_process_entry(
    root_text: str,
    create_published: Any,
    keep_running: Any,
    request_id: str,
) -> None:
    root = Path(root_text)
    harness = FileLeaseLifecycleHarness.__new__(FileLeaseLifecycleHarness)
    harness.root = root
    harness.registry_path = root / "registry.json"
    harness.registry_control_path = root / "registry-control.json"
    harness.provider_path = root / "provider-truth.json"
    harness.provider_control_path = root / "provider-control.json"
    harness.alert_path = root / "alerts.json"
    harness.local_worker_pid_path = root / "local-worker.pid"
    harness.journal_dir = root / "controller-journal"
    registry = harness.registry()
    guardians = harness.workers()
    os.environ["VAST_BROKER_CONTROL_HOST_ID"] = INITIATING_HOST
    test_root = str(Path(__file__).resolve().parent)
    package_root = str(Path(__file__).resolve().parents[1] / "src")
    inherited_path = os.environ.get("PYTHONPATH", "")
    os.environ["PYTHONPATH"] = os.pathsep.join(
        part for part in (package_root, test_root, inherited_path) if part
    )
    os.environ["VBR_TEST_PROVIDER_TRUTH_PATH"] = str(harness.provider_path)
    os.environ["VBR_TEST_PROVIDER_CONTROL_PATH"] = str(harness.provider_control_path)
    os.environ["VBR_TEST_LOCAL_WORKER_PID_FILE"] = str(harness.local_worker_pid_path)
    gate = GuardianReadinessGate(registry, guardians, clock=time.time)
    from vast_broker.process_supervisor import ProcessLeaseSupervisor

    local_supervisor = ProcessLeaseSupervisor(
        harness.journal_dir,
        provider_factory="test_guardian_recovery:_worker_provider_factory",
        poll_seconds=0.05,
        startup_timeout_seconds=5,
        heartbeat_timeout_seconds=2,
        terminate_timeout_seconds=0.5,
        popen=_record_local_worker_process,
    )
    controller = LeaseController(
        FileProviderClient(harness.provider_path, harness.provider_control_path),
        LeaseJournal(harness.journal_dir),
        local_supervisor,
        guardian_gate=gate,
    )

    def wait_while_lease_is_live(_instance: dict[str, Any]) -> str:
        # Parent terminates this process after create and registry publication.
        create_published.set()
        keep_running.wait(120)
        return "released"

    controller.run(request_id, _plan(), wait_while_lease_is_live)


def _worker_provider_factory() -> FileProviderClient:
    return FileProviderClient(
        Path(os.environ["VBR_TEST_PROVIDER_TRUTH_PATH"]),
        Path(os.environ["VBR_TEST_PROVIDER_CONTROL_PATH"]),
    )


def _record_local_worker_process(command: list[str], **kwargs: Any) -> subprocess.Popen:
    process = subprocess.Popen(command, **kwargs)
    Path(os.environ["VBR_TEST_LOCAL_WORKER_PID_FILE"]).write_text(
        str(process.pid), encoding="ascii"
    )
    return process


def _spawn_live_public_lease(harness: FileLeaseLifecycleHarness, request_id: str):
    context = multiprocessing.get_context("spawn")
    create_published = context.Event()
    keep_running = context.Event()
    process = context.Process(
        target=_controller_process_entry,
        args=(str(harness.root), create_published, keep_running, request_id),
        name=f"lease-controller-{request_id}",
    )
    process.start()
    if not create_published.wait(20):
        exit_code = process.exitcode
        if process.is_alive():
            process.terminate()
            process.join(5)
        _terminate_local_worker(harness)
        pytest.fail(f"public LeaseController did not reach its operation callback (exit={exit_code})")
    try:
        record = harness.registry().read(request_id)
        assert record is not None
        assert record.owned_instance_ids
        assert record.owned_volume_ids
        assert process.is_alive()
    except BaseException:
        if process.is_alive():
            process.terminate()
            process.join(5)
        _terminate_local_worker(harness)
        raise
    return process


@contextmanager
def _live_public_lease(harness: FileLeaseLifecycleHarness, request_id: str):
    process = _spawn_live_public_lease(harness, request_id)
    try:
        snapshot = harness.registry().read(request_id)
        assert snapshot is not None
        deadline = time.monotonic() + 5
        while not harness.local_worker_pid_path.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert harness.local_worker_pid_path.exists()
        assert int(harness.local_worker_pid_path.read_text(encoding="ascii")) != process.pid
        yield process, snapshot
    finally:
        if process.is_alive():
            _terminate_controller(process)
        _terminate_local_worker(harness)


def _terminate_controller(process: multiprocessing.Process) -> None:
    process.terminate()
    process.join(10)
    if process.is_alive():
        process.kill()
        process.join(5)
    assert not process.is_alive()


def _terminate_local_worker(harness: FileLeaseLifecycleHarness) -> None:
    if not harness.local_worker_pid_path.exists():
        return
    worker_pid = int(harness.local_worker_pid_path.read_text(encoding="ascii"))
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(worker_pid), "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=5,
            )
        else:
            os.kill(worker_pid, signal.SIGTERM)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        pass
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.kill(worker_pid, 0)
        except OSError:
            harness.local_worker_pid_path.unlink(missing_ok=True)
            return
        time.sleep(0.05)
    if os.name != "nt":
        os.kill(worker_pid, signal.SIGKILL)


def _assert_recovery_receipt(
    worker: FileRecoveryGuardian,
    receipt: GuardianRecoveryReceipt,
    request_id: str,
    fence: int,
    *,
    snapshot: LeaseRegistrySnapshot | None = None,
):
    snapshot = snapshot or worker.registry.read(request_id)
    assert snapshot is not None
    return validate_guardian_recovery_receipt(
        receipt,
        descriptor=worker.descriptor,
        request_id=request_id,
        operation_fence=fence,
        owned_instance_ids=snapshot.owned_instance_ids,
        owned_volume_ids=snapshot.owned_volume_ids,
    )


@pytest.fixture(autouse=True)
def initiating_control_host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VAST_BROKER_CONTROL_HOST_ID", INITIATING_HOST)


def test_public_lease_operation_blocks_create_if_one_guardian_cannot_ack(tmp_path: Path) -> None:
    harness = FileLeaseLifecycleHarness(tmp_path)
    first = harness.guardian("guardian-a", "host-a", "domain-a")
    second = harness.guardian(
        "guardian-b", "host-b", "domain-b", registry_credential_available=False
    )
    ctl = _direct_controller(harness, (first, second))

    with pytest.raises(LeaseError, match="no create was attempted") as error:
        ctl.run("missing-guardian-ack", _plan(), lambda _: pytest.fail("operation must not run"))

    truth = harness.provider_truth.read()
    assert truth["create_count"] == 0
    assert set(truth["instances"]) == {"unrelated-instance"}
    assert set(truth["volumes"]) == {"9000"}
    assert error.value.result["state"] == "FAILED_CLEAN"


@pytest.mark.parametrize(
    ("substitute", "kind"),
    [
        ("logged-in Chrome/CDP session", GuardianKind.LOCAL_BROWSER_SESSION),
        ("Jev browser automation", GuardianKind.LOCAL_BROWSER_SESSION),
        ("scheduled GitHub workflow", GuardianKind.SCHEDULED_WORKFLOW),
        ("same-host process", GuardianKind.SAME_HOST_PROCESS),
    ],
)
def test_public_lease_operation_rejects_non_guardian_substitutes_before_create(
    tmp_path: Path,
    substitute: str,
    kind: GuardianKind,
) -> None:
    harness = FileLeaseLifecycleHarness(tmp_path)
    guardians = (
        harness.guardian("substitute-a", "host-a", "domain-a", kind=kind),
        harness.guardian("substitute-b", "host-b", "domain-b", kind=kind),
    )
    ctl = _direct_controller(harness, guardians)

    with pytest.raises(LeaseError, match="no create was attempted"):
        ctl.run(f"reject-{kind.value}", _plan(), lambda _: pytest.fail(f"{substitute} must not run"))

    truth = harness.provider_truth.read()
    assert truth["create_count"] == 0
    assert set(truth["instances"]) == {"unrelated-instance"}
    assert set(truth["volumes"]) == {"9000"}


def test_remote_guardian_recovers_after_controller_and_local_worker_process_loss(tmp_path: Path) -> None:
    harness = FileLeaseLifecycleHarness(tmp_path)
    first, second = harness.workers()
    assert first.provider is not second.provider
    request_id = "controller-process-loss"
    with _live_public_lease(harness, request_id) as (process, snapshot):
        # Kill the controller and its in-host worker separately, as process
        # termination does not reliably take descendants down on every OS.
        _terminate_controller(process)
        _terminate_local_worker(harness)
        recovered = first.reconcile(request_id=request_id, operation_fence=snapshot.operation_fence)
        _assert_recovery_receipt(first, recovered, request_id, snapshot.operation_fence)
        assert recovered.state == RecoveryState.VERIFIED_ABSENT
        assert recovered.provider_absence_confirmed is True

        truth_after_first = harness.provider_truth.read()
        assert truth_after_first["create_count"] == 1
        assert set(truth_after_first["instances"]) == {"unrelated-instance"}
        assert set(truth_after_first["volumes"]) == {"9000"}

        # A second independent worker can safely repeat the fenced reconciliation.
        repeated = second.reconcile(request_id=request_id, operation_fence=snapshot.operation_fence)
        _assert_recovery_receipt(second, repeated, request_id, snapshot.operation_fence)
        assert repeated.state == RecoveryState.VERIFIED_ABSENT
        truth_after_repeat = harness.provider_truth.read()
        assert truth_after_repeat["create_count"] == 1
        assert set(truth_after_repeat["instances"]) == {"unrelated-instance"}
        assert set(truth_after_repeat["volumes"]) == {"9000"}
        assert harness.alert_path.exists()


@pytest.mark.parametrize(
    "outage",
    [
        "guardian-a",
        "guardian-b",
        "guardian-a-registry-credential",
        "guardian-b-registry-credential",
        "registry",
        "network",
        "vast-api",
    ],
)
def test_outage_stays_pending_then_recovers_idempotently_after_restore(tmp_path: Path, outage: str) -> None:
    harness = FileLeaseLifecycleHarness(tmp_path)
    first, second = harness.workers()
    request_id = f"restore-{outage}"
    with _live_public_lease(harness, request_id) as (process, snapshot):
        _terminate_controller(process)

        active = [first, second]
        stopped: FileRecoveryGuardian | None = None
        if outage == "guardian-a":
            stopped = first
            stopped.available = False
            active = [second]
        elif outage == "guardian-b":
            stopped = second
            stopped.available = False
            active = [first]
        elif outage == "guardian-a-registry-credential":
            first.registry.credential_available = False
            active = [second]
        elif outage == "guardian-b-registry-credential":
            second.registry.credential_available = False
            active = [first]
        elif outage == "registry":
            harness.registry_control.set_available(False)
        elif outage == "network":
            harness.provider_control.set_outage(network=False)
        elif outage == "vast-api":
            harness.provider_control.set_outage(vast=False)
        else:  # pragma: no cover - exhaustive parameter list
            raise AssertionError(outage)

        if outage in {"registry", "network", "vast-api"}:
            first_receipt = first.reconcile(request_id=request_id, operation_fence=snapshot.operation_fence)
            _assert_recovery_receipt(
                first, first_receipt, request_id, snapshot.operation_fence, snapshot=snapshot
            )
            assert first_receipt.state == RecoveryState.CLEANUP_PENDING
            second_receipt = second.reconcile(request_id=request_id, operation_fence=snapshot.operation_fence)
            _assert_recovery_receipt(
                second, second_receipt, request_id, snapshot.operation_fence, snapshot=snapshot
            )
            assert second_receipt.state == RecoveryState.CLEANUP_PENDING
        else:
            for worker in active:
                receipt = worker.reconcile(request_id=request_id, operation_fence=snapshot.operation_fence)
                _assert_recovery_receipt(worker, receipt, request_id, snapshot.operation_fence)
                assert receipt.state == RecoveryState.VERIFIED_ABSENT

        if outage in {"registry", "network", "vast-api"}:
            pending_truth = harness.provider_truth.read()
            assert set(pending_truth["instances"]) == {"unrelated-instance", *snapshot.owned_instance_ids}
            assert set(pending_truth["volumes"]) == {"9000", *snapshot.owned_volume_ids}
        else:
            recovered_truth = harness.provider_truth.read()
            assert set(recovered_truth["instances"]) == {"unrelated-instance"}
            assert set(recovered_truth["volumes"]) == {"9000"}

        # Restore the failed path and service; repeated fenced cleanup must preserve
        # the same provider truth without a duplicate create or unrelated deletion.
        if stopped is not None:
            stopped.available = True
        if outage == "guardian-a-registry-credential":
            first.registry.credential_available = True
        if outage == "guardian-b-registry-credential":
            second.registry.credential_available = True
        if outage == "registry":
            harness.registry_control.set_available(True)
        if outage == "network":
            harness.provider_control.set_outage(network=True)
        if outage == "vast-api":
            harness.provider_control.set_outage(vast=True)

        for worker in (first, second):
            receipt = worker.reconcile(request_id=request_id, operation_fence=snapshot.operation_fence)
            _assert_recovery_receipt(worker, receipt, request_id, snapshot.operation_fence)
            assert receipt.state == RecoveryState.VERIFIED_ABSENT

        final_truth = harness.provider_truth.read()
        assert final_truth["create_count"] == 1
        assert set(final_truth["instances"]) == {"unrelated-instance"}
        assert set(final_truth["volumes"]) == {"9000"}


def _run_to_unpublished_ambiguous_create(
    harness: FileLeaseLifecycleHarness,
    request_id: str,
) -> tuple[FileRecoveryGuardian, FileRecoveryGuardian, LeaseRegistrySnapshot]:
    first, second = harness.workers()
    ctl = _direct_controller(harness, (first, second))
    original_create = ctl.provider.create_instance

    def create_then_lose_response(offer_id: Any, params: dict[str, Any]) -> dict[str, Any]:
        created = original_create(offer_id, params)
        # The provider accepted create, but the initiating controller cannot list
        # instances to reconcile before it gives up. The guardian registry has only
        # CREATE_INTENT and both acknowledgements; no resource IDs were published.
        harness.provider_control.set_outage(instance_listing=False)
        raise TimeoutError("fake create response was lost after provider acceptance")

    ctl.provider.create_instance = create_then_lose_response
    with pytest.raises(LeaseError, match="ambiguous") as error:
        ctl.run(request_id, _plan(), lambda _: pytest.fail("ambiguous create must not hand out access"))
    assert error.value.result["state"] == "CREATE_UNCERTAIN"

    snapshot = harness.registry().read(request_id)
    assert snapshot is not None
    assert snapshot.state == "CREATE_INTENT"
    assert snapshot.owned_instance_ids == ()
    assert snapshot.owned_volume_ids == ()
    provider_state = harness.provider_truth.read()
    created = [row for row in provider_state["instances"].values() if row["label"] == snapshot.attempt_label]
    assert len(created) == 1
    assert set(snapshot.preexisting_instance_ids) == {"unrelated-instance"}
    assert set(snapshot.preexisting_volume_ids) == {"9000"}
    return first, second, snapshot


def test_guardian_discovers_unpublished_ambiguous_create_and_cleans_only_request_resources(tmp_path: Path) -> None:
    harness = FileLeaseLifecycleHarness(tmp_path)
    first, second, initial_snapshot = _run_to_unpublished_ambiguous_create(
        harness, "unpublished-create-recovery"
    )
    provider_state = harness.provider_truth.read()
    created = next(
        row for iid, row in provider_state["instances"].items() if iid not in initial_snapshot.preexisting_instance_ids
    )
    created_id = str(created["id"])
    created_volume_ids = tuple(created["volume_ids"])

    # A pre-existing resource is given the same label to prove that the durable
    # pre-create ID baseline wins over label matching when excluding ownership.
    provider_state["instances"]["unrelated-instance"]["label"] = initial_snapshot.attempt_label
    harness.provider_truth.write(provider_state)
    harness.provider_control.set_outage(instance_listing=True)

    original_destroy_instance = first.provider.destroy_instance
    original_destroy_volume = first.provider.destroy_volume
    published_at_destroy: list[tuple[str, str]] = []

    def destroy_instance_after_publication(instance_id: str) -> dict[str, bool]:
        durable = harness.registry().read(initial_snapshot.request_id)
        assert durable is not None and durable.durable is True
        assert created_id in durable.owned_instance_ids
        assert set(created_volume_ids).issubset(durable.owned_volume_ids)
        published_at_destroy.append(("instance", instance_id))
        return original_destroy_instance(instance_id)

    def destroy_volume_after_publication(volume_id: str) -> dict[str, bool]:
        durable = harness.registry().read(initial_snapshot.request_id)
        assert durable is not None and durable.durable is True
        assert created_id in durable.owned_instance_ids
        assert volume_id in durable.owned_volume_ids
        published_at_destroy.append(("volume", volume_id))
        return original_destroy_volume(volume_id)

    first.provider.destroy_instance = destroy_instance_after_publication
    first.provider.destroy_volume = destroy_volume_after_publication

    receipt = first.reconcile(
        request_id=initial_snapshot.request_id,
        operation_fence=initial_snapshot.operation_fence,
    )
    _assert_recovery_receipt(first, receipt, initial_snapshot.request_id, initial_snapshot.operation_fence)
    assert receipt.state == RecoveryState.VERIFIED_ABSENT
    assert receipt.provider_absence_confirmed is True
    assert receipt.destroy_requests_instance_ids == (created_id,)
    assert set(receipt.destroy_requests_volume_ids) == set(created_volume_ids)
    assert published_at_destroy == [
        ("instance", created_id),
        *(('volume', volume_id) for volume_id in created_volume_ids),
    ]

    published = harness.registry().read(initial_snapshot.request_id)
    assert published is not None
    assert published.operation_fence == initial_snapshot.operation_fence
    assert published.intent_digest == initial_snapshot.intent_digest
    assert published.attempt_label == initial_snapshot.attempt_label
    assert published.preexisting_instance_ids == initial_snapshot.preexisting_instance_ids
    assert published.preexisting_volume_ids == initial_snapshot.preexisting_volume_ids
    assert published.owned_instance_ids == (created_id,)
    assert set(published.owned_volume_ids) == set(created_volume_ids)

    truth = harness.provider_truth.read()
    assert truth["create_count"] == 1
    assert set(truth["instances"]) == {"unrelated-instance"}
    assert truth["instances"]["unrelated-instance"]["label"] == initial_snapshot.attempt_label
    assert set(truth["volumes"]) == {"9000"}

    repeated = second.reconcile(
        request_id=initial_snapshot.request_id,
        operation_fence=initial_snapshot.operation_fence,
    )
    _assert_recovery_receipt(second, repeated, initial_snapshot.request_id, initial_snapshot.operation_fence)
    assert repeated.state == RecoveryState.VERIFIED_ABSENT
    final_truth = harness.provider_truth.read()
    assert final_truth["create_count"] == 1
    assert set(final_truth["instances"]) == {"unrelated-instance"}
    assert set(final_truth["volumes"]) == {"9000"}


@pytest.mark.parametrize(
    ("unavailable_listing", "published_instances_before_restore"),
    [("instance_listing", ()), ("volume_listing", ("1",))],
)
def test_unpublished_create_discovery_listing_failure_stays_pending_then_recovers(
    tmp_path: Path,
    unavailable_listing: str,
    published_instances_before_restore: tuple[str, ...],
) -> None:
    harness = FileLeaseLifecycleHarness(tmp_path)
    first, second, initial_snapshot = _run_to_unpublished_ambiguous_create(
        harness, f"discovery-failure-{unavailable_listing}"
    )
    truth_before = harness.provider_truth.read()
    created_id = next(
        iid for iid in truth_before["instances"] if iid not in initial_snapshot.preexisting_instance_ids
    )
    created_volume = truth_before["instances"][created_id]["volume_ids"][0]

    if unavailable_listing == "volume_listing":
        harness.provider_control.set_outage(instance_listing=True, volume_listing=False)

    pending = first.reconcile(
        request_id=initial_snapshot.request_id,
        operation_fence=initial_snapshot.operation_fence,
    )
    _assert_recovery_receipt(first, pending, initial_snapshot.request_id, initial_snapshot.operation_fence)
    assert pending.state == RecoveryState.CLEANUP_PENDING
    assert pending.provider_absence_confirmed is False
    current = harness.registry().read(initial_snapshot.request_id)
    assert current is not None
    assert current.owned_instance_ids == published_instances_before_restore
    assert current.owned_volume_ids == ()
    unchanged = harness.provider_truth.read()
    assert set(unchanged["instances"]) == {"unrelated-instance", created_id}
    assert set(unchanged["volumes"]) == {"9000", created_volume}
    assert unchanged["events"] == [["create", created_id]]

    harness.provider_control.set_outage(instance_listing=True, volume_listing=True)
    recovered = second.reconcile(
        request_id=initial_snapshot.request_id,
        operation_fence=initial_snapshot.operation_fence,
    )
    _assert_recovery_receipt(second, recovered, initial_snapshot.request_id, initial_snapshot.operation_fence)
    assert recovered.state == RecoveryState.VERIFIED_ABSENT
    final = harness.provider_truth.read()
    assert set(final["instances"]) == {"unrelated-instance"}
    assert set(final["volumes"]) == {"9000"}


@pytest.mark.parametrize(
    ("listing_kind", "malformed_call", "missing_field"),
    [
        ("volumes", 2, "instances"),
        ("instances", 3, "id"),
        ("volumes", 3, "instances"),
        ("volumes", 3, "id"),
    ],
)
def test_malformed_successful_post_destroy_listings_stay_pending_then_recover(
    tmp_path: Path,
    listing_kind: str,
    malformed_call: int,
    missing_field: str,
) -> None:
    harness = FileLeaseLifecycleHarness(tmp_path)
    first, second, initial_snapshot = _run_to_unpublished_ambiguous_create(
        harness, f"malformed-listing-{listing_kind}-{malformed_call}"
    )
    truth = harness.provider_truth.read()
    created_id = next(
        iid for iid in truth["instances"] if iid not in initial_snapshot.preexisting_instance_ids
    )
    created_volume_id = truth["instances"][created_id]["volume_ids"][0]
    harness.provider_control.set_outage(instance_listing=True, volume_listing=True)

    method_name = "list_instances" if listing_kind == "instances" else "list_volumes"
    original_listing = getattr(first.provider, method_name)
    call_count = 0

    def malformed_listing() -> list[dict[str, Any]]:
        nonlocal call_count
        call_count += 1
        rows = original_listing()
        if call_count == malformed_call:
            resource_id = created_id if listing_kind == "instances" else (
                "9000" if malformed_call == 3 else created_volume_id
            )
            row = next(row for row in rows if str(row.get("id")) == resource_id)
            row.pop(missing_field)
        return rows

    setattr(first.provider, method_name, malformed_listing)
    pending = first.reconcile(
        request_id=initial_snapshot.request_id,
        operation_fence=initial_snapshot.operation_fence,
    )

    _assert_recovery_receipt(first, pending, initial_snapshot.request_id, initial_snapshot.operation_fence)
    assert pending.state == RecoveryState.CLEANUP_PENDING
    assert pending.provider_absence_confirmed is False
    published = harness.registry().read(initial_snapshot.request_id)
    assert published is not None and published.durable is True
    assert published.owned_instance_ids == (created_id,)
    assert published.owned_volume_ids == (created_volume_id,)

    recovered = second.reconcile(
        request_id=initial_snapshot.request_id,
        operation_fence=initial_snapshot.operation_fence,
    )
    _assert_recovery_receipt(second, recovered, initial_snapshot.request_id, initial_snapshot.operation_fence)
    assert recovered.state == RecoveryState.VERIFIED_ABSENT
    final = harness.provider_truth.read()
    assert set(final["instances"]) == {"unrelated-instance"}
    assert set(final["volumes"]) == {"9000"}


def test_file_backed_registry_rejects_stale_fence_and_keeps_resource_publication_monotonic(tmp_path: Path) -> None:
    harness = FileLeaseLifecycleHarness(tmp_path)
    registry = harness.registry()
    initial = LeaseCreateIntent(
        request_id="fenced-registry-contract",
        operation_id="fenced-registry-contract",
        attempt_id="attempt-a",
        attempt_label="vbr-fenced-test",
        operation_fence=11,
        deadline_epoch=time.time() + 600,
        initiating_host_id=INITIATING_HOST,
    )
    reservation = registry.reserve_fence(
        request_id=initial.request_id,
        attempt_id=initial.attempt_id,
        operation_fence=initial.operation_fence,
    )
    assert reservation.durable
    registry.publish_intent(initial)
    workers = harness.workers()
    gate = GuardianReadinessGate(registry, workers, clock=time.time)
    ready = gate.arm_before_create(initial)
    gate.publish_owned_resources(initial, ready, instance_ids=("1201",), volume_ids=("9201",))
    same = registry.publish_owned_resources(
        request_id=initial.request_id,
        operation_fence=initial.operation_fence,
        intent_digest=initial.digest,
        instance_ids=("1202",),
        volume_ids=("9202",),
    )
    assert set(same.owned_instance_ids) == {"1201", "1202"}
    assert set(same.owned_volume_ids) == {"9201", "9202"}
    with pytest.raises(ValueError, match="stale or reused"):
        registry.reserve_fence(
            request_id=initial.request_id,
            attempt_id="attempt-b",
            operation_fence=10,
        )
