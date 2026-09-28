import json
import pathlib
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from scripts import sync_offers  # noqa: E402


class SyncOffersTests(unittest.TestCase):
    def test_fetch_sends_no_availability_or_verification_filters(self):
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return b'{"offers": []}'

        requests = []

        def fake_urlopen(request, timeout):
            requests.append((request, timeout))
            return Response()

        with patch.object(sync_offers, "urlopen", fake_urlopen):
            self.assertEqual(sync_offers.fetch("ondemand", 100, "test-secret"), [])
        request, timeout = requests[0]
        payload = json.loads(request.data.decode("utf-8"))
        self.assertEqual(payload, {"limit": 100, "type": "ondemand"})
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(timeout, 45)

    def test_snapshot_keeps_mixed_states_and_marks_limit_saturation(self):
        results = {
            "ondemand": [
                {"id": 1, "verification": "verified"},
                {"id": 2, "verification": "deverified"},
                {"id": 3, "verification": "unverified"},
            ],
            "bid": [],
            "reserved": [],
        }
        snapshot = sync_offers.build_snapshot(results, limit=3, captured_at="2026-09-28T00:00:00+00:00")
        self.assertEqual(snapshot["filters"]["verification"], "not filtered")
        self.assertIn("prepaid commitment", snapshot["pricing_interpretation"]["reserved"])
        self.assertEqual(snapshot["possibly_truncated_types"], ["ondemand"])
        self.assertEqual(snapshot["verification_counts"], {"verified": 1, "deverified": 1, "unverified": 1, "unknown": 0})
        self.assertEqual(len(snapshot["offers"]["ondemand"]), 3)

    def test_snapshot_strips_credential_fields_and_exact_token_occurrences(self):
        secret = "test-api-key-do-not-persist"
        snapshot = sync_offers.build_snapshot(
            {"ondemand": [{"id": 1, "access_token": secret, "notes": f"host {secret}", "gpu_name": "Fixture"}]},
            limit=10,
            token=secret,
        )
        serialized = json.dumps(snapshot)
        self.assertNotIn(secret, serialized)
        self.assertNotIn("access_token", serialized)
        self.assertIn("[REDACTED]", serialized)

    def test_write_snapshot_replaces_file_with_json(self):
        with tempfile.TemporaryDirectory() as folder:
            destination = pathlib.Path(folder) / "nested" / "latest.json"
            sync_offers.write_snapshot({"counts": {"ondemand": 0}}, destination)
            self.assertEqual(json.loads(destination.read_text(encoding="utf-8")), {"counts": {"ondemand": 0}})
            self.assertEqual(list(destination.parent.iterdir()), [destination])


if __name__ == "__main__":
    unittest.main()
