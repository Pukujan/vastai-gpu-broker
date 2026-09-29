import unittest

from vast_broker.research import resolve_request


def record(repo, sha, *, card=None, files=None, tags=None):
    return {"id": repo, "sha": sha, "cardData": card or {}, "siblings": files or [], "tags": tags or []}


BASE = "synthetic-lab/orbit-8b"
records = [
    record(BASE, "base123", card={"model_type": "orbit", "architectures": ["OrbitForCausalLM"]},
           files=[{"rfilename": "config.json", "size": 90, "sha256": "c"}, {"rfilename": "model.safetensors", "size": 1000, "sha256": "w"}]),
    record("community/orbit-int4", "q41", card={"base_model": BASE, "format": "safetensors", "quantization_config": {"bits": 4}},
           files=[{"rfilename": "config.json", "size": 80}, {"rfilename": "weights.safetensors", "size": 700}]),
    record("community/orbit-q4-gguf", "q42", card={"base_model": [BASE], "bits": 4},
           files=[{"rfilename": "orbit-Q4_K_M.gguf", "size": 640}], tags=["gguf"]),
    record("unrelated/orbit-int4", "q43", card={"base_model": "synthetic-lab/other", "bits": 4},
           files=[{"rfilename": "weights.gguf", "size": 640}]),
]


class ResearchRoutingTests(unittest.TestCase):
    def test_a25_family_discovers_only_structurally_linked_candidates_and_asks_selection(self):
        result = resolve_request({"model_query": "Orbit 8B", "records": records})
        self.assertEqual(result["state"], "MODEL_CONFIRMATION_REQUIRED")
        self.assertTrue(result["candidate_selection_required"])
        self.assertEqual({c["repo_id"] for c in result["candidates"]}, {BASE, "community/orbit-int4", "community/orbit-q4-gguf"})
        self.assertEqual({c["revision"] for c in result["candidates"]}, {"base123", "q41", "q42"})
        self.assertTrue(all("offer_statistics" in c and "requirements" in c for c in result["candidates"]))
        self.assertFalse(result["paid_action_allowed"])

    def test_a26_official_base_does_not_resolve_open_artifact_and_publisher_only_excludes_community(self):
        open_result = resolve_request({"base_id": BASE, "records": records})
        self.assertTrue(open_result["candidate_selection_required"])
        self.assertEqual(len(open_result["candidates"]), 3)
        restricted = resolve_request({"base_id": BASE, "publisher_only": True, "records": records})
        self.assertEqual([c["repo_id"] for c in restricted["candidates"]], [BASE])

    def test_a27_exact_repository_and_files_stays_narrow(self):
        request = {"exact_repo": "https://huggingface.co/community/orbit-int4", "base_id": BASE,
                   "artifact_files": ["weights.safetensors"], "records": records}
        result = resolve_request(request)
        self.assertEqual(result["resolution_mode"], "exact_artifact")
        self.assertFalse(result["candidate_selection_required"])
        self.assertEqual([c["repo_id"] for c in result["candidates"]], ["community/orbit-int4"])
        self.assertEqual(result["candidates"][0]["identity"]["files"], ["weights.safetensors"])

    def test_a28_scoped_selection_removes_only_model_question(self):
        scope = {"base_ids": [BASE], "quantization_bits": 4, "formats": ["gguf", "safetensors"], "ranking_objective": "cheapest_valid"}
        request = {"resolution_mode": "scoped_auto_select", "selection_scope": scope,
                   "selection_authorization": {"mode": "scoped_auto_select", "scope": scope,
                                               "provenance": {"type": "task_instruction", "value": "any compatible 4-bit"}},
                   "records": records}
        result = resolve_request(request)
        self.assertEqual(result["state"], "EVIDENCE_REQUIRED")
        self.assertFalse(result["candidate_selection_required"])
        self.assertFalse(result["paid_action_allowed"])
        self.assertEqual({c["repo_id"] for c in result["candidates"]}, {"community/orbit-int4", "community/orbit-q4-gguf"})
        self.assertNotIn(BASE, {c["repo_id"] for c in result["candidates"]})

    def test_scoped_selection_without_matching_authorization_still_requires_confirmation(self):
        scope = {"base_ids": [BASE], "quantization_bits": 4}
        result = resolve_request({"resolution_mode": "scoped_auto_select", "selection_scope": scope,
                                  "selection_authorization": {"mode": "scoped_auto_select", "scope": scope,
                                                              "provenance": "untrusted string"}, "records": records})
        self.assertTrue(result["candidate_selection_required"])
        self.assertEqual(result["state"], "MODEL_CONFIRMATION_REQUIRED")
        self.assertFalse(result["paid_action_allowed"])

    def test_a29_retrieval_failure_differs_from_missing_artifact_and_no_candidate(self):
        transient = resolve_request({"model_query": "unknown"}, fetcher=lambda query: {"error": "offline"})
        missing = resolve_request({"exact_repo": "community/nope", "artifact_files": ["w.gguf"], "records": records})
        out_of_scope = resolve_request({"base_id": BASE, "selection_scope": {"quantization_bits": 2}, "records": records})
        self.assertIn("SOURCE_RETRIEVAL_FAILED", transient["reason_codes"])
        self.assertIn("ARTIFACT_NOT_FOUND", missing["reason_codes"])
        self.assertIn("NO_CANDIDATE_IN_SCOPE", out_of_scope["reason_codes"])
        self.assertEqual(transient["explore_alternatives_required"], False)

    def test_m20_narrowing_family_to_exact_removes_confirmation(self):
        ambiguous = resolve_request({"base_id": BASE, "records": records})
        exact = resolve_request({"exact_repo": "community/orbit-int4", "base_id": BASE,
                                 "artifact_files": ["weights.safetensors"], "records": records})
        self.assertTrue(ambiguous["candidate_selection_required"])
        self.assertFalse(exact["candidate_selection_required"])
        self.assertEqual(len(exact["candidates"]), 1)

    def test_m21_official_qualifier_does_not_authorize_community_weights(self):
        result = resolve_request({"base_id": BASE, "model_query": "official Orbit", "records": records})
        self.assertTrue(result["candidate_selection_required"])
        self.assertIn("community/orbit-int4", {c["repo_id"] for c in result["candidates"]})

    def test_m22_restoring_exact_file_manifest_resumes_same_candidate(self):
        absent = resolve_request({"exact_repo": "community/orbit-int4", "artifact_files": ["missing.safetensors"], "records": records})
        restored = resolve_request({"exact_repo": "community/orbit-int4", "artifact_files": ["weights.safetensors"], "records": records})
        self.assertIn("ARTIFACT_NOT_FOUND", absent["reason_codes"])
        self.assertTrue(absent["explore_alternatives_required"])
        self.assertFalse(restored["candidate_selection_required"])

    def test_m23_scope_allows_only_in_scope_quantization_revision_research(self):
        scope = {"base_ids": [BASE], "quantization_bits": 4}
        req = {"resolution_mode": "scoped_auto_select", "selection_scope": scope,
               "selection_authorization": {"mode": "scoped_auto_select", "scope": scope,
                                           "provenance": {"type": "user", "value": "choose 4 bit"}}, "records": records}
        four_bit = resolve_request(req)
        req["selection_scope"] = {"base_ids": [BASE], "quantization_bits": 2}
        outside_scope = resolve_request(req)
        self.assertEqual(len(four_bit["candidates"]), 2)
        self.assertEqual(outside_scope["candidates"], [])
        self.assertFalse(outside_scope["candidate_selection_required"])


if __name__ == "__main__":
    unittest.main()
