from __future__ import annotations

import pytest

from vast_broker import VastRentaiService
from vast_broker import rentai
from vast_broker.journal import LeaseJournal


class Provider:
    def list_instances(self):
        return []

    def list_volumes(self):
        return []


def test_public_search_uses_normalized_live_market_search(monkeypatch):
    marker = {"offers": [], "digest": "fresh"}
    observed = {}

    def fake_search(filters, **kwargs):
        observed.update(filters=filters, **kwargs)
        return marker

    monkeypatch.setattr(rentai, "search_offers", fake_search)
    client = object()
    service = VastRentaiService(search_client=client)

    assert service.search({"gpu_ram": {"gte": 15000}}, page_size=17, rental_types=("ondemand",)) is marker
    assert observed == {
        "filters": {"gpu_ram": {"gte": 15000}},
        "client": client,
        "page_size": 17,
        "max_pages": 20,
        "disk_gb": None,
        "rental_types": ("ondemand",),
    }


def test_create_rejects_a_proposal_without_its_confirmation_digest(tmp_path):
    class Controller:
        def run(self, *_args, **_kwargs):
            raise AssertionError("invalid proposal reached lease creation")

    service = VastRentaiService(
        provider=Provider(), journal=LeaseJournal(tmp_path), controller=Controller(),
    )
    with pytest.raises(ValueError, match="proposal confirmation digest mismatch"):
        service.create(
            {"request_id": "trial-1"}, "wrong-digest",
            current_proposal={"request_id": "trial-1"}, plan={}, operation=lambda _lease: None,
        )


def test_status_reports_missing_request_without_contacting_provider(tmp_path):
    class UnavailableProvider:
        def list_instances(self):
            raise AssertionError("missing request must not query Vast")

        def list_volumes(self):
            raise AssertionError("missing request must not query Vast")

    service = VastRentaiService(provider=UnavailableProvider(), journal=LeaseJournal(tmp_path))
    assert service.status("missing") == {"request_id": "missing", "state": "NOT_FOUND"}
