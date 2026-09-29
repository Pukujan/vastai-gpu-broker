"""JSON-first CLI for the same router used by adopters' Python integrations."""
from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
from pathlib import Path
import re
import sys
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from .provider import VastOffersClient
from .journal import LeaseJournal
from .lease import LeaseError
from .openjev import (
    OPENJEV_MODEL_REPOSITORY,
    OPENJEV_MODEL_REVISION,
    LIVE_WINDOW_ATTEMPTS,
    LIVE_WINDOW_INTERVAL_SECONDS,
    LIVE_WINDOW_MAX_FAILURES,
    LIVE_WINDOW_SECONDS,
    OpenJevTrialOperation,
    deployment_recipe,
    lease_plan_from_proposal,
    trial_limit_floor_errors,
    trial_steps,
    validate_health_response,
    validate_probe_response,
)
from .rentai import VastRentaiService
from .research import resolve_request
from .router import candidate_key, route_request


def _read_json(path: str) -> Any:
    with Path(path).open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _write_json(value: Any, path: str | None = None) -> None:
    encoded = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, default=str)
    if path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(target.suffix + ".tmp")
        temporary.write_text(encoded + "\n", encoding="utf-8")
        temporary.replace(target)
    else:
        print(encoded)


def _load_factory(spec: str) -> Any:
    module_name, separator, callable_name = spec.partition(":")
    if not separator or not module_name or not callable_name:
        raise ValueError("guardian factory must be module:callable")
    factory = getattr(importlib.import_module(module_name), callable_name)
    if not callable(factory):
        raise ValueError("guardian factory must identify a callable")
    return factory()


def lease_provider_factory() -> VastOffersClient:
    """Build the independent supervisor's scoped Vast lifecycle client."""
    api_key = os.environ.get("VAST_BROKER_LEASE_API_KEY")
    if not api_key:
        raise ValueError("VAST_BROKER_LEASE_API_KEY is not configured")
    return VastOffersClient(api_key=api_key)


def _trial_report(route: dict[str, Any], lease: dict[str, Any] | None,
                  record: dict[str, Any] | None) -> dict[str, Any]:
    result = record.get("operation_result") if isinstance(record, dict) else None
    if not isinstance(result, dict):
        result = {}
    plan = record.get("plan") if isinstance(record, dict) else None
    instance = lease.get("instance") if isinstance(lease, dict) else None
    instance_id = instance.get("id") if isinstance(instance, dict) else None
    absence = record.get("absence_evidence") if isinstance(record, dict) else None
    owned_ids = absence.get("owned_instance_ids") if isinstance(absence, dict) else None
    inventory = absence.get("instance_inventory") if isinstance(absence, dict) else None
    result_digest = record.get("operation_result_sha256") if isinstance(record, dict) else None
    try:
        encoded_result = json.dumps(
            result, ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
        result_digest_matches = (
            isinstance(result_digest, str)
            and re.fullmatch(r"[0-9a-f]{64}", result_digest) is not None
            and hashlib.sha256(encoded_result).hexdigest() == result_digest
        )
    except (TypeError, ValueError, UnicodeEncodeError):
        result_digest_matches = False
    inventory_valid = (
        isinstance(inventory, dict)
        and inventory.get("complete") is True
        and isinstance(inventory.get("instance_count"), int)
        and not isinstance(inventory.get("instance_count"), bool)
        and inventory["instance_count"] >= 0
        and isinstance(inventory.get("instance_ids_sha256"), str)
        and re.fullmatch(r"[0-9a-f]{64}", inventory["instance_ids_sha256"]) is not None
    )
    try:
        operation_completed = datetime.fromisoformat(record["operation_completed_at_utc"])
        inventory_checked = datetime.fromisoformat(absence["checked_at_utc"])
        cleanup_confirmed = datetime.fromisoformat(record["confirmed_absent_at_utc"])
        timestamps_valid = (
            operation_completed.tzinfo is not None
            and inventory_checked.tzinfo is not None
            and cleanup_confirmed.tzinfo is not None
            and operation_completed <= inventory_checked <= cleanup_confirmed
        )
    except (KeyError, TypeError, ValueError, AttributeError):
        timestamps_valid = False
    response_valid = False
    step_receipts_valid = False
    try:
        checked_probe = validate_probe_response(json.dumps(
            result["response"], ensure_ascii=False, allow_nan=False, separators=(",", ":")
        ))
        reported_identity = result.get("model_identity")
        step_receipts = result.get("step_receipts")
        expected_step_names = {step.name for step in trial_steps()}
        step_receipts_valid = (
            isinstance(step_receipts, dict)
            and set(step_receipts) == expected_step_names | {"model_identity"}
            and all(
                isinstance(step_receipts.get(name), str)
                and re.fullmatch(r"[0-9a-f]{64}", step_receipts[name]) is not None
                for name in expected_step_names
            )
            and validate_health_response(step_receipts["model_identity"])["model"]
                == checked_probe["model_identity"]["model"]
            and validate_health_response(step_receipts["model_identity"])["method"]
                == checked_probe["model_identity"]["method"]
        )
        response_valid = (
            result.get("choice") == checked_probe["choice"]
            and result.get("probabilities") == checked_probe["probabilities"]
            and reported_identity == checked_probe["model_identity"]
            and isinstance(result.get("response_sha256"), str)
            and re.fullmatch(r"[0-9a-f]{64}", result["response_sha256"]) is not None
        )
    except (KeyError, TypeError, ValueError, UnicodeEncodeError):
        pass
    window = result.get("live_window")
    window_ok = (
        isinstance(window, dict)
        and window.get("attempts") == LIVE_WINDOW_ATTEMPTS
        and window.get("max_allowed_failures") == LIVE_WINDOW_MAX_FAILURES
        and window.get("interval_seconds") == LIVE_WINDOW_INTERVAL_SECONDS
        and window.get("window_seconds") == LIVE_WINDOW_SECONDS
        and isinstance(window.get("failures"), int)
        and not isinstance(window.get("failures"), bool)
        and 0 <= window["failures"] <= LIVE_WINDOW_MAX_FAILURES
    )
    inference_ok = (
        result.get("state") == "inference_verified"
        and result.get("instance_id") == str(instance_id)
        and result_digest_matches
        and response_valid
        and step_receipts_valid
        and window_ok
        and isinstance(plan, dict)
        and plan.get("proposal_digest") == route.get("proposal_digest")
    )
    cleanup_ok = (
        isinstance(record, dict)
        and record.get("request_id") == route.get("request_id")
        and record.get("state") == "DESTROYED"
        and isinstance(absence, dict)
        and absence.get("owned_instance_ids_absent") is True
        and absence.get("owned_volume_ids_absent") is True
        and isinstance(absence.get("owned_volume_ids"), list)
        and isinstance(owned_ids, list)
        and len(owned_ids) == 1
        and str(instance_id) in owned_ids
        and inventory_valid
        and timestamps_valid
    )
    return {
        "request_id": route.get("request_id"),
        "proposal_digest": route.get("proposal_digest"),
        "state": "OPENJEV_LIVE_TEST_PASSED" if inference_ok and cleanup_ok else "OPENJEV_LIVE_TEST_INCOMPLETE",
        "instance_id": instance_id,
        "offer": route.get("proposal", {}).get("offer"),
        "cost_bound": route.get("proposal", {}).get("cost_bound"),
        "inference": ({
            "state": result.get("state"),
            "choice": result.get("choice"),
            "probabilities": result.get("probabilities"),
            "model_identity": result.get("model_identity"),
            "live_window": window,
            "response_sha256": result.get("response_sha256"),
        } if inference_ok else None),
        "operation_result_sha256": record.get("operation_result_sha256") if isinstance(record, dict) else None,
        "cleanup": {
            "state": record.get("state") if isinstance(record, dict) else (lease or {}).get("state"),
            "verified_at_utc": record.get("confirmed_absent_at_utc") if isinstance(record, dict) else None,
            "absence_evidence": absence,
        },
    }


def _run_openjev(args: argparse.Namespace) -> int:
    request = _read_json(args.request)
    evidence = _read_json(args.evidence)
    limits = _read_json(args.limits)
    if not isinstance(request, dict) or not isinstance(evidence, dict) or not isinstance(limits, dict):
        raise ValueError("request, evidence and limits files must each contain JSON objects")
    try:
        hourly_cap = Decimal(str(limits["max_hourly_usd"]))
    except (KeyError, InvalidOperation, ValueError, TypeError):
        hourly_cap = None
    if hourly_cap is not None and hourly_cap.is_finite() and hourly_cap > Decimal("0.20"):
        blocked = {
            "request_id": request.get("request_id"),
            "state": "BLOCKED_OWNER_HOURLY_CAP",
            "reason": "Open-Jev trial cannot exceed the owner's $0.20 all-in hourly ceiling",
            "paid_action_allowed": False,
        }
        _write_json(blocked, args.output)
        return 2
    below_floor = trial_limit_floor_errors(limits)
    if below_floor:
        raise ValueError("Open-Jev caps conflict with the pinned trial window: "
                         + ", ".join(below_floor))

    search_key = os.environ.get("VAST_BROKER_SEARCH_API_KEY")
    lease_key = os.environ.get("VAST_BROKER_LEASE_API_KEY")
    if not search_key or not lease_key:
        raise ValueError("scoped search and lease Vast credentials must be configured in the local secret store")
    if not os.environ.get("VAST_BROKER_CONTROL_HOST_ID"):
        raise ValueError("VAST_BROKER_CONTROL_HOST_ID must identify this control host")
    journal_dir = os.environ.get("VAST_BROKER_JOURNAL_DIR")
    if not journal_dir:
        raise ValueError("VAST_BROKER_JOURNAL_DIR must point to private durable local storage")
    gate_factory = os.environ.get("VAST_BROKER_GUARDIAN_GATE_FACTORY")
    if not gate_factory:
        raise ValueError("two remote recovery paths and their shared registry are not configured")

    # Resolve the exact candidate before binding the pinned runtime recipe.
    resolution = resolve_request(request)
    disk = limits.get("temporary_disk_gb")
    if isinstance(disk, bool) or not isinstance(disk, int):
        raise ValueError("Open-Jev run requires an integer temporary disk limit")
    recipes: dict[str, Any] = {}
    for candidate in resolution.get("candidates", []):
        identity = candidate.get("identity", {})
        if (identity.get("artifact_repo") == OPENJEV_MODEL_REPOSITORY
                and identity.get("revision") == OPENJEV_MODEL_REVISION):
            recipes[candidate_key(candidate)] = deployment_recipe(identity, disk)
    if len(recipes) != 1:
        raise ValueError("request must resolve to the exact pinned Open-Jev 9B artifact")
    request["deployment_recipes_by_candidate"] = recipes

    read_client = VastOffersClient(api_key=search_key)
    lease_client = VastOffersClient(api_key=lease_key)
    search_service = VastRentaiService(search_client=read_client)

    def search_current(constraints: Any, requested_disk: float | int | None) -> dict[str, Any]:
        return search_service.search(constraints, disk_gb=requested_disk)

    route = route_request(request, evidence_by_candidate=evidence, limits=limits,
                          offer_searcher=search_current,
                          allowed_rental_types=("ondemand",))
    if route.get("state") != "READY_TO_RUN" or not isinstance(route.get("proposal"), dict):
        _write_json(route, args.output)
        return 2
    _write_json({"state": route["state"], "proposal": route["proposal"],
                 "proposal_digest": route["proposal_digest"]}, args.proposal_output)

    proposal = route["proposal"]
    plan = lease_plan_from_proposal(proposal)
    required_provider_factory = "vast_broker.cli:lease_provider_factory"
    configured_provider_factory = os.environ.get("VAST_BROKER_PROVIDER_FACTORY")
    if configured_provider_factory not in (None, required_provider_factory):
        raise ValueError("Open-Jev runner requires its pinned Vast lifecycle provider factory")
    os.environ["VAST_BROKER_PROVIDER_FACTORY"] = required_provider_factory
    journal = LeaseJournal(journal_dir)
    service = VastRentaiService(
        provider=lease_client, search_client=read_client, journal=journal,
        guardian_gate=_load_factory(gate_factory),
    )
    request_id = str(route["request_id"])
    if journal.load(request_id) is not None:
        _write_json(_trial_report(route, None, None), args.output)
        return 1
    plan["proposal_digest"] = route["proposal_digest"]
    failure: BaseException | None = None
    lease: dict[str, Any] | None = None
    try:
        lease = service.create(
            proposal,
            route["proposal_digest"],
            current_proposal=proposal,
            plan=plan,
            operation=OpenJevTrialOperation(lease_client),
        )
    except BaseException as exc:
        failure = exc
        if isinstance(exc, LeaseError):
            lease = exc.result
    record = journal.load(str(route["request_id"]))
    report = _trial_report(route, lease, record)
    _write_json(report, args.output)
    if failure is not None or report["state"] != "OPENJEV_LIVE_TEST_PASSED":
        return 1
    return 0


def _lease_journal() -> LeaseJournal:
    journal_dir = os.environ.get("VAST_BROKER_JOURNAL_DIR")
    if not journal_dir:
        raise ValueError("VAST_BROKER_JOURNAL_DIR must point to private durable storage")
    return LeaseJournal(journal_dir)


def _status_request(args: argparse.Namespace) -> int:
    journal = _lease_journal()
    if journal.load(args.request_id) is None:
        result = {"request_id": args.request_id, "state": "NOT_FOUND"}
        _write_json(result, args.output)
        return 2
    service = VastRentaiService(provider=lease_provider_factory(), journal=journal)
    result = service.status(args.request_id)
    _write_json(result, args.output)
    return 0 if result["instance_inventory_complete"] and result["volume_inventory_complete"] else 1


def _destroy_request(args: argparse.Namespace) -> int:
    journal = _lease_journal()
    if journal.load(args.request_id) is None:
        result = {"request_id": args.request_id, "state": "NOT_FOUND", "destroyed": False}
        _write_json(result, args.output)
        return 2
    service = VastRentaiService(provider=lease_provider_factory(), journal=journal)
    result = service.destroy(args.request_id)
    _write_json(result, args.output)
    return 0 if result["destroyed"] else 1


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="vast-broker",
        description="Evidence-first Vast rental search and guarded Open-Jev lifecycle tools")
    commands = parser.add_subparsers(dest="command", required=True)
    route = commands.add_parser("route", help="advance one request through evidence, quote and cap gates")
    route.add_argument("--request", required=True, help="JSON model/workload request")
    route.add_argument("--evidence", help="JSON map from candidate key to captured evidence")
    route.add_argument("--market", help="fresh normalized market JSON (never a launch-time cache)")
    route.add_argument("--limits", help="explicit owner spend and lifecycle limits JSON")
    route.add_argument("--output", help="write the structured route result to this path")

    search = commands.add_parser("search", aliases=["offers"],
                                 help="perform a read-only live marketplace search")
    search.add_argument(
        "--filters",
        help=(
            "path to a JSON file with offer constraints; rental type, pagination, "
            "and marketplace status are controlled by this command"
        ),
    )
    search.add_argument("--disk-gb", type=float, help="disk allocation used for price statistics")
    search.add_argument("--page-size", type=int, default=100)
    search.add_argument("--max-pages", type=int, default=20)
    search.add_argument(
        "--rental-type", choices=("ondemand", "bid", "reserved"), action="append",
        help="rental type to include; may be repeated (default: all three)",
    )
    search.add_argument("--output", help="write normalized offers to this path")

    trial = commands.add_parser(
        "create", aliases=["run-openjev"],
        help="create one guarded Open-Jev 9B trial, run inference, and verify teardown"
    )
    trial.add_argument("--request", required=True, help="exact artifact request JSON")
    trial.add_argument("--evidence", required=True, help="captured source evidence by candidate key")
    trial.add_argument("--limits", required=True, help="explicit finite owner spend and lifecycle limits JSON")
    trial.add_argument("--output", required=True, help="private JSON result receipt path")
    trial.add_argument("--proposal-output", required=True, help="write the fresh quote and bound proposal before create")

    status = commands.add_parser("status", help="inspect one journaled lease and refresh its provider status")
    status.add_argument("request_id", help="request ID returned by the broker")
    status.add_argument("--output", help="write the structured status to this path")

    destroy = commands.add_parser("destroy", help="destroy and verify cleanup for one broker-owned request")
    destroy.add_argument("request_id", help="request ID returned by the broker; arbitrary Vast IDs are not accepted")
    destroy.add_argument("--output", help="write the structured cleanup receipt to this path")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "route":
            request = _read_json(args.request)
            if not isinstance(request, dict):
                raise ValueError("request file must contain a JSON object")
            evidence = _read_json(args.evidence) if args.evidence else None
            market = _read_json(args.market) if args.market else None
            limits = _read_json(args.limits) if args.limits else None
            result = route_request(request, evidence_by_candidate=evidence,
                                   market=market, limits=limits)
            _write_json(result, args.output)
            # Routing outcomes are machine-readable results, including valid
            # research/input gates. They are not process failures.
            return 0
        if args.command in {"offers", "search"}:
            api_key = os.environ.get("VAST_BROKER_SEARCH_API_KEY") or os.environ.get("VAST_API_KEY")
            if not api_key:
                raise ValueError(
                    "VAST_BROKER_SEARCH_API_KEY (or legacy VAST_API_KEY) must be supplied by the configured secret store"
                )
            filters = _read_json(args.filters) if args.filters else {}
            if not isinstance(filters, dict):
                raise ValueError("filters file must contain a JSON object")
            result = VastRentaiService(search_client=VastOffersClient(api_key=api_key)).search(
                filters, page_size=args.page_size, max_pages=args.max_pages, disk_gb=args.disk_gb,
                rental_types=tuple(args.rental_type or ("ondemand", "bid", "reserved")),
            )
            _write_json(result, args.output)
            return 0
        if args.command in {"run-openjev", "create"}:
            return _run_openjev(args)
        if args.command == "status":
            return _status_request(args)
        if args.command == "destroy":
            return _destroy_request(args)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        # Do not print provider exception bodies or request headers. The Vast
        # adapter already emits sanitized errors; local file errors are safe.
        print(f"vast-broker: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"vast-broker: operation failed ({type(exc).__name__})", file=sys.stderr)
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
