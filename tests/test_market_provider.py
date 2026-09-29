import pathlib
import sys
import unittest
from urllib.error import HTTPError
from unittest.mock import Mock, patch

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
            if url.endswith("/api/v0/volumes") and method == "GET":
                return {"volumes": [{"id": 99, "instances": [{"id": 12}]}]}
            if url.endswith("/instances/12") and method == "GET":
                return {"instances": {"id": 12}}
            if url.endswith("/api/v0/volumes") and method == "DELETE":
                return {"success": True}
            return {"success": True}

        client = VastOffersClient("not-a-real-key", transport=transport)
        client.create_instance(5, {"image": "fixture"})
        self.assertEqual(client.list_instances(), [{"id": 11}, {"id": 13}])
        self.assertEqual(client.list_volumes(), [{"id": 99, "instances": [{"id": 12}]}])
        self.assertEqual(client.get_instance(12), {"id": 12})
        client.stop_instance(12)
        client.destroy_instance(12)
        client.destroy_volume(99)
        client.change_bid(12, "0.25")
        self.assertIn(("PUT", "https://console.vast.ai/api/v0/asks/5", {"image": "fixture"}), calls)
        self.assertIn(("GET", "https://console.vast.ai/api/v1/instances?limit=25", None), calls)
        self.assertIn(("GET", "https://console.vast.ai/api/v1/instances?limit=25&after_token=page-two", None), calls)
        self.assertIn(("GET", "https://console.vast.ai/api/v0/volumes", None), calls)
        self.assertIn(("DELETE", "https://console.vast.ai/api/v0/volumes", {"id": 99}), calls)
        self.assertIn(("PUT", "https://console.vast.ai/api/v0/instances/12", {"state": "stopped"}), calls)
        self.assertIn(("DELETE", "https://console.vast.ai/api/v0/instances/12", None), calls)
        self.assertIn(("PUT", "https://console.vast.ai/api/v0/instances/bid_price/12", {"client_id": "me", "price": "0.25"}), calls)

    def test_reserved_quote_annotation_cannot_be_sent_as_direct_create(self):
        calls = []
        client = VastOffersClient("fake", transport=lambda *args: calls.append(args) or {"success": True})
        with self.assertRaisesRegex(ValueError, "post-create conversion"):
            client.create_instance(5, {"image": "fixture", "_broker_rental_type": "reserved"})
        self.assertEqual(calls, [])

    def test_instance_command_uses_documented_route_and_validates_result_url(self):
        calls = []
        result_url = "https://s3.amazonaws.com/vast.ai/instance_logs/private-signed-result"

        def transport(method, url, headers, payload):
            calls.append((method, url, payload))
            return {"success": True, "result_url": result_url}

        client = VastOffersClient("sentinel", transport=transport)
        result = client.execute_instance(42, "printf '%s' ready")
        self.assertEqual(calls, [(
            "PUT", "https://console.vast.ai/api/v0/instances/command/42",
            {"command": "printf '%s' ready"},
        )])
        self.assertEqual(result, {"success": True, "result_url": result_url})

        for untrusted in (
            "http://s3.amazonaws.com/log",
            "https://example.org/log",
            "https://s3.amazonaws.com.evil.org/log",
        ):
            bad = VastOffersClient("sentinel", transport=lambda *_: {"success": True, "result_url": untrusted})
            with self.assertRaisesRegex(VastAPIError, "result URL was invalid"):
                bad.execute_instance(42, "printf ready")

        with self.assertRaisesRegex(ValueError, "512 characters"):
            client.execute_instance(42, "x" * 513)

    def test_instance_result_download_is_bounded_and_never_follows_untrusted_hosts(self):
        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def read(self, size):
                assert size == 33
                return b"typed result"

        client = VastOffersClient("sentinel")
        opener = Mock()
        opener.open.return_value = FakeResponse()
        with patch("vast_broker.provider.build_opener", return_value=opener):
            result = client.read_instance_command_result(
                "https://s3.amazonaws.com/vast.ai/log?signature=hidden", max_bytes=32
            )
        self.assertEqual(result, "typed result")
        opener.open.assert_called_once()
        self.assertNotIn("hidden", repr(result))

        with patch("vast_broker.provider.build_opener") as fetch:
            with self.assertRaisesRegex(ValueError, "documented HTTPS result host"):
                client.read_instance_command_result("https://example.org/log")
        fetch.assert_not_called()

    def test_signed_instance_result_rejects_http_redirects(self):
        client = VastOffersClient("sentinel")
        opener = Mock()
        opener.open.side_effect = HTTPError(
            "https://s3.amazonaws.com/vast.ai/log?signature=hidden", 302,
            "redirect", {}, None,
        )
        with patch("vast_broker.provider.build_opener", return_value=opener):
            with self.assertRaisesRegex(VastAPIError, "HTTP 302") as error:
                client.read_instance_command_result(
                    "https://s3.amazonaws.com/vast.ai/log?signature=hidden"
                )
        self.assertNotIn("hidden", str(error.exception))

    def test_destroy_acknowledgement_is_only_a_response_and_list_failure_is_not_empty(self):
        client = VastOffersClient("fake", transport=lambda *_: {"success": True})
        self.assertEqual(client.destroy_instance(9), {"success": True})

        malformed = VastOffersClient("fake", transport=lambda *_: {"instances": "unknown"})
        with self.assertRaises(VastAPIError):
            malformed.list_instances()

        malformed_volumes = VastOffersClient("fake", transport=lambda *_: {"volumes": "unknown"})
        with self.assertRaises(VastAPIError):
            malformed_volumes.list_volumes()

        invalid_volume_id = VastOffersClient("fake", transport=lambda *_: {"volumes": [{"id": 0}]})
        with self.assertRaises(VastAPIError):
            invalid_volume_id.list_volumes()

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

    def test_complete_inventory_rejects_missing_non_numeric_and_duplicate_ids(self):
        malformed_rows = (
            [{"name": "missing id"}],
            [{"id": True}],
            [{"id": "not-an-instance-id"}],
            [{"id": 11}, {"id": 11}],
        )
        for rows in malformed_rows:
            client = VastOffersClient(
                "fake",
                transport=lambda *_, rows=rows: {
                    "success": True, "total_instances": len(rows), "next_token": None,
                    "instances": rows,
                },
            )
            with self.subTest(rows=rows), self.assertRaises(VastAPIError):
                client.list_instances()


if __name__ == "__main__":
    unittest.main()
