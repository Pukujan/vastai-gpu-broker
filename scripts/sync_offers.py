#!/usr/bin/env python3
"""Fetch a timestamped, read-only snapshot of current Vast.ai offers."""

from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


API_URL = "https://console.vast.ai/api/v0/bundles"
OFFER_TYPES = ("ondemand", "bid", "reserved")
OUTPUT = Path(__file__).resolve().parents[1] / "data" / "latest.json"


def fetch(offer_type: str, limit: int, token: str) -> list[dict]:
    body = {
        "limit": limit,
        "type": offer_type,
        "rentable": {"eq": True},
        "rented": {"eq": False},
    }
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

    results: dict[str, list[dict]] = {}
    try:
        for offer_type in OFFER_TYPES:
            results[offer_type] = fetch(offer_type, limit, token)
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError, ValueError) as error:
        # Keep the last known-good cache intact if any query fails.
        print(f"Offer refresh failed; existing snapshot left unchanged: {error}", file=sys.stderr)
        return 1

    snapshot = {
        "captured_at_utc": datetime.now(timezone.utc).isoformat(),
        "source": API_URL,
        "filter": {
            "rentable": {"eq": True},
            "rented": {"eq": False},
            "verification": "not filtered",
        },
        "limit_per_type": limit,
        "possibly_truncated_types": [
            offer_type for offer_type, offers in results.items() if len(offers) >= limit
        ],
        "counts": {offer_type: len(offers) for offer_type, offers in results.items()},
        "offers": results,
    }

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=OUTPUT.parent, delete=False, suffix=".tmp"
        ) as temporary:
            temporary_path = Path(temporary.name)
            json.dump(snapshot, temporary, ensure_ascii=False, separators=(",", ":"))
            temporary.write("\n")
        temporary_path.replace(OUTPUT)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()

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
