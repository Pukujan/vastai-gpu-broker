"""Research-first request router and paid-action gate.

This module joins artifact discovery, source-backed deployment evidence, live
market data, limits, and explicit payment authorization. It returns an action
for the adopting agent; it never executes model-card commands or creates a Vast
instance from :func:`route_request`.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
import math
from typing import Any, Callable
from urllib.parse import urlparse

from .evidence import assess_deployment_evidence, estimate_resources, validate_evidence
from .market import NETWORK_MAX_USD_PER_TB, quote_candidates
from .research import resolve_request


REQUIRED_LIMITS = (
    "max_hourly_usd", "max_total_usd", "max_runtime_seconds",
    "max_network_usd", "temporary_disk_gb", "start_deadline_seconds",
    "cold_start_timeout_seconds", "idle_timeout_seconds",
    "hung_request_timeout_seconds",
)
RESEARCH_FIELDS = (
    "selected_artifact_files", "architecture", "total_parameters",
    "quantization", "official_hardware_requirements", "runtime_and_inference",
    "quality_benchmarks", "resource_profile", "tested_configuration",
)
MAX_QUOTE_AGE_SECONDS = 120
MAX_COMPARISON_AGE_SECONDS = 900


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      default=str)


def _digest(value: Any) -> str:
    return sha256(_canonical(value).encode("utf-8")).hexdigest()


def candidate_key(candidate: Mapping[str, Any]) -> str:
    """Stable lookup key binding submitted evidence to this exact manifest."""
    return str(candidate.get("manifest_digest") or _digest(candidate.get("identity", {})))


def _source_integrity(evidence: Mapping[str, Any]) -> dict[str, Any]:
    """Require retrievable, integrity-checked source captures for every claim.

    A URL and a digest pasted into JSON are only assertions. The digest is
    recomputed from the captured text here, and cited excerpts must occur in
    that capture. Semantic relevance still requires the researcher's locator
    and claim to be reviewed; no text classifier is treated as proof.
    """
    sources = {s.get("source_id"): s for s in evidence.get("sources", [])
               if isinstance(s, Mapping)}
    errors: list[str] = []
    checked: list[str] = []
    for idx, source in enumerate(evidence.get("sources", [])):
        if not isinstance(source, Mapping):
            errors.append(f"source[{idx}]_not_object")
            continue
        source_id = str(source.get("source_id", idx))
        parsed = urlparse(str(source.get("url", "")))
        local_capture = str(source.get("role", "")).startswith("local_")
        if not local_capture and (parsed.scheme != "https" or not parsed.netloc):
            errors.append(f"source_{source_id}_must_use_https")
        capture = source.get("content")
        if not isinstance(capture, str):
            errors.append(f"source_{source_id}_missing_capture")
        else:
            actual = "sha256:" + sha256(capture.encode("utf-8")).hexdigest()
            if source.get("content_digest") != actual:
                errors.append(f"source_{source_id}_digest_mismatch")
            else:
                checked.append(source_id)
        if not source.get("locator"):
            errors.append(f"source_{source_id}_missing_locator")
    for idx, claim in enumerate(evidence.get("claims", [])):
        if not isinstance(claim, Mapping):
            continue
        excerpt = claim.get("support_excerpt")
        refs = claim.get("source_ids", [])
        if claim.get("provenance") != "unknown" and not isinstance(excerpt, str):
            errors.append(f"claim[{idx}]_missing_support_excerpt")
            continue
        if isinstance(excerpt, str) and refs:
            if not any(excerpt and excerpt in str(sources.get(ref, {}).get("content", ""))
                       for ref in refs):
                errors.append(f"claim[{idx}]_excerpt_not_in_cited_capture")
    return {"valid": not errors, "errors": sorted(set(errors)),
            "verified_source_ids": sorted(set(checked))}


def _claims(evidence: Mapping[str, Any], field: str) -> list[Mapping[str, Any]]:
    return [c for c in evidence.get("claims", []) if isinstance(c, Mapping)
            and c.get("field") == field]


def review_candidate(candidate: Mapping[str, Any], evidence: Mapping[str, Any],
                     workload: Mapping[str, Any]) -> dict[str, Any]:
    """Check evidence provenance and report what is proven, estimated, unknown."""
    identity = candidate.get("identity", {})
    shape = validate_evidence(evidence, identity)
    captures = _source_integrity(evidence)
    deployment_review = assess_deployment_evidence(evidence, identity, workload)
    errors = sorted(set(shape["errors"] + captures["errors"]))
    claims = shape["claims"]
    fields = {str(c.get("field")) for c in claims}
    missing = [field for field in RESEARCH_FIELDS if field not in fields]
    official = next(iter(_claims(evidence, "official_hardware_requirements")), None)
    official_status = None
    if official is not None and isinstance(official.get("value"), Mapping):
        official_status = official["value"].get("status")
        if official.get("provenance") != "publisher_requirement":
            errors.append("official_hardware_claim_must_use_publisher_requirement_provenance")
    artifact_claim = next(iter(_claims(evidence, "selected_artifact_files")), None)
    if artifact_claim is not None and artifact_claim.get("value") != identity.get("files"):
        errors.append("selected_files_claim_does_not_match_resolved_manifest")
    quant_claim = next(iter(_claims(evidence, "quantization")), None)
    if quant_claim is not None and quant_claim.get("value") != identity.get("quantization"):
        errors.append("quantization_claim_does_not_match_resolved_artifact")
    architecture_claim = next(iter(_claims(evidence, "architecture")), None)
    if (architecture_claim is not None and candidate.get("architecture")
            and architecture_claim.get("value") != candidate.get("architecture")):
        errors.append("architecture_claim_does_not_match_resolved_artifact_metadata")
    matching_tests = deployment_review.get("tested_profiles", [])
    official_value = official.get("value", {}) if isinstance(official, Mapping) else {}
    if (deployment_review.get("valid") and not matching_tests
            and isinstance(official_value, Mapping)
            and official_value.get("status") == "published"
            and official_value.get("minimum_gpu_ram_bytes") is not None
            and official_value.get("min_gpu_ram_mb") is None):
        # The current offer filter consumes GPU VRAM requirements in MB. Do not
        # accept a byte-valued minimum that the planner cannot enforce.
        deployment_review = {**deployment_review, "state": "OFFICIAL_REQUIREMENT_NOT_ROUTABLE",
                             "valid": False,
                             "research_tasks": ["provide the publisher GPU-memory minimum in the planner's supported MB field or a workload-matched tested GPU profile"]}
    resource_profile = evidence.get("resource_profile")
    estimate = None
    if isinstance(resource_profile, Mapping):
        profile = {**dict(resource_profile), "sources": shape["sources"]}
        estimate = estimate_resources(profile, candidate.get("selected_files", candidate.get("files", [])),
                                       identity=identity)
    if not captures["valid"] or not shape["valid"] or errors:
        state = "EVIDENCE_INVALID"
    elif missing:
        state = "EXTERNAL_RESEARCH_REQUIRED"
    elif not deployment_review.get("valid"):
        state = deployment_review.get("state", "FIT_UNRESOLVED")
        missing = list(deployment_review.get("research_tasks", []))
    else:
        state = "RESEARCH_REVIEWED"
    return {
        "state": state, "valid": state == "RESEARCH_REVIEWED",
        "candidate_key": candidate_key(candidate), "identity": dict(identity),
        "errors": errors, "missing_research": missing,
        "official_hardware_status": official_status,
        "matching_tested_configurations": matching_tests,
        "deployment_evidence_review": deployment_review,
        "research_facts": {name: [dict(c) for c in claims if c.get("field") == name]
                           for name in ("architecture", "total_parameters", "quantization",
                                        "official_hardware_requirements", "quality_benchmarks",
                                        "runtime_and_inference")},
        "resource_estimate": estimate,
        "fit_claim": "workload-matched external/local test or sourced publisher requirement"
                    if state == "RESEARCH_REVIEWED" else "unknown; estimates alone do not prove fit",
        "paid_action_allowed": False,
    }


def _evidence_for(evidence_by_candidate: Any, candidate: Mapping[str, Any]) -> Mapping[str, Any] | None:
    if not isinstance(evidence_by_candidate, Mapping):
        return None
    value = evidence_by_candidate.get(candidate_key(candidate))
    if value is None:
        value = evidence_by_candidate.get(candidate.get("identity", {}).get("artifact_repo") + "@"
                                          + candidate.get("identity", {}).get("revision", ""))
    return value if isinstance(value, Mapping) else None


def _fit_constraints(review: Mapping[str, Any], evidence: Mapping[str, Any]) -> dict[str, Any]:
    """Build offer filters only from sourced minima or exact tested GPU names."""
    out: dict[str, Any] = {
        "require_rentable": True,
        "exclude_rented": True,
        # Owner's durable price policy: every direction must be known and at or
        # below the $3/TB displayed-rate ceiling before automatic selection.
        "max_upload_usd_per_tb_vast_cli_display": str(NETWORK_MAX_USD_PER_TB),
        "max_download_usd_per_tb_vast_cli_display": str(NETWORK_MAX_USD_PER_TB),
    }
    official = next(iter(_claims(evidence, "official_hardware_requirements")), None)
    if official and isinstance(official.get("value"), Mapping):
        values = official["value"]
        # Values are used only when a non-unknown, source-backed claim passed review.
        for field in ("min_gpu_ram_mb", "min_gpus", "min_cpu_cores", "min_cpu_ram_mb", "min_disk_gb",
                      "min_download_mb_s", "min_upload_mb_s"):
            if values.get(field) is not None:
                out[field] = values[field]
    names = sorted({str(t.get("gpu_name")) for t in review.get("matching_tested_configurations", [])
                    if t.get("gpu_name")})
    if names:
        out["gpu_name"] = names
    return out


def _limits_missing(limits: Mapping[str, Any] | None, rental_type: str | None) -> list[str]:
    limits = limits or {}
    missing = [key for key in REQUIRED_LIMITS if key not in limits or limits.get(key) is None]
    if rental_type == "bid":
        missing.extend(key for key in ("max_bid_usd_per_machine_hour", "bid_increment_usd", "max_bid_attempts")
                       if key not in limits or limits.get(key) is None)
    return sorted(set(missing))


def _limits_valid(limits: Mapping[str, Any]) -> bool:
    for key in REQUIRED_LIMITS:
        value = limits.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            return False
        if value < 0 or (key not in {"temporary_disk_gb", "max_network_usd"} and value == 0):
            return False
        if key == "temporary_disk_gb" and value == 0:
            return False
    bid_fields = ("max_bid_usd_per_machine_hour", "bid_increment_usd", "max_bid_attempts")
    present = [key for key in bid_fields if key in limits]
    if present:
        if len(present) != len(bid_fields):
            return False
        bid_cap, increment, attempts = (limits[key] for key in bid_fields)
        if (isinstance(bid_cap, bool) or not isinstance(bid_cap, (int, float)) or not math.isfinite(bid_cap)
                or bid_cap <= 0 or isinstance(increment, bool) or not isinstance(increment, (int, float))
                or not math.isfinite(increment) or increment <= 0 or isinstance(attempts, bool)
                or not isinstance(attempts, int) or attempts < 0):
            return False
    return True


def _bounded_cost(offer: Mapping[str, Any], limits: Mapping[str, Any]) -> dict[str, Any] | None:
    """Worst-case machine exposure over the authorized startup/runtime envelope."""
    try:
        hourly = Decimal(str(offer["machine_hour_usd"]))
        runtime = Decimal(str(limits["max_runtime_seconds"]))
        startup = Decimal(str(limits["start_deadline_seconds"]))
        network = Decimal(str(limits["max_network_usd"]))
        if offer.get("rental_type") == "bid":
            if limits.get("max_bid_usd_per_machine_hour") is None:
                return None
            hourly = max(hourly, Decimal(str(limits["max_bid_usd_per_machine_hour"])))
        if not all(x.is_finite() and x >= 0 for x in (hourly, runtime, startup, network)):
            return None
    except (KeyError, InvalidOperation, ValueError, TypeError):
        return None
    machine = hourly * (runtime + startup) / Decimal(3600)
    total = machine + network
    return {"machine_and_included_storage_usd": float(machine),
            "network_allowance_usd": float(network), "worst_case_total_usd": float(total),
            "hourly_rate_basis_usd_per_machine_hour": float(hourly),
            "temporary_disk_gb": float(limits["temporary_disk_gb"]),
            "time_envelope_seconds": float(runtime + startup),
            "formula": "machine_hour_usd * (max_runtime_seconds + start_deadline_seconds) / 3600 + max_network_usd",
            "status": "bounded_estimate_not_invoice"}


def _market_snapshot_problem(market: Mapping[str, Any], now: datetime,
                             requested_disk: Any) -> tuple[str | None, float]:
    """Check quote age and the exact search dimensions used to price a disk."""
    if (not isinstance(market.get("as_of_utc"), str)
            or not isinstance(market.get("offers"), list)
            or not isinstance(market.get("digest"), str)
            or not isinstance(market.get("query_filters"), list)):
        return "MARKET_SNAPSHOT_INVALID_OR_QUERY_PROVENANCE_MISSING", float("inf")
    try:
        captured_at = datetime.fromisoformat(market["as_of_utc"].replace("Z", "+00:00"))
        if captured_at.tzinfo is None or now.tzinfo is None:
            raise ValueError
        age = (now.astimezone(timezone.utc) - captured_at.astimezone(timezone.utc)).total_seconds()
    except (TypeError, ValueError, OverflowError):
        return "MARKET_SNAPSHOT_INVALID_OR_STALE", float("inf")
    if age < -5 or age > MAX_COMPARISON_AGE_SECONDS:
        return "MARKET_SNAPSHOT_INVALID_OR_STALE", age
    query_filters = market["query_filters"]
    required_types = {"ondemand", "bid", "reserved"}
    if (not query_filters or any(not isinstance(query, Mapping) for query in query_filters)
            or not required_types.issubset({str(query.get("type", "")).casefold() for query in query_filters})):
        return "MARKET_QUERY_PARTITIONS_INCOMPLETE", age
    if requested_disk is not None:
        try:
            expected_disk = Decimal(str(requested_disk))
            quoted_disk = Decimal(str(market["allocated_storage_gb"]))
            query_disks = [Decimal(str(query["allocated_storage"])) for query in query_filters]
        except (KeyError, InvalidOperation, ValueError, TypeError):
            return "MARKET_DISK_ALLOCATION_UNPROVEN", age
        if (not expected_disk.is_finite() or not quoted_disk.is_finite()
                or any(not value.is_finite() for value in query_disks)
                or quoted_disk != expected_disk or any(value != expected_disk for value in query_disks)):
            return "MARKET_DISK_ALLOCATION_MISMATCH", age
    return None, age


def _market_is_cached(market: Mapping[str, Any]) -> bool:
    """Read current and compatibility cache metadata without trusting labels."""
    for name in ("offer_cache", "cache_metadata"):
        metadata = market.get(name)
        if isinstance(metadata, Mapping) and (metadata.get("cache_hit") is True
                                              or metadata.get("authorization_eligible") is False):
            return True
    return False


def _authorization_valid(request: Mapping[str, Any]) -> bool:
    auth = request.get("paid_authorization")
    return (isinstance(auth, Mapping) and auth.get("authorized") is True
            and isinstance(auth.get("scope"), str)
            and auth.get("scope") in {"exact_artifact", "scoped_auto_select"}
            and isinstance(auth.get("provenance"), Mapping)
            and bool(auth["provenance"].get("type") and auth["provenance"].get("value")))


def route_request(request: Mapping[str, Any], *, evidence_by_candidate: Mapping[str, Any] | None = None,
                  market: Mapping[str, Any] | None = None, limits: Mapping[str, Any] | None = None,
                  offer_searcher: Callable[[Mapping[str, Any], float | int | None], Mapping[str, Any]] | None = None,
                  fetcher: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None,
                  now_utc: datetime | None = None) -> dict[str, Any]:
    """Advance a hosting request through research, quote, confirmation and caps.

    Market input must be a current normalized result from ``search_offers``.
    Snapshot files are not accepted as launch-time quotes. The return value is
    advisory until :func:`authorize_run` revalidates it immediately before the
    lease controller is invoked.
    """
    workload = request.get("workload", {})
    simultaneous = (request.get("simultaneous_model_set")
                    or (workload.get("simultaneous_model_set") if isinstance(workload, Mapping) else None))
    if isinstance(simultaneous, Sequence) and not isinstance(simultaneous, (str, bytes)) and len(simultaneous) > 1:
        return {"request_id": request.get("request_id"), "state": "SIMULTANEOUS_MODEL_SET_UNSUPPORTED",
                "next_action": "jointly_research_and_plan_every_model_before_any_create",
                "reason_codes": ["JOINT_MODEL_SET_PLANNER_NOT_IMPLEMENTED"],
                "required_inputs": ["joint per-device model placement, all-model fit evidence, and aggregate cost plan"],
                "paid_action_allowed": False}
    if request.get("bid_mode") == "parallel" or request.get("parallel_bid_candidates", 1) not in (None, 1):
        return {"request_id": request.get("request_id"), "state": "PARALLEL_BID_RACE_UNSUPPORTED",
                "next_action": "use_single_bounded_offer_or_wait_for_aggregate_race_coordinator",
                "reason_codes": ["PARALLEL_BID_COORDINATOR_NOT_IMPLEMENTED"],
                "required_inputs": ["aggregate bid exposure accounting, durable task deduplication, and verified loser cleanup"],
                "paid_action_allowed": False}
    resolution = resolve_request(request, fetcher=fetcher)
    if resolution["state"] in {"RESEARCH_REQUIRED", "MODEL_CONFIRMATION_REQUIRED"} and not resolution.get("candidates"):
        return {**resolution, "request_id": request.get("request_id"), "required_inputs": [],
                "paid_action_allowed": False}
    candidates = list(resolution.get("candidates", []))
    if not candidates:
        return {**resolution, "state": "RESEARCH_REQUIRED", "paid_action_allowed": False}
    selected_ref = request.get("selected_candidate")
    if selected_ref:
        candidates = [c for c in candidates if selected_ref in {
            c.get("repo_id"), c.get("identity", {}).get("artifact_repo"), candidate_key(c)}]
        if not candidates:
            return {"state": "RESEARCH_REQUIRED", "next_action": "resolve_selected_candidate_within_original_scope",
                    "reason_codes": ["SELECTED_CANDIDATE_NOT_IN_SCOPE"], "paid_action_allowed": False}
    reviewed: dict[str, dict[str, Any]] = {}
    candidate_evidence: dict[str, Mapping[str, Any]] = {}
    for candidate in candidates:
        key = candidate_key(candidate)
        supplied = _evidence_for(evidence_by_candidate, candidate)
        if supplied is None:
            reviewed[key] = {"state": "EXTERNAL_RESEARCH_REQUIRED", "valid": False,
                             "candidate_key": key, "identity": candidate.get("identity"),
                             "missing_research": list(RESEARCH_FIELDS), "paid_action_allowed": False}
        else:
            candidate_evidence[key] = supplied
            reviewed[key] = review_candidate(candidate, supplied, request.get("workload", {}))
    if any(not r.get("valid") for r in reviewed.values()):
        return {"request_id": request.get("request_id"), "state": "EXTERNAL_RESEARCH_REQUIRED",
                "next_action": "research_exact_candidate_from_captured_primary_and_tested_sources",
                "research_tasks": ["read the exact Hugging Face artifact card, config, file manifest and quantization metadata",
                                   "search publisher/runtime documentation for stated hardware and inference requirements",
                                   "when official numeric minima are absent, capture exact-variant external deployment evidence; do not infer fit from parameter count",
                                   "derive VRAM/RAM/storage terms only from cited files, architecture/runtime docs or measured deployments; preserve unknowns",
                                   "provide exact runtime, interface, simultaneous workload, and a real inference acceptance probe"],
                "candidate_reviews": reviewed, "required_inputs": ["captured source-backed candidate evidence"],
                "paid_action_allowed": False}
    constraints = {key: _fit_constraints(reviewed[key], candidate_evidence[key])
                   for key in reviewed}
    requested_disk = (limits or {}).get("temporary_disk_gb")
    if market is None and offer_searcher is None:
        return {"request_id": request.get("request_id"), "state": "LIVE_OFFERS_REQUIRED",
                "next_action": "search_current_vast_offers_for_each_evidenced_candidate",
                "research_tasks": ["query all permitted Vast rental types without verification filtering",
                                   "retain verification labels and statuses, network/storage components, and truncation details",
                                   "refresh immediately before any paid action"],
                "candidate_reviews": reviewed, "required_inputs": ["fresh complete or explicitly bounded Vast search"],
                "paid_action_allowed": False}
    candidate_markets: dict[str, Mapping[str, Any]] = {}
    if offer_searcher is not None:
        for key in sorted(constraints):
            try:
                fresh_market = offer_searcher(constraints[key], requested_disk)
            except Exception as exc:
                return {"request_id": request.get("request_id"), "state": "LIVE_OFFERS_REQUIRED",
                        "reason_codes": ["LIVE_OFFER_QUERY_FAILED"],
                        "error_type": type(exc).__name__, "candidate_key": key,
                        "paid_action_allowed": False}
            candidate_markets[key] = fresh_market
    else:
        candidate_markets = {key: market for key in constraints}  # type: ignore[dict-item]

    now = now_utc or datetime.now(timezone.utc)
    ages: dict[str, float] = {}
    quote_stats: dict[str, Any] = {}
    for key, candidate_market in candidate_markets.items():
        problem, age = _market_snapshot_problem(candidate_market, now, requested_disk)
        if problem:
            return {"request_id": request.get("request_id"), "state": "LIVE_OFFERS_REQUIRED",
                    "reason_codes": [problem], "candidate_key": key, "paid_action_allowed": False}
        ages[key] = age
        quote_stats[key] = quote_candidates(
            {key: constraints[key]}, market=dict(candidate_market), disk_gb=requested_disk
        )[key]
    comparisons = {}
    for candidate in candidates:
        key = candidate_key(candidate)
        comparisons[key] = {"identity": candidate["identity"], "research": reviewed[key],
                            "offer_comparison": quote_stats.get(key),
                            "market_as_of_utc": candidate_markets[key].get("as_of_utc"),
                            "market_digest": candidate_markets[key].get("digest")}
    ambiguous = bool(resolution.get("candidate_selection_required")) and not selected_ref
    if ambiguous:
        return {"request_id": request.get("request_id"), "state": "MODEL_CONFIRMATION_REQUIRED",
                "next_action": "ask_user_to_select_exact_researched_artifact",
                "candidate_selection_required": True, "candidate_comparisons": comparisons,
                "market_as_of_utc_by_candidate": {key: value.get("as_of_utc") for key, value in candidate_markets.items()},
                "required_inputs": ["exact artifact selection"], "paid_action_allowed": False}
    scoped_auto = resolution.get("resolution_mode") == "scoped_auto_select" and not selected_ref
    if scoped_auto and len(candidates) > 1:
        choices = []
        for option in candidates:
            option_key = candidate_key(option)
            eligible = quote_stats[option_key]["ranking"].get("eligible", [])
            actionable = [o for o in eligible if o.get("rental_type") in {"ondemand", "bid"}
                          and o.get("machine_hour_usd") is not None]
            if actionable:
                try:
                    price = Decimal(str(actionable[0]["machine_hour_usd"]))
                except (InvalidOperation, ValueError, TypeError):
                    continue
                if price.is_finite() and price >= 0:
                    choices.append((price, str(option["identity"].get("artifact_repo", "")),
                                    str(option["identity"].get("revision", "")), option, actionable[0]))
        if not choices:
            return {"request_id": request.get("request_id"), "state": "BLOCKED_CAPACITY_OR_FIT",
                    "next_action": "report_no_in_scope_candidate_with_an_actionable_offer",
                    "candidate_comparisons": comparisons, "paid_action_allowed": False}
        choices.sort(key=lambda item: (item[0], item[1], item[2], str(item[4].get("id", ""))))
        candidate = choices[0][3]
    else:
        candidate = candidates[0]
    key = candidate_key(candidate)
    ranking = quote_stats[key]["ranking"]
    if not ranking.get("eligible"):
        return {"request_id": request.get("request_id"), "state": "BLOCKED_CAPACITY_OR_FIT",
                "next_action": "report_no_current_offer_proven_to_fit",
                "candidate_comparisons": comparisons,
                "required_inputs": ["new compatible listing or explicitly authorized bounded validation"],
                "paid_action_allowed": False}
    action_offers = [o for o in ranking["eligible"] if o.get("rental_type") in {"ondemand", "bid"}]
    reserved_offers = [o for o in ranking["eligible"] if o.get("rental_type") == "reserved"]
    if not action_offers and reserved_offers:
        return {"request_id": request.get("request_id"), "state": "RESERVED_PREPAYMENT_NOT_AUTHORIZED",
                "next_action": "report_reserved_equivalent_separately_and_require_exact_conversion_terms_and_prepaid_cap",
                "reserved_offers": reserved_offers,
                "required_inputs": ["owner authorization for reserved conversion and its full commitment price"],
                "paid_action_allowed": False}
    if not action_offers:
        return {"request_id": request.get("request_id"), "state": "BLOCKED_CAPACITY_OR_FIT",
                "next_action": "report_no_actionable_on_demand_or_interruptible_offer",
                "candidate_comparisons": comparisons, "paid_action_allowed": False}
    rental_type = action_offers[0].get("rental_type")
    missing = _limits_missing(limits, rental_type)
    if missing or not _limits_valid(limits or {}):
        return {"request_id": request.get("request_id"), "state": "PAID_LIMITS_REQUIRED",
                "next_action": "request_concrete_owner_limits_before_any_create_call",
                "required_inputs": missing or ["valid finite non-negative limit values"],
                "candidate_comparisons": comparisons, "paid_action_allowed": False}
    under_hourly_cap = []
    for item in action_offers:
        try:
            item_rate = Decimal(str(item.get("machine_hour_usd")))
        except (InvalidOperation, TypeError, ValueError):
            continue
        if item_rate.is_finite() and item_rate >= 0 and item_rate <= Decimal(str(limits["max_hourly_usd"])):
            under_hourly_cap.append(item)
    if not under_hourly_cap:
        return {"request_id": request.get("request_id"), "state": "BLOCKED_HOURLY_CAP",
                "next_action": "report_candidate_prices_above_owner_hourly_cap",
                "candidate_comparisons": comparisons, "paid_action_allowed": False}
    costed_offers = []
    for item in under_hourly_cap:
        if item.get("rental_type") == "bid":
            if (limits.get("max_bid_usd_per_machine_hour") is None
                    or limits["max_bid_usd_per_machine_hour"] > limits["max_hourly_usd"]):
                continue
        bound = _bounded_cost(item, limits)
        if bound is not None and bound["worst_case_total_usd"] <= limits["max_total_usd"]:
            costed_offers.append((item, bound))
    if not costed_offers:
        if any(o.get("rental_type") == "bid" for o in under_hourly_cap) and limits.get("max_bid_usd_per_machine_hour") is not None and limits["max_bid_usd_per_machine_hour"] > limits["max_hourly_usd"]:
            return {"request_id": request.get("request_id"), "state": "BLOCKED_HOURLY_CAP",
                    "next_action": "report_interruptible_bid_cap_exceeds_owner_hourly_cap",
                    "candidate_comparisons": comparisons, "paid_action_allowed": False}
        return {"request_id": request.get("request_id"), "state": "BLOCKED_TOTAL_CAP",
                "next_action": "report_bounded_cost_above_owner_total_cap",
                "candidate_comparisons": comparisons,
                "paid_action_allowed": False}
    # Rank by the complete bounded exposure rather than the offer's lowest
    # observed bid floor; escalation remains capped and visible to the owner.
    costed_offers.sort(key=lambda pair: (pair[1]["worst_case_total_usd"], str(pair[0].get("id", ""))))
    selected_offer, cost_bound = costed_offers[0]
    selected_market = candidate_markets[key]
    quote_too_old = ages[key] > MAX_QUOTE_AGE_SECONDS
    quote_from_cache = _market_is_cached(selected_market)
    if quote_too_old or quote_from_cache:
        return {"request_id": request.get("request_id"), "state": "LIVE_REFRESH_REQUIRED",
                "next_action": "force_live_offer_refresh_before_paid_action",
                "reason_codes": (["MARKET_QUOTE_TOO_OLD_FOR_PAID_ACTION"] if quote_too_old else [])
                               + (["MARKET_CACHE_HIT_NOT_AUTHORIZED_FOR_PAID_ACTION"] if quote_from_cache else []),
                "candidate_comparisons": comparisons,
                "selected_candidate": candidate["identity"], "selected_offer": selected_offer,
                "required_inputs": ["bypass the local comparison cache and re-query Vast immediately before confirmation/create"],
                "paid_action_allowed": False}
    if not _authorization_valid(request):
        return {"request_id": request.get("request_id"), "state": "PRICE_CONFIRMATION_REQUIRED",
                "next_action": "present_exact_artifact_offer_and_bounded_cost_for_confirmation",
                "selected_candidate": candidate["identity"], "offer": selected_offer,
                "cost_bound": cost_bound,
                "limits": dict(limits or {}), "candidate_comparisons": comparisons,
                "paid_action_allowed": False}
    recipes = request.get("deployment_recipes_by_candidate")
    recipe = (recipes.get(key) if isinstance(recipes, Mapping) and key in recipes
              else request.get("deployment_recipe"))
    if not isinstance(recipe, Mapping) or not recipe:
        return {"request_id": request.get("request_id"), "state": "DEPLOYMENT_RECIPE_REQUIRED",
                "next_action": "research_and_review_an_exact_immutable_runtime_recipe_for_the_selected_artifact",
                "required_inputs": ["deployment recipe with exact identity, image, and create parameters"],
                "selected_candidate": candidate["identity"], "offer": selected_offer,
                "cost_bound": cost_bound, "candidate_comparisons": comparisons,
                "paid_action_allowed": False}
    if recipe.get("identity") != candidate["identity"]:
        return {"request_id": request.get("request_id"), "state": "DEPLOYMENT_RECIPE_INVALID",
                "reason_codes": ["RECIPE_ARTIFACT_IDENTITY_MISMATCH"],
                "required_inputs": ["recipe bound to the exact selected artifact revision and files"],
                "paid_action_allowed": False}
    recipe_params = recipe.get("create_params")
    if (not isinstance(recipe.get("image"), str) or not recipe.get("image")
            or not isinstance(recipe_params, Mapping)
            or str(recipe_params.get("disk")) != str(limits.get("temporary_disk_gb"))):
        return {"request_id": request.get("request_id"), "state": "DEPLOYMENT_RECIPE_INVALID",
                "reason_codes": ["RECIPE_IMAGE_OR_DISK_ALLOCATION_INVALID"],
                "required_inputs": ["recipe image and exact authorized temporary disk allocation"],
                "paid_action_allowed": False}
    proposal = {"request_id": request.get("request_id"), "identity": candidate["identity"],
                "candidate_key": key, "offer": selected_offer, "cost_bound": cost_bound,
                "market_digest": selected_market.get("digest"), "market_as_of_utc": selected_market.get("as_of_utc"),
                "market_cache_hit": _market_is_cached(selected_market),
                "evidence_digest": _digest(candidate_evidence[key]),
                "limits": dict(limits or {}), "paid_authorization": dict(request["paid_authorization"]),
                "deployment_recipe": dict(recipe)}
    return {"request_id": request.get("request_id"), "state": "READY_TO_RUN",
            "next_action": "revalidate_quote_and_evidence_then_run_bounded_inference_and_destroy",
            "proposal": proposal, "proposal_digest": _digest(proposal),
            "candidate_comparisons": comparisons, "paid_action_allowed": True}


def authorize_run(proposal: Mapping[str, Any], confirmation_digest: str,
                  *, current_proposal: Mapping[str, Any], lease_controller: Any,
                  plan: Mapping[str, Any], operation: Callable[[dict[str, Any]], Any]) -> dict[str, Any]:
    """Execute only the exact proposal that was confirmed or task-authorized.

    Callers must rebuild ``current_proposal`` from fresh evidence and live offers
    immediately before this operation. The digest mismatch fails closed.
    ``operation`` is application-owned executable code; model-card shell
    snippets and unreviewed remote scripts are never accepted here.
    """
    expected = _digest(proposal)
    if not confirmation_digest or confirmation_digest != expected:
        raise ValueError("proposal confirmation digest mismatch; paid create is blocked")
    if _digest(current_proposal) != expected:
        raise ValueError("fresh evidence or quote changed; replan and reconfirm")
    if proposal.get("candidate_key") is None or not proposal.get("market_digest"):
        raise ValueError("proposal omits bound candidate or market evidence")
    if proposal.get("market_cache_hit") is not False:
        raise ValueError("cached offer comparisons cannot authorize a paid create; force a live refresh")
    try:
        captured = datetime.fromisoformat(str(current_proposal.get("market_as_of_utc", "")).replace("Z", "+00:00"))
        age = (datetime.now(timezone.utc) - captured.astimezone(timezone.utc)).total_seconds()
        if captured.tzinfo is None or age < -5 or age > MAX_QUOTE_AGE_SECONDS:
            raise ValueError
    except (TypeError, ValueError):
        raise ValueError("fresh market quote is missing or expired; paid create is blocked") from None
    limits = proposal.get("limits", {})
    if not isinstance(limits, Mapping) or not _limits_valid(limits):
        raise ValueError("proposal has missing or invalid paid limits")
    if not callable(operation):
        raise ValueError("a trusted installer and inference operation is required")
    _validate_plan_binding(proposal, plan, limits)
    return lease_controller.run(str(proposal.get("request_id") or ""), plan, operation)


def _validate_plan_binding(proposal: Mapping[str, Any], plan: Mapping[str, Any],
                           limits: Mapping[str, Any]) -> None:
    """Fail closed unless the lease controller will create the approved deployment.

    A proposal digest alone does not bind the separately supplied LeaseController
    plan. These checks bind its paid fields to the quote, owner caps and captured
    recipe before any call reaches ``LeaseController.run``.
    """
    if not isinstance(plan, Mapping):
        raise ValueError("lease plan is missing or malformed; paid create is blocked")
    offer = proposal.get("offer")
    if not isinstance(offer, Mapping) or offer.get("id") is None:
        raise ValueError("proposal omits its selected live offer")
    offer_id = offer.get("id")
    planned_id = plan.get("offer_id")
    if (isinstance(offer_id, bool) or isinstance(planned_id, bool)
            or planned_id is None or str(planned_id) != str(offer_id)):
        raise ValueError("lease plan offer_id differs from the approved offer")
    rental_type = str(offer.get("rental_type", "")).lower()
    plan_type = str(plan.get("rental_type", plan.get("type", ""))).lower()
    if rental_type not in {"ondemand", "bid"} or plan_type != rental_type:
        raise ValueError("lease plan rental_type differs from the approved offer")

    # Keep all controller-enforced clocks at or below their authorized values.
    # Exact equality also prevents the plan from silently changing the envelope
    # that was shown in the confirmation proposal.
    for field in ("max_runtime_seconds", "start_deadline_seconds",
                  "cold_start_timeout_seconds", "idle_timeout_seconds",
                  "hung_request_timeout_seconds"):
        try:
            approved = Decimal(str(limits[field]))
            planned = Decimal(str(plan[field]))
        except (KeyError, InvalidOperation, ValueError, TypeError):
            raise ValueError(f"lease plan is missing authorized {field}") from None
        if not approved.is_finite() or not planned.is_finite() or planned != approved:
            raise ValueError(f"lease plan {field} differs from the approved limit")

    try:
        disk = Decimal(str(plan["create_params"]["disk"]))
        approved_disk = Decimal(str(limits["temporary_disk_gb"]))
        network = Decimal(str(plan["max_network_usd"]))
        approved_network = Decimal(str(limits["max_network_usd"]))
    except (KeyError, InvalidOperation, ValueError, TypeError):
        raise ValueError("lease plan must bind requested disk and network allowance") from None
    if (not disk.is_finite() or disk != approved_disk or not network.is_finite()
            or network != approved_network):
        raise ValueError("lease plan disk or network allowance differs from approved limits")
    create_params = plan.get("create_params")
    if not isinstance(create_params, Mapping):
        raise ValueError("lease plan must include explicit create_params")

    recipe = proposal.get("deployment_recipe")
    if not isinstance(recipe, Mapping) or not recipe:
        raise ValueError("proposal does not bind an approved deployment recipe")
    if recipe.get("identity") != proposal.get("identity"):
        raise ValueError("deployment recipe identity differs from the approved artifact")
    image = recipe.get("image")
    if not isinstance(image, str) or not image or create_params.get("image") != image:
        raise ValueError("lease plan image differs from the approved deployment recipe")
    recipe_params = recipe.get("create_params")
    if not isinstance(recipe_params, Mapping):
        raise ValueError("approved deployment recipe must include exact create parameters")
    actual_base_params = dict(create_params)
    actual_base_params.pop("price", None)  # bid price is checked against its separate approved bid fields below
    if dict(recipe_params) != actual_base_params:
        raise ValueError("lease create parameters differ from the approved deployment recipe")
    if plan.get("recipe_digest") != _digest(recipe):
        raise ValueError("lease plan recipe digest differs from the approved deployment recipe")
    if plan.get("identity") != proposal.get("identity"):
        raise ValueError("lease plan artifact identity differs from the approved artifact")

    try:
        quoted_hourly = Decimal(str(offer["machine_hour_usd"]))
        max_hourly = Decimal(str(limits["max_hourly_usd"]))
    except (KeyError, InvalidOperation, ValueError, TypeError):
        raise ValueError("approved offer is missing a valid machine-hour quote") from None
    if (not quoted_hourly.is_finite() or quoted_hourly < 0 or not max_hourly.is_finite()
            or quoted_hourly > max_hourly):
        raise ValueError("approved offer exceeds the hourly spend limit")
    if rental_type == "ondemand":
        if create_params.get("price") is not None:
            raise ValueError("on-demand create cannot include an interruptible bid price")
    else:
        try:
            bid_cap = Decimal(str(limits["max_bid_usd_per_machine_hour"]))
            planned_cap = Decimal(str(plan["max_bid_usd_per_machine_hour"]))
            start_bid = Decimal(str(plan["starting_bid_usd_per_machine_hour"]))
            bid_step = Decimal(str(plan["bid_increment_usd"]))
            approved_step = Decimal(str(limits["bid_increment_usd"]))
            attempts = plan["max_bid_attempts"]
            approved_attempts = limits["max_bid_attempts"]
            request_price = Decimal(str(create_params["price"]))
            quoted_bid = Decimal(str(offer["bid_current_machine_hour_usd"]))
        except (KeyError, InvalidOperation, ValueError, TypeError):
            raise ValueError("bid plan must bind its exact starting machine-hour price and escalation limits") from None
        if (not all(x.is_finite() for x in (bid_cap, planned_cap, start_bid, bid_step,
                                            approved_step, request_price, quoted_bid))
                or planned_cap != bid_cap
                or bid_cap > max_hourly or bid_cap <= 0 or start_bid <= 0 or start_bid > bid_cap
                or bid_step != approved_step or attempts != approved_attempts
                or request_price != start_bid or start_bid != quoted_bid):
            raise ValueError("bid plan differs from the approved machine-hour bid limits")
    bound = _bounded_cost(offer, limits)
    try:
        total_cap = Decimal(str(limits["max_total_usd"]))
        stated_total = Decimal(str(proposal["cost_bound"]["worst_case_total_usd"]))
        computed_total = Decimal(str(bound["worst_case_total_usd"])) if bound else None
    except (KeyError, InvalidOperation, ValueError, TypeError):
        raise ValueError("proposal is missing a valid bounded-cost estimate") from None
    if (computed_total is None or not total_cap.is_finite() or computed_total > total_cap
            or stated_total != computed_total):
        raise ValueError("proposal cost bound does not match the approved limits")


__all__ = ["route_request", "review_candidate", "candidate_key", "authorize_run"]
