"""JSON-first CLI for the same router used by adopters' Python integrations."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
from typing import Any

from .market import search_offers
from .provider import VastOffersClient
from .router import route_request


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


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="vast-broker",
        description="Research-first routing and read-only Vast marketplace tools")
    commands = parser.add_subparsers(dest="command", required=True)
    route = commands.add_parser("route", help="advance one request through evidence, quote and cap gates")
    route.add_argument("--request", required=True, help="JSON model/workload request")
    route.add_argument("--evidence", help="JSON map from candidate key to captured evidence")
    route.add_argument("--market", help="fresh normalized market JSON (never a launch-time cache)")
    route.add_argument("--limits", help="explicit owner spend and lifecycle limits JSON")
    route.add_argument("--output", help="write the structured route result to this path")

    offers = commands.add_parser("offers", help="perform a read-only live marketplace search")
    offers.add_argument("--filters", help="optional JSON object of documented offer constraints")
    offers.add_argument("--disk-gb", type=float, help="disk allocation used for price statistics")
    offers.add_argument("--page-size", type=int, default=100)
    offers.add_argument("--max-pages", type=int, default=20)
    offers.add_argument("--output", help="write normalized offers to this path")
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
        if args.command == "offers":
            if not os.environ.get("VAST_API_KEY"):
                raise ValueError("VAST_API_KEY must be supplied by the configured secret store")
            filters = _read_json(args.filters) if args.filters else {}
            if not isinstance(filters, dict):
                raise ValueError("filters file must contain a JSON object")
            result = search_offers(filters, client=VastOffersClient(), page_size=args.page_size,
                                   max_pages=args.max_pages, disk_gb=args.disk_gb)
            _write_json(result, args.output)
            return 0
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
