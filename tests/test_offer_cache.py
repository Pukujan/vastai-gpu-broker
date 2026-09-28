import pathlib
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from vast_broker.offer_cache import OfferCache  # noqa: E402


class Clock:
    def __init__(self):
        self.current = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.current


class OfferCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = pathlib.Path(self.temp_dir.name) / "offers.sqlite3"
        self.clock = Clock()
        self.calls = []

        def live_search(filters, *, disk_gb, rental_types, page_size, max_pages):
            self.calls.append({"filters": filters, "disk_gb": disk_gb,
                               "rental_types": rental_types, "page_size": page_size,
                               "max_pages": max_pages})
            return {"as_of_utc": self.clock.current.isoformat(),
                    "offers": [{"id": len(self.calls)}],
                    "completeness": {"complete": True, "truncated": False,
                                     "warnings": []}}

        self.live_search = live_search
        self.cache = OfferCache(self.database, self.live_search, clock=self.clock)

    def tearDown(self):
        self.cache.close()
        self.temp_dir.cleanup()

    def test_cache_hit_returns_capture_fetch_expiry_and_hit_metadata(self):
        first = self.cache.search_offers({"gpu_ram": {"gte": 16000}}, disk_gb=64)
        self.clock.current += timedelta(seconds=599)
        second = self.cache.search_offers({"gpu_ram": {"gte": 16000}}, disk_gb=64)

        self.assertEqual(len(self.calls), 1)
        self.assertEqual(first["offer_cache"]["cache_hit"], False)
        self.assertEqual(second["offer_cache"]["cache_hit"], True)
        self.assertEqual(second["offer_cache"]["source"], "cache")
        self.assertEqual(second["offer_cache"]["authorization_eligible"], False)
        self.assertEqual(second["offer_cache"]["ttl_seconds"], 600)
        self.assertEqual(second["offer_cache"]["captured_at_utc"], first["offer_cache"]["captured_at_utc"])
        self.assertEqual(second["offer_cache"]["fetched_at_utc"], first["offer_cache"]["fetched_at_utc"])
        self.assertEqual(second["offer_cache"]["expires_at_utc"], first["offer_cache"]["expires_at_utc"])

    def test_expired_record_is_not_returned_and_is_replaced_by_live_result(self):
        first = self.cache.search_offers()
        self.clock.current += timedelta(seconds=600)
        refreshed = self.cache.search_offers()

        self.assertEqual(len(self.calls), 2)
        self.assertEqual(first["offers"][0]["id"], 1)
        self.assertEqual(refreshed["offers"][0]["id"], 2)
        self.assertFalse(refreshed["offer_cache"]["cache_hit"])
        self.assertEqual(refreshed["offer_cache"]["fetched_at_utc"], self.clock.current.isoformat())

    def test_key_isolated_by_filters_disk_rental_types_and_pagination(self):
        base = self.cache.search_offers({"gpu_ram": {"gte": 16000}}, disk_gb=64,
                                        rental_types=("ondemand",), page_size=25, max_pages=3)
        alternatives = [
            self.cache.search_offers({"gpu_ram": {"gte": 24000}}, disk_gb=64,
                                     rental_types=("ondemand",), page_size=25, max_pages=3),
            self.cache.search_offers({"gpu_ram": {"gte": 16000}}, disk_gb=128,
                                     rental_types=("ondemand",), page_size=25, max_pages=3),
            self.cache.search_offers({"gpu_ram": {"gte": 16000}}, disk_gb=64,
                                     rental_types=("bid",), page_size=25, max_pages=3),
            self.cache.search_offers({"gpu_ram": {"gte": 16000}}, disk_gb=64,
                                     rental_types=("ondemand",), page_size=50, max_pages=3),
            self.cache.search_offers({"gpu_ram": {"gte": 16000}}, disk_gb=64,
                                     rental_types=("ondemand",), page_size=25, max_pages=4),
        ]
        again = self.cache.search_offers({"gpu_ram": {"gte": 16000}}, disk_gb=64,
                                         rental_types=("ondemand",), page_size=25, max_pages=3)

        self.assertEqual(len(self.calls), 6)
        self.assertFalse(base["offer_cache"]["cache_hit"])
        self.assertTrue(all(not result["offer_cache"]["cache_hit"] for result in alternatives))
        self.assertTrue(again["offer_cache"]["cache_hit"])

    def test_force_refresh_bypasses_fresh_entry_then_replaces_it(self):
        first = self.cache.search_offers()
        self.clock.current += timedelta(seconds=10)
        forced = self.cache.search_offers(force_refresh=True)
        cached = self.cache.search_offers()

        self.assertEqual(len(self.calls), 2)
        self.assertEqual(first["offers"][0]["id"], 1)
        self.assertEqual(forced["offers"][0]["id"], 2)
        self.assertFalse(forced["offer_cache"]["cache_hit"])
        self.assertTrue(forced["offer_cache"]["authorization_eligible"])
        self.assertEqual(cached["offers"][0]["id"], 2)
        self.assertTrue(cached["offer_cache"]["cache_hit"])

    def test_fresh_entry_survives_reopening_the_sqlite_cache(self):
        first = self.cache.search_offers({"gpu_name": "Fixture"})
        self.cache.close()
        reopened = OfferCache(self.database, self.live_search, clock=self.clock)
        self.cache = reopened
        second = reopened.search_offers({"gpu_name": "Fixture"})

        self.assertEqual(len(self.calls), 1)
        self.assertEqual(first["offers"], second["offers"])
        self.assertTrue(second["offer_cache"]["cache_hit"])

    def test_search_error_is_not_cached_or_replaced_by_old_result(self):
        def failing_search(*args, **kwargs):
            self.calls.append("failed")
            raise RuntimeError("temporary provider failure")

        self.cache.live_search = failing_search
        with self.assertRaisesRegex(RuntimeError, "temporary provider failure"):
            self.cache.search_offers()
        self.assertEqual(self.calls, ["failed"])

        # A subsequent successful call must execute live search rather than hit
        # a failure marker or stale fallback.
        self.cache.live_search = self.live_search
        result = self.cache.search_offers()
        self.assertFalse(result["offer_cache"]["cache_hit"])
        self.assertEqual(len(self.calls), 2)

    def test_partial_and_truncated_state_remains_attached_on_cache_hit(self):
        partial_result = {"as_of_utc": self.clock.current.isoformat(),
                          "offers": [{"id": 7}],
                          "completeness": {"complete": False, "truncated": True,
                                           "warnings": ["page limit reached"]}}
        self.cache.live_search = lambda *args, **kwargs: partial_result
        first = self.cache.search_offers(page_size=1)
        cached = self.cache.search_offers(page_size=1)

        self.assertFalse(first["offer_cache"]["cache_hit"])
        self.assertTrue(cached["offer_cache"]["cache_hit"])
        self.assertTrue(cached["offer_cache"]["partial"])
        self.assertTrue(cached["offer_cache"]["truncated"])
        self.assertEqual(cached["completeness"]["warnings"], ["page limit reached"])

    def test_filter_values_are_not_stored_in_sqlite_cache_key(self):
        secret = "do-not-persist-this-filter-secret"
        self.cache.search_offers({"api_token": secret, "gpu_name": "Fixture"})
        self.cache.close()
        raw_db = self.database.read_bytes()
        self.assertNotIn(secret.encode(), raw_db)
        connection = sqlite3.connect(self.database)
        try:
            key = connection.execute("SELECT cache_key FROM offer_cache").fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(len(key), 64)
        self.assertTrue(all(character in "0123456789abcdef" for character in key))
        self.cache = OfferCache(self.database, self.live_search, clock=self.clock)

    def test_ttl_is_bounded_and_defaults_to_ten_minutes(self):
        self.assertEqual(self.cache.ttl_seconds, 600)
        for ttl in (0, -1, 901, float("inf"), True):
            with self.subTest(ttl=ttl), self.assertRaises(ValueError):
                OfferCache(":memory:", self.live_search, ttl_seconds=ttl, clock=self.clock)
        short_cache = OfferCache(":memory:", self.live_search, ttl_seconds=900, clock=self.clock)
        self.assertEqual(short_cache.ttl_seconds, 900)
        short_cache.close()


if __name__ == "__main__":
    unittest.main()
