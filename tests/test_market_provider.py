import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from vast_broker.provider import VastAPIError, VastOffersClient  # noqa: E402


class ProviderTests(unittest.TestCase):
    def test_search_posts_documented_route_and_never_returns_key(self):
        captured = {}

        def transport(url, headers, payload):
            captured.update(url=url, headers=headers, payload=payload)
            return {"offers": []}

        result = VastOffersClient("sentinel", transport=transport).search_page({"limit": 10, "type": "bid"})
        self.assertEqual(captured["url"], "https://console.vast.ai/api/v0/bundles")
        self.assertEqual(captured["headers"]["Authorization"], "Bearer sentinel")
        self.assertEqual(captured["payload"]["type"], "bid")
        self.assertNotIn("sentinel", repr(result))

    def test_errors_are_sanitized(self):
        client = VastOffersClient("sentinel", transport=lambda *_: (_ for _ in ()).throw(ValueError("sentinel")))
        with self.assertRaises(VastAPIError) as caught:
            client.search_page({})
        self.assertNotIn("sentinel", str(caught.exception))

    def test_instance_adapter_uses_official_paths_and_machine_hour_bid(self):
        calls = []

        def transport(method, url, headers, payload):
            calls.append((method, url, payload))
            if url.endswith("/api/v1/instances?limit=25"):
                return {"success": True, "total_instances": 2, "next_token": "page-two", "instances": [{"id": 11}]}
            if url.endswith("/api/v1/instances?limit=25&after_token=page-two"):
                return {"success": True, "total_instances": 2, "next_token": None, "instances": [{"id": 13}]}
            if url.endswith("/instances/12") and method == "GET":
                return {"instances": {"id": 12}}
            return {"success": True}

        client = VastOffersClient("not-a-real-key", transport=transport)
        client.create_instance(5, {"image": "fixture"})
        self.assertEqual(client.list_instances(), [{"id": 11}, {"id": 13}])
        self.assertEqual(client.get_instance(12), {"id": 12})
        client.stop_instance(12)
        client.destroy_instance(12)
        client.change_bid(12, "0.25")
        self.assertIn(("PUT", "https://console.vast.ai/api/v0/asks/5", {"image": "fixture"}), calls)
        self.assertIn(("GET", "https://console.vast.ai/api/v1/instances?limit=25", None), calls)
        self.assertIn(("GET", "https://console.vast.ai/api/v1/instances?limit=25&after_token=page-two", None), calls)
        self.assertIn(("PUT", "https://console.vast.ai/api/v0/instances/12", {"state": "stopped"}), calls)
        self.assertIn(("DELETE", "https://console.vast.ai/api/v0/instances/12", None), calls)
        self.assertIn(("PUT", "https://console.vast.ai/api/v0/instances/bid_price/12", {"client_id": "me", "price": "0.25"}), calls)

    def test_reserved_quote_annotation_cannot_be_sent_as_direct_create(self):
        calls = []
        client = VastOffersClient("fake", transport=lambda *args: calls.append(args) or {"success": True})
        with self.assertRaisesRegex(ValueError, "post-create conversion"):
            client.create_instance(5, {"image": "fixture", "_broker_rental_type": "reserved"})
        self.assertEqual(calls, [])

    def test_destroy_acknowledgement_is_only_a_response_and_list_failure_is_not_empty(self):
        client = VastOffersClient("fake", transport=lambda *_: {"success": True})
        self.assertEqual(client.destroy_instance(9), {"success": True})

        malformed = VastOffersClient("fake", transport=lambda *_: {"instances": "unknown"})
        with self.assertRaises(VastAPIError):
            malformed.list_instances()

    def test_incomplete_or_unbounded_instance_listing_is_not_absence(self):
        incomplete = VastOffersClient(
            "fake",
            transport=lambda *_: {
                "success": True,
                "total_instances": 2,
                "next_token": None,
                "instances": [{"id": 9}],
            },
        )
        with self.assertRaises(VastAPIError):
            incomplete.list_instances()

        capped = VastOffersClient(
            "fake",
            transport=lambda *_: {
                "success": True,
                "total_instances": 2,
                "next_token": "more",
                "instances": [{"id": 9}],
            },
        )
        with self.assertRaises(VastAPIError):
            capped.list_instances(max_pages=1)


if __name__ == "__main__":
    unittest.main()
