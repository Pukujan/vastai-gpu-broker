"""Authenticated HTTP adapter and service for independently hosted guardians."""
from __future__ import annotations

import argparse
import hmac
import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlsplit

from .guardians import (
    GuardianAck,
    GuardianCapabilities,
    GuardianDescriptor,
    GuardianKind,
    GuardianReadinessError,
    GuardianRecoveryReceipt,
    GuardianReadinessGate,
    RecoveryState,
)
from .recovery_worker import (
    GuardianRecoveryWorker,
    RecoveryNotDue,
    alert_sink_from_environment,
    guardian_descriptor_from_environment,
)

_LOG = logging.getLogger("vast_broker.guardian")
_MAX_BODY_BYTES = 16_384


def _capabilities_to_wire(value: GuardianCapabilities) -> dict[str, bool]:
    return {
        "reconcile_ambiguous_create": value.reconcile_ambiguous_create,
        "destroy_owned_instances": value.destroy_owned_instances,
        "destroy_owned_volumes": value.destroy_owned_volumes,
        "verify_provider_absence": value.verify_provider_absence,
        "emit_actionable_alert": value.emit_actionable_alert,
    }


def _ack_from_wire(value: Any) -> GuardianAck:
    if not isinstance(value, dict) or not isinstance(value.get("capabilities"), dict):
        raise ValueError("guardian acknowledgement is malformed")
    try:
        ack = GuardianAck(
            guardian_id=value["guardian_id"], request_id=value["request_id"],
            operation_fence=value["operation_fence"], intent_digest=value["intent_digest"],
            registry_id=value["registry_id"], control_host_id=value["control_host_id"],
            failure_domain_id=value["failure_domain_id"],
            capabilities=GuardianCapabilities(**value["capabilities"]),
            read_revision=value["read_revision"],
        )
    except (KeyError, TypeError, ValueError):
        raise ValueError("guardian acknowledgement is malformed") from None
    if (
        any(not isinstance(item, str) or not item for item in (
            ack.guardian_id, ack.request_id, ack.intent_digest, ack.registry_id,
            ack.control_host_id, ack.failure_domain_id,
        ))
        or isinstance(ack.operation_fence, bool) or not isinstance(ack.operation_fence, int)
        or isinstance(ack.read_revision, bool) or not isinstance(ack.read_revision, int)
    ):
        raise ValueError("guardian acknowledgement is malformed")
    return ack


def _ack_to_wire(value: GuardianAck) -> dict[str, Any]:
    return {
        "guardian_id": value.guardian_id,
        "request_id": value.request_id,
        "operation_fence": value.operation_fence,
        "intent_digest": value.intent_digest,
        "registry_id": value.registry_id,
        "control_host_id": value.control_host_id,
        "failure_domain_id": value.failure_domain_id,
        "capabilities": _capabilities_to_wire(value.capabilities),
        "read_revision": value.read_revision,
    }


def _receipt_from_wire(value: Any) -> GuardianRecoveryReceipt:
    if not isinstance(value, dict):
        raise ValueError("guardian recovery receipt is malformed")
    try:
        receipt = GuardianRecoveryReceipt(
            request_id=value["request_id"], operation_fence=value["operation_fence"],
            guardian_id=value["guardian_id"], failure_domain_id=value["failure_domain_id"],
            state=RecoveryState(value["state"]),
            destroy_requests_instance_ids=tuple(value.get("destroy_requests_instance_ids", ())),
            destroy_requests_volume_ids=tuple(value.get("destroy_requests_volume_ids", ())),
            unresolved_instance_ids=tuple(value.get("unresolved_instance_ids", ())),
            unresolved_volume_ids=tuple(value.get("unresolved_volume_ids", ())),
            provider_absence_confirmed=value.get("provider_absence_confirmed") is True,
            alert_receipt_id=value.get("alert_receipt_id"),
            hard_deletion_guaranteed=value.get("hard_deletion_guaranteed") is True,
            observed_at_epoch=value.get("observed_at_epoch"),
        )
    except (KeyError, TypeError, ValueError):
        raise ValueError("guardian recovery receipt is malformed") from None
    return receipt


def _receipt_to_wire(value: GuardianRecoveryReceipt) -> dict[str, Any]:
    return {
        "request_id": value.request_id,
        "operation_fence": value.operation_fence,
        "guardian_id": value.guardian_id,
        "failure_domain_id": value.failure_domain_id,
        "state": value.state.value,
        "destroy_requests_instance_ids": list(value.destroy_requests_instance_ids),
        "destroy_requests_volume_ids": list(value.destroy_requests_volume_ids),
        "unresolved_instance_ids": list(value.unresolved_instance_ids),
        "unresolved_volume_ids": list(value.unresolved_volume_ids),
        "provider_absence_confirmed": value.provider_absence_confirmed,
        "alert_receipt_id": value.alert_receipt_id,
        "hard_deletion_guaranteed": value.hard_deletion_guaranteed,
        "observed_at_epoch": value.observed_at_epoch,
    }


class _RejectRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class HTTPRecoveryGuardian:
    """Broker-side client for one remote guardian service."""

    def __init__(
        self,
        *,
        base_url: str,
        bearer_token: str,
        descriptor: GuardianDescriptor,
        timeout: float = 20.0,
    ):
        parsed = urlsplit(base_url) if isinstance(base_url, str) else None
        local_http = parsed is not None and parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost"}
        if parsed is None or not parsed.hostname or not (parsed.scheme == "https" or local_http):
            raise ValueError("guardian endpoint must use HTTPS, except for loopback tests")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("guardian endpoint URL cannot contain credentials, query, or fragment")
        if not isinstance(bearer_token, str) or len(bearer_token) < 32:
            raise ValueError("guardian bearer token must contain at least 32 characters")
        if not isinstance(descriptor, GuardianDescriptor):
            raise TypeError("guardian descriptor is required")
        if descriptor.kind != GuardianKind.INDEPENDENT_SERVICE or not descriptor.capabilities.recovery_complete:
            raise ValueError("remote guardian descriptor is incomplete")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
            raise ValueError("guardian timeout must be positive")
        self.base_url = base_url.rstrip("/")
        self._bearer_token = bearer_token
        self._descriptor = descriptor
        self.timeout = float(timeout)
        self._opener = urllib.request.build_opener(_RejectRedirect)

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
        result = self._post("/v1/acknowledge", {
            "registry_id": registry_id,
            "request_id": request_id,
            "operation_fence": operation_fence,
            "intent_digest": intent_digest,
        })
        ack = _ack_from_wire(result.get("ack"))
        if (
            ack.guardian_id != self.descriptor.guardian_id
            or ack.control_host_id != self.descriptor.control_host_id
            or ack.failure_domain_id != self.descriptor.failure_domain_id
            or ack.capabilities != self.descriptor.capabilities
            or ack.request_id != request_id
            or ack.operation_fence != operation_fence
            or ack.intent_digest != intent_digest
            or ack.registry_id != registry_id
        ):
            raise GuardianReadinessError("guardian_ack_invalid", "remote guardian returned a mismatched durable acknowledgement")
        return ack

    def reconcile(self, *, request_id: str, operation_fence: int) -> GuardianRecoveryReceipt:
        result = self._post("/v1/reconcile", {
            "request_id": request_id,
            "operation_fence": operation_fence,
        })
        receipt = _receipt_from_wire(result.get("receipt"))
        if (
            receipt.request_id != request_id
            or receipt.operation_fence != operation_fence
            or receipt.guardian_id != self.descriptor.guardian_id
            or receipt.failure_domain_id != self.descriptor.failure_domain_id
        ):
            raise GuardianReadinessError("recovery_receipt_mismatch", "remote guardian returned a mismatched recovery receipt", cleanup_required=True)
        return receipt

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        body = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
        request = urllib.request.Request(
            self.base_url + path,
            data=body,
            headers={
                "Authorization": f"Bearer {self._bearer_token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                raw = response.read(_MAX_BODY_BYTES + 1)
        except urllib.error.HTTPError as exc:
            raise GuardianReadinessError(
                "guardian_http_error",
                f"remote guardian returned HTTP {exc.code}",
                cleanup_required=path.endswith("/reconcile"),
            ) from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise GuardianReadinessError(
                "guardian_unavailable",
                "remote recovery guardian could not be reached",
                cleanup_required=path.endswith("/reconcile"),
            ) from None
        if len(raw) > _MAX_BODY_BYTES:
            raise GuardianReadinessError("guardian_response_too_large", "remote guardian response exceeded its limit", cleanup_required=True)
        try:
            result = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise GuardianReadinessError("guardian_response_invalid", "remote guardian response was not valid JSON", cleanup_required=True) from None
        if not isinstance(result, dict):
            raise GuardianReadinessError("guardian_response_invalid", "remote guardian response was not an object", cleanup_required=True)
        return result


def create_handler(worker: GuardianRecoveryWorker, bearer_token: str):
    if not isinstance(bearer_token, str) or len(bearer_token) < 32:
        raise ValueError("guardian bearer token must contain at least 32 characters")

    class GuardianHandler(BaseHTTPRequestHandler):
        server_version = "vast-broker-guardian/1"

        def log_message(self, _format: str, *_args: Any) -> None:
            # Avoid logging lease identifiers, provider responses, or auth headers.
            return

        def _send(self, status: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _authorized(self) -> bool:
            supplied = self.headers.get("Authorization", "")
            expected = f"Bearer {bearer_token}"
            return hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8"))

        def _read_body(self) -> dict[str, Any]:
            raw_length = self.headers.get("Content-Length")
            if not raw_length or not raw_length.isdigit():
                raise ValueError("content length is invalid")
            length = int(raw_length)
            if not 1 <= length <= _MAX_BODY_BYTES:
                raise ValueError("request body size is invalid")
            if self.headers.get_content_type() != "application/json":
                raise ValueError("content type must be application/json")
            value = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(value, dict):
                raise ValueError("request body must be an object")
            return value

        def do_GET(self) -> None:
            if self.path != "/health":
                self._send(404, {"error": "not_found"})
                return
            self._send(200, {"ok": True})

        def do_POST(self) -> None:
            if not self._authorized():
                self._send(401, {"error": "unauthorized"})
                return
            try:
                body = self._read_body()
                if self.path == "/v1/acknowledge":
                    ack = worker.acknowledge_intent(
                        registry_id=body.get("registry_id"),
                        request_id=body.get("request_id"),
                        operation_fence=body.get("operation_fence"),
                        intent_digest=body.get("intent_digest"),
                    )
                    self._send(200, {"ack": _ack_to_wire(ack)})
                elif self.path == "/v1/reconcile":
                    receipt = worker.reconcile(
                        request_id=body.get("request_id"),
                        operation_fence=body.get("operation_fence"),
                    )
                    self._send(200, {"receipt": _receipt_to_wire(receipt)})
                else:
                    self._send(404, {"error": "not_found"})
            except RecoveryNotDue:
                self._send(409, {"error": "recovery_not_due"})
            except GuardianReadinessError as exc:
                status = 503 if exc.cleanup_required else 409
                self._send(status, {"error": exc.reason_code})
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError):
                self._send(400, {"error": "invalid_request"})
            except Exception as exc:
                _LOG.error("guardian request failed (%s)", type(exc).__name__)
                self._send(500, {"error": "internal_error"})

    return GuardianHandler


def _scheduler(worker: GuardianRecoveryWorker, interval_seconds: float, stop: threading.Event) -> None:
    while not stop.is_set():
        try:
            worker.run_once()
        except Exception as exc:
            _LOG.error("guardian reconciliation pass failed (%s)", type(exc).__name__)
        stop.wait(interval_seconds)


def serve(worker: GuardianRecoveryWorker, *, bearer_token: str, bind: str, port: int, poll_seconds: float) -> None:
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError("guardian port is invalid")
    if isinstance(poll_seconds, bool) or not isinstance(poll_seconds, (int, float)) or not 1 <= poll_seconds <= 3600:
        raise ValueError("guardian poll interval must be between 1 and 3600 seconds")
    server = ThreadingHTTPServer((bind, port), create_handler(worker, bearer_token))
    server.daemon_threads = True
    stop = threading.Event()
    thread = threading.Thread(
        target=_scheduler, args=(worker, float(poll_seconds), stop),
        name="vast-broker-recovery", daemon=True,
    )
    thread.start()
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        stop.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def worker_from_environment() -> GuardianRecoveryWorker:
    from .provider import VastOffersClient
    from .r2_registry import R2SharedLeaseRegistry

    api_key = os.environ.get("VAST_API_KEY", "")
    if not api_key:
        raise ValueError("VAST_API_KEY is not configured")
    provider = VastOffersClient(api_key=api_key)
    registry = R2SharedLeaseRegistry.from_environment()
    alert_sink = alert_sink_from_environment()
    descriptor = guardian_descriptor_from_environment(alert_sink=alert_sink)
    worker = GuardianRecoveryWorker(
        registry=registry,
        provider=provider,
        descriptor=descriptor,
        alert_sink=alert_sink,
    )
    # Prove both read paths are configured before advertising the service.
    provider.list_instances()
    provider.list_volumes()
    registry.list_records()
    return worker


def guardian_gate_from_environment() -> GuardianReadinessGate:
    """Build the broker gate from one HTTP service plus another remote path."""
    from .r2_registry import R2SharedLeaseRegistry
    from .github_guardian import github_guardian_from_environment

    capabilities = GuardianCapabilities(True, True, True, True, True)
    registry = R2SharedLeaseRegistry.from_environment()
    guardians: list[Any] = []
    for index in (1, 2):
        prefix = f"VAST_BROKER_GUARDIAN_{index}_"
        kind = os.environ.get(prefix + "TYPE", "http").strip().casefold()
        if kind == "github_actions":
            guardians.append(github_guardian_from_environment(registry))
            continue
        if kind != "http":
            raise ValueError("guardian type must be http or github_actions")
        values = {name: os.environ.get(prefix + name, "").strip() for name in (
            "URL", "TOKEN", "ID", "CONTROL_HOST_ID", "FAILURE_DOMAIN_ID",
        )}
        if any(not value for value in values.values()):
            raise ValueError("both remote recovery guardian configurations are required")
        descriptor = GuardianDescriptor(
            guardian_id=values["ID"],
            control_host_id=values["CONTROL_HOST_ID"],
            failure_domain_id=values["FAILURE_DOMAIN_ID"],
            kind=GuardianKind.INDEPENDENT_SERVICE,
            trusted=True,
            capabilities=capabilities,
        )
        guardians.append(HTTPRecoveryGuardian(
            base_url=values["URL"], bearer_token=values["TOKEN"], descriptor=descriptor,
        ))
    return GuardianReadinessGate(
        registry, guardians, clock=time.time,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run an independent Vast lease recovery service")
    parser.add_argument("--bind", default=os.environ.get("VAST_BROKER_GUARDIAN_BIND", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8080")))
    parser.add_argument("--poll-seconds", type=float, default=float(os.environ.get("VAST_BROKER_GUARDIAN_POLL_SECONDS", "30")))
    args = parser.parse_args(argv)
    token = os.environ.get("VAST_BROKER_GUARDIAN_HTTP_TOKEN", "")
    if len(token) < 32:
        raise ValueError("VAST_BROKER_GUARDIAN_HTTP_TOKEN must contain at least 32 characters")
    worker = worker_from_environment()
    serve(worker, bearer_token=token, bind=args.bind, port=args.port, poll_seconds=args.poll_seconds)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
