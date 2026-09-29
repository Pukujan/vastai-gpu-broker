"""GitHub-hosted recovery path for a second independent guardian service."""
from __future__ import annotations

import os
import subprocess
import time
from typing import Any, Callable

from .guardians import (
    GuardianAck,
    GuardianCapabilities,
    GuardianDescriptor,
    GuardianKind,
    GuardianReadinessError,
    GuardianRecoveryReceipt,
    LeaseRegistrySnapshot,
)


class GitHubActionsGuardian:
    """Dispatch a trusted workflow and wait for its durable R2 acknowledgement.

    This is intended as one recovery path alongside a separately hosted
    continuously running service. GitHub Actions alone is not a P45 guardian.
    """

    def __init__(
        self,
        *,
        registry: Any,
        repository: str,
        workflow: str,
        descriptor: GuardianDescriptor,
        dispatch: Callable[[dict[str, Any]], None] | None = None,
        poll_seconds: float = 2.0,
        timeout_seconds: float = 120.0,
        clock: Callable[[], float] = time.monotonic,
        sleep_fn: Callable[[float], None] = time.sleep,
    ):
        if not isinstance(repository, str) or repository.count("/") != 1:
            raise ValueError("GitHub repository must use owner/name form")
        if not isinstance(workflow, str) or not workflow.strip() or "/" in workflow:
            raise ValueError("guardian workflow must be a file name or workflow ID")
        if not isinstance(descriptor, GuardianDescriptor):
            raise TypeError("guardian descriptor is required")
        if descriptor.kind != GuardianKind.INDEPENDENT_SERVICE or not descriptor.capabilities.recovery_complete:
            raise ValueError("GitHub guardian descriptor is incomplete")
        if any(isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0 for value in (poll_seconds, timeout_seconds)):
            raise ValueError("GitHub guardian timing values must be positive")
        self.registry = registry
        self.repository = repository
        self.workflow = workflow
        self._descriptor = descriptor
        self.dispatch = dispatch or self._dispatch_with_gh
        self.poll_seconds = float(poll_seconds)
        self.timeout_seconds = float(timeout_seconds)
        self.clock = clock
        self.sleep_fn = sleep_fn

    @property
    def descriptor(self) -> GuardianDescriptor:
        return self._descriptor

    def acknowledge_intent(
        self,
        *,
        registry_id: str,
        request_id: str,
        operation_fence: int,
        intent_digest: str,
    ) -> GuardianAck:
        before = self.registry.read(request_id)
        if (
            not isinstance(before, LeaseRegistrySnapshot)
            or before.registry_id != registry_id
            or before.operation_fence != operation_fence
            or before.intent_digest != intent_digest
            or before.state != "CREATE_INTENT"
        ):
            raise GuardianReadinessError("github_guardian_intent_mismatch", "shared lease intent is missing or mismatched")
        self._trigger({
            "action": "acknowledge",
            "request_id": request_id,
            "operation_fence": operation_fence,
            "intent_digest": intent_digest,
            "registry_id": registry_id,
        })
        ack = self._wait_for_ack(request_id, operation_fence, intent_digest, registry_id)
        return ack

    def reconcile(self, *, request_id: str, operation_fence: int) -> GuardianRecoveryReceipt:
        before = self.registry.read(request_id)
        if (
            not isinstance(before, LeaseRegistrySnapshot)
            or before.operation_fence != operation_fence
            or before.registry_id != self.registry.registry_id
        ):
            raise GuardianReadinessError("github_guardian_fence_mismatch", "shared lease record is missing or mismatched", cleanup_required=True)
        prior = self.registry.latest_recovery_receipt(request_id, self.descriptor.guardian_id)
        before_revision = before.revision
        self._trigger({
            "action": "reconcile",
            "request_id": request_id,
            "operation_fence": operation_fence,
        })
        deadline = self.clock() + self.timeout_seconds
        while self.clock() < deadline:
            current = self.registry.read(request_id)
            receipt = self.registry.latest_recovery_receipt(request_id, self.descriptor.guardian_id)
            if (
                isinstance(current, LeaseRegistrySnapshot)
                and current.operation_fence == operation_fence
                and current.revision > before_revision
                and isinstance(receipt, GuardianRecoveryReceipt)
                and receipt.observed_at_epoch is not None
                and (
                    prior is None
                    or prior.observed_at_epoch is None
                    or receipt.observed_at_epoch > prior.observed_at_epoch
                )
                and receipt.request_id == request_id
                and receipt.operation_fence == operation_fence
                and receipt.guardian_id == self.descriptor.guardian_id
            ):
                return receipt
            self.sleep_fn(min(self.poll_seconds, max(0.0, deadline - self.clock())))
        raise GuardianReadinessError(
            "github_guardian_reconcile_timeout",
            "GitHub recovery workflow did not publish a fresh cleanup receipt",
            cleanup_required=True,
        )

    def request_cleanup(self, *, request_id: str, operation_fence: int) -> None:
        """Dispatch the backup workflow after the durable cleanup flag is set."""
        snapshot = self.registry.read(request_id)
        if (
            not isinstance(snapshot, LeaseRegistrySnapshot)
            or snapshot.operation_fence != operation_fence
            or snapshot.registry_id != self.registry.registry_id
            or snapshot.state == "FENCE_RESERVED"
            or not snapshot.cleanup_requested
        ):
            raise GuardianReadinessError(
                "github_guardian_cleanup_signal_missing",
                "GitHub recovery cannot dispatch without the matching durable cleanup signal",
                cleanup_required=True,
            )
        if snapshot.state == "VERIFIED_ABSENT":
            return
        self._trigger({
            "action": "reconcile",
            "request_id": request_id,
            "operation_fence": operation_fence,
        })

    def _wait_for_ack(
        self, request_id: str, operation_fence: int, intent_digest: str, registry_id: str,
    ) -> GuardianAck:
        deadline = self.clock() + self.timeout_seconds
        while self.clock() < deadline:
            current = self.registry.read(request_id)
            if isinstance(current, LeaseRegistrySnapshot):
                matches = [item for item in current.guardian_acks if item.guardian_id == self.descriptor.guardian_id]
                if matches:
                    ack = matches[0]
                    if (
                        current.registry_id == registry_id
                        and current.operation_fence == operation_fence
                        and current.intent_digest == intent_digest
                        and ack.request_id == request_id
                        and ack.operation_fence == operation_fence
                        and ack.intent_digest == intent_digest
                        and ack.registry_id == registry_id
                        and ack.control_host_id == self.descriptor.control_host_id
                        and ack.failure_domain_id == self.descriptor.failure_domain_id
                        and ack.capabilities == self.descriptor.capabilities
                    ):
                        return ack
                    raise GuardianReadinessError("github_guardian_ack_mismatch", "GitHub workflow wrote a mismatched lease acknowledgement")
            self.sleep_fn(min(self.poll_seconds, max(0.0, deadline - self.clock())))
        raise GuardianReadinessError("github_guardian_ack_timeout", "GitHub recovery workflow did not durably acknowledge the lease")

    def _trigger(self, fields: dict[str, Any]) -> None:
        try:
            self.dispatch(fields)
        except Exception:
            raise GuardianReadinessError(
                "github_guardian_dispatch_failed",
                "GitHub recovery workflow could not be dispatched",
                cleanup_required=fields.get("action") == "reconcile",
            ) from None

    def _dispatch_with_gh(self, fields: dict[str, Any]) -> None:
        command = [
            "gh", "workflow", "run", self.workflow,
            "--repo", self.repository,
            "--ref", "main",
        ]
        for name, value in fields.items():
            command.extend(("--field", f"{name}={value}"))
        try:
            result = subprocess.run(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired):
            raise RuntimeError("GitHub workflow dispatch failed") from None
        if result.returncode != 0:
            raise RuntimeError("GitHub workflow dispatch failed")


def github_guardian_from_environment(registry: Any) -> GitHubActionsGuardian:
    names = {
        "repository": "VAST_BROKER_GITHUB_REPOSITORY",
        "workflow": "VAST_BROKER_GITHUB_GUARDIAN_WORKFLOW",
        "guardian_id": "VAST_BROKER_GITHUB_GUARDIAN_ID",
        "control_host_id": "VAST_BROKER_GITHUB_CONTROL_HOST_ID",
        "failure_domain_id": "VAST_BROKER_GITHUB_FAILURE_DOMAIN_ID",
    }
    values = {field: os.environ.get(name, "").strip() for field, name in names.items()}
    if any(not value for value in values.values()):
        raise ValueError("GitHub recovery guardian configuration is incomplete")
    descriptor = GuardianDescriptor(
        guardian_id=values["guardian_id"],
        control_host_id=values["control_host_id"],
        failure_domain_id=values["failure_domain_id"],
        kind=GuardianKind.INDEPENDENT_SERVICE,
        trusted=True,
        capabilities=GuardianCapabilities(True, True, True, True, True),
    )
    return GitHubActionsGuardian(
        registry=registry,
        repository=values["repository"],
        workflow=values["workflow"],
        descriptor=descriptor,
    )
