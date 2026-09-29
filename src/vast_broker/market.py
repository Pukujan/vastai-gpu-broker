"""Live offer retrieval, normalization, transparent quotes, and ranking."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable

from .provider import VastOffersClient

SCHEMA_VERSION = "1.1"
SEARCH_RENTAL_TYPES = ("ondemand", "bid", "reserved")
DIRECT_CREATE_TYPES = ("ondemand", "bid")
# Public compatibility name: these are search quote types, not all create modes.
RENTAL_TYPES = SEARCH_RENTAL_TYPES
VERIFICATION_STATES = ("verified", "deverified", "unverified")
NETWORK_MAX_USD_PER_TB = Decimal("3")
_RATE_SERIALIZATION_TOLERANCE = Decimal("0.000000001")


def network_rate_tier(rate_usd_per_tb: Any) -> str:
    """Classify the user's per-direction max using Vast CLI's displayed units."""
    rate = _decimal(rate_usd_per_tb)
    if rate is None or rate < 0:
        return "unknown"
    if rate < Decimal("1"):
        return "preferred"
    if rate < Decimal("2"):
        return "acceptable"
    if rate <= NETWORK_MAX_USD_PER_TB:
        return "expensive_at_or_below_hard_max"
    return "over_hard_max"


def _decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return number if number.is_finite() else None


def _json_number(value: Decimal | None) -> float | None:
    return float(value) if value is not None and value.is_finite() else None


def _extract_offers(response: dict[str, Any]) -> list[dict[str, Any]]:
    offers = response.get("offers", [])
    if isinstance(offers, dict):
        # Some examples show a single object while the response schema says array.
        offers = [offers]
    if not isinstance(offers, list):
        return []
    return [item for item in offers if isinstance(item, dict)]


def normalize_offer(
    raw: dict[str, Any],
    *,
    rental_type: str | None = None,
    query_filters: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a JSON-safe normalized record while preserving the exact raw row.

    Vast documents gpu_ram/gpu_total_ram and cpu_ram in MB, disk_space and
    allocated_storage in GB, network rates in MB/s and network costs in $/GB.
    Prices remain decimal USD; no unit is inferred from a malformed value.
    """
    raw_copy = json.loads(json.dumps(raw, default=str))
    kind = rental_type or raw.get("type") or raw.get("rental_type")
    if not kind:
        kind = "bid" if raw.get("is_bid") is True else "ondemand"
    if kind not in SEARCH_RENTAL_TYPES:
        kind = "unknown"
    verification = raw.get("verification")
    if verification not in VERIFICATION_STATES:
        if raw.get("is_vm_deverified") is True:
            verification = "deverified"
        elif raw.get("vericode") == 1:
            verification = "verified"
        else:
            verification = "unknown"

    def price(*fields: str) -> Decimal | None:
        for field in fields:
            parsed = _decimal(raw.get(field))
            if parsed is not None and parsed >= 0:
                return parsed
        return None

    # dph_total is Vast's documented total $/hour offer price. The public
    # schema does not fully define whether every rental type folds requested
    # disk into that number, so retain compute and storage components too.
    total = price("dph_total")
    compute = price("dph_base")
    storage_monthly = price("storage_cost")
    # Keep monthly storage pricing raw: no sourced calendar-month-to-hour
    # conversion is established for a running or stopped instance.
    upload_cost_gb = price("inet_up_cost")
    download_cost_gb = price("inet_down_cost")
    # Match the official Vast CLI's displayed $/TB convention. This is a
    # provider display-equivalent, not a claim about the transfer meter's byte
    # divisor. Keep the source $/GB values intact.
    upload_cost_tb = upload_cost_gb * Decimal(1024) if upload_cost_gb is not None else None
    download_cost_tb = download_cost_gb * Decimal(1024) if download_cost_gb is not None else None
    native_upload_tb = price("internet_up_cost_per_tb")
    native_download_tb = price("internet_down_cost_per_tb")
    upload_tb_present = raw.get("internet_up_cost_per_tb") is not None
    download_tb_present = raw.get("internet_down_cost_per_tb") is not None
    upload_tb_conflict = upload_tb_present and (
        native_upload_tb is None or (upload_cost_tb is not None
        and abs(native_upload_tb - upload_cost_tb) > _RATE_SERIALIZATION_TOLERANCE)
    )
    download_tb_conflict = download_tb_present and (
        native_download_tb is None or (download_cost_tb is not None
        and abs(native_download_tb - download_cost_tb) > _RATE_SERIALIZATION_TOLERANCE)
    )
    upload_policy_tb = None if upload_tb_conflict else upload_cost_tb
    download_policy_tb = None if download_tb_conflict else download_cost_tb
    upload_rate_basis = (
        "conflict" if upload_tb_conflict
        else "native_only_unit_basis_required" if native_upload_tb is not None and upload_cost_tb is None
        else "cli_display_from_per_gb_crosschecked_native_per_tb" if native_upload_tb is not None
        else "cli_display_from_per_gb" if upload_cost_tb is not None
        else "unknown"
    )
    download_rate_basis = (
        "conflict" if download_tb_conflict
        else "native_only_unit_basis_required" if native_download_tb is not None and download_cost_tb is None
        else "cli_display_from_per_gb_crosschecked_native_per_tb" if native_download_tb is not None
        else "cli_display_from_per_gb" if download_cost_tb is not None
        else "unknown"
    )
    gpu_mem_bw = _decimal(raw.get("gpu_mem_bw"))
    gpu_name = raw.get("gpu_name")
    # NVIDIA's GA102 whitepaper specifies 936 GB/s nominal peak for the RTX
    # 3090. Vast's field is explicitly only a listing report; its measurement
    # method is undocumented, so this comparison is diagnostic, never a fit or
    # performance verdict.
    is_rtx_3090 = isinstance(gpu_name, str) and "RTX 3090" in gpu_name.upper() and "3090 TI" not in gpu_name.upper()
    nominal_bw = Decimal("936") if is_rtx_3090 else None
    bw_assessment = None
    if gpu_mem_bw is not None and nominal_bw is not None:
        gap_percent = (nominal_bw - gpu_mem_bw) / nominal_bw * Decimal(100)
        bw_assessment = {
            "state": "reported_below_vendor_nominal_peak" if gpu_mem_bw < nominal_bw else "reported_at_or_above_vendor_nominal_peak",
            "listing_value_gbps": float(gpu_mem_bw),
            "vendor_nominal_peak_gbps": float(nominal_bw),
            "below_reference_percent": float(gap_percent) if gpu_mem_bw < nominal_bw else 0.0,
            "vendor_reference_url": "https://www.nvidia.com/content/PDF/nvidia-ampere-ga-102-gpu-architecture-whitepaper-v2.1.pdf",
            "interpretation": "listing-versus-nominal diagnostic only; Vast listing measurement method is unspecified and this is not runtime throughput",
        }
    # dph_total is the offer's machine-hour quote; dph_base is the separate
    # compute indication, not a per-GPU rate. Bid floor is kept separately.
    bid_floor = price("min_bid")
    normalized = {
        "id": raw.get("id", raw.get("ask_contract_id")),
        "rental_type": kind,
        "pricing_model": {
            "ondemand": "on_demand_hourly",
            "bid": "interruptible_bid_hourly",
            "reserved": "prepaid_reserved_conversion_quote",
        }.get(kind, "unknown"),
        "creation_mode": "convert_existing_ondemand" if kind == "reserved" else "direct_offer_acceptance" if kind in DIRECT_CREATE_TYPES else "unknown",
        "reserved_commitment_terms": None if kind == "reserved" else {},
        "verification": verification,
        "rentable": raw.get("rentable") if isinstance(raw.get("rentable"), bool) else None,
        "rented": raw.get("rented") if isinstance(raw.get("rented"), bool) else None,
        "gpu_name": raw.get("gpu_name"),
        "num_gpus": _json_number(_decimal(raw.get("num_gpus"))),
        "gpu_ram_mb_per_gpu": _json_number(_decimal(raw.get("gpu_ram"))),
        "gpu_total_ram_mb": _json_number(_decimal(raw.get("gpu_total_ram"))),
        "gpu_memory_bandwidth_reported_gbps": _json_number(gpu_mem_bw),
        "gpu_memory_bandwidth_provenance": "vast_listing_field; reported GB/s; measurement method unspecified",
        "gpu_memory_bandwidth_vendor_comparison": bw_assessment,
        "cpu_cores": _json_number(_decimal(raw.get("cpu_cores_effective", raw.get("cpu_cores")))),
        "cpu_ram_mb": _json_number(_decimal(raw.get("cpu_ram"))),
        "disk_space_gb": _json_number(_decimal(raw.get("disk_space"))),
        "machine_hour_usd": _json_number(total),
        "price_components": {
            "provider_total_machine_hour_usd": _json_number(total),
            "compute_machine_hour_usd": _json_number(compute),
            "requested_disk_allocation_gb": _json_number(_decimal(query_filters.get("allocated_storage"))) if query_filters is not None else None,
            "storage_usd_per_gb_month": _json_number(storage_monthly),
            "upload_network_usd_per_gb": _json_number(upload_cost_gb),
            "download_network_usd_per_gb": _json_number(download_cost_gb),
            "upload_network_usd_per_tb_vast_cli_display": _json_number(upload_cost_tb),
            "download_network_usd_per_tb_vast_cli_display": _json_number(download_cost_tb),
            "upload_network_usd_per_tb_native": _json_number(native_upload_tb),
            "download_network_usd_per_tb_native": _json_number(native_download_tb),
            "upload_network_policy_rate_usd_per_tb": _json_number(upload_policy_tb),
            "download_network_policy_rate_usd_per_tb": _json_number(download_policy_tb),
            "network_policy_rate_basis": {"upload": upload_rate_basis, "download": download_rate_basis},
            "network_unit_state": {"upload": upload_rate_basis, "download": download_rate_basis},
            "total_quote_source": "Vast dph_total for this search query and rental type",
            "additive_rule": "provider total is authoritative; compute/storage fields are diagnostics and must not be added to provider total a second time",
        },
        "compute_machine_hour_usd": _json_number(compute),
        "storage_usd_per_gb_month": _json_number(storage_monthly),
        "storage_usd_per_gb_hour": None,
        "bid_floor_machine_hour_usd": _json_number(bid_floor),
        "bid_current_machine_hour_usd": _json_number(price("dph_total") if kind == "bid" else None),
        "upload_mb_s": _json_number(_decimal(raw.get("inet_up"))),
        "download_mb_s": _json_number(_decimal(raw.get("inet_down"))),
        "upload_usd_per_gb": _json_number(upload_cost_gb),
        "download_usd_per_gb": _json_number(download_cost_gb),
        "upload_usd_per_tb_vast_cli_display": _json_number(upload_cost_tb),
        "download_usd_per_tb_vast_cli_display": _json_number(download_cost_tb),
        "upload_usd_per_tb_native": _json_number(native_upload_tb),
        "download_usd_per_tb_native": _json_number(native_download_tb),
        "upload_usd_per_tb_policy": _json_number(upload_policy_tb),
        "download_usd_per_tb_policy": _json_number(download_policy_tb),
        "upload_network_rate_basis": upload_rate_basis,
        "download_network_rate_basis": download_rate_basis,
        "upload_network_rate_tier": network_rate_tier(upload_policy_tb),
        "download_network_rate_tier": network_rate_tier(download_policy_tb),
        "duration_seconds": _json_number(_decimal(raw.get("duration"))),
        "raw": raw_copy,
    }
    if query_filters is not None:
        exact_filters = _sanitize_query_filters(query_filters)
        normalized["query_filters"] = exact_filters
        normalized["allocated_storage_gb"] = _json_number(_decimal(exact_filters.get("allocated_storage")))
    return normalized


def _sanitize_query_filters(filters: dict[str, Any]) -> dict[str, Any]:
    """Copy the sent query while removing any credential-shaped filters."""
    sensitive = ("apikey", "authorization", "credential", "password", "privatekey", "secret", "token")

    def clean(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                str(key): clean(item)
                for key, item in value.items()
                if not any(part in "".join(ch for ch in str(key).casefold() if ch.isalnum()) for part in sensitive)
            }
        if isinstance(value, list):
            return [clean(item) for item in value]
        return value

    return clean(filters)


def documented_search_filters(constraints: dict[str, Any]) -> dict[str, Any]:
    """Translate evidenced minimums into Vast's documented filter schema.

    Availability and verification stay descriptive and are evaluated against
    returned offer rows; this function only emits resource filters with known
    field/unit mappings.
    """
    mapping = {
        "min_gpu_ram_mb": ("gpu_ram", "gte"),
        "min_gpus": ("num_gpus", "gte"),
        "min_cpu_cores": ("cpu_cores_effective", "gte"),
        "min_cpu_ram_mb": ("cpu_ram", "gte"),
        "min_disk_gb": ("disk_space", "gte"),
        "min_download_mb_s": ("inet_down", "gte"),
        "min_upload_mb_s": ("inet_up", "gte"),
        "min_gpu_memory_bandwidth_gbps": ("gpu_mem_bw", "gte"),
    }
    output: dict[str, Any] = {}
    for source, (target, operator) in mapping.items():
        value = _decimal(constraints.get(source))
        if value is not None and value > 0:
            output[target] = {operator: _json_number(value)}
    names = constraints.get("gpu_name")
    if isinstance(names, (list, tuple, set)):
        exact = sorted({str(name) for name in names if isinstance(name, str) and name.strip()})
        if len(exact) == 1:
            output["gpu_name"] = {"eq": exact[0]}
        elif exact:
            output["gpu_name"] = {"in": exact}
    elif isinstance(names, str) and names.strip():
        output["gpu_name"] = {"eq": names}
    return output


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def _deduplicate(rows: Iterable[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    by_id: dict[tuple[str, str], dict[str, Any]] = {}
    conflicts: list[dict[str, Any]] = []
    out: list[dict[str, Any]] = []
    for row in rows:
        if row.get("id") is None:
            # Without a stable ID we cannot safely merge possibly distinct capacity.
            row = dict(row, identity_uncertain=True)
            out.append(row)
            continue
        key = (str(row["id"]), str(row.get("rental_type", "unknown")))
        old = by_id.get(key)
        if old is None:
            by_id[key] = row
            out.append(row)
        elif _canonical(old["raw"]) == _canonical(row["raw"]):
            continue
        else:
            conflicts.append({"offer_id": key[0], "rental_type": key[1], "reason": "conflicting_duplicate_records"})
            # Keep both observations visible but mark both ineligible pending refresh.
            old["identity_uncertain"] = True
            row["identity_uncertain"] = True
            out.append(row)
    return out, conflicts


def _page_rows(response: dict[str, Any]) -> list[dict[str, Any]]:
    rows = _extract_offers(response)
    # Support both a standard array and a provider wrapper without presuming a total.
    return rows


CONSTRAINT_FILTER_KEYS = frozenset({
    "min_gpu_ram_mb", "min_gpus", "min_cpu_cores", "min_cpu_ram_mb",
    "min_disk_gb", "min_download_mb_s", "min_upload_mb_s",
    "min_gpu_memory_bandwidth_gbps", "gpu_name",
})
BROKER_ONLY_FILTER_KEYS = frozenset({
    "require_rentable", "exclude_rented", "rental_types", "verification",
    "reserved_conversion_authorized", "reserved_commitment",
})


def normalize_search_filters(filters: Mapping[str, Any]) -> dict[str, Any]:
    """Build the provider query from documented constraint names or raw filters.

    Documented constraint names translate through ``documented_search_filters``.
    Broker-side availability and cost ceilings never reach the provider query;
    they are evaluated against returned rows in ``rank_offers``. Any other key
    passes through byte-identical, so raw Vast operator objects keep working.
    One field supplied in both the constraint and native forms is a mix error,
    not a silent override.
    """
    constraints: dict[str, Any] = {}
    query: dict[str, Any] = {}
    for key, value in filters.items():
        if key in BROKER_ONLY_FILTER_KEYS or key.endswith("_usd_per_tb_vast_cli_display"):
            continue
        if key in CONSTRAINT_FILTER_KEYS and (
                key != "gpu_name" or isinstance(value, (str, list, tuple, set))):
            constraints[key] = value
        else:
            query[key] = value
    translated = documented_search_filters(constraints)
    clash = sorted(set(translated) & set(query))
    if clash:
        raise ValueError("filters mix constraint and Vast-native forms for "
                         + ", ".join(clash))
    query.update(translated)
    return query


def search_offers(
    filters: dict[str, Any] | None = None,
    *,
    client: Any = None,
    page_size: int = 100,
    max_pages: int = 20,
    disk_gb: float | int | None = None,
    rental_types: Iterable[str] = RENTAL_TYPES,
) -> dict[str, Any]:
    """Search live offers in documented rental-type partitions.

    Vast's current endpoint docs define a maximum ``limit`` and filter operators,
    but do not define an offset/cursor parameter. We use offset continuation only
    when the provider explicitly returns ``next_offset``; otherwise a page at
    the limit is reported as truncated instead of pretending it is complete.
    Verification is never filtered: the published API example's boolean filter
    would exclude unverified and deverified machines, and support for the string
    status field as a query filter is not established. All returned statuses and
    their observed counts are reported.
    """
    if page_size < 1 or max_pages < 1:
        raise ValueError("page_size and max_pages must be positive")
    client = client or VastOffersClient()
    filters = dict(filters or {})
    reserved = {"type", "limit", "offset", "verified", "verification", "rentable", "rented"}
    if reserved.intersection(filters):
        raise ValueError("filters cannot override type, pagination, or marketplace status fields")
    filters = normalize_search_filters(filters)
    requested_types = tuple(dict.fromkeys(rental_types))
    if any(kind not in SEARCH_RENTAL_TYPES for kind in requested_types):
        raise ValueError("unsupported rental type")
    if disk_gb is not None:
        requested_disk = _decimal(disk_gb)
        if requested_disk is None or requested_disk < 0:
            raise ValueError("disk_gb must be a finite non-negative number")
        if "allocated_storage" in filters:
            raise ValueError("specify allocated_storage through disk_gb only")
        filters["allocated_storage"] = float(requested_disk)
    elif "allocated_storage" in filters:
        requested_disk = _decimal(filters["allocated_storage"])
        if requested_disk is None or requested_disk < 0:
            raise ValueError("allocated_storage must be a finite non-negative number of GB")
        filters["allocated_storage"] = float(requested_disk)
    allocated_storage_gb = _json_number(_decimal(filters.get("allocated_storage")))
    all_rows: list[dict[str, Any]] = []
    pages = 0
    warnings: list[str] = []
    partition_reports: list[dict[str, Any]] = []
    sent_queries: list[dict[str, Any]] = []
    truncated = False
    for kind in requested_types:
        offset: int | None = 0
        partition_count = 0
        for page in range(max_pages):
            payload: dict[str, Any] = dict(filters)
            payload.update({"type": kind, "limit": page_size})
            # Offset is best-effort only; the current published schema does
            # not document it. Do not send it on the first page.
            if offset:
                payload["offset"] = offset
            safe_query = _sanitize_query_filters(payload)
            sent_queries.append(safe_query)
            response = client.search_page(payload)
            pages += 1
            raw_rows = _page_rows(response)
            partition_count += len(raw_rows)
            all_rows.extend(normalize_offer(r, rental_type=kind, query_filters=safe_query) for r in raw_rows)
            next_offset = response.get("next_offset")
            if next_offset is not None:
                try:
                    next_offset = int(next_offset)
                except (TypeError, ValueError):
                    warnings.append("provider returned malformed next_offset")
                    truncated = True
                    break
                if next_offset <= (offset or 0) or not raw_rows:
                    break
                offset = next_offset
                if page == max_pages - 1:
                    truncated = True
                continue
            if len(raw_rows) >= page_size:
                truncated = True
                warnings.append("page reached limit; endpoint docs do not define cursor/offset pagination")
            break
        partition_reports.append({"rental_type": kind, "observed_count": partition_count})
    offers, conflicts = _deduplicate(all_rows)
    if conflicts:
        warnings.append("conflicting duplicate offer IDs require live revalidation")
    verification_counts: dict[str, int] = {state: 0 for state in (*VERIFICATION_STATES, "unknown")}
    by_type_verification: dict[str, dict[str, int]] = {}
    for offer in offers:
        state = offer.get("verification", "unknown")
        if state not in verification_counts:
            state = "unknown"
        verification_counts[state] += 1
        type_counts = by_type_verification.setdefault(
            str(offer.get("rental_type", "unknown")),
            {name: 0 for name in (*VERIFICATION_STATES, "unknown")},
        )
        type_counts[state] += 1
    digest_offers = sorted((_canonical(o) for o in offers))
    payload_for_digest = {
        "offers": digest_offers,
        "filters": filters,
        "query_filters": sent_queries,
        "partitions": partition_reports,
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "as_of_utc": datetime.now(timezone.utc).isoformat(),
        "offers": offers,
        "query_filters": sent_queries,
        "allocated_storage_gb": allocated_storage_gb,
        "completeness": {
            "complete": not truncated,
            "truncated": truncated,
            "pages": pages,
            "limit_per_page": page_size,
            "max_pages_per_partition": max_pages,
            "partitions": partition_reports,
            "observed_verification_counts": verification_counts,
            "observed_verification_counts_by_rental_type": by_type_verification,
            "warnings": list(dict.fromkeys(warnings)),
        },
        "conflicts": conflicts,
        "digest": hashlib.sha256(_canonical(payload_for_digest).encode("utf-8")).hexdigest(),
    }


def _matches(offer: dict[str, Any], constraints: dict[str, Any], *, actionable: bool = True) -> list[str]:
    """Apply explicit resource/availability/cost constraints, never fit inference."""
    failures: list[str] = []
    raw = offer.get("raw", {})
    numeric = {
        "min_gpu_ram_mb": offer.get("gpu_ram_mb_per_gpu"),
        "min_gpus": offer.get("num_gpus"),
        "min_cpu_cores": offer.get("cpu_cores"),
        "min_cpu_ram_mb": offer.get("cpu_ram_mb"),
        "min_disk_gb": offer.get("disk_space_gb"),
        "max_machine_hour_usd": offer.get("machine_hour_usd"),
        "min_download_mb_s": offer.get("download_mb_s"),
        "min_upload_mb_s": offer.get("upload_mb_s"),
        "min_gpu_memory_bandwidth_gbps": offer.get("gpu_memory_bandwidth_reported_gbps"),
        "min_duration_seconds": offer.get("duration_seconds"),
    }
    for key, actual in numeric.items():
        required = _decimal(constraints.get(key))
        if required is None:
            continue
        actual_d = _decimal(actual)
        if actual_d is None or (actual_d < required if key.startswith("min_") else actual_d > required):
            failures.append(key)
    for direction in ("upload", "download"):
        cap = _decimal(constraints.get(f"max_{direction}_usd_per_tb_vast_cli_display"))
        if cap is None:
            continue
        rate_state = offer.get("price_components", {}).get("network_unit_state", {}).get(direction)
        if rate_state == "conflict":
            failures.append(f"{direction}_network_price_unit_conflict")
            continue
        if rate_state not in {"cli_display_from_per_gb", "cli_display_from_per_gb_crosschecked_native_per_tb"}:
            failures.append(f"{direction}_network_price_unit_basis_required")
            continue
        rate = _decimal(offer.get(f"{direction}_usd_per_tb_policy"))
        if rate is None:
            failures.append(f"{direction}_network_price_unknown_per_tb")
        elif rate > cap:
            failures.append(f"{direction}_network_price_above_per_tb_cap")
    if constraints.get("gpu_name") and raw.get("gpu_name") not in constraints["gpu_name"] if isinstance(constraints.get("gpu_name"), (list, tuple, set)) else bool(constraints.get("gpu_name")) and raw.get("gpu_name") != constraints["gpu_name"]:
        failures.append("gpu_name")
    if constraints.get("rental_types") and offer.get("rental_type") not in constraints["rental_types"]:
        failures.append("rental_type")
    if actionable and offer.get("rental_type") == "reserved":
        if constraints.get("reserved_conversion_authorized") is not True:
            failures.append("reserved_requires_explicit_conversion_authorization")
        commitment = constraints.get("reserved_commitment")
        if not isinstance(commitment, dict) or not _valid_reserved_commitment(commitment):
            failures.append("reserved_requires_exact_commitment_terms")
        # The current public create adapter accepts offers directly; it has no
        # conversion/prepayment API operation. An authorized quote remains a
        # comparison result until that distinct operation is implemented.
        failures.append("reserved_conversion_flow_not_supported")
    # verification is descriptive by default, an explicit request is honored.
    if constraints.get("verification") and offer.get("verification") not in constraints["verification"]:
        failures.append("verification")
    if constraints.get("require_rentable", True) and offer.get("rentable") is not True:
        failures.append("not_rentable_or_availability_unknown")
    if constraints.get("exclude_rented", True) and offer.get("rented") is True:
        failures.append("already_rented")
    if offer.get("identity_uncertain"):
        failures.append("conflicting_or_missing_offer_identity")
    return failures


def candidate_price_statistics(
    offers: Iterable[dict[str, Any]], *, disk_gb: float | int | None = None,
) -> dict[str, Any]:
    """Descriptive price samples by rental type; sample unit is machine/hour."""
    distinct, conflicts = _deduplicate(list(offers))
    output: dict[str, Any] = {}
    for kind in (*SEARCH_RENTAL_TYPES, "unknown"):
        rows = [o for o in distinct if o.get("rental_type") == kind]
        sample_rows = [o for o in rows if not o.get("identity_uncertain")]
        prices = sorted(p for p in (_decimal(o.get("machine_hour_usd")) for o in sample_rows) if p is not None)
        mean = sum(prices, Decimal(0)) / len(prices) if prices else None
        bid_floors = sorted(p for p in (_decimal(o.get("bid_floor_machine_hour_usd")) for o in sample_rows) if p is not None)
        # Preserve raw monthly rates; do not divide by an assumed number of
        # hours per month to fabricate an hourly running/stopped quote.
        storage_monthly = [_decimal(o.get("storage_usd_per_gb_month")) for o in sample_rows]
        known_monthly = [v for v in storage_monthly if v is not None]
        compute = sorted(p for p in (_decimal(o.get("compute_machine_hour_usd")) for o in sample_rows) if p is not None)
        output[kind] = {
            "unique_offer_count": len(sample_rows),
            "uncertain_record_count": len(rows) - len(sample_rows),
            "pricing_interpretation": {
                "ondemand": "direct on-demand hourly offer quote",
                "bid": "interruptible machine-hour bid; current quote and minimum bid are reported separately",
                "reserved": "descriptive reserved quote only; conversion requires prepayment and a commitment term, neither inferred from hourly fields",
                "unknown": "unknown rental pricing model",
            }[kind],
            "price_sample_count": len(prices),
            "machine_hour_usd": {
                "min": _json_number(min(prices)) if prices else None,
                "median": _json_number(_decimal_median(prices)) if prices else None,
                "mean": _json_number(mean),
                "sample_unit": "USD per machine-hour",
                "includes_components": "provider dph_total; endpoint documentation does not fully specify requested-disk treatment",
            },
            "bid_floor_machine_hour_usd": {
                "min": _json_number(min(bid_floors)) if bid_floors else None,
                "median": _json_number(_decimal_median(bid_floors)) if bid_floors else None,
                "mean": _json_number(sum(bid_floors, Decimal(0)) / len(bid_floors)) if bid_floors else None,
            },
            "compute_machine_hour_usd": {"min": _json_number(min(compute)) if compute else None},
            "requested_disk_gb": disk_gb,
            "requested_disk_storage_hour_usd": {
                "min": None, "median": None, "mean": None,
                "sample_count": 0,
                "unknown_count": len(sample_rows),
                "basis": "unknown; no sourced conversion from monthly storage rate to an hourly stopped/running charge",
            },
            "storage_usd_per_gb_month": {
                "min": _json_number(min(known_monthly)) if known_monthly else None,
                "median": _json_number(_decimal_median(known_monthly)) if known_monthly else None,
                "mean": _json_number(sum(known_monthly, Decimal(0)) / len(known_monthly)) if known_monthly else None,
                "sample_count": len(known_monthly),
                "unknown_count": len(sample_rows) - len(known_monthly),
            },
            "gpu_memory_bandwidth_reported_gbps": _stats([o.get("gpu_memory_bandwidth_reported_gbps") for o in sample_rows]),
            "network": {
                "upload_mb_s": _stats([o.get("upload_mb_s") for o in sample_rows]),
                "download_mb_s": _stats([o.get("download_mb_s") for o in sample_rows]),
                "upload_usd_per_gb": _stats([o.get("upload_usd_per_gb") for o in sample_rows]),
                "download_usd_per_gb": _stats([o.get("download_usd_per_gb") for o in sample_rows]),
                "upload_usd_per_tb_vast_cli_display": _stats([o.get("upload_usd_per_tb_vast_cli_display") for o in sample_rows]),
                "download_usd_per_tb_vast_cli_display": _stats([o.get("download_usd_per_tb_vast_cli_display") for o in sample_rows]),
                "upload_usd_per_tb_native": _stats([o.get("upload_usd_per_tb_native") for o in sample_rows]),
                "download_usd_per_tb_native": _stats([o.get("download_usd_per_tb_native") for o in sample_rows]),
                "upload_usd_per_tb_policy": _stats([o.get("upload_usd_per_tb_policy") for o in sample_rows]),
                "download_usd_per_tb_policy": _stats([o.get("download_usd_per_tb_policy") for o in sample_rows]),
                "rate_basis_counts": {
                    direction: {
                        basis: sum(1 for offer in sample_rows if offer.get(f"{direction}_network_rate_basis") == basis)
                        for basis in sorted({offer.get(f"{direction}_network_rate_basis", "unknown") for offer in sample_rows})
                    }
                    for direction in ("upload", "download")
                },
                "per_direction_hard_max_usd_per_tb": float(NETWORK_MAX_USD_PER_TB),
                "rate_tier_policy": "<1 preferred; 1-<2 acceptable; 2-3 expensive; >3 rejected; official Vast CLI display-equivalent uses $/GB * 1024",
            },
            "sample_complete": True,
        }
    return {"by_rental_type": output, "conflicts": conflicts}


def _valid_reserved_commitment(commitment: dict[str, Any]) -> bool:
    term = _decimal(commitment.get("term_months"))
    prepaid = _decimal(commitment.get("prepaid_amount_usd"))
    quote = _decimal(commitment.get("quoted_hourly_usd"))
    return all(value is not None and value > 0 for value in (term, prepaid, quote))


def _stats(values: Iterable[Any]) -> dict[str, Any]:
    sample = sorted(p for p in (_decimal(v) for v in values) if p is not None)
    return {
        "min": _json_number(min(sample)) if sample else None,
        "median": _json_number(_decimal_median(sample)) if sample else None,
        "mean": _json_number(sum(sample, Decimal(0)) / len(sample)) if sample else None,
        "sample_count": len(sample),
    }


def _decimal_median(values: list[Decimal]) -> Decimal:
    middle = len(values) // 2
    if len(values) % 2:
        return values[middle]
    return (values[middle - 1] + values[middle]) / Decimal(2)


def rank_offers(
    offers: Iterable[dict[str, Any]],
    constraints: dict[str, Any] | None = None,
    *,
    completeness: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Deterministic constraint-first ranking; incomplete inputs are scoped.

    Current rentability is required by default and already-rented offers are
    excluded; their statuses remain present in the source snapshot and rejection
    reasons. Eligible offers sort by known machine-hour quote, then ID. This
    function never claims task-total cost or model fit, and marks global-cheapest
    claims unavailable when any source partition was capped/truncated.
    """
    constraints = dict(constraints or {})
    unique, conflicts = _deduplicate(list(offers))
    eligible: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for offer in unique:
        reasons = _matches(offer, constraints)
        if reasons:
            rejected.append({"offer_id": offer.get("id"), "reasons": sorted(set(reasons))})
        else:
            eligible.append(offer)
    # Unknown price sorts after every known value; equal values are stable by ID.
    eligible.sort(key=lambda o: (
        _decimal(o.get("machine_hour_usd")) is None,
        _decimal(o.get("machine_hour_usd")) or Decimal(0),
        str(o.get("id", "")),
        _canonical(o.get("raw", {})),
    ))
    complete = bool((completeness or {}).get("complete", False))
    return {
        "eligible": eligible,
        "rejected": rejected,
        "eligible_count": len(eligible),
        "ranking_objective": "lowest known provider machine-hour quote, then offer ID; eligible constraints first",
        "cheapest_claim": "global_market" if complete else "cheapest_among_observed_eligible",
        "global_cheapest_established": complete,
        "quote_freshness": {"as_of_utc": datetime.now(timezone.utc).isoformat(), "must_refresh_before_paid_action": True},
        "digest": hashlib.sha256(_canonical({
            "eligible_quotes": sorted(
                _canonical({k: v for k, v in offer.items() if k != "raw"}) for offer in eligible
            ),
            "constraints": constraints,
            "completeness": completeness or {},
        }).encode()).hexdigest(),
        "conflicts": conflicts,
    }


def quote_candidates(
    candidate_constraints: dict[str, dict[str, Any]],
    *,
    market: dict[str, Any],
    disk_gb: float | int | None = None,
) -> dict[str, Any]:
    """Return candidate-scoped counts, prices and ranked offers from one snapshot."""
    result: dict[str, Any] = {}
    for candidate_id in sorted(candidate_constraints):
        constraints = candidate_constraints[candidate_id]
        ranked = rank_offers(market.get("offers", []), constraints, completeness=market.get("completeness"))
        # Report candidate-specific pricing observations for all matching search
        # types, while rank_offers separately excludes unsupported reserved
        # conversion offers from direct-create eligibility.
        comparison = [
            offer for offer in market.get("offers", [])
            if not _matches(offer, constraints, actionable=False)
        ]
        result[candidate_id] = {
            "constraints": constraints,
            "statistics": candidate_price_statistics(comparison, disk_gb=disk_gb),
            "ranking": ranked,
            "sample_complete": bool(market.get("completeness", {}).get("complete")),
            "as_of_utc": market.get("as_of_utc"),
            "market_digest": market.get("digest"),
        }
    return result
