"""Durable, compare-and-swap lease records stored in Cloudflare R2."""
from __future__ import annotations

import json
import math
import os
import time
from typing import Any
from urllib.parse import quote, unquote

from .guardians import (
    FenceReservation,
    GuardianAck,
    GuardianCapabilities,
    GuardianDescriptor,
    GuardianRecoveryReceipt,
    GuardianKind,
    LeaseCreateIntent,
    LeaseRegistrySnapshot,
    RecoveryState,
    validate_guardian_recovery_receipt,
)


class R2RegistryError(RuntimeError):
    """A sanitized shared-registry failure."""


def _ack_from_dict(item: dict[str, Any]) -> GuardianAck:
    return GuardianAck(
        guardian_id=item["guardian_id"], request_id=item["request_id"],
        operation_fence=item["operation_fence"], intent_digest=item["intent_digest"],
        registry_id=item["registry_id"], control_host_id=item["control_host_id"],
        failure_domain_id=item["failure_domain_id"],
        capabilities=GuardianCapabilities(**item["capabilities"]),
        read_revision=item["read_revision"],
    )


def _recovery_from_dict(item: dict[str, Any]) -> GuardianRecoveryReceipt:
    return GuardianRecoveryReceipt(
        request_id=item["request_id"], operation_fence=item["operation_fence"],
        guardian_id=item["guardian_id"], failure_domain_id=item["failure_domain_id"],
        state=RecoveryState(item["state"]),
        destroy_requests_instance_ids=tuple(item.get("destroy_requests_instance_ids", ())),
        destroy_requests_volume_ids=tuple(item.get("destroy_requests_volume_ids", ())),
        unresolved_instance_ids=tuple(item.get("unresolved_instance_ids", ())),
        unresolved_volume_ids=tuple(item.get("unresolved_volume_ids", ())),
        provider_absence_confirmed=item.get("provider_absence_confirmed") is True,
        alert_receipt_id=item.get("alert_receipt_id"),
        hard_deletion_guaranteed=item.get("hard_deletion_guaranteed") is True,
        observed_at_epoch=item.get("observed_at_epoch"),
    )


def _ack_to_dict(ack: GuardianAck) -> dict[str, Any]:
    return {
        "guardian_id": ack.guardian_id,
        "request_id": ack.request_id,
        "operation_fence": ack.operation_fence,
        "intent_digest": ack.intent_digest,
        "registry_id": ack.registry_id,
        "control_host_id": ack.control_host_id,
        "failure_domain_id": ack.failure_domain_id,
        "capabilities": {
            "reconcile_ambiguous_create": ack.capabilities.reconcile_ambiguous_create,
            "destroy_owned_instances": ack.capabilities.destroy_owned_instances,
            "destroy_owned_volumes": ack.capabilities.destroy_owned_volumes,
            "verify_provider_absence": ack.capabilities.verify_provider_absence,
            "emit_actionable_alert": ack.capabilities.emit_actionable_alert,
        },
        "read_revision": ack.read_revision,
    }


def _recovery_to_dict(receipt: GuardianRecoveryReceipt) -> dict[str, Any]:
    return {
        "request_id": receipt.request_id,
        "operation_fence": receipt.operation_fence,
        "guardian_id": receipt.guardian_id,
        "failure_domain_id": receipt.failure_domain_id,
        "state": receipt.state.value,
        "destroy_requests_instance_ids": list(receipt.destroy_requests_instance_ids),
        "destroy_requests_volume_ids": list(receipt.destroy_requests_volume_ids),
        "unresolved_instance_ids": list(receipt.unresolved_instance_ids),
        "unresolved_volume_ids": list(receipt.unresolved_volume_ids),
        "provider_absence_confirmed": receipt.provider_absence_confirmed,
        "alert_receipt_id": receipt.alert_receipt_id,
        "hard_deletion_guaranteed": receipt.hard_deletion_guaranteed,
        "observed_at_epoch": receipt.observed_at_epoch,
    }


class R2SharedLeaseRegistry:
    """Implement the fenced lease registry contract with conditional S3 writes."""

    def __init__(
        self,
        *,
        bucket: str,
        endpoint_url: str,
        access_key_id: str,
        secret_access_key: str,
        account_id: str,
        prefix: str = "vast-broker/v1/",
        client: Any | None = None,
        cas_attempts: int = 12,
    ) -> None:
        if not all(isinstance(value, str) and value.strip() for value in (
            bucket, endpoint_url, access_key_id, secret_access_key, account_id, prefix
        )):
            raise ValueError("R2 registry configuration is incomplete")
        if isinstance(cas_attempts, bool) or not isinstance(cas_attempts, int) or cas_attempts < 1:
            raise ValueError("cas_attempts must be a positive integer")
        if client is None:
            try:
                import boto3
            except ImportError:
                raise R2RegistryError("install the recovery extra to use the R2 registry") from None
            client = boto3.client(
                "s3", endpoint_url=endpoint_url.rstrip("/"), region_name="auto",
                aws_access_key_id=access_key_id, aws_secret_access_key=secret_access_key,
            )
        self.bucket = bucket
        self.prefix = prefix.rstrip("/") + "/"
        self.client = client
        self.cas_attempts = cas_attempts
        self.registry_id = f"cloudflare-r2:{account_id}:{bucket}:{self.prefix}"

    @classmethod
    def from_environment(cls, *, prefix: str | None = None) -> "R2SharedLeaseRegistry":
        names = {
            "bucket": "VAST_BROKER_R2_BUCKET",
            "endpoint_url": "VAST_BROKER_R2_ENDPOINT",
            "access_key_id": "VAST_BROKER_R2_ACCESS_KEY_ID",
            "secret_access_key": "VAST_BROKER_R2_SECRET_ACCESS_KEY",
            "account_id": "VAST_BROKER_R2_ACCOUNT_ID",
        }
        values = {field: os.environ.get(env, "") for field, env in names.items()}
        if prefix is not None:
            values["prefix"] = prefix
        return cls(**values)

    def _key(self, request_id: str) -> str:
        if not isinstance(request_id, str) or not request_id.strip():
            raise ValueError("request_id must be a non-empty string")
        return f"{self.prefix}leases/{quote(request_id, safe='')}.json"

    def _read(self, request_id: str) -> tuple[dict[str, Any] | None, str | None]:
        try:
            response = self.client.get_object(Bucket=self.bucket, Key=self._key(request_id))
        except Exception as exc:
            code = getattr(exc, "response", {}).get("Error", {}).get("Code")
            if code in {"NoSuchKey", "404", "NotFound"}:
                return None, None
            raise R2RegistryError(f"R2 registry read failed ({type(exc).__name__})") from None
        try:
            raw = response["Body"].read()
            value = json.loads(raw.decode("utf-8"))
            etag = response.get("ETag")
            if not isinstance(value, dict) or not isinstance(etag, str) or not etag:
                raise ValueError("invalid registry object")
            return value, etag
        except Exception as exc:
            raise R2RegistryError(f"R2 registry record is invalid ({type(exc).__name__})") from None

    def _write(
        self,
        request_id: str,
        value: dict[str, Any],
        *,
        etag: str | None,
        create_only: bool = False,
    ) -> None:
        params: dict[str, Any] = {
            "Bucket": self.bucket,
            "Key": self._key(request_id),
            "Body": json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8"),
            "ContentType": "application/json",
        }
        if create_only:
            params["IfNoneMatch"] = "*"
        elif etag is not None:
            params["IfMatch"] = etag
        else:
            raise ValueError("an R2 compare-and-swap condition is required")
        try:
            self.client.put_object(**params)
        except Exception as exc:
            code = getattr(exc, "response", {}).get("Error", {}).get("Code")
            status = getattr(exc, "response", {}).get("ResponseMetadata", {}).get("HTTPStatusCode")
            if code in {"PreconditionFailed", "ConditionalRequestConflict"} or status in {409, 412}:
                raise R2RegistryError("R2 registry compare-and-swap conflict") from None
            raise R2RegistryError(f"R2 registry write failed ({type(exc).__name__})") from None

    def _snapshot(self, row: dict[str, Any] | None) -> LeaseRegistrySnapshot | None:
        if row is None or row.get("state") == "FENCE_RESERVED":
            return None
        cleanup_requested = row.get("cleanup_requested", False)
        cleanup_requested_at = row.get("cleanup_requested_at_epoch")
        if not isinstance(cleanup_requested, bool):
            raise R2RegistryError("R2 cleanup request flag is malformed")
        if cleanup_requested_at is not None and (
            isinstance(cleanup_requested_at, bool)
            or not isinstance(cleanup_requested_at, (int, float))
            or not math.isfinite(cleanup_requested_at)
            or cleanup_requested_at <= 0
        ):
            raise R2RegistryError("R2 cleanup request timestamp is malformed")
        if cleanup_requested != (cleanup_requested_at is not None):
            raise R2RegistryError("R2 cleanup request flag and timestamp disagree")
        return LeaseRegistrySnapshot(
            registry_id=row["registry_id"], request_id=row["request_id"],
            operation_fence=row["operation_fence"], intent_digest=row["intent_digest"],
            state=row["state"], deadline_epoch=row["deadline_epoch"],
            attempt_label=row["attempt_label"],
            preexisting_instance_ids=tuple(row["preexisting_instance_ids"]),
            preexisting_volume_ids=tuple(row["preexisting_volume_ids"]),
            durable=True, revision=row["revision"],
            guardian_acks=tuple(_ack_from_dict(item) for item in row.get("guardian_acks", [])),
            owned_instance_ids=tuple(row.get("owned_instance_ids", [])),
            owned_volume_ids=tuple(row.get("owned_volume_ids", [])),
            cleanup_requested=cleanup_requested,
            cleanup_requested_at_epoch=cleanup_requested_at,
        )

    def reserve_fence(self, *, request_id: str, attempt_id: str, operation_fence: int) -> FenceReservation:
        if not isinstance(attempt_id, str) or not attempt_id.strip():
            raise ValueError("attempt_id must be a non-empty string")
        if isinstance(operation_fence, bool) or not isinstance(operation_fence, int) or operation_fence < 1:
            raise ValueError("operation_fence must be positive")
        for _ in range(self.cas_attempts):
            row, etag = self._read(request_id)
            if row is not None:
                if row.get("attempt_id") == attempt_id and row.get("operation_fence") == operation_fence:
                    return FenceReservation(self.registry_id, request_id, attempt_id, operation_fence, True)
                if not (
                    row.get("state") == RecoveryState.VERIFIED_ABSENT.value
                    and operation_fence > row.get("operation_fence", 0)
                ):
                    raise R2RegistryError("request already has a reserved, active, or newer fence")
                next_row = {
                    "schema_version": 1, "registry_id": self.registry_id, "request_id": request_id,
                    "attempt_id": attempt_id, "operation_fence": operation_fence,
                    "state": "FENCE_RESERVED", "revision": row.get("revision", 0) + 1,
                    "guardian_acks": [], "owned_instance_ids": [], "owned_volume_ids": [],
                    "cleanup_requested": False, "cleanup_requested_at_epoch": None,
                    "recovery_receipts": [],
                }
                try:
                    self._write(request_id, next_row, etag=etag)
                    return FenceReservation(self.registry_id, request_id, attempt_id, operation_fence, True)
                except R2RegistryError as exc:
                    if "compare-and-swap" not in str(exc):
                        raise
                    continue
            try:
                self._write(request_id, {
                    "schema_version": 1, "registry_id": self.registry_id, "request_id": request_id,
                    "attempt_id": attempt_id, "operation_fence": operation_fence,
                    "state": "FENCE_RESERVED", "revision": 1,
                    "guardian_acks": [], "owned_instance_ids": [], "owned_volume_ids": [],
                    "cleanup_requested": False, "cleanup_requested_at_epoch": None,
                    "recovery_receipts": [],
                }, etag=None, create_only=True)
                return FenceReservation(self.registry_id, request_id, attempt_id, operation_fence, True)
            except R2RegistryError as exc:
                if "compare-and-swap" not in str(exc):
                    raise
        raise R2RegistryError("R2 registry remained busy while reserving a lease fence")

    def publish_intent(self, intent: LeaseCreateIntent) -> LeaseRegistrySnapshot:
        for _ in range(self.cas_attempts):
            row, etag = self._read(intent.request_id)
            if row is None or row.get("attempt_id") != intent.attempt_id or row.get("operation_fence") != intent.operation_fence:
                raise R2RegistryError("lease intent has no matching reserved fence")
            if row.get("state") != "FENCE_RESERVED":
                snapshot = self._snapshot(row)
                if snapshot and snapshot.intent_digest == intent.digest:
                    return snapshot
                raise R2RegistryError("lease intent conflicts with the reserved attempt")
            row.update({
                "state": "CREATE_INTENT", "intent_digest": intent.digest,
                "deadline_epoch": intent.deadline_epoch, "attempt_label": intent.attempt_label,
                "initiating_host_id": intent.initiating_host_id,
                "preexisting_instance_ids": list(intent.preexisting_instance_ids),
                "preexisting_volume_ids": list(intent.preexisting_volume_ids),
                "revision": row["revision"] + 1,
            })
            try:
                self._write(intent.request_id, row, etag=etag)
                return self._snapshot(row)  # type: ignore[return-value]
            except R2RegistryError as exc:
                if "compare-and-swap" not in str(exc):
                    raise
        raise R2RegistryError("R2 registry remained busy while publishing lease intent")

    def read(self, request_id: str) -> LeaseRegistrySnapshot | None:
        row, _ = self._read(request_id)
        return self._snapshot(row)

    def acknowledge_guardian(self, ack: GuardianAck) -> LeaseRegistrySnapshot:
        for _ in range(self.cas_attempts):
            row, etag = self._read(ack.request_id)
            if row is None or row.get("state") == "FENCE_RESERVED":
                raise R2RegistryError("guardian acknowledgement has no published lease intent")
            if (row.get("operation_fence") != ack.operation_fence or row.get("intent_digest") != ack.intent_digest
                    or ack.registry_id != self.registry_id):
                raise R2RegistryError("guardian acknowledgement does not match the shared lease intent")
            acks = row.setdefault("guardian_acks", [])
            wire = _ack_to_dict(ack)
            existing = next((item for item in acks if item.get("guardian_id") == ack.guardian_id), None)
            if existing is not None:
                if existing == wire:
                    snapshot = self._snapshot(row)
                    return snapshot  # type: ignore[return-value]
                raise R2RegistryError("guardian ID already has a different acknowledgement")
            acks.append(wire)
            row["revision"] += 1
            try:
                self._write(ack.request_id, row, etag=etag)
                return self._snapshot(row)  # type: ignore[return-value]
            except R2RegistryError as exc:
                if "compare-and-swap" not in str(exc):
                    raise
        raise R2RegistryError("R2 registry remained busy while saving guardian acknowledgement")

    def publish_owned_resources(
        self, *, request_id: str, operation_fence: int, intent_digest: str,
        instance_ids: tuple[str, ...], volume_ids: tuple[str, ...],
    ) -> LeaseRegistrySnapshot:
        for _ in range(self.cas_attempts):
            row, etag = self._read(request_id)
            if row is None or row.get("operation_fence") != operation_fence or row.get("intent_digest") != intent_digest:
                raise R2RegistryError("owned resources do not match the active fenced intent")
            if row.get("state") not in {"CREATE_INTENT", "LEASE_OWNED", "CLEANUP_PENDING"}:
                raise R2RegistryError("cannot add resources before create intent or after cleanup is verified")
            pre_instances = set(row.get("preexisting_instance_ids", []))
            pre_volumes = set(row.get("preexisting_volume_ids", []))
            next_instances = set(row.get("owned_instance_ids", [])) | set(instance_ids)
            next_volumes = set(row.get("owned_volume_ids", [])) | set(volume_ids)
            if next_instances & pre_instances or next_volumes & pre_volumes:
                raise R2RegistryError("owned resource list overlaps the pre-create account inventory")
            if (next_instances == set(row.get("owned_instance_ids", []))
                    and next_volumes == set(row.get("owned_volume_ids", []))):
                snapshot = self._snapshot(row)
                return snapshot  # type: ignore[return-value]
            row["owned_instance_ids"] = sorted(next_instances)
            row["owned_volume_ids"] = sorted(next_volumes)
            # Any new owned resource invalidates prior absence receipts. The
            # guardians must reconcile the expanded set before the fence is
            # terminal or can be reused.
            row["recovery_receipts"] = []
            row["state"] = "LEASE_OWNED"
            row["revision"] += 1
            try:
                self._write(request_id, row, etag=etag)
                return self._snapshot(row)  # type: ignore[return-value]
            except R2RegistryError as exc:
                if "compare-and-swap" not in str(exc):
                    raise
        raise R2RegistryError("R2 registry remained busy while publishing owned resources")

    def request_cleanup(self, *, request_id: str, operation_fence: int) -> LeaseRegistrySnapshot:
        """Durably signal early cleanup without conflating it with recovery status."""
        if isinstance(operation_fence, bool) or not isinstance(operation_fence, int) or operation_fence < 1:
            raise ValueError("operation_fence must be positive")
        for _ in range(self.cas_attempts):
            row, etag = self._read(request_id)
            if (
                row is None
                or row.get("operation_fence") != operation_fence
                or row.get("state") == "FENCE_RESERVED"
            ):
                raise R2RegistryError("cleanup request has no matching published lease fence")
            if row.get("state") == RecoveryState.VERIFIED_ABSENT.value:
                return self._snapshot(row)  # type: ignore[return-value]
            if row.get("state") not in {"CREATE_INTENT", "LEASE_OWNED", "CLEANUP_PENDING"}:
                raise R2RegistryError("cleanup request cannot be added to this lease state")
            if row.get("cleanup_requested") is True:
                snapshot = self._snapshot(row)
                return snapshot  # type: ignore[return-value]
            row["cleanup_requested"] = True
            row["cleanup_requested_at_epoch"] = time.time()
            row["revision"] += 1
            try:
                self._write(request_id, row, etag=etag)
                return self._snapshot(row)  # type: ignore[return-value]
            except R2RegistryError as exc:
                if "compare-and-swap" not in str(exc):
                    raise
        raise R2RegistryError("R2 registry remained busy while publishing cleanup request")

    def list_records(self, *, include_terminal: bool = False) -> list[dict[str, Any]]:
        prefix = f"{self.prefix}leases/"
        token: str | None = None
        records: list[dict[str, Any]] = []
        try:
            while True:
                params: dict[str, Any] = {"Bucket": self.bucket, "Prefix": prefix}
                if token:
                    params["ContinuationToken"] = token
                page = self.client.list_objects_v2(**params)
                for item in page.get("Contents", []):
                    key = item.get("Key")
                    if not isinstance(key, str) or not key.endswith(".json"):
                        continue
                    encoded_request_id = key[len(prefix):-5]
                    request_id = unquote(encoded_request_id)
                    if quote(request_id, safe="") != encoded_request_id:
                        raise R2RegistryError("R2 registry listing contains a malformed lease key")
                    row, _ = self._read(request_id)
                    if row is None:
                        continue
                    if include_terminal or row.get("state") != RecoveryState.VERIFIED_ABSENT.value:
                        records.append(row)
                if not page.get("IsTruncated"):
                    break
                token = page.get("NextContinuationToken")
                if not isinstance(token, str) or not token:
                    raise R2RegistryError("R2 registry listing pagination was incomplete")
        except R2RegistryError:
            raise
        except Exception as exc:
            raise R2RegistryError(f"R2 registry listing failed ({type(exc).__name__})") from None
        return records

    def record_recovery_receipt(self, receipt: GuardianRecoveryReceipt) -> None:
        for _ in range(self.cas_attempts):
            row, etag = self._read(receipt.request_id)
            if row is None or row.get("operation_fence") != receipt.operation_fence:
                raise R2RegistryError("recovery receipt has no matching fenced lease")
            ack_rows = row.get("guardian_acks", [])
            if not isinstance(ack_rows, list) or any(not isinstance(item, dict) for item in ack_rows):
                raise R2RegistryError("shared lease guardian acknowledgements are malformed")
            matching_ack = next((item for item in ack_rows if item.get("guardian_id") == receipt.guardian_id), None)
            if not isinstance(matching_ack, dict):
                raise R2RegistryError("recovery receipt has no matching acknowledged guardian")
            try:
                descriptor = GuardianDescriptor(
                    guardian_id=matching_ack["guardian_id"],
                    control_host_id=matching_ack["control_host_id"],
                    failure_domain_id=matching_ack["failure_domain_id"],
                    kind=GuardianKind.INDEPENDENT_SERVICE,
                    trusted=True,
                    capabilities=GuardianCapabilities(**matching_ack["capabilities"]),
                )
            except (KeyError, TypeError, ValueError):
                raise R2RegistryError("shared lease guardian acknowledgement is malformed") from None
            validate_guardian_recovery_receipt(
                receipt, descriptor=descriptor, request_id=receipt.request_id,
                operation_fence=receipt.operation_fence,
                owned_instance_ids=tuple(row.get("owned_instance_ids", [])),
                owned_volume_ids=tuple(row.get("owned_volume_ids", [])),
            )
            items = row.setdefault("recovery_receipts", [])
            wire = _recovery_to_dict(receipt)
            items = [item for item in items if item.get("guardian_id") != receipt.guardian_id]
            items.append(wire)
            row["recovery_receipts"] = items
            receipts_by_guardian = {item.get("guardian_id"): item for item in items if isinstance(item, dict)}
            ready_guardian_ids = {
                item.get("guardian_id") for item in ack_rows if isinstance(item, dict)
            }
            all_guardians_verified = (
                len(ready_guardian_ids) >= 2
                and ready_guardian_ids.issubset(receipts_by_guardian)
                and all(
                    receipts_by_guardian[guardian_id].get("state") == RecoveryState.VERIFIED_ABSENT.value
                    and receipts_by_guardian[guardian_id].get("provider_absence_confirmed") is True
                    and not receipts_by_guardian[guardian_id].get("unresolved_instance_ids")
                    and not receipts_by_guardian[guardian_id].get("unresolved_volume_ids")
                    for guardian_id in ready_guardian_ids
                )
            )
            if all_guardians_verified:
                row["state"] = RecoveryState.VERIFIED_ABSENT.value
            else:
                row["state"] = "CLEANUP_PENDING"
            row["revision"] += 1
            try:
                self._write(receipt.request_id, row, etag=etag)
                return
            except R2RegistryError as exc:
                if "compare-and-swap" not in str(exc):
                    raise
        raise R2RegistryError("R2 registry remained busy while saving recovery receipt")

    def latest_recovery_receipt(self, request_id: str, guardian_id: str) -> GuardianRecoveryReceipt | None:
        row, _ = self._read(request_id)
        if row is None:
            return None
        items = row.get("recovery_receipts", [])
        if not isinstance(items, list):
            raise R2RegistryError("R2 recovery receipts are malformed")
        matching = [item for item in items if item.get("guardian_id") == guardian_id]
        return _recovery_from_dict(matching[-1]) if matching else None
