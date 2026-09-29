"""Model artifact research and confirmation routing (standard library only).

Public API: ``resolve_request(request: dict, *, fetcher=None) -> dict``. A fetcher
is a read-only callable accepting a query mapping and returning ``{"records":
[Hub record mappings...]}``; it never receives executable card content. The
module consumes normalized Hugging Face Hub record shapes and emits a
JSON-serializable candidate manifest and next route. Records are only candidates
when repository identity, file inventory and base/artifact relationships are
structurally present. It does not infer compatibility from parameter count.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from hashlib import sha256
import json
import re
from typing import Any
from urllib.parse import urlparse


def _norm_repo(value: str | None) -> str | None:
    if not value:
        return None
    value = value.strip()
    parsed = urlparse(value)
    if parsed.scheme and parsed.netloc:
        parts = [p for p in parsed.path.split("/") if p]
        if len(parts) >= 2 and parts[0] == "models":
            return "/".join(parts[1:3])
        if parsed.netloc.lower() in {"huggingface.co", "www.huggingface.co"} and len(parts) >= 2:
            return "/".join(parts[:2])
        return None
    value = value.removeprefix("hf://").strip("/")
    return value if re.fullmatch(r"[^/\s]+/[^/\s]+", value) else None


def _files(record: Mapping[str, Any]) -> list[dict[str, Any]]:
    out = []
    for f in record.get("siblings", record.get("files", [])) or []:
        if not isinstance(f, Mapping):
            continue
        path = f.get("rfilename", f.get("path", f.get("name")))
        if not path:
            continue
        size = f.get("size_bytes", f.get("size"))
        out.append({"path": str(path), "size_bytes": size if type(size) is int and size >= 0 else None,
                    "sha256": f.get("sha256") or f.get("lfs", {}).get("oid") if isinstance(f.get("lfs", {}), Mapping) else f.get("sha256")})
    return sorted(out, key=lambda f: f["path"])


def _card(record: Mapping[str, Any]) -> dict[str, Any]:
    card = record.get("cardData", record.get("card_data", {}))
    return dict(card) if isinstance(card, Mapping) else {}


def _model_config(record: Mapping[str, Any]) -> dict[str, Any]:
    config = record.get("config", {})
    return dict(config) if isinstance(config, Mapping) else {}


def _explicit_base_ids(record: Mapping[str, Any]) -> set[str]:
    card = _card(record)
    found = set()
    for key in ("base_model", "base_model_id", "base_id", "base_models"):
        values = card.get(key, record.get(key))
        if isinstance(values, str): values = [values]
        if isinstance(values, Sequence):
            found.update(x for x in (_norm_repo(str(v)) for v in values) if x)
    return found


def _quantization(record: Mapping[str, Any]) -> tuple[str | None, int | None]:
    card = _card(record)
    config = _model_config(record)
    q = card.get("quantization_config", record.get("quantization_config", config.get("quantization_config", {})))
    q = q if isinstance(q, Mapping) else {}
    fmt = card.get("format", record.get("format"))
    bits = card.get("bits", card.get("bit_width", q.get("bits", q.get("w_bit", record.get("bits")))))
    if not fmt:
        tags = [str(x).lower() for x in record.get("tags", [])]
        if any(str(f["path"]).lower().endswith(".gguf") for f in _files(record)) or "gguf" in tags:
            fmt = "gguf"
        elif q or bits is not None:
            fmt = "transformers"
    if bits is None:
        # GGUF's standardized quant type is encoded in the filename. This is
        # explicit artifact metadata, not inference from model size/count.
        for f in _files(record):
            match = re.search(r"(?:^|[-_.])Q([1-8])(?:_|[-.])", str(f["path"]), re.IGNORECASE)
            if match:
                bits = int(match.group(1))
                break
    try:
        bits = int(bits) if bits is not None else None
    except (ValueError, TypeError):
        bits = None
    return (str(fmt).lower() if fmt else None), bits


def _is_structural_base(record: Mapping[str, Any]) -> bool:
    card = _card(record)
    config = _model_config(record)
    # Only publisher/base indicators from Hub metadata count. Names that merely
    # contain "base" are not enough to turn community weights into base models.
    return bool(card.get("model_type") or card.get("architectures") or config.get("model_type")
                or config.get("architectures") or record.get("model_type")) and not _explicit_base_ids(record)


def _manifest(record: Mapping[str, Any], base_id: str | None = None) -> dict[str, Any] | None:
    repo = _norm_repo(str(record.get("id", record.get("repo_id", record.get("url", "")))))
    if not repo:
        return None
    revision = record.get("sha", record.get("revision", record.get("commit")))
    files = _files(record)
    if not revision or not files:
        return None
    fmt, bits = _quantization(record)
    card = _card(record)
    config = _model_config(record)
    explicit_bases = _explicit_base_ids(record)
    role = "base" if _is_structural_base(record) else "artifact" if explicit_bases else "unresolved"
    if base_id and role == "artifact" and base_id not in explicit_bases:
        return None
    identity = {"base_id": base_id or (repo if role == "base" else next(iter(sorted(explicit_bases)), None)),
                "artifact_repo": repo, "revision": str(revision), "format": fmt,
                "quantization": {"bits": bits, "name": card.get("quantization") or card.get("quantization_method") or (card.get("quantization_config", {}).get("quant_method") if isinstance(card.get("quantization_config", {}), Mapping) else None) or (config.get("quantization_config", {}).get("quant_method") if isinstance(config.get("quantization_config", {}), Mapping) else None)},
                "files": [f["path"] for f in files]}
    return {"identity": identity, "repo_id": repo, "revision": str(revision), "role": role,
            "base_ids": sorted(explicit_bases), "files": files,
            "artifact_disk_bytes": sum(f["size_bytes"] for f in files) if all(f["size_bytes"] is not None for f in files) else None,
            "architecture": card.get("model_type") or config.get("model_type"), "architectures": card.get("architectures", config.get("architectures", [])),
            "pipeline_tag": record.get("pipeline_tag"), "license": card.get("license"),
            "gated": record.get("gated"), "private": record.get("private"),
            "quantization": identity["quantization"], "readme_text": None,
            "requirements": [], "quality_runtime_evidence": [], "offer_statistics": {},
            "source_captures": [{**dict(src), "scope": identity} for src in record.get("captures", [])
                                if isinstance(src, Mapping) and src.get("available")]
                               + ([{**dict(record["metadata_source"]), "scope": identity}]
                                  if isinstance(record.get("metadata_source"), Mapping) else []),
            "manifest_digest": sha256(json.dumps({"identity": identity, "files": files}, sort_keys=True).encode()).hexdigest()}


def _select_files(candidate: Mapping[str, Any], requested: Sequence[str]) -> dict[str, Any] | None:
    wanted = sorted(set(str(p) for p in requested))
    by_path = {f["path"]: f for f in candidate["files"]}
    if not wanted or any(path not in by_path for path in wanted):
        return None
    selected = [by_path[path] for path in wanted]
    result = dict(candidate)
    result["identity"] = {**candidate["identity"], "files": wanted}
    result["selected_files"] = selected
    result["artifact_disk_bytes"] = sum(f["size_bytes"] for f in selected) if all(f["size_bytes"] is not None for f in selected) else None
    result["manifest_digest"] = sha256(json.dumps({"identity": result["identity"], "files": selected}, sort_keys=True).encode()).hexdigest()
    return result


def _scope_allows(c: Mapping[str, Any], scope: Mapping[str, Any]) -> bool:
    ident = c["identity"]
    allowed_bases = scope.get("base_ids")
    if allowed_bases and ident.get("base_id") not in allowed_bases: return False
    bits = ident.get("quantization", {}).get("bits")
    if scope.get("quantization_bits") is not None and bits != scope["quantization_bits"]: return False
    if scope.get("formats") and ident.get("format") not in scope["formats"]: return False
    if scope.get("providers") and ident.get("artifact_repo", "").split("/")[0] not in scope["providers"]: return False
    if scope.get("publisher_only") and ident.get("artifact_repo") != ident.get("base_id"): return False
    return True


def _auth_valid(request: Mapping[str, Any], scope: Mapping[str, Any]) -> bool:
    auth = request.get("selection_authorization", {})
    if not isinstance(auth, Mapping) or auth.get("mode") != "scoped_auto_select": return False
    declared = auth.get("scope")
    provenance = auth.get("provenance")
    return (isinstance(declared, Mapping) and dict(declared) == dict(scope)
            and isinstance(provenance, Mapping) and bool(provenance.get("type") and provenance.get("value")))


def _route(mode: str, candidates: list[dict[str, Any]], auth: bool,
           retrieval_failed: bool = False, exact_missing: bool = False) -> dict[str, Any]:
    if retrieval_failed:
        state, reason = "RESEARCH_REQUIRED", "SOURCE_RETRIEVAL_FAILED"
        tasks = ["retry_hub_retrieval"]
    elif exact_missing:
        state, reason = "RESEARCH_REQUIRED", "ARTIFACT_NOT_FOUND"
        tasks = ["resolve_requested_artifact_files_and_revision"]
    elif not candidates:
        state, reason = "RESEARCH_REQUIRED", "NO_CANDIDATE_IN_SCOPE"
        tasks = ["discover_candidates_within_scope"]
    elif mode in {"family_discovery", "base_identified_artifact_open", "scoped_auto_select"} and not auth:
        state, reason = "MODEL_CONFIRMATION_REQUIRED", "CANDIDATE_SELECTION_REQUIRED"
        tasks = ["compare_exact_candidates_requirements_quality_runtime_and_live_offers", "ask_user_to_select_exact_artifact"]
    elif mode == "scoped_auto_select" and auth:
        state, reason = "EVIDENCE_REQUIRED", None
        tasks = ["validate_candidate_scoped_evidence_and_cost_limits"]
    else:
        state, reason = "EVIDENCE_REQUIRED", None
        tasks = ["resolve_revision_manifest_dependencies_runtime_and_evidence"]
    return {"state": state, "next_action": tasks[0], "research_tasks": tasks,
            "reason_codes": [reason] if reason else [], "resolution_mode": mode,
            "candidates": candidates, "candidate_selection_required": state == "MODEL_CONFIRMATION_REQUIRED",
            "explore_alternatives_required": bool(exact_missing or (not retrieval_failed and not candidates and mode != "exact_artifact")),
            "question_reason": reason if state == "MODEL_CONFIRMATION_REQUIRED" else None,
            "paid_action_allowed": False}


def resolve_request(request: Mapping[str, Any], *, fetcher: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None) -> dict[str, Any]:
    """Resolve candidate artifacts and return a gated model/evidence route.

    ``request`` may contain ``model_query``, ``base_id``, ``exact_repo`` plus
    ``artifact_files``, ``selection_scope``, ``publisher_only`` and
    ``selection_authorization``. Optional ``records`` enables deterministic
    tests/offline use. Fetcher exceptions are retry routes, never artifact
    absence. The result carries separate resource evidence input and offer stats
    placeholders per candidate; routing itself never selects a candidate unless
    explicit valid scoped authorization is supplied.
    """
    exact_repo = _norm_repo(request.get("exact_repo") or request.get("model_url"))
    base = _norm_repo(request.get("base_id"))
    mode = request.get("resolution_mode")
    if not mode:
        mode = "exact_artifact" if exact_repo and request.get("artifact_files") else "base_identified_artifact_open" if base else "family_discovery"
    if mode not in {"family_discovery", "base_identified_artifact_open", "exact_artifact", "scoped_auto_select"}:
        return _route("family_discovery", [], False, retrieval_failed=True)
    scope = dict(request.get("selection_scope", {}))
    if request.get("publisher_only"):
        scope["publisher_only"] = True
    query = {"kind": "exact_repository" if exact_repo else "base_family" if base else "family",
             "repository": exact_repo, "base_id": base, "query": request.get("model_query"),
             "include_community_artifacts": not scope.get("publisher_only", False)}
    retrieval_info: Mapping[str, Any] = {}
    try:
        if "records" in request:
            records = request["records"]
        else:
            if fetcher is None:
                from .huggingface import HuggingFaceHubClient
                fetcher = HuggingFaceHubClient()
            result = fetcher(query)
            if not isinstance(result, Mapping) or result.get("error"):
                return _route(mode, [], _auth_valid(request, scope), retrieval_failed=True)
            records = result.get("records", [])
            retrieval_info = result.get("retrieval", {}) if isinstance(result.get("retrieval", {}), Mapping) else {}
    except Exception:
        return _route(mode, [], _auth_valid(request, scope), retrieval_failed=True)
    if not isinstance(records, Sequence) or isinstance(records, (str, bytes)):
        return _route(mode, [], _auth_valid(request, scope), retrieval_failed=True)
    candidates: list[dict[str, Any]] = []
    if exact_repo:
        found = [r for r in records if isinstance(r, Mapping) and _norm_repo(str(r.get("id", r.get("repo_id", r.get("url", ""))))) == exact_repo]
        for r in found:
            m = _manifest(r, base)
            if m:
                selected = _select_files(m, request.get("artifact_files", [])) if request.get("artifact_files") else m
                if selected: candidates.append(selected)
        routed = _route("exact_artifact", candidates, True, exact_missing=not candidates)
        if retrieval_info: routed["retrieval"] = dict(retrieval_info)
        return routed
    # Resolve base records strictly through repository identity and Hub structural
    # metadata, then include community artifacts only when their card explicitly
    # names that base. A model-card README or shell snippet is never executed.
    base_records = [r for r in records if isinstance(r, Mapping) and
                    (not base or _norm_repo(str(r.get("id", r.get("repo_id", "")))) == base) and _is_structural_base(r)]
    resolved_bases = {_norm_repo(str(r.get("id", r.get("repo_id", "")))) for r in base_records}
    if base and not resolved_bases:
        # The specified repository may expose a sparse model card, but it can
        # still be represented as a base only when explicitly identified as such.
        resolved_bases = set()
    for r in base_records:
        m = _manifest(r, _norm_repo(str(r.get("id", r.get("repo_id", "")))))
        if m: candidates.append(m)
    for r in records:
        if not isinstance(r, Mapping): continue
        linked = _explicit_base_ids(r)
        for candidate_base in sorted(resolved_bases):
            if candidate_base in linked:
                m = _manifest(r, candidate_base)
                if m: candidates.append(m)
    candidates = [c for c in candidates if _scope_allows(c, scope)]
    by_identity = {json.dumps(c["identity"], sort_keys=True): c for c in candidates}
    candidates = sorted(by_identity.values(), key=lambda c: (c["identity"].get("base_id") or "", c["repo_id"], c["revision"]))
    valid_auth = mode == "scoped_auto_select" and _auth_valid(request, scope)
    result = _route(mode, candidates, valid_auth)
    if valid_auth and candidates:
        # Candidate ranking is intentionally deferred to evidence and market
        # planning. Record permitted scope and avoid the model-question gate.
        result["selection_scope"] = scope
        result["selection_authorization_provenance"] = dict(request["selection_authorization"]["provenance"])
    if retrieval_info:
        result["retrieval"] = dict(retrieval_info)
    return result
