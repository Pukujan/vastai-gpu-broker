"""Source-backed model evidence and resource estimates.

Public JSON interface: ``validate_evidence(evidence, identity) -> dict`` and
``estimate_resources(profile, files, *, identity=None) -> dict``. Inputs and
outputs are plain JSON-compatible mappings. Missing consequential facts are
represented as ``value: None`` with a reason; parameter-count-only heuristics
and arbitrary model-card commands are deliberately not accepted.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from hashlib import sha256
from math import isfinite
from typing import Any
from urllib.parse import urlparse

PROVENANCE = {
    "publisher_requirement", "runtime_requirement", "artifact_measured",
    "formula_derived", "external_tested_configuration", "local_measured",
    "unknown",
}


def _same_identity(actual: Mapping[str, Any], expected: Mapping[str, Any]) -> bool:
    keys = ("base_id", "artifact_repo", "revision", "format", "quantization", "files")
    return all(k not in expected or actual.get(k) == expected.get(k) for k in keys)


def _identity_errors(identity: Mapping[str, Any]) -> list[str]:
    errors = []
    for field in ("base_id", "artifact_repo", "revision", "format"):
        if not isinstance(identity.get(field), str) or not identity[field].strip():
            errors.append(f"identity_{field}_unresolved")
    revision = str(identity.get("revision", "")).strip().lower()
    if revision in {"main", "master", "latest", "head", "default"} or revision.startswith("refs/heads/"):
        errors.append("identity_revision_is_mutable")
    quant = identity.get("quantization")
    if not isinstance(quant, Mapping) or not (type(quant.get("bits")) is int and quant["bits"] > 0 or
                                               isinstance(quant.get("name"), str) and quant["name"].strip()):
        errors.append("identity_quantization_unresolved")
    files = identity.get("files")
    if not isinstance(files, Sequence) or isinstance(files, (str, bytes)) or not files or any(not isinstance(x, str) or not x for x in files):
        errors.append("identity_selected_files_unresolved")
    elif len(set(files)) != len(files):
        errors.append("identity_selected_files_duplicated")
    return errors


def _source_errors(source: Mapping[str, Any], expected_identity: Mapping[str, Any], index: int) -> list[str]:
    errors = []
    sid = str(source.get("source_id", index))
    required = ("source_id", "url", "retrieved_at", "content_digest", "role", "locator", "content")
    for key in required:
        if not source.get(key):
            errors.append(f"source_{sid}_missing_{key}")
    parsed = urlparse(str(source.get("url", "")))
    local_capture = str(source.get("role", "")).lower().startswith("local")
    if (not local_capture and (parsed.scheme != "https" or not parsed.netloc)
            or parsed.username or parsed.password):
        errors.append(f"source_{sid}_must_use_public_https_url")
    stamp = source.get("retrieved_at")
    try:
        datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except ValueError:
        errors.append(f"source_{sid}_invalid_retrieval_time")
    content = source.get("content")
    digest = source.get("content_digest")
    if isinstance(content, str):
        actual = "sha256:" + sha256(content.encode("utf-8")).hexdigest()
        if digest != actual:
            errors.append(f"source_{sid}_digest_mismatch")
    elif content is not None:
        errors.append(f"source_{sid}_invalid_capture")
    if source.get("scope") and not _same_identity(source["scope"], expected_identity):
        errors.append(f"source_{sid}_identity_scope_mismatch")
    return errors


def validate_evidence(evidence: Mapping[str, Any], identity: Mapping[str, Any]) -> dict[str, Any]:
    """Validate exact identity, captured source integrity, provenance and scope.

    This is a structural verifier, not a semantic web crawler: captured material
    and excerpts can be verified byte-for-byte, while whether a human-facing
    source actually entails a claim remains a review/evaluator responsibility.
    """
    source_list = [s for s in evidence.get("sources", []) if isinstance(s, Mapping)]
    sources = {s.get("source_id"): s for s in source_list}
    errors: list[str] = _identity_errors(identity)
    seen_sources: dict[Any, Mapping[str, Any]] = {}
    for i, src in enumerate(evidence.get("sources", [])):
        if not isinstance(src, Mapping):
            continue
        sid = src.get("source_id")
        if sid in seen_sources and dict(seen_sources[sid]) != dict(src):
            errors.append(f"source_{sid}_conflicting_duplicate_id")
        seen_sources[sid] = src
    claims_out = []
    seen_fields: dict[str, Mapping[str, Any]] = {}
    roles = {sid: str(src.get("role", "")).lower() for sid, src in sources.items()}
    compatible_roles = {
        "publisher_requirement": ("publisher", "model_config"),
        "runtime_requirement": ("runtime",),
        "artifact_measured": ("publisher", "artifact", "manifest", "model_config", "huggingface_hub_model_info"),
        "external_tested_configuration": ("external", "deployment", "test"),
        "local_measured": ("local",),
    }
    for i, claim in enumerate(evidence.get("claims", [])):
        if not isinstance(claim, Mapping):
            errors.append(f"claim[{i}]_not_object")
            continue
        field_name = claim.get("field")
        if not isinstance(field_name, str) or not field_name.strip():
            errors.append(f"claim[{i}]_field_missing")
        elif field_name in seen_fields and any(seen_fields[field_name].get(k) != claim.get(k)
                                                for k in ("value", "provenance", "source_ids", "scope")):
            errors.append(f"claim_{field_name}_conflicting_duplicate")
        else:
            seen_fields[field_name] = claim
        provenance = claim.get("provenance")
        if provenance not in PROVENANCE:
            errors.append(f"claim[{i}]_invalid_provenance")
        refs = claim.get("source_ids", [])
        if not isinstance(refs, list):
            errors.append(f"claim[{i}]_source_ids_must_be_list")
            refs = []
        if provenance != "unknown" and (not refs or any(ref not in sources for ref in refs)):
            errors.append(f"claim[{i}]_missing_source")
        if provenance == "unknown" and claim.get("value") is not None:
            errors.append(f"claim[{i}]_unknown_has_value")
        scope = claim.get("scope", {})
        if provenance != "unknown" and (not isinstance(scope, Mapping) or not _same_identity(scope, identity)
                                         or _identity_errors(scope)):
            errors.append(f"claim[{i}]_identity_scope_mismatch")
        if provenance in compatible_roles and refs:
            allowed = compatible_roles[provenance]
            if not any(any(token in roles.get(ref, "") for token in allowed) for ref in refs):
                errors.append(f"claim[{i}]_source_role_incompatible_with_provenance")
        excerpt = claim.get("support_excerpt")
        if provenance != "unknown" and (not isinstance(excerpt, str) or not excerpt.strip()):
            errors.append(f"claim[{i}]_missing_support_excerpt")
        elif isinstance(excerpt, str) and refs and not any(excerpt in str(sources.get(ref, {}).get("content", "")) for ref in refs):
            errors.append(f"claim[{i}]_excerpt_not_in_cited_capture")
        if provenance == "formula_derived" and isinstance(claim.get("value"), Mapping):
            value = claim["value"]
            numeric = any(isinstance(x, (int, float)) and not isinstance(x, bool)
                          for x in value.values())
            if numeric and (not value.get("formula") or not value.get("input_source_ids")):
                errors.append(f"claim[{i}]_formula_inputs_missing")
        for ref in refs:
            src = sources.get(ref)
            if src and not all(src.get(k) for k in ("url", "retrieved_at", "content_digest", "role")):
                errors.append(f"source_{ref}_incomplete")
        claims_out.append(dict(claim))
    for i, src in enumerate(evidence.get("sources", [])):
        if not isinstance(src, Mapping):
            errors.append(f"source[{i}]_not_object")
        else:
            errors.extend(_source_errors(src, identity, i))
    return {"valid": not errors, "errors": sorted(set(errors)), "claims": claims_out,
            "sources": [dict(s) for s in evidence.get("sources", []) if isinstance(s, Mapping)]}


def assess_deployment_evidence(evidence: Mapping[str, Any], identity: Mapping[str, Any],
                               workload: Mapping[str, Any]) -> dict[str, Any]:
    """Distinguish official minimums, absent official specs, and tested profiles.

    An external or local success supports only its exact artifact/runtime/hardware
    profile and workload. It never establishes a proven minimum. Missing official
    numeric requirements always request external deployment research; estimates
    from `estimate_resources` are not accepted as test evidence.
    """
    integrity = validate_evidence(evidence, identity)
    if not integrity["valid"]:
        return {"state": "EVIDENCE_INVALID", "valid": False, "errors": integrity["errors"],
                "minimum_proven": False, "tested_profiles": [], "fit_claim": "unknown"}
    claims = [c for c in integrity["claims"] if isinstance(c, Mapping)]
    official = next((c for c in claims if c.get("field") == "official_hardware_requirements"), None)
    official_value = official.get("value") if official else None
    status = official_value.get("status") if isinstance(official_value, Mapping) else None
    minima_fields = ("minimum_gpu_ram_bytes", "min_gpu_ram_mb")
    has_numeric_minimum = bool(isinstance(official_value, Mapping) and any(
        isinstance(official_value.get(k), (int, float)) and not isinstance(official_value.get(k), bool)
        and isfinite(official_value[k]) and official_value[k] > 0 for k in minima_fields))
    source_map = {s.get("source_id"): s for s in integrity["sources"]}
    absence_verified = False
    if status == "not_published" and official is not None and isinstance(official_value, Mapping):
        sections = official_value.get("searched_sections")
        refs = set(official.get("source_ids", []))
        absence_verified = isinstance(sections, list) and bool(sections)
        if absence_verified:
            for section in sections:
                if not isinstance(section, Mapping):
                    absence_verified = False
                    break
                sid = section.get("source_id")
                src = source_map.get(sid)
                query = str(section.get("query", "")).lower()
                if (sid not in refs or not src or "publisher" not in str(src.get("role", "")).lower()
                        and "model_config" not in str(src.get("role", "")).lower()
                        or section.get("locator") != src.get("locator")
                        or section.get("conclusion") != "no_numeric_hardware_minimum_found"
                        or not any(word in query for word in ("gpu", "hardware", "memory", "minimum"))):
                    absence_verified = False
                    break
    tested = []
    for claim in claims:
        if claim.get("field") != "tested_configuration" or claim.get("provenance") not in {
                "external_tested_configuration", "local_measured"}:
            continue
        value = claim.get("value")
        if not isinstance(value, Mapping) or value.get("inference_succeeded") is not True:
            continue
        if dict(value.get("workload", {})) != dict(workload):
            continue
        if (not isinstance(value.get("gpu_name"), str) or not value["gpu_name"].strip()
                or type(value.get("gpu_ram_bytes")) is not int or value["gpu_ram_bytes"] <= 0
                or not isinstance(value.get("runtime"), str) or not value["runtime"].strip()):
            continue
        required_role = "local" if claim.get("provenance") == "local_measured" else None
        cited = [source_map[sid] for sid in claim.get("source_ids", []) if sid in source_map]
        if not cited or not any((required_role and required_role in str(s.get("role", "")).lower())
                                or (not required_role and any(t in str(s.get("role", "")).lower()
                                                               for t in ("external", "deployment", "test"))) for s in cited):
            continue
        tested.append({**dict(value), "provenance": claim["provenance"],
                       "source_ids": list(claim.get("source_ids", [])), "scope": dict(identity),
                       "supports_only": "this exact tested profile and workload"})
    if status == "not_published" and has_numeric_minimum:
        state = "OFFICIAL_REQUIREMENT_CONFLICT"
        tasks = ["resolve the contradiction between not_published status and supplied numeric GPU requirement"]
    elif status == "not_published" and not absence_verified:
        state = "OFFICIAL_ABSENCE_UNVERIFIED"
        tasks = ["capture exact publisher hardware/config sections searched, search terms, and the source-backed absence result"]
    elif status == "not_published" and not tested:
        state = "EXTERNAL_DEPLOYMENT_RESEARCH_REQUIRED"
        tasks = ["retrieve exact-variant external deployment reports and smaller failed/successful tiers; preserve fit as unknown if none are found"]
    elif status == "published" and not has_numeric_minimum:
        state = "OFFICIAL_REQUIREMENT_INCOMPLETE"
        tasks = ["capture the publisher's numeric minimum and exact applicability, or record that no numeric minimum is published"]
    elif status not in {"published", "not_published"}:
        state = "OFFICIAL_REQUIREMENT_STATUS_UNRESOLVED"
        tasks = ["search exact publisher artifact documentation/configuration and capture the result"]
    elif tested:
        state = "TESTED_PROFILE_ONLY" if not has_numeric_minimum else "SOURCED_REQUIREMENT_AND_TEST"
        tasks = []
    else:
        state = "PUBLISHER_REQUIREMENT_ONLY"
        tasks = ["use only the sourced requirement; do not claim tested fit without a matching deployment result"]
    return {"state": state, "valid": state in {"TESTED_PROFILE_ONLY", "SOURCED_REQUIREMENT_AND_TEST", "PUBLISHER_REQUIREMENT_ONLY"},
            "official_status": status, "official_numeric_minimum_present": has_numeric_minimum,
            "official_absence_verified": absence_verified,
            "tested_profiles": tested, "minimum_proven": False,
            "fit_claim": "exact tested profile only" if tested else "publisher requirement only" if has_numeric_minimum else "unknown",
            "research_tasks": tasks, "errors": [],
            "semantic_review_required": True}


def _known(value: Any, source_ids: Sequence[str], formula: str | None = None,
           assumptions: Sequence[str] = ()) -> dict[str, Any]:
    return {"value": value, "unit": "bytes", "formula": formula,
            "source_ids": list(source_ids), "assumptions": list(assumptions),
            "reason": None if value is not None else "required sourced input or allocation rule missing"}


def _file_bytes(files: Sequence[Mapping[str, Any]]) -> tuple[int | None, list[str]]:
    """Count unique physical files; conflicting sizes/checksums are unresolved."""
    unique: dict[str, Mapping[str, Any]] = {}
    conflict: list[str] = []
    for f in files:
        name = str(f.get("path", ""))
        if not name:
            conflict.append("file path missing")
            continue
        prior = unique.get(name)
        if prior and (prior.get("size_bytes") != f.get("size_bytes") or
                      (prior.get("sha256") and f.get("sha256") and prior.get("sha256") != f.get("sha256"))):
            conflict.append(f"conflicting manifest entry for {name}")
        else:
            unique[name] = f
    if conflict or not unique or any(type(f.get("size_bytes")) is not int or f["size_bytes"] < 0 for f in unique.values()):
        return None, conflict or ["exact selected artifact file sizes are incomplete"]
    return sum(f["size_bytes"] for f in unique.values()), []


def estimate_resources(profile: Mapping[str, Any], files: Sequence[Mapping[str, Any]], *,
                        identity: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Return sourced, componentized estimates without promoting unknowns to zero.

    Tensor weight formula is accepted only with source IDs for the exact resident
    parameter placement and loaded bytes/parameter. MoE active parameters are
    reported but never used as resident weight storage.
    KV formula is limited to explicitly conventional full-attention layouts.
    """
    artifact_bytes, file_errors = _file_bytes(files)
    profile_identity = profile.get("identity", {})
    identity_ok = identity is None or _same_identity(profile_identity, identity)
    source_records: dict[Any, Mapping[str, Any]] = {}
    conflicting_source_ids: set[Any] = set()
    for source in profile.get("sources", []):
        if not isinstance(source, Mapping):
            continue
        sid = source.get("source_id")
        if sid in source_records and dict(source_records[sid]) != dict(source):
            conflicting_source_ids.add(sid)
        source_records[sid] = source
    def scoped(ids: Sequence[str]) -> bool:
        if not ids:
            return False
        for sid in ids:
            src = source_records.get(sid)
            if not src or sid in conflicting_source_ids or _source_errors(src, profile_identity, list(source_records).index(sid)):
                return False
            if src.get("scope") and not _same_identity(src["scope"], profile_identity):
                return False
        return True
    src_default = list(profile.get("source_ids", []))
    arch = profile.get("architecture")
    total = profile.get("total_parameters")
    total_sources = profile.get("total_parameters_source_ids", src_default)
    resident_parameters = profile.get("resident_vram_parameters")
    resident_sources = profile.get("resident_vram_parameters_source_ids", [])
    residency_policy = profile.get("residency_policy")
    if resident_parameters is None and residency_policy == "all_weights_on_device":
        resident_parameters, resident_sources = total, list(total_sources) + list(profile.get("residency_policy_source_ids", []))
    bpp = profile.get("loaded_bytes_per_parameter")
    bpp_sources = profile.get("loaded_bytes_per_parameter_source_ids", src_default)
    resident_ram_parameters = profile.get("resident_ram_parameters")
    resident_ram_sources = profile.get("resident_ram_parameters_source_ids", [])
    weight = None
    weight_formula = None
    weight_sources: list[str] = []
    if identity_ok and type(resident_parameters) is int and resident_parameters >= 0 and type(bpp) in (int, float) and isfinite(bpp) and bpp >= 0 and scoped(resident_sources) and scoped(bpp_sources):
        weight = int(resident_parameters * bpp)
        weight_formula = "resident_vram_parameters * loaded_bytes_per_parameter"
        weight_sources = sorted(set(resident_sources + bpp_sources))
    ram_weight = None
    ram_weight_sources: list[str] = []
    ram_weight_formula = None
    if identity_ok and type(resident_ram_parameters) is int and resident_ram_parameters >= 0 and type(bpp) in (int, float) and isfinite(bpp) and bpp >= 0 and scoped(resident_ram_sources) and scoped(bpp_sources):
        ram_weight = int(resident_ram_parameters * bpp)
        ram_weight_sources = sorted(set(resident_ram_sources + bpp_sources))
        ram_weight_formula = "resident_ram_parameters * loaded_bytes_per_parameter"
    weight_assumption = []
    if arch == "moe":
        weight_assumption.append("MoE residency comes from the sourced placement policy/count; active parameters are not a residency proxy")
    if not identity_ok:
        weight_assumption.append("profile identity does not match requested artifact")
    cache = None
    cache_formula = None
    cache_sources: list[str] = []
    layout = profile.get("cache_layout")
    dims = ("concurrent_sequences", "stored_tokens", "full_attention_layers", "kv_heads", "head_dimension", "bytes_per_cache_element")
    if identity_ok and layout == "conventional_full_attention" and all(type(profile.get(k)) is int and profile[k] >= 0 for k in dims):
        cache = 2
        for key in dims:
            cache *= profile[key]
        cache_formula = "2 * concurrent_sequences * stored_tokens * full_attention_layers * kv_heads * head_dimension * bytes_per_cache_element"
        cache_sources = list(profile.get("cache_source_ids", src_default))
        if not scoped(cache_sources):
            cache = None
    unknown = []
    if profile.get("active_parameters") is None:
        unknown.append({"field": "active_parameters", "reason": "not supplied by a scoped source"})
    for field in ("activation_bytes", "runtime_overhead_bytes", "loading_transient_bytes", "allocator_overhead_bytes", "cache_ram_bytes"):
        if profile.get(field) is None:
            unknown.append({"field": field, "reason": "not supplied by a scoped source or supported formula"})
    for field in ("runtime_image_disk_bytes", "runtime_cache_disk_bytes", "temporary_disk_bytes", "dependency_disk_bytes"):
        if profile.get(field) is None:
            unknown.append({"field": field, "reason": "disk allocation is not sourced; artifact bytes are not total runtime storage"})
    if layout != "conventional_full_attention" and profile.get("cache_vram_bytes") is None:
        unknown.append({"field": "cache_vram_bytes", "reason": "architecture-specific cache allocation rule is unknown"})
    lower = weight + cache if weight is not None and cache is not None else weight if weight is not None else None
    unresolved = bool(unknown) or any(x is None for x in (weight, cache))
    def sourced_value(field: str, source_field: str) -> Any:
        value = profile.get(field)
        return value if scoped(profile.get(source_field, [])) else None
    terms = {
        "artifact_disk_bytes": _known(artifact_bytes if scoped(profile.get("file_manifest_source_ids", [])) else None, list(profile.get("file_manifest_source_ids", [])), "sum(size_bytes for unique selected physical files)"),
        "resident_weight_vram_bytes": _known(weight, weight_sources, weight_formula, weight_assumption),
        "resident_weight_ram_bytes": _known(ram_weight if ram_weight is not None else sourced_value("resident_weight_ram_bytes", "resident_weight_ram_source_ids"), ram_weight_sources or profile.get("resident_weight_ram_source_ids", []), ram_weight_formula),
        "cache_vram_bytes": _known(cache if cache is not None else sourced_value("cache_vram_bytes", "cache_vram_source_ids"), cache_sources or profile.get("cache_vram_source_ids", []), cache_formula),
        "cache_ram_bytes": _known(sourced_value("cache_ram_bytes", "cache_ram_source_ids"), profile.get("cache_ram_source_ids", [])),
        "activation_bytes": _known(sourced_value("activation_bytes", "activation_source_ids"), profile.get("activation_source_ids", [])),
        "runtime_overhead_bytes": _known(sourced_value("runtime_overhead_bytes", "runtime_overhead_source_ids"), profile.get("runtime_overhead_source_ids", [])),
        "loading_transient_bytes": _known(sourced_value("loading_transient_bytes", "loading_transient_source_ids"), profile.get("loading_transient_source_ids", [])),
        "allocator_overhead_bytes": _known(sourced_value("allocator_overhead_bytes", "allocator_overhead_source_ids"), profile.get("allocator_overhead_source_ids", [])),
        "runtime_image_disk_bytes": _known(sourced_value("runtime_image_disk_bytes", "runtime_image_disk_source_ids"), profile.get("runtime_image_disk_source_ids", [])),
        "runtime_cache_disk_bytes": _known(sourced_value("runtime_cache_disk_bytes", "runtime_cache_disk_source_ids"), profile.get("runtime_cache_disk_source_ids", [])),
        "temporary_disk_bytes": _known(sourced_value("temporary_disk_bytes", "temporary_disk_source_ids"), profile.get("temporary_disk_source_ids", [])),
        "dependency_disk_bytes": _known(sourced_value("dependency_disk_bytes", "dependency_disk_source_ids"), profile.get("dependency_disk_source_ids", [])),
    }
    if any(terms[k]["value"] is None for k in ("resident_weight_vram_bytes", "cache_vram_bytes", "activation_bytes", "runtime_overhead_bytes", "loading_transient_bytes", "allocator_overhead_bytes")):
        unresolved = True
    term_scope = {"identity": dict(profile_identity), "runtime": profile.get("runtime"),
                  "runtime_version": profile.get("runtime_version"), "workload": profile.get("workload")}
    for term in terms.values():
        term["scope"] = term_scope
    all_unknown = list(unknown)
    known_unknown_fields = {item["field"] for item in all_unknown}
    for field, term in terms.items():
        if term["value"] is None and field not in known_unknown_fields:
            all_unknown.append({"field": field, "reason": term["reason"]})
            known_unknown_fields.add(field)
    all_unknown.extend({"field": "artifact_files", "reason": e} for e in file_errors)
    return {"identity": dict(profile_identity), "identity_matches": identity_ok,
            "architecture": arch, "total_parameters": total, "active_parameters": profile.get("active_parameters"),
            "resident_vram_parameters": resident_parameters, "resident_ram_parameters": profile.get("resident_ram_parameters"),
            "residency_policy": residency_policy,
            "parameter_source_ids": {"total": total_sources, "active": profile.get("active_parameters_source_ids", []),
                                     "resident_vram": resident_sources, "resident_ram": resident_ram_sources},
            "quantization": profile.get("quantization"), "terms": terms,
            "known_lower_bound_bytes": lower, "unknowns": all_unknown,
            "fit_state": "unknown" if unresolved or not identity_ok else "estimate_only",
            "is_publisher_requirement": False, "artifact_file_errors": file_errors}
