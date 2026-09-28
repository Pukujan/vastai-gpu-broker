"""Vast.ai REST adapter for market reads and explicitly invoked instance APIs.

No requests are made on import or client construction. Paid methods run only
when an application explicitly calls them after its own authorization gates.
"""

from __future__ import annotations

import inspect
import json
import os
from decimal import Decimal, InvalidOperation
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


class VastAPIError(RuntimeError):
    """A sanitized API/transport error (never includes request headers)."""


class VastOffersClient:
    """Vast API adapter. The market path uses ``search_page`` only.

    ``transport`` can be a ``(method, url, headers, payload) -> dict`` callable
    for tests. The original three-argument ``(url, headers, payload)`` form is
    still accepted for search-only fakes. API keys are never returned or logged.
    """

    def __init__(
        self,
        api_key: str | None = None,
        *,
        transport: Callable[..., dict[str, Any]] | None = None,
        base_url: str = "https://console.vast.ai",
        timeout: float = 30.0,
    ) -> None:
        self._api_key = api_key if api_key is not None else os.environ.get("VAST_API_KEY")
        self._transport = transport
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        api_version: str = "v0",
    ) -> dict[str, Any]:
        if not self._api_key:
            raise VastAPIError("VAST_API_KEY is not configured")
        if api_version not in {"v0", "v1"}:
            raise ValueError("unsupported Vast API version")
        url = f"{self.base_url}/api/{api_version}{path}"
        headers = {"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"}
        if self._transport is not None:
            try:
                try:
                    arity = len(inspect.signature(self._transport).parameters)
                except (TypeError, ValueError):
                    arity = 4
                if arity == 3:
                    result = self._transport(url, headers, payload or {})
                else:
                    result = self._transport(method, url, headers, payload)
            except Exception as exc:  # Never disclose transport exception details.
                raise VastAPIError(f"Vast API transport failed ({type(exc).__name__})") from None
        else:
            body = json.dumps(payload, separators=(",", ":")).encode("utf-8") if payload is not None else None
            request = Request(url, data=body, headers=headers, method=method)
            try:
                with urlopen(request, timeout=self.timeout) as response:
                    result = json.loads(response.read().decode("utf-8"))
            except HTTPError as exc:
                raise VastAPIError(f"Vast API HTTP {exc.code}") from None
            except (URLError, TimeoutError, OSError) as exc:
                raise VastAPIError(f"Vast API transport failed ({type(exc).__name__})") from None
            except (UnicodeDecodeError, json.JSONDecodeError):
                raise VastAPIError("Vast API returned invalid JSON") from None
        if not isinstance(result, dict):
            raise VastAPIError("Vast API response was not an object")
        if result.get("error"):
            code = str(result.get("error"))[:80]
            raise VastAPIError(f"Vast API error: {code}")
        return result

    def search_page(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Fetch one offer-search page from documented POST /bundles."""
        return self._request("POST", "/bundles", payload)

    def create_instance(self, offer_id: int, params: dict[str, Any]) -> dict[str, Any]:
        """Accept an offer directly via PUT /asks/{offer_id}.

        Reserved is a post-create conversion with prepayment, not a direct
        create type. Reject any reserved selector annotation here so quote
        metadata cannot be sent as an ordinary hourly create request.
        """
        selected_type = params.get("_broker_rental_type", params.get("rental_type"))
        if isinstance(selected_type, str) and selected_type.strip().casefold() == "reserved":
            raise ValueError("reserved pricing requires an authorized post-create conversion, not direct offer acceptance")
        body = {key: value for key, value in params.items() if key not in {"_broker_rental_type", "rental_type"}}
        return self._request("PUT", f"/asks/{_positive_id(offer_id)}", body)

    def list_instances(self, *, max_pages: int = 100) -> list[dict[str, Any]]:
        """Return a complete paginated account listing or fail closed.

        Vast documents ``GET /api/v1/instances`` with keyset pagination, a
        maximum of 25 rows per page, and ``next_token``. The lifecycle controller
        may use a successful complete result to verify absence. Any failed,
        malformed, repeated-token, count-inconsistent, or page-capped listing
        raises instead; an incomplete page is never an empty/absent result.
        """
        if isinstance(max_pages, bool) or not isinstance(max_pages, int) or max_pages < 1:
            raise ValueError("max_pages must be a positive integer")
        token: str | None = None
        seen_tokens: set[str] = set()
        rows: list[dict[str, Any]] = []
        expected_total: int | None = None
        for page_index in range(max_pages):
            params: dict[str, Any] = {"limit": 25}
            if token is not None:
                params["after_token"] = token
            response = self._request(
                "GET", "/instances?" + urlencode(params), api_version="v1"
            )
            if response.get("success") is not True:
                raise VastAPIError("instance listing did not confirm success")
            page_rows = response.get("instances")
            if not isinstance(page_rows, list) or any(not isinstance(item, dict) for item in page_rows):
                raise VastAPIError("instance listing returned an invalid shape")
            total = response.get("total_instances")
            if isinstance(total, bool) or not isinstance(total, int) or total < 0:
                raise VastAPIError("instance listing omitted a valid total count")
            if expected_total is not None and total != expected_total:
                raise VastAPIError("instance count changed during paginated listing")
            expected_total = total
            rows.extend(page_rows)
            next_token = response.get("next_token")
            if next_token is None:
                if len(rows) != expected_total:
                    raise VastAPIError("instance listing ended before the reported total")
                return rows
            if not isinstance(next_token, str) or not next_token or next_token in seen_tokens:
                raise VastAPIError("instance listing returned an invalid pagination token")
            seen_tokens.add(next_token)
            if page_index == max_pages - 1:
                break
            token = next_token
        raise VastAPIError("instance listing exceeded its page limit; absence is unverified")

    def get_instance(self, instance_id: int) -> dict[str, Any]:
        """Fetch by ID using documented GET /instances/{id}."""
        response = self._request("GET", f"/instances/{_positive_id(instance_id)}")
        item = response.get("instances", response)
        if isinstance(item, list):
            item = item[0] if len(item) == 1 else None
        if not isinstance(item, dict):
            raise VastAPIError("instance lookup returned an invalid shape")
        return item

    def stop_instance(self, instance_id: int) -> dict[str, Any]:
        """Request ``state=stopped`` using documented PUT /instances/{id}."""
        return self._request("PUT", f"/instances/{_positive_id(instance_id)}", {"state": "stopped"})

    def destroy_instance(self, instance_id: int) -> dict[str, Any]:
        """Request documented DELETE /instances/{id}; acknowledgement isn't proof.

        Callers must verify absence through a fresh successful provider listing.
        """
        return self._request("DELETE", f"/instances/{_positive_id(instance_id)}")

    def change_bid(self, instance_id: int, price_usd_per_machine_hour: Decimal | str) -> dict[str, Any]:
        """Change bid at documented route using USD per machine-hour."""
        try:
            price = Decimal(str(price_usd_per_machine_hour))
        except (InvalidOperation, ValueError, TypeError):
            raise ValueError("bid price must be a finite non-negative decimal") from None
        # Vast documents interruptible bid prices in the range $0.001-$128
        # per machine per hour for instance creation and bid changes.
        if not price.is_finite() or price < Decimal("0.001") or price > Decimal("128"):
            raise ValueError("bid price must be between $0.001 and $128 per machine-hour")
        return self._request(
            "PUT",
            f"/instances/bid_price/{_positive_id(instance_id)}",
            {"client_id": "me", "price": str(price)},
        )


def _positive_id(value: Any) -> int:
    if isinstance(value, bool):
        raise ValueError("ID must be a positive integer")
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ValueError("ID must be a positive integer") from None
    if number < 1 or str(number) != str(value).strip():
        raise ValueError("ID must be a positive integer")
    return number
