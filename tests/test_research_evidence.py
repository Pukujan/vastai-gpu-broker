import unittest
from hashlib import sha256

from vast_broker.evidence import assess_deployment_evidence, estimate_resources, validate_evidence


IDENTITY = {"base_id": "synthetic-lab/orbit", "artifact_repo": "community/orbit-q4", "revision": "abc123",
            "format": "gguf", "quantization": {"bits": 4}, "files": ["weights.gguf", "adapter.safetensors"]}
def source(source_id, url, role, content):
    return {"source_id": source_id, "url": url, "retrieved_at": "2026-01-01T00:00:00Z",
            "content_digest": "sha256:" + sha256(content.encode()).hexdigest(), "role": role,
            "locator": "captured section", "content": content, "scope": IDENTITY}


SOURCES = [
    source("hub-manifest", "https://huggingface.co/community/orbit-q4/tree/abc123", "artifact_manifest", "manifest at revision abc123"),
    source("model-config", "https://huggingface.co/community/orbit-q4/blob/abc123/config.json", "model_config", "model config at revision abc123"),
    source("publisher-card", "https://huggingface.co/community/orbit-q4/blob/abc123/README.md", "publisher_model_card",
           "Hardware requirements section: no numeric GPU minimum is published for this artifact."),
    source("runtime-doc", "https://runtime.example/docs/cache", "runtime_allocation", "runtime cache placement and layout docs"),
    source("external-test", "https://example.org/report", "external_deployment_test", "exact artifact served on the reported GPU"),
]


def profile(**overrides):
    value = {"identity": IDENTITY, "sources": SOURCES, "source_ids": ["model-config"],
             "file_manifest_source_ids": ["hub-manifest"], "architecture": "dense",
             "total_parameters": 10_000_000, "total_parameters_source_ids": ["model-config"],
             "residency_policy": "all_weights_on_device", "residency_policy_source_ids": ["runtime-doc"],
             "loaded_bytes_per_parameter": 0.5, "loaded_bytes_per_parameter_source_ids": ["runtime-doc"],
             "cache_layout": "conventional_full_attention", "concurrent_sequences": 2, "stored_tokens": 1024,
             "full_attention_layers": 20, "kv_heads": 4, "head_dimension": 64,
             "bytes_per_cache_element": 2, "cache_source_ids": ["model-config", "runtime-doc"],
             "activation_bytes": None, "runtime_overhead_bytes": None, "loading_transient_bytes": None,
             "allocator_overhead_bytes": None, "resident_weight_ram_bytes": None, "cache_ram_bytes": None}
    value.update(overrides)
    return value


FILES = [{"path": "weights.gguf", "size_bytes": 700, "sha256": "a"},
         {"path": "adapter.safetensors", "size_bytes": 100, "sha256": "b"},
         {"path": "weights.gguf", "size_bytes": 700, "sha256": "a"}]


class EvidenceAndResourceTests(unittest.TestCase):
    def test_claims_keep_external_configuration_provenance_and_exact_scope(self):
        evidence = {"sources": SOURCES, "claims": [{"field": "tested_gpu", "value": "A100 80GB",
                    "provenance": "external_tested_configuration", "source_ids": ["external-test"], "scope": IDENTITY,
                    "support_excerpt": "exact artifact served"}]}
        result = validate_evidence(evidence, IDENTITY)
        self.assertTrue(result["valid"])
        self.assertEqual(result["claims"][0]["provenance"], "external_tested_configuration")
        bad = {"sources": SOURCES, "claims": [{"field": "tested_gpu", "value": "A100",
                "provenance": "publisher_requirement", "source_ids": ["missing"], "scope": {**IDENTITY, "revision": "different"}}]}
        self.assertIn("claim[0]_missing_source", validate_evidence(bad, IDENTITY)["errors"])
        self.assertIn("claim[0]_identity_scope_mismatch", validate_evidence(bad, IDENTITY)["errors"])

    def test_capture_digest_excerpt_and_identity_are_required(self):
        claim = {"field": "total_parameters", "value": 1000, "provenance": "artifact_measured",
                 "source_ids": ["model-config"], "scope": IDENTITY, "support_excerpt": "model config"}
        evidence = {"sources": [dict(SOURCES[1])], "claims": [claim]}
        self.assertTrue(validate_evidence(evidence, IDENTITY)["valid"])
        tampered = {"sources": [{**SOURCES[1], "content": "edited"}], "claims": [claim]}
        self.assertIn("source_model-config_digest_mismatch", validate_evidence(tampered, IDENTITY)["errors"])
        incomplete = {k: v for k, v in IDENTITY.items() if k != "quantization"}
        self.assertIn("identity_quantization_unresolved", validate_evidence({"sources": [], "claims": []}, incomplete)["errors"])
        mutable = {**IDENTITY, "revision": "main"}
        self.assertIn("identity_revision_is_mutable", validate_evidence({"sources": [], "claims": []}, mutable)["errors"])

    def test_conflicting_duplicate_source_or_claim_ids_are_rejected(self):
        claim = {"field": "total_parameters", "value": 1000, "provenance": "artifact_measured",
                 "source_ids": ["model-config"], "scope": IDENTITY, "support_excerpt": "model config"}
        duplicate_source = {**SOURCES[1], "content": "contradictory captured content"}
        result = validate_evidence({"sources": [SOURCES[1], duplicate_source], "claims": [claim]}, IDENTITY)
        self.assertIn("source_model-config_conflicting_duplicate_id", result["errors"])
        other_claim = {**claim, "value": 1001}
        result = validate_evidence({"sources": SOURCES, "claims": [claim, other_claim]}, IDENTITY)
        self.assertIn("claim_total_parameters_conflicting_duplicate", result["errors"])

    def test_missing_official_minimum_requires_external_research_and_test_is_profile_only(self):
        base_claims = [{"field": "official_hardware_requirements",
                        "provenance": "publisher_requirement", "source_ids": ["publisher-card"],
                        "scope": IDENTITY, "support_excerpt": "no numeric GPU minimum is published",
                        "value": {"status": "not_published", "searched_sections": [{
                            "source_id": "publisher-card", "locator": "captured section",
                            "query": "GPU hardware minimum VRAM", "conclusion": "no_numeric_hardware_minimum_found"}]}}]
        no_test = assess_deployment_evidence({"sources": SOURCES, "claims": base_claims}, IDENTITY, {"context": 128})
        self.assertEqual(no_test["state"], "EXTERNAL_DEPLOYMENT_RESEARCH_REQUIRED")
        self.assertFalse(no_test["minimum_proven"])
        tested = {"field": "tested_configuration", "value": {"inference_succeeded": True,
                  "gpu_name": "Test GPU", "gpu_ram_bytes": 80_000_000_000, "runtime": "runtime-x",
                  "workload": {"context": 128}}, "provenance": "external_tested_configuration",
                  "source_ids": ["external-test"], "scope": IDENTITY, "support_excerpt": "exact artifact served"}
        reviewed = assess_deployment_evidence({"sources": SOURCES, "claims": base_claims + [tested]}, IDENTITY, {"context": 128})
        self.assertEqual(reviewed["state"], "TESTED_PROFILE_ONLY")
        self.assertEqual(len(reviewed["tested_profiles"]), 1)
        self.assertFalse(reviewed["minimum_proven"])
        self.assertTrue(reviewed["semantic_review_required"])

    def test_published_status_without_numeric_minimum_does_not_count_as_fit(self):
        claim = {"field": "official_hardware_requirements", "value": {"status": "published"},
                 "provenance": "publisher_requirement", "source_ids": ["model-config"],
                 "scope": IDENTITY, "support_excerpt": "model config"}
        result = assess_deployment_evidence({"sources": SOURCES, "claims": [claim]}, IDENTITY, {})
        self.assertEqual(result["state"], "OFFICIAL_REQUIREMENT_INCOMPLETE")

    def test_official_absence_without_search_record_does_not_trigger_external_fit_status(self):
        claim = {"field": "official_hardware_requirements", "value": {"status": "not_published"},
                 "provenance": "publisher_requirement", "source_ids": ["publisher-card"],
                 "scope": IDENTITY, "support_excerpt": "no numeric GPU minimum is published"}
        result = assess_deployment_evidence({"sources": SOURCES, "claims": [claim]}, IDENTITY, {})
        self.assertEqual(result["state"], "OFFICIAL_ABSENCE_UNVERIFIED")
        self.assertFalse(result["official_absence_verified"])

    def test_a31_exact_artifact_bytes_and_unknown_resource_components(self):
        result = estimate_resources(profile(), FILES, identity=IDENTITY)
        self.assertEqual(result["terms"]["artifact_disk_bytes"]["value"], 800)
        self.assertEqual(result["terms"]["resident_weight_vram_bytes"]["value"], 5_000_000)
        self.assertIsNone(result["terms"]["runtime_overhead_bytes"]["value"])
        self.assertIsNone(result["terms"]["activation_bytes"]["value"])
        self.assertEqual(result["fit_state"], "unknown")
        self.assertFalse(result["is_publisher_requirement"])

    def test_a32_moe_residency_uses_total_not_active_parameters(self):
        a = estimate_resources(profile(architecture="moe", total_parameters=12_000_000, active_parameters=2_000_000,
                                       active_parameters_source_ids=["model-config"]), FILES)
        b = estimate_resources(profile(architecture="moe", total_parameters=24_000_000, active_parameters=2_000_000,
                                       active_parameters_source_ids=["model-config"]), FILES)
        self.assertEqual(a["terms"]["resident_weight_vram_bytes"]["value"], 6_000_000)
        self.assertEqual(b["terms"]["resident_weight_vram_bytes"]["value"], 12_000_000)
        self.assertTrue(any("MoE residency comes from the sourced placement policy/count" in x for x in b["terms"]["resident_weight_vram_bytes"]["assumptions"]))

    def test_a33_conventional_gqa_cache_uses_kv_heads_and_scope_sources(self):
        result = estimate_resources(profile(), FILES)
        # 2 * seq * tokens * layers * kv heads * head dim * bytes.
        self.assertEqual(result["terms"]["cache_vram_bytes"]["value"], 2 * 2 * 1024 * 20 * 4 * 64 * 2)
        hybrid = estimate_resources(profile(cache_layout="hybrid_recurrent", cache_source_ids=["runtime-doc"]), FILES)
        self.assertIsNone(hybrid["terms"]["cache_vram_bytes"]["value"])
        self.assertEqual(hybrid["fit_state"], "unknown")

    def test_a34_disk_file_bytes_are_not_loaded_resident_memory(self):
        result = estimate_resources(profile(loaded_bytes_per_parameter=None, loaded_bytes_per_parameter_source_ids=[]), FILES)
        self.assertEqual(result["terms"]["artifact_disk_bytes"]["value"], 800)
        self.assertIsNone(result["terms"]["resident_weight_vram_bytes"]["value"])
        self.assertNotEqual(result["terms"]["artifact_disk_bytes"]["value"], result["terms"]["resident_weight_vram_bytes"]["value"])

    def test_m24_conventional_cache_scales_with_context_and_concurrency(self):
        a = estimate_resources(profile(), FILES)
        b = estimate_resources(profile(stored_tokens=2048, concurrent_sequences=3), FILES)
        self.assertEqual(b["terms"]["cache_vram_bytes"]["value"], a["terms"]["cache_vram_bytes"]["value"] * 3)

    def test_m25_more_total_expert_weights_increases_moe_residency_at_fixed_active_count(self):
        a = estimate_resources(profile(architecture="moe", total_parameters=20, active_parameters=5), FILES)
        b = estimate_resources(profile(architecture="moe", total_parameters=80, active_parameters=5), FILES)
        self.assertGreater(b["terms"]["resident_weight_vram_bytes"]["value"], a["terms"]["resident_weight_vram_bytes"]["value"])

    def test_m26_duplicate_shard_reference_is_counted_once_and_revision_mismatch_blocks_formula(self):
        result = estimate_resources(profile(), FILES + [FILES[0]])
        self.assertEqual(result["terms"]["artifact_disk_bytes"]["value"], 800)
        mismatch = {**IDENTITY, "revision": "other"}
        blocked = estimate_resources(profile(), FILES, identity=mismatch)
        self.assertFalse(blocked["identity_matches"])
        self.assertIsNone(blocked["terms"]["resident_weight_vram_bytes"]["value"])

    def test_m27_removing_runtime_source_turns_derived_term_unknown(self):
        p = profile(sources=[s for s in SOURCES if s["source_id"] != "runtime-doc"])
        result = estimate_resources(p, FILES)
        self.assertIsNone(result["terms"]["resident_weight_vram_bytes"]["value"])
        self.assertIsNone(result["terms"]["cache_vram_bytes"]["value"])
        self.assertEqual(result["fit_state"], "unknown")


if __name__ == "__main__":
    unittest.main()
