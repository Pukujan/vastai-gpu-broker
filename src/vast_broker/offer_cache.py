"""SQLite cache for read-only offer comparisons.

Cached quotes are convenience data, never fresh authorization evidence. Callers
must request a live refresh for paid planning and must not pass cache hits to the
paid authorization path.
"""
from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import math
from pathlib import Path
import sqlite3
import threading
from typing import Any

from .market import SEARCH_RENTAL_TYPES


DEFAULT_TTL_SECONDS = 600
MAX_TTL_SECONDS = 900
_CACHE_SCHEMA = "offer-cache-v2"


def _utc_now(clock: Callable[[], Any]) -> datetime:
    value = clock()
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("cache clock must return an aware datetime")
        return value.astimezone(timezone.utc)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("cache clock must return a finite epoch or aware datetime")
    try:
        if not math.isfinite(value):
            raise ValueError
        return datetime.fromtimestamp(value, timezone.utc)
    except (OverflowError, OSError, ValueError):
        raise ValueError("cache clock must return a finite, supported epoch") from None


def _stamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _parse_stamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("cache timestamp must include a timezone")
    return parsed.astimezone(timezone.utc)


def _json_copy(value: Any) -> Any:
    """Copy JSON-compatible input without stringifying unknown objects."""
    return json.loads(json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False))


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False)


class OfferCache:
    """Cache injected live-search results in a local SQLite database.

    The cache key is a SHA-256 digest of the normalized filters, requested disk,
    rental types, page size and page limit. Filter values are never written into
    the database or logged. An injected search exception is propagated and is
    never cached.

    Args:
        database: SQLite path, or ``":memory:"`` for a process-local cache.
        live_search: Function with the same keyword arguments as
            ``market.search_offers``.
        ttl_seconds: Freshness interval from 1 through 900 seconds. Defaults to
            600 seconds.
        clock: Test seam returning an aware datetime or finite Unix timestamp.
    """

    def __init__(self, database: str | Path, live_search: Callable[..., Mapping[str, Any]], *,
                 ttl_seconds: int | float = DEFAULT_TTL_SECONDS,
                 clock: Callable[[], Any] = lambda: datetime.now(timezone.utc)):
        if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, (int, float)):
            raise ValueError("ttl_seconds must be a finite value from 1 through 900")
        try:
            ttl_is_valid = math.isfinite(ttl_seconds) and 1 <= ttl_seconds <= MAX_TTL_SECONDS
        except OverflowError:
            ttl_is_valid = False
        if not ttl_is_valid:
            raise ValueError("ttl_seconds must be a finite value from 1 through 900")
        if not callable(live_search):
            raise ValueError("live_search must be callable")
        if not callable(clock):
            raise ValueError("clock must be callable")
        self.ttl_seconds = float(ttl_seconds)
        self.live_search = live_search
        self.clock = clock
        self._lock = threading.RLock()
        path = str(database)
        if path != ":memory:":
            Path(path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(path, timeout=30, check_same_thread=False)
        with self._connection:
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute(
                """CREATE TABLE IF NOT EXISTS offer_cache (
                    cache_key TEXT PRIMARY KEY,
                    result_json TEXT NOT NULL,
                    captured_at_utc TEXT NOT NULL,
                    fetched_at_utc TEXT NOT NULL,
                    expires_at_utc TEXT NOT NULL
                )"""
            )

    def close(self) -> None:
        """Close the local SQLite connection."""
        with self._lock:
            self._connection.close()

    def __enter__(self) -> "OfferCache":
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.close()

    def search_offers(self, filters: Mapping[str, Any] | None = None, *,
                      disk_gb: int | float | None = None,
                      rental_types: Iterable[str] = SEARCH_RENTAL_TYPES,
                      page_size: int = 100, max_pages: int = 20,
                      force_refresh: bool = False) -> dict[str, Any]:
        """Return a fresh cached result or call the injected live search.

        Set ``force_refresh=True`` to bypass an otherwise-fresh cache entry.
        A failed forced refresh raises its original exception; stale or old cache
        contents are never returned as a fallback.
        """
        if filters is None:
            filters = {}
        if not isinstance(filters, Mapping):
            raise ValueError("filters must be a mapping")
        try:
            normalized_filters = _json_copy(dict(filters))
        except (TypeError, ValueError, OverflowError):
            raise ValueError("filters must contain only finite JSON-compatible values") from None
        if not isinstance(normalized_filters, dict) or any(not isinstance(k, str) for k in normalized_filters):
            raise ValueError("filters must be a JSON object with string keys")
        if disk_gb is not None:
            try:
                disk_is_valid = (not isinstance(disk_gb, bool) and isinstance(disk_gb, (int, float))
                                 and math.isfinite(disk_gb) and disk_gb >= 0)
            except OverflowError:
                disk_is_valid = False
            if not disk_is_valid:
                raise ValueError("disk_gb must be a finite non-negative number or null")
            try:
                normalized_disk: float | None = float(disk_gb)
            except OverflowError:
                raise ValueError("disk_gb must be representable as a finite number") from None
        else:
            normalized_disk = None
        if isinstance(rental_types, (str, bytes)):
            raise ValueError("rental_types must be an iterable of supported rental type names")
        try:
            normalized_types = tuple(dict.fromkeys(rental_types))
        except TypeError:
            raise ValueError("rental_types must be an iterable of supported rental type names") from None
        if any(not isinstance(kind, str) or kind not in SEARCH_RENTAL_TYPES for kind in normalized_types):
            raise ValueError("rental_types contains an unsupported rental type")
        if isinstance(page_size, bool) or not isinstance(page_size, int) or page_size < 1:
            raise ValueError("page_size must be a positive integer")
        if isinstance(max_pages, bool) or not isinstance(max_pages, int) or max_pages < 1:
            raise ValueError("max_pages must be a positive integer")
        if not isinstance(force_refresh, bool):
            raise ValueError("force_refresh must be a boolean")

        query = {
            "schema": _CACHE_SCHEMA,
            "filters": normalized_filters,
            "disk_gb": normalized_disk,
            "rental_types": list(normalized_types),
            "page_size": page_size,
            "max_pages": max_pages,
        }
        # Only this digest is persisted. Do not include query or filters in errors/logs.
        cache_key = sha256(_canonical(query).encode("utf-8")).hexdigest()
        now = _utc_now(self.clock)
        if not force_refresh:
            cached = self._read_fresh(cache_key, now)
            if cached is not None:
                return cached

        # Exceptions and malformed return values are deliberately not written.
        result = self.live_search(
            deepcopy(normalized_filters), disk_gb=normalized_disk,
            rental_types=normalized_types, page_size=page_size, max_pages=max_pages,
        )
        if not isinstance(result, Mapping):
            raise ValueError("live search returned a malformed result; no cache entry was written")
        try:
            result_copy = _json_copy(dict(result))
        except (TypeError, ValueError, OverflowError):
            raise ValueError("live search returned a non-JSON result; no cache entry was written") from None

        fetched = _utc_now(self.clock)
        captured = fetched
        source_capture = result_copy.get("as_of_utc")
        if isinstance(source_capture, str):
            try:
                captured = _parse_stamp(source_capture)
            except ValueError:
                # Keep the original result as supplied; cache metadata has a
                # trustworthy local capture time even for malformed source time.
                pass
        expires = fetched + timedelta(seconds=self.ttl_seconds)
        completeness = result_copy.get("completeness")
        truncated = bool(isinstance(completeness, Mapping) and completeness.get("truncated") is True)
        partial = bool(isinstance(completeness, Mapping) and completeness.get("complete") is False) or truncated
        result_json = _canonical(result_copy)
        with self._lock, self._connection:
            self._connection.execute(
                """INSERT INTO offer_cache
                   (cache_key, result_json, captured_at_utc, fetched_at_utc, expires_at_utc)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(cache_key) DO UPDATE SET
                     result_json=excluded.result_json,
                     captured_at_utc=excluded.captured_at_utc,
                     fetched_at_utc=excluded.fetched_at_utc,
                     expires_at_utc=excluded.expires_at_utc""",
                (cache_key, result_json, _stamp(captured), _stamp(fetched), _stamp(expires)),
            )
        return self._attach_metadata(result_copy, cache_hit=False, captured=captured,
                                     fetched=fetched, expires=expires,
                                     partial=partial, truncated=truncated)

    # Short spelling for callers treating the cache as a search adapter.
    search = search_offers

    def _read_fresh(self, cache_key: str, now: datetime) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT result_json, captured_at_utc, fetched_at_utc, expires_at_utc "
                "FROM offer_cache WHERE cache_key = ?", (cache_key,),
            ).fetchone()
            if row is None:
                return None
            try:
                expires = _parse_stamp(row[3])
                captured = _parse_stamp(row[1])
                fetched = _parse_stamp(row[2])
            except (TypeError, ValueError):
                self._delete(cache_key)
                return None
            if now >= expires:
                self._delete(cache_key)
                return None
            try:
                result = json.loads(row[0])
            except (TypeError, json.JSONDecodeError):
                self._delete(cache_key)
                return None
        if not isinstance(result, dict):
            self._delete(cache_key)
            return None
        completeness = result.get("completeness")
        truncated = bool(isinstance(completeness, Mapping) and completeness.get("truncated") is True)
        partial = bool(isinstance(completeness, Mapping) and completeness.get("complete") is False) or truncated
        return self._attach_metadata(result, cache_hit=True, captured=captured,
                                     fetched=fetched, expires=expires,
                                     partial=partial, truncated=truncated)

    def _delete(self, cache_key: str) -> None:
        with self._lock, self._connection:
            self._connection.execute("DELETE FROM offer_cache WHERE cache_key = ?", (cache_key,))

    @staticmethod
    def _attach_metadata(result: dict[str, Any], *, cache_hit: bool,
                         captured: datetime, fetched: datetime, expires: datetime,
                         partial: bool, truncated: bool) -> dict[str, Any]:
        output = deepcopy(result)
        output["offer_cache"] = {
            "cache_hit": cache_hit,
            "source": "cache" if cache_hit else "live_search",
            "captured_at_utc": _stamp(captured),
            "fetched_at_utc": _stamp(fetched),
            "expires_at_utc": _stamp(expires),
            "ttl_seconds": (expires - fetched).total_seconds(),
            "partial": partial,
            "truncated": truncated,
            "authorization_eligible": not cache_hit,
        }
        return output


__all__ = ["DEFAULT_TTL_SECONDS", "MAX_TTL_SECONDS", "OfferCache"]
