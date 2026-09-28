import json
import unittest

from vast_broker.huggingface import HuggingFaceHubClient
from vast_broker.research import resolve_request


class HubClientTests(unittest.TestCase):
    def test_public_family_search_hydrates_revision_and_captures_only_small_text_files(self):
        calls = []
        info = {"id": "org/model", "sha": "abcdef123456", "config": {"model_type": "toy", "architectures": ["ToyForCausalLM"]},
                "siblings": [{"rfilename": "README.md", "size": 60}, {"rfilename": "config.json", "size": 20},
                             {"rfilename": "weights.safetensors", "size": 50000000, "lfs": {"oid": "a" * 64}}]}
        def transport(url, timeout, limit):
            calls.append((url, timeout, limit))
            if "/api/models?" in url:
                return json.dumps([{"id": "org/model"}]).encode()
            if "/api/models/org/model?blobs=true" in url:
                return json.dumps(info).encode()
            if "/README.md" in url:
                return b"---\nlicense: mit\n---\nsmall card"
            if "/config.json" in url:
                return b'{"model_type":"toy"}'
            raise AssertionError("must not fetch model weight bodies")
        client = HuggingFaceHubClient(transport=transport)
        result = resolve_request({"model_query": "toy model"}, fetcher=client)
        candidate = result["candidates"][0]
        self.assertEqual(candidate["revision"], "abcdef123456")
        self.assertEqual(candidate["architecture"], "toy")
        self.assertEqual(candidate["artifact_disk_bytes"], 50000080)
        self.assertTrue(all(s.get("content_digest") for s in candidate["source_captures"]))
        self.assertEqual(sum("resolve" in url for url, _, _ in calls), 2)
        self.assertFalse(any("weights.safetensors" in url and "resolve" in url for url, _, _ in calls))
        self.assertTrue(all(timeout == 10 and limit > 0 for _, timeout, limit in calls))

    def test_exact_repository_url_uses_exact_info_query(self):
        calls = []
        info = {"id": "org/exact", "sha": "rev1", "cardData": {"model_type": "toy"},
                "siblings": [{"rfilename": "config.json", "size": 5}]}
        def transport(url, timeout, limit):
            calls.append(url)
            if "/api/models/org/exact?blobs=true" in url:
                return json.dumps(info).encode()
            if "/config.json" in url:
                return b"{}"
            raise AssertionError("unexpected lookup")
        result = resolve_request({"exact_repo": "https://huggingface.co/org/exact", "artifact_files": ["config.json"]},
                                 fetcher=HuggingFaceHubClient(transport=transport))
        self.assertEqual([c["repo_id"] for c in result["candidates"]], ["org/exact"])
        self.assertTrue(any("/api/models/org/exact?blobs=true" in url for url in calls))
        self.assertFalse(any("/api/models?" in url for url in calls))

    def test_errors_are_sanitized_and_transport_exception_cannot_claim_absence(self):
        def broken(url, timeout, limit):
            raise RuntimeError("token=hf_secret_private detail")
        result = resolve_request({"exact_repo": "org/model", "artifact_files": ["weights.safetensors"]},
                                 fetcher=HuggingFaceHubClient(transport=broken))
        self.assertIn("SOURCE_RETRIEVAL_FAILED", result["reason_codes"])
        self.assertFalse(result["explore_alternatives_required"])
        self.assertNotIn("hf_secret_private", str(result))


if __name__ == "__main__":
    unittest.main()
