#!/usr/bin/env python3
"""Fetch and atomically save a sanitized, timestamped Vast offer snapshot."""

from __future__ import annotations

import json
import os
import sys
import tempfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


API_URL = "https://console.vast.ai/api/v0/bundles"
OFFER_TYPES = ("ondemand", "bid", "reserved")
VERIFICATION_STATES = ("verified", "deverified", "unverified", "unknown")
OUTPUT = Path(__file__).resolve().parents[1] / "data" / "latest.json"
SENSITIVE_KEY_PARTS = ("apikey", "authorization", "credential", "password", "privatekey", "secret", "token")


def fetch(offer_type: str, limit: int, token: str) -> list[dict[str, Any]]:
    """Fetch one documented rental type without availability/verification filters."""
    body = {"limit": limit, "type": offer_type}
    request = Request(
        API_URL,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urlopen(request, timeout=45) as response:
        payload = json.loads(response.read().decode("utf-8"))

    offers = payload.get("offers", []) if isinstance(payload, dict) else payload
    if isinstance(offers, dict):
        offers = [offers]
    if not isinstance(offers, list) or not all(isinstance(item, dict) for item in offers):
        raise ValueError(f"Unexpected Vast response for offer type {offer_type!r}")
    return offers


def _clean_string(value: str, token: str | None) -> str:
    if token:
        value = value.replace(token, "[REDACTED]")
    return value


def sanitize_offer(value: Any, *, token: str | None = None) -> Any:
    """Remove credential-shaped fields and any exact API-key occurrence."""
    if isinstance(value, dict):
        clean: dict[str, Any] = {}
        for key, item in value.items():
            normalized_key = "".join(character for character in str(key).casefold() if character.isalnum())
            if any(part in normalized_key for part in SENSITIVE_KEY_PARTS):
                continue
            clean[str(key)] = sanitize_offer(item, token=token)
        return clean
    if isinstance(value, list):
        return [sanitize_offer(item, token=token) for item in value]
    if isinstance(value, str):
        return _clean_string(value, token)
    return value


def build_snapshot(
    results: dict[str, list[dict[str, Any]]],
    *,
    limit: int,
    captured_at: str | None = None,
    token: str | None = None,
) -> dict[str, Any]:
    """Create a deterministic, sanitized payload except for its capture time."""
    safe_results = {
        offer_type: [sanitize_offer(offer, token=token) for offer in offers]
        for offer_type, offers in results.items()
    }
    verification_counts: dict[str, int] = {state: 0 for state in VERIFICATION_STATES}
    verification_by_type: dict[str, dict[str, int]] = {}
    for offer_type, offers in safe_results.items():
        counts = Counter(
            offer.get("verification", "unknown")
            if offer.get("verification") in VERIFICATION_STATES[:-1]
            else "unknown"
            for offer in offers
        )
        verification_by_type[offer_type] = {state: counts.get(state, 0) for state in VERIFICATION_STATES}
        for state in VERIFICATION_STATES:
            verification_counts[state] += counts.get(state, 0)

    return {
        "schema_version": "1.0",
        "captured_at_utc": captured_at or datetime.now(timezone.utc).isoformat(),
        "source": API_URL,
        "filters": {"verification": "not filtered", "rentable": "not filtered", "rented": "not filtered"},
        "pricing_interpretation": {
            "ondemand": "direct on-demand hourly offer quote",
            "bid": "interruptible bid quote; does not guarantee startup",
            "reserved": "comparison-only reserved conversion quote; requires a separate prepaid commitment after an on-demand create",
        },
        "limit_per_type": limit,
        "possibly_truncated_types": [
            offer_type for offer_type, offers in safe_results.items() if len(offers) >= limit
        ],
        "counts": {offer_type: len(offers) for offer_type, offers in safe_results.items()},
        "verification_counts": verification_counts,
        "verification_counts_by_type": verification_by_type,
        "offers": safe_results,
    }


def write_snapshot(snapshot: dict[str, Any], output: Path = OUTPUT) -> None:
    """Atomically replace output only after the full snapshot is serialized."""
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=output.parent, delete=False, suffix=".tmp"
        ) as temporary:
            temporary_path = Path(temporary.name)
            json.dump(snapshot, temporary, ensure_ascii=False, separators=(",", ":"))
            temporary.write("\n")
        temporary_path.replace(output)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()


def main() -> int:
    token = os.environ.get("VAST_API_KEY", "").strip()
    if not token:
        print("Set VAST_API_KEY in the process environment.", file=sys.stderr)
        return 2

    try:
        limit = int(os.environ.get("VAST_OFFER_LIMIT", "1000"))
    except ValueError:
        print("VAST_OFFER_LIMIT must be an integer.", file=sys.stderr)
        return 2
    if not 1 <= limit <= 10000:
        print("VAST_OFFER_LIMIT must be between 1 and 10000.", file=sys.stderr)
        return 2

    results: dict[str, list[dict[str, Any]]] = {}
    try:
        for offer_type in OFFER_TYPES:
            results[offer_type] = fetch(offer_type, limit, token)
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError, ValueError, OSError) as error:
        # Never print provider response bodies, request headers, or exception text.
        print(
            f"Offer refresh failed ({type(error).__name__}); existing snapshot left unchanged.",
            file=sys.stderr,
        )
        return 1

    snapshot = build_snapshot(results, limit=limit, token=token)
    write_snapshot(snapshot)
    print(f"Saved {sum(snapshot['counts'].values())} offers to {OUTPUT}")
    print(f"Captured at {snapshot['captured_at_utc']}")
    if snapshot["possibly_truncated_types"]:
        print(
            "Warning: one or more result types reached the response limit; "
            "treat the snapshot as possibly incomplete.",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
