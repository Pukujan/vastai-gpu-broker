import hashlib
import json
import random
import unittest
from datetime import datetime, timezone
from copy import deepcopy

from vast_broker.market import normalize_offer
from vast_broker.research import resolve_request
from vast_broker.router import authorize_run, candidate_key, review_candidate, route_request
from vast_broker import router as router_module


BASE = "example/tiny-dense"
REPO = "community/tiny-dense-q4"
WORKLOAD = {"context_tokens": 128, "concurrency": 1}
RECORDS = [{
    "id": REPO, "sha": "abc123", "cardData": {
        "base_model": BASE, "model_type": "tiny_dense", "format": "gguf", "bits": 4,
    }, "siblings": [{"rfilename": "tiny-Q4_K_M.gguf", "size": 4096, "sha256": "weight"}],
}]


def _source(source_id, role, url, content):
    return {"source_id": source_id, "role": role, "url": url,
            "retrieved_at": "2026-09-28T12:00:00+00:00", "locator": "captured document section",
            "content": content, "content_digest": "sha256:" + hashlib.sha256(content.encode()).hexdigest()}


def evidence(candidate, *, with_test=True):
    identity = candidate["identity"]
    publisher_text = "Publisher card: exact selected file tiny-Q4_K_M.gguf; architecture tiny_dense; 1000 parameters; Q4 GGUF; no numeric GPU minimum is published; no benchmark is listed."
    test_text = "External deployment report: runtime test served the exact artifact successfully on Test GPU 1."
    sources = [_source("publisher", "publisher_model_card", "https://huggingface.co/community/tiny-dense-q4",
                       publisher_text),
               _source("test", "external_deployment_test", "https://example.org/test-report",
                       test_text)]
    claims = [
        {"field": "selected_artifact_files", "value": identity["files"], "provenance": "artifact_measured",
         "source_ids": ["publisher"], "scope": identity, "support_excerpt": "tiny-Q4_K_M.gguf"},
        {"field": "architecture", "value": "tiny_dense", "provenance": "artifact_measured",
         "source_ids": ["publisher"], "scope": identity, "support_excerpt": "architecture tiny_dense"},
        {"field": "total_parameters", "value": 1000, "provenance": "artifact_measured",
         "source_ids": ["publisher"], "scope": identity, "support_excerpt": "1000 parameters"},
        {"field": "quantization", "value": identity["quantization"], "provenance": "artifact_measured",
         "source_ids": ["publisher"], "scope": identity, "support_excerpt": "Q4 GGUF"},
        {"field": "official_hardware_requirements", "value": {"status": "not_published",
         "searched_sections": [{"source_id": "publisher", "locator": "captured document section",
                                "query": "GPU hardware memory minimum",
                                "conclusion": "no_numeric_hardware_minimum_found"}]},
         "provenance": "publisher_requirement", "source_ids": ["publisher"], "scope": identity,
         "support_excerpt": "no numeric GPU minimum is published"},
        {"field": "runtime_and_inference", "value": {"runtime": "runtime-x", "interface": "http"},
         "provenance": "external_tested_configuration", "source_ids": ["test"], "scope": identity,
         "support_excerpt": "runtime test served"},
        {"field": "quality_benchmarks", "value": {"status": "not_published"},
         "provenance": "publisher_requirement", "source_ids": ["publisher"], "scope": identity,
         "support_excerpt": "no benchmark is listed"},
        {"field": "resource_profile", "value": {"source-backed": True}, "provenance": "formula_derived",
         "source_ids": ["publisher"], "scope": identity, "support_excerpt": "1000 parameters"},
    ]
    if with_test:
        claims.append({"field": "tested_configuration", "value": {
            "inference_succeeded": True, "gpu_name": "Test GPU 1", "gpu_ram_bytes": 8192,
            "runtime": "runtime-x", "workload": WORKLOAD,
        }, "provenance": "external_tested_configuration", "source_ids": ["test"],
            "scope": identity, "support_excerpt": "runtime test served"})
    else:
        claims.append({"field": "tested_configuration", "value": None, "provenance": "unknown",
                       "source_ids": [], "scope": identity})
    return {"sources": sources, "claims": claims,
            "resource_profile": {"identity": identity, "architecture": "dense",
                                 "total_parameters": 1000, "sources": sources,
                                 "file_manifest_source_ids": ["publisher"]}}


def market_snapshot():
    offer = normalize_offer({"id": 11, "type": "ondemand", "rentable": True, "rented": False,
                             "verification": "unverified", "gpu_name": "Test GPU 1", "gpu_ram": 8192,
                             "cpu_cores": 8, "cpu_ram": 16384, "disk_space": 40,
                             "inet_up_cost": 0.0005, "inet_down_cost": 0.0005,
                             "dph_total": 0.2, "dph_base": 0.18, "storage_cost": 1.46})
    return {"as_of_utc": "2026-09-28T12:00:00+00:00", "digest": "market-digest",
            "offers": [offer], "completeness": {"complete": True},
            "allocated_storage_gb": 40,
            "query_filters": [{"type": kind, "limit": 100, "allocated_storage": 40}
                              for kind in ("ondemand", "bid", "reserved")]}


LIMITS = {"max_hourly_usd": 1, "max_total_usd": 3, "max_runtime_seconds": 7200,
          "max_network_usd": 0, "temporary_disk_gb": 40, "start_deadline_seconds": 300,
          "cold_start_timeout_seconds": 300, "idle_timeout_seconds": 900,
          "hung_request_timeout_seconds": 300}


class RouterGateTests(unittest.TestCase):
    def setUp(self):
        request = {"exact_repo": REPO, "base_id": BASE, "artifact_files": ["tiny-Q4_K_M.gguf"],
                   "workload": WORKLOAD, "records": RECORDS, "request_id": "test-route"}
        self.request = request
        self.candidate = resolve_request(request)["candidates"][0]
        self.key = candidate_key(self.candidate)
        self.request["deployment_recipe"] = {
            "identity": self.candidate["identity"], "image": "runtime@sha256:abc",
            "create_params": {"image": "runtime@sha256:abc", "disk": 40},
        }
        self.evidence = evidence(self.candidate)
        self.market = market_snapshot()

    def test_official_spec_absence_without_tested_evidence_stays_unresolved(self):
        reviewed = review_candidate(self.candidate, evidence(self.candidate, with_test=False), WORKLOAD)
        self.assertEqual(reviewed["state"], "EXTERNAL_DEPLOYMENT_RESEARCH_REQUIRED")
        self.assertIn("preserve fit as unknown", reviewed["missing_research"][0])
        self.assertFalse(reviewed["paid_action_allowed"])

    def test_unverified_absence_of_publisher_minimum_cannot_reach_review_or_paid_plan(self):
        fabricated = evidence(self.candidate)
        official = next(c for c in fabricated["claims"] if c["field"] == "official_hardware_requirements")
        official["value"].pop("searched_sections")
        reviewed = review_candidate(self.candidate, fabricated, WORKLOAD)
        self.assertEqual(reviewed["state"], "OFFICIAL_ABSENCE_UNVERIFIED")
        self.assertFalse(reviewed["valid"])
        self.assertFalse(reviewed["paid_action_allowed"])
        self.request["paid_authorization"] = {"authorized": True, "scope": "exact_artifact",
                                               "provenance": {"type": "user_task", "value": "host this exact artifact"}}
        result = route_request(self.request, evidence_by_candidate={self.key: fabricated}, market=self.market,
                               limits=LIMITS,
                               now_utc=datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc))
        self.assertEqual(result["state"], "EXTERNAL_RESEARCH_REQUIRED")
        self.assertFalse(result["paid_action_allowed"])

    def test_estimates_and_candidate_files_cannot_override_exact_scope_or_source_digest(self):
        malformed = evidence(self.candidate)
        malformed["claims"][0]["value"] = ["other.gguf"]
        reviewed = review_candidate(self.candidate, malformed, WORKLOAD)
        self.assertEqual(reviewed["state"], "EVIDENCE_INVALID")
        self.assertIn("selected_files_claim_does_not_match_resolved_manifest", reviewed["errors"])
        self.assertFalse(reviewed["paid_action_allowed"])

    def test_research_and_live_offer_still_require_concrete_spend_limits(self):
        result = route_request(self.request, evidence_by_candidate={self.key: self.evidence},
                               market=self.market, now_utc=datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc))
        self.assertEqual(result["state"], "PAID_LIMITS_REQUIRED")
        self.assertTrue(result["required_inputs"])
        self.assertFalse(result["paid_action_allowed"])

    def test_exact_artifact_and_valid_caps_can_reach_reviewable_run_plan(self):
        self.request["paid_authorization"] = {"authorized": True, "scope": "exact_artifact",
                                              "provenance": {"type": "user_task", "value": "host this exact artifact"}}
        result = route_request(self.request, evidence_by_candidate={self.key: self.evidence}, market=self.market,
                               limits=LIMITS,
                               now_utc=datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc))
        self.assertEqual(result["state"], "READY_TO_RUN")
        self.assertTrue(result["paid_action_allowed"])
        self.assertEqual(result["proposal"]["offer"]["verification"], "unverified")

    def test_router_applies_owner_network_ceiling_to_each_direction(self):
        self.request["paid_authorization"] = {"authorized": True, "scope": "exact_artifact",
                                              "provenance": {"type": "user_task", "value": "host this exact artifact"}}
        expensive = deepcopy(self.market)
        raw = dict(expensive["offers"][0]["raw"])
        raw["inet_down_cost"] = 0.003
        expensive["offers"] = [normalize_offer(raw)]
        result = route_request(self.request, evidence_by_candidate={self.key: self.evidence}, market=expensive,
                               limits=LIMITS,
                               now_utc=datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc))
        self.assertEqual(result["state"], "BLOCKED_CAPACITY_OR_FIT")
        rejected = result["candidate_comparisons"][self.key]["offer_comparison"]["ranking"]["rejected"]
        self.assertIn("download_network_price_above_per_tb_cap", rejected[0]["reasons"])

    def test_hourly_cap_includes_network_and_startup_cost(self):
        self.request["paid_authorization"] = {
            "authorized": True, "scope": "exact_artifact",
            "provenance": {"type": "user_task", "value": "host this exact artifact"},
        }
        market = deepcopy(self.market)
        raw = dict(market["offers"][0]["raw"])
        raw.update({"id": 23, "dph_total": 0.17})
        market["offers"] = [normalize_offer(raw)]
        limits = {**LIMITS, "max_hourly_usd": 0.20, "max_total_usd": 0.30,
                  "max_runtime_seconds": 3600, "max_network_usd": 0.05}
        result = route_request(self.request, evidence_by_candidate={self.key: self.evidence},
                               market=market, limits=limits,
                               now_utc=datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc))
        self.assertEqual(result["state"], "BLOCKED_HOURLY_CAP")
        self.assertFalse(result["paid_action_allowed"])

    def test_under_cap_offer_counts_network_inside_hourly_and_total_bounds(self):
        self.request["paid_authorization"] = {
            "authorized": True, "scope": "exact_artifact",
            "provenance": {"type": "user_task", "value": "host this exact artifact"},
        }
        market = deepcopy(self.market)
        raw = dict(market["offers"][0]["raw"])
        raw.update({"id": 24, "dph_total": 0.1233333333333333})
        market["offers"] = [normalize_offer(raw)]
        limits = {**LIMITS, "max_hourly_usd": 0.20, "max_total_usd": 0.20,
                  "max_runtime_seconds": 3600, "max_network_usd": 0.05}
        result = route_request(self.request, evidence_by_candidate={self.key: self.evidence},
                               market=market, limits=limits,
                               now_utc=datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc))
        self.assertEqual(result["state"], "READY_TO_RUN")
        bound = result["proposal"]["cost_bound"]
        self.assertAlmostEqual(bound["all_in_hourly_usd"], bound["worst_case_total_usd"])
        self.assertLessEqual(bound["all_in_hourly_usd"], limits["max_hourly_usd"])
        self.assertLessEqual(bound["worst_case_total_usd"], limits["max_total_usd"])

    def test_seeded_cost_fuzzer_never_relaxes_hourly_cap_when_network_or_gpu_price_rises(self):
        rng = random.Random(20260929)
        for case in range(500):
            runtime = rng.choice((300, 900, 1800, 3600, 7200))
            startup = rng.choice((60, 120, 300, 600))
            machine_rate = round(rng.uniform(0.001, 0.24), 6)
            network_budget = round(rng.uniform(0, 0.12), 6)
            offer = {"rental_type": "ondemand", "machine_hour_usd": machine_rate}
            limits = {"max_runtime_seconds": runtime, "start_deadline_seconds": startup,
                      "max_network_usd": network_budget, "temporary_disk_gb": 60}
            base = router_module._bounded_cost(offer, limits)
            self.assertIsNotNone(base, case)

            # Pick caps on both sides of the computed rate, then increase one
            # paid component. A cost increase cannot turn a rejection into a pass.
            hourly_cap = round(base["all_in_hourly_usd"] * rng.uniform(0.5, 1.5), 6)
            limits["max_hourly_usd"] = hourly_cap
            base_allowed = router_module._within_hourly_cap(base, limits)

            higher_network_limits = dict(limits)
            higher_network_limits["max_network_usd"] = network_budget + round(rng.uniform(0.000001, 0.05), 6)
            higher_network = router_module._bounded_cost(offer, higher_network_limits)
            self.assertGreaterEqual(higher_network["all_in_hourly_usd"], base["all_in_hourly_usd"], case)
            if not base_allowed:
                self.assertFalse(router_module._within_hourly_cap(higher_network, higher_network_limits), case)

            higher_gpu_offer = {**offer, "machine_hour_usd": machine_rate + round(rng.uniform(0.000001, 0.1), 6)}
            higher_gpu = router_module._bounded_cost(higher_gpu_offer, limits)
            self.assertGreaterEqual(higher_gpu["all_in_hourly_usd"], base["all_in_hourly_usd"], case)
            if not base_allowed:
                self.assertFalse(router_module._within_hourly_cap(higher_gpu, limits), case)

    def test_stale_or_malformed_market_data_blocks_any_plan(self):
        result = route_request(self.request, evidence_by_candidate={self.key: self.evidence},
                               market=self.market, limits=LIMITS,
                               now_utc=datetime(2026, 9, 28, 12, 3, tzinfo=timezone.utc))
        self.assertEqual(result["state"], "LIVE_REFRESH_REQUIRED")
        self.assertIn("MARKET_QUOTE_TOO_OLD_FOR_PAID_ACTION", result["reason_codes"])

    def test_scoped_auto_selection_picks_lowest_proven_candidate_without_reasking(self):
        records = [{"id": BASE, "sha": "base", "cardData": {"model_type": "tiny_dense"},
                    "siblings": [{"rfilename": "config.json", "size": 100}]},
                   {"id": "community/aaa-expensive", "sha": "q4a",
                    "cardData": {"base_model": BASE, "model_type": "tiny_dense", "format": "gguf", "bits": 4},
                    "siblings": [{"rfilename": "tiny-Q4_K_M.gguf", "size": 4096}]},
                   {"id": "community/zzz-cheap", "sha": "q4b",
                    "cardData": {"base_model": BASE, "model_type": "tiny_dense", "format": "gguf", "bits": 4},
                    "siblings": [{"rfilename": "tiny-Q4_K_M.gguf", "size": 4096}]}]
        scope = {"base_ids": [BASE], "quantization_bits": 4, "formats": ["gguf"]}
        request = {"resolution_mode": "scoped_auto_select", "base_id": BASE,
                   "selection_scope": scope,
                   "selection_authorization": {"mode": "scoped_auto_select", "scope": scope,
                                               "provenance": {"type": "user", "value": "any compatible four-bit"}},
                   "paid_authorization": {"authorized": True, "scope": "scoped_auto_select",
                                          "provenance": {"type": "user_task", "value": "host cheapest"}},
                   "workload": WORKLOAD, "records": records, "request_id": "scoped-cheapest"}
        candidates = resolve_request(request)["candidates"]
        request["deployment_recipes_by_candidate"] = {
            candidate_key(candidate): {
                "identity": candidate["identity"], "image": "runtime@sha256:abc",
                "create_params": {"image": "runtime@sha256:abc", "disk": 40},
            }
            for candidate in candidates
        }
        evidences = {}
        for candidate in candidates:
            item = evidence(candidate)
            test_claim = next(c for c in item["claims"] if c["field"] == "tested_configuration")
            test_claim["value"]["gpu_name"] = "Expensive GPU" if "aaa-" in candidate["repo_id"] else "Cheap GPU"
            evidences[candidate_key(candidate)] = item
        offers = [
            normalize_offer({"id": 21, "type": "ondemand", "rentable": True, "rented": False,
                             "gpu_name": "Expensive GPU", "gpu_ram": 8192, "dph_total": 0.4,
                             "inet_up_cost": 0.0005, "inet_down_cost": 0.0005}),
            normalize_offer({"id": 22, "type": "ondemand", "rentable": True, "rented": False,
                             "gpu_name": "Cheap GPU", "gpu_ram": 8192, "dph_total": 0.2,
                             "inet_up_cost": 0.0005, "inet_down_cost": 0.0005}),
        ]
        market = {"as_of_utc": "2026-09-28T12:00:00+00:00", "digest": "two-gpu-market",
                  "offers": offers, "completeness": {"complete": True},
                  "allocated_storage_gb": 40,
                  "query_filters": [{"type": kind, "limit": 100, "allocated_storage": 40}
                                    for kind in ("ondemand", "bid", "reserved")]}
        result = route_request(request, evidence_by_candidate=evidences, market=market, limits=LIMITS,
                               now_utc=datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc))
        self.assertEqual(result["state"], "READY_TO_RUN")
        self.assertEqual(result["proposal"]["identity"]["artifact_repo"], "community/zzz-cheap")
        self.assertFalse(result["candidate_comparisons"][candidate_key(candidates[0])]["research"]["paid_action_allowed"])

    def _authorized_run_fixture(self):
        recipe = {"identity": self.candidate["identity"], "image": "runtime@sha256:abc",
                  "create_params": {"image": "runtime@sha256:abc", "disk": 40}}
        proposal = {"request_id": "test-route", "identity": self.candidate["identity"],
                    "candidate_key": self.key, "offer": self.market["offers"][0],
                    "cost_bound": router_module._bounded_cost(self.market["offers"][0], LIMITS),
                    "market_digest": "market-digest", "market_as_of_utc": datetime.now(timezone.utc).isoformat(),
                    "market_cache_hit": False,
                    "evidence_digest": "evidence-digest", "limits": dict(LIMITS),
                    "deployment_recipe": recipe}
        plan = {"offer_id": 11, "rental_type": "ondemand", "max_runtime_seconds": 7200,
                "start_deadline_seconds": 300, "cold_start_timeout_seconds": 300,
                "idle_timeout_seconds": 900, "hung_request_timeout_seconds": 300,
                "max_network_usd": 0, "identity": self.candidate["identity"],
                "recipe_digest": hashlib.sha256(json.dumps(recipe, sort_keys=True, separators=(",", ":"),
                                                   ensure_ascii=False, default=str).encode()).hexdigest(),
                "create_params": {"image": recipe["image"], "disk": 40}}
        return proposal, plan

    def test_authorize_run_binds_controller_plan_to_confirmed_offer_recipe_and_limits(self):
        proposal, plan = self._authorized_run_fixture()

        class Controller:
            called = False

            def run(self, request_id, submitted_plan, operation):
                self.called = True
                self.asserted_request = request_id
                self.asserted_plan = submitted_plan
                return {"state": "DESTROYED"}

        controller = Controller()
        digest = hashlib.sha256(json.dumps(proposal, sort_keys=True, separators=(",", ":"),
                                           ensure_ascii=False, default=str).encode()).hexdigest()
        result = authorize_run(proposal, digest, current_proposal=proposal, lease_controller=controller,
                               plan=plan, operation=lambda _: None)
        self.assertEqual(result["state"], "DESTROYED")
        self.assertTrue(controller.called)

    def test_authorize_run_rejects_plan_mutations_before_controller_create(self):
        proposal, plan = self._authorized_run_fixture()

        class Controller:
            called = False

            def run(self, *_):
                self.called = True
                return {}

        mutations = [
            lambda p: p.update(offer_id=12),
            lambda p: p.update(rental_type="bid"),
            lambda p: p.update(max_runtime_seconds=7199),
            lambda p: p.update(cold_start_timeout_seconds=301),
            lambda p: p.update(idle_timeout_seconds=899),
            lambda p: p.update(start_deadline_seconds=299),
            lambda p: p["create_params"].update(disk=1),
            lambda p: p["create_params"].update(image="other:latest"),
            lambda p: p["create_params"].update(onstart="unreviewed command"),
            lambda p: p.update(max_network_usd=0.01),
            lambda p: p.update(recipe_digest="0" * 64),
            lambda p: p.update(identity={"artifact_repo": "other/model"}),
        ]
        digest = hashlib.sha256(json.dumps(proposal, sort_keys=True, separators=(",", ":"),
                                           ensure_ascii=False, default=str).encode()).hexdigest()
        for mutate in mutations:
            controller = Controller()
            altered = deepcopy(plan)
            mutate(altered)
            with self.subTest(plan=altered), self.assertRaises(ValueError):
                authorize_run(proposal, digest, current_proposal=proposal, lease_controller=controller,
                              plan=altered, operation=lambda _: None)
            self.assertFalse(controller.called)

    def test_authorize_run_fails_closed_without_a_recipe_in_the_confirmed_proposal(self):
        proposal, plan = self._authorized_run_fixture()
        del proposal["deployment_recipe"]
        digest = hashlib.sha256(json.dumps(proposal, sort_keys=True, separators=(",", ":"),
                                           ensure_ascii=False, default=str).encode()).hexdigest()

        class Controller:
            called = False

            def run(self, *_):
                self.called = True

        controller = Controller()
        with self.assertRaisesRegex(ValueError, "approved deployment recipe"):
            authorize_run(proposal, digest, current_proposal=proposal, lease_controller=controller,
                          plan=plan, operation=lambda _: None)
        self.assertFalse(controller.called)

    def test_authorize_run_binds_bid_create_and_escalation_to_live_bid_quote_and_caps(self):
        bid_offer = normalize_offer({"id": 31, "type": "bid", "rentable": True, "rented": False,
                                     "gpu_name": "Test GPU 1", "gpu_ram": 8192, "dph_total": 0.2,
                                     "min_bid": 0.1})
        limits = {**LIMITS, "max_bid_usd_per_machine_hour": 0.5,
                  "bid_increment_usd": 0.1, "max_bid_attempts": 2}
        recipe = {"identity": self.candidate["identity"], "image": "runtime@sha256:abc",
                  "create_params": {"image": "runtime@sha256:abc", "disk": 40}}
        proposal = {"request_id": "test-route", "identity": self.candidate["identity"],
                    "candidate_key": self.key, "offer": bid_offer,
                    "cost_bound": router_module._bounded_cost(bid_offer, limits),
                    "market_digest": "market-digest", "market_as_of_utc": datetime.now(timezone.utc).isoformat(),
                    "market_cache_hit": False,
                    "evidence_digest": "evidence-digest", "limits": limits,
                    "deployment_recipe": recipe}
        recipe_digest = hashlib.sha256(json.dumps(recipe, sort_keys=True, separators=(",", ":"),
                                           ensure_ascii=False, default=str).encode()).hexdigest()
        plan = {"offer_id": 31, "rental_type": "bid", "max_runtime_seconds": 7200,
                "start_deadline_seconds": 300, "cold_start_timeout_seconds": 300,
                "idle_timeout_seconds": 900, "hung_request_timeout_seconds": 300,
                "max_network_usd": 0, "identity": self.candidate["identity"],
                "recipe_digest": recipe_digest, "max_bid_usd_per_machine_hour": 0.5,
                "starting_bid_usd_per_machine_hour": 0.2, "bid_increment_usd": 0.1,
                "max_bid_attempts": 2,
                "create_params": {"image": recipe["image"], "disk": 40, "price": "0.2"}}

        class Controller:
            called = False

            def run(self, *_):
                self.called = True
                return {"state": "DESTROYED"}

        def confirmed_digest():
            return hashlib.sha256(json.dumps(proposal, sort_keys=True, separators=(",", ":"),
                                             ensure_ascii=False, default=str).encode()).hexdigest()

        controller = Controller()
        authorize_run(proposal, confirmed_digest(), current_proposal=proposal,
                      lease_controller=controller, plan=plan, operation=lambda _: None)
        self.assertTrue(controller.called)
        plan["starting_bid_usd_per_machine_hour"] = 0.15
        plan["create_params"]["price"] = "0.15"
        controller = Controller()
        with self.assertRaisesRegex(ValueError, "machine-hour bid"):
            authorize_run(proposal, confirmed_digest(), current_proposal=proposal,
                          lease_controller=controller, plan=plan, operation=lambda _: None)
        self.assertFalse(controller.called)


if __name__ == "__main__":
    unittest.main()
