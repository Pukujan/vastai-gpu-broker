import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from vast_broker.market import (  # noqa: E402
    candidate_price_statistics,
    normalize_offer,
    network_rate_tier,
    quote_candidates,
    rank_offers,
    search_offers,
)


def row(offer_id, price, **updates):
    value = {
        "id": offer_id,
        "gpu_name": "Fixture GPU",
        "num_gpus": 1,
        "gpu_ram": 24576,
        "gpu_total_ram": 24576,
        "cpu_cores_effective": 8,
        "cpu_ram": 32768,
        "disk_space": 500,
        "dph_total": price,
        "dph_base": price,
        "storage_cost": 7.30,
        "min_bid": 0.1,
        "inet_up": 100,
        "inet_down": 200,
        "inet_up_cost": 0.02,
        "inet_down_cost": 0.04,
        "duration": 3600,
        "verification": "unverified",
        "rentable": True,
        "rented": False,
    }
    value.update(updates)
    return value


class FakeProvider:
    def __init__(self, pages):
        self.pages = list(pages)
        self.payloads = []

    def search_page(self, payload):
        self.payloads.append(payload)
        index = len(self.payloads) - 1
        return self.pages[index] if index < len(self.pages) else {"offers": []}


class MarketOfferTests(unittest.TestCase):
    def test_normalization_preserves_raw_and_exact_units(self):
        raw = row(1, "0.125", gpu_ram=24576, cpu_ram=32768, disk_space=512,
                  inet_up_cost=0.02, inet_down_cost=0.04)
        offer = normalize_offer(raw, rental_type="bid")
        self.assertEqual(offer["machine_hour_usd"], 0.125)
        self.assertEqual(offer["bid_floor_machine_hour_usd"], 0.1)
        self.assertEqual(offer["bid_current_machine_hour_usd"], 0.125)
        self.assertEqual(offer["gpu_ram_mb_per_gpu"], 24576)
        self.assertEqual(offer["cpu_ram_mb"], 32768)
        self.assertEqual(offer["disk_space_gb"], 512)
        self.assertIsNone(offer["storage_usd_per_gb_hour"])
        self.assertEqual(offer["upload_usd_per_gb"], 0.02)
        self.assertEqual(offer["raw"], raw)

    def test_queries_rental_types_without_verification_filter(self):
        fake = FakeProvider([{"offers": []}] * 3)
        result = search_offers({"gpu_ram": {"gte": 16000}}, client=fake, page_size=20)
        self.assertEqual(result["completeness"]["pages"], 3)
        self.assertEqual(len(fake.payloads), 3)
        self.assertEqual({p["type"] for p in fake.payloads}, {"ondemand", "bid", "reserved"})
        self.assertTrue(all("verification" not in p and "verified" not in p for p in fake.payloads))
        self.assertTrue(all("rentable" not in p and "rented" not in p for p in fake.payloads))
        self.assertTrue(result["completeness"]["complete"])
        self.assertEqual(result["query_filters"], fake.payloads)
        self.assertIsNone(result["allocated_storage_gb"])

    def test_mixed_verification_statuses_survive_unfiltered_live_retrieval(self):
        rows = [
            row(1, "0.10", verification="verified"),
            row(2, "0.20", verification="deverified"),
            row(3, "0.30", verification="unverified"),
        ]
        fake = FakeProvider([{"offers": rows}])
        result = search_offers(client=fake, page_size=4, rental_types=("ondemand",))
        self.assertEqual([o["verification"] for o in result["offers"]], ["verified", "deverified", "unverified"])
        self.assertNotIn("verification", fake.payloads[0])
        self.assertNotIn("verified", fake.payloads[0])
        self.assertEqual(
            result["completeness"]["observed_verification_counts"],
            {"verified": 1, "deverified": 1, "unverified": 1, "unknown": 0},
        )
        self.assertEqual(
            result["completeness"]["observed_verification_counts_by_rental_type"]["ondemand"]["deverified"],
            1,
        )

    def test_limit_saturation_is_reported_as_truncated_without_cheapest_claim(self):
        fake = FakeProvider([{"offers": [row(i, i / 10) for i in range(3)]}])
        market = search_offers(client=fake, page_size=3, rental_types=("ondemand",))
        self.assertTrue(market["completeness"]["truncated"])
        ranked = rank_offers(market["offers"], completeness=market["completeness"])
        self.assertEqual(ranked["cheapest_claim"], "cheapest_among_observed_eligible")
        self.assertFalse(ranked["global_cheapest_established"])

    def test_explicit_cursor_paginates_and_keeps_types_separate(self):
        fake = FakeProvider([
            {"offers": [row(1, "0.20"), row(2, "0.30")], "next_offset": 2},
            {"offers": [row(3, "0.40")]},
        ])
        result = search_offers(client=fake, page_size=2, max_pages=3, disk_gb=120, rental_types=("ondemand",))
        self.assertEqual([o["id"] for o in result["offers"]], [1, 2, 3])
        self.assertEqual(fake.payloads[1]["offset"], 2)
        self.assertEqual(fake.payloads[0]["allocated_storage"], 120)
        self.assertEqual(result["query_filters"], fake.payloads)
        self.assertEqual(result["allocated_storage_gb"], 120)
        self.assertEqual(result["offers"][0]["query_filters"], fake.payloads[0])
        self.assertEqual(result["offers"][0]["allocated_storage_gb"], 120)
        self.assertTrue(result["completeness"]["complete"])

    def test_documented_constraint_names_are_translated_for_the_provider(self):
        provider = FakeProvider([{"offers": [row(1, "0.18")]}])
        result = search_offers(
            {
                "gpu_name": ["RTX 5060 Ti"],
                "min_gpus": 1,
                "min_gpu_ram_mb": 15000,
                "require_rentable": True,
                "exclude_rented": True,
                "max_upload_usd_per_tb_vast_cli_display": "3",
                "max_download_usd_per_tb_vast_cli_display": "3",
            },
            client=provider,
            rental_types=("ondemand",),
        )
        for payload in provider.payloads:
            self.assertEqual(payload["gpu_name"], {"eq": "RTX 5060 Ti"})
            self.assertEqual(payload["num_gpus"], {"gte": 1})
            self.assertEqual(payload["gpu_ram"], {"gte": 15000})
            for internal in ("min_gpus", "min_gpu_ram_mb", "require_rentable",
                             "exclude_rented", "max_upload_usd_per_tb_vast_cli_display",
                             "max_download_usd_per_tb_vast_cli_display"):
                self.assertNotIn(internal, payload)
        self.assertEqual(result["offers"][0]["gpu_name"], "Fixture GPU")

    def test_multiple_gpu_names_use_the_documented_in_operator(self):
        provider = FakeProvider([{"offers": []}])
        search_offers({"gpu_name": ["RTX 3090", "RTX 5060 Ti"]}, client=provider,
                      rental_types=("ondemand",))
        self.assertEqual(provider.payloads[0]["gpu_name"],
                         {"in": ["RTX 3090", "RTX 5060 Ti"]})

    def test_native_operator_objects_still_pass_through_byte_identical(self):
        provider = FakeProvider([{"offers": []}])
        filters = {"gpu_name": {"eq": "RTX 5060 Ti"}, "num_gpus": {"gte": 1},
                   "gpu_ram": {"gte": 15000}, "reliability": {"gte": 0.97}}
        result = search_offers(dict(filters), client=provider, rental_types=("ondemand",))
        self.assertEqual({k: provider.payloads[0][k] for k in filters}, filters)
        self.assertEqual(result["query_filters"][0]["reliability"], {"gte": 0.97})

    def test_mixing_constraint_and_native_forms_for_one_field_fails_closed(self):
        provider = FakeProvider([{"offers": []}])
        with self.assertRaises(ValueError):
            search_offers({"min_gpus": 1, "num_gpus": {"gte": 1}},
                          client=provider, rental_types=("ondemand",))
        self.assertEqual(provider.payloads, [])

    def test_query_filters_capture_explicit_allocation_and_are_sanitized(self):
        fake = FakeProvider([{"offers": [row(1, "0.20")]}])
        result = search_offers(
            {"gpu_ram": {"gte": 16000}, "api_token": "must-not-be-returned"},
            client=fake,
            page_size=10,
            rental_types=("ondemand",),
            disk_gb=32,
        )
        self.assertEqual(result["allocated_storage_gb"], 32)
        self.assertEqual(result["query_filters"][0]["allocated_storage"], 32)
        self.assertEqual(result["offers"][0]["allocated_storage_gb"], 32)
        self.assertNotIn("api_token", result["query_filters"][0])
        self.assertNotIn("must-not-be-returned", repr(result))

    def test_identical_duplicates_do_not_inflate_samples_but_conflicts_are_marked(self):
        offer = normalize_offer(row(7, "0.40"))
        stats = candidate_price_statistics([offer, offer], disk_gb=10)
        self.assertEqual(stats["by_rental_type"]["ondemand"]["unique_offer_count"], 1)
        conflicting = normalize_offer(row(7, "0.50"))
        ranked = rank_offers([offer, conflicting], completeness={"complete": True})
        self.assertTrue(all("conflicting_or_missing_offer_identity" in r["reasons"] for r in ranked["rejected"]))
        self.assertFalse(ranked["eligible"])

    def test_constraint_first_ranking_is_deterministic_and_verification_neutral(self):
        offers = [
            normalize_offer(row(2, "0.20", verification="deverified")),
            normalize_offer(row(1, "0.20", verification="unverified")),
            normalize_offer(row(3, "0.01", gpu_ram=8192)),
        ]
        constraints = {"min_gpu_ram_mb": 16000}
        a = rank_offers(offers, constraints, completeness={"complete": True})
        b = rank_offers(reversed(offers), constraints, completeness={"complete": True})
        self.assertEqual([x["id"] for x in a["eligible"]], [1, 2])
        self.assertEqual([x["id"] for x in b["eligible"]], [1, 2])
        self.assertEqual(a["rejected"][0]["reasons"], ["min_gpu_ram_mb"])
        self.assertEqual(a["cheapest_claim"], "global_market")

    def test_current_availability_is_a_default_constraint_while_states_stay_visible(self):
        offers = [
            normalize_offer(row(1, "0.10", rentable=False)),
            normalize_offer(row(2, "0.20", rentable=True, rented=True)),
            normalize_offer(row(3, "0.30", rentable=True, rented=False)),
        ]
        ranked = rank_offers(offers, completeness={"complete": True})
        self.assertEqual([o["id"] for o in ranked["eligible"]], [3])
        self.assertIn("not_rentable_or_availability_unknown", ranked["rejected"][0]["reasons"])
        self.assertIn("already_rented", ranked["rejected"][1]["reasons"])

    def test_candidate_statistics_are_scoped_and_price_components_do_not_double_count(self):
        offers = [normalize_offer(row(1, "0.20")), normalize_offer(row(2, "0.40", gpu_ram=8192))]
        market = {"offers": offers, "completeness": {"complete": True}, "as_of_utc": "now", "digest": "abc"}
        quotes = quote_candidates({"big": {"min_gpu_ram_mb": 16000}, "small": {}}, market=market, disk_gb=10)
        big = quotes["big"]["statistics"]["by_rental_type"]["ondemand"]
        self.assertEqual(big["unique_offer_count"], 1)
        self.assertEqual(big["machine_hour_usd"]["mean"], 0.2)
        self.assertIsNone(big["requested_disk_storage_hour_usd"]["mean"])
        self.assertEqual(big["requested_disk_storage_hour_usd"]["unknown_count"], 1)
        self.assertEqual(big["storage_usd_per_gb_month"]["mean"], 7.3)
        self.assertEqual(big["machine_hour_usd"]["sample_unit"], "USD per machine-hour")
        self.assertEqual(quotes["big"]["market_digest"], "abc")

    def test_reserved_observations_remain_reported_but_not_direct_create_candidates(self):
        reserved = normalize_offer(row(9, "0.15", type="reserved"), rental_type="reserved")
        stats = candidate_price_statistics([reserved])
        reserved_stats = stats["by_rental_type"]["reserved"]
        self.assertEqual(reserved_stats["unique_offer_count"], 1)
        self.assertEqual(reserved_stats["machine_hour_usd"]["mean"], 0.15)
        self.assertIn("prepayment", reserved_stats["pricing_interpretation"])
        self.assertEqual(reserved["creation_mode"], "convert_existing_ondemand")
        self.assertIsNone(reserved["reserved_commitment_terms"])

        default = rank_offers([reserved], completeness={"complete": True})
        self.assertFalse(default["eligible"])
        self.assertIn("reserved_requires_explicit_conversion_authorization", default["rejected"][0]["reasons"])
        authorized = rank_offers(
            [reserved],
            {
                "reserved_conversion_authorized": True,
                "reserved_commitment": {"term_months": 1, "prepaid_amount_usd": 20, "quoted_hourly_usd": 0.15},
            },
            completeness={"complete": True},
        )
        self.assertFalse(authorized["eligible"])
        self.assertIn("reserved_conversion_flow_not_supported", authorized["rejected"][0]["reasons"])

        market = {"offers": [reserved], "completeness": {"complete": True}}
        quote = quote_candidates({"candidate": {}}, market=market)["candidate"]
        self.assertEqual(quote["statistics"]["by_rental_type"]["reserved"]["unique_offer_count"], 1)

    def test_nonfinite_prices_stay_unknown(self):
        self.assertIsNone(normalize_offer(row(1, float("nan")))["machine_hour_usd"])

    def test_network_tiers_use_both_directions_and_exact_owner_boundaries(self):
        self.assertEqual(network_rate_tier("0.999999"), "preferred")
        self.assertEqual(network_rate_tier("1"), "acceptable")
        self.assertEqual(network_rate_tier("1.999999"), "acceptable")
        self.assertEqual(network_rate_tier("2"), "expensive_at_or_below_hard_max")
        self.assertEqual(network_rate_tier("3"), "expensive_at_or_below_hard_max")
        self.assertEqual(network_rate_tier("3.000001"), "over_hard_max")

        offer = normalize_offer(row(
            17, "0.20", inet_up_cost="0.0009765625", inet_down_cost="0.0029296875",
        ))
        self.assertEqual(offer["upload_usd_per_tb_vast_cli_display"], 1.0)
        self.assertEqual(offer["download_usd_per_tb_vast_cli_display"], 3.0)
        accepted = rank_offers([offer], {
            "max_upload_usd_per_tb_vast_cli_display": 3,
            "max_download_usd_per_tb_vast_cli_display": 3,
        }, completeness={"complete": True})
        self.assertEqual(len(accepted["eligible"]), 1)
        rejected = rank_offers([offer], {
            "max_upload_usd_per_tb_vast_cli_display": 3,
            "max_download_usd_per_tb_vast_cli_display": 2.999,
        }, completeness={"complete": True})
        self.assertIn("download_network_price_above_per_tb_cap", rejected["rejected"][0]["reasons"])

    def test_conflicting_native_per_tb_and_cli_display_rates_fail_closed(self):
        offer = normalize_offer(row(
            18, "0.20", inet_up_cost="0.01", internet_up_cost_per_tb="1.23",
        ))
        self.assertEqual(offer["price_components"]["network_unit_state"]["upload"], "conflict")
        result = rank_offers([offer], {
            "max_upload_usd_per_tb_vast_cli_display": 3,
            "max_download_usd_per_tb_vast_cli_display": 3,
        }, completeness={"complete": True})
        self.assertIn("upload_network_price_unit_conflict", result["rejected"][0]["reasons"])

    def test_native_per_tb_without_per_gb_mapping_does_not_pass_cli_basis_gate(self):
        offer = normalize_offer(row(
            20, "0.20", inet_up_cost=None, internet_up_cost_per_tb="1.25",
        ))
        self.assertEqual(offer["upload_network_rate_basis"], "native_only_unit_basis_required")
        self.assertIsNone(offer["upload_usd_per_tb_policy"])
        result = rank_offers([offer], {
            "max_upload_usd_per_tb_vast_cli_display": 3,
            "max_download_usd_per_tb_vast_cli_display": 3,
        }, completeness={"complete": True})
        self.assertIn("upload_network_price_unit_basis_required", result["rejected"][0]["reasons"])

    def test_native_per_tb_is_only_a_cross_check_for_documented_cli_mapping(self):
        offer = normalize_offer(row(
            21, "0.20", inet_up_cost="0.00390625", internet_up_cost_per_tb="4.0",
        ))
        self.assertEqual(offer["upload_network_rate_basis"], "cli_display_from_per_gb_crosschecked_native_per_tb")
        self.assertEqual(offer["upload_usd_per_tb_vast_cli_display"], 4.0)
        self.assertEqual(offer["upload_usd_per_tb_policy"], 4.0)
        self.assertEqual(offer["upload_network_rate_tier"], "over_hard_max")

    def test_missing_network_price_is_not_zero_under_explicit_owner_cap(self):
        raw = row(19, "0.20")
        raw.pop("inet_up_cost")
        offer = normalize_offer(raw)
        result = rank_offers([offer], {
            "max_upload_usd_per_tb_vast_cli_display": 3,
            "max_download_usd_per_tb_vast_cli_display": 3,
        }, completeness={"complete": True})
        self.assertIn("upload_network_price_unit_basis_required", result["rejected"][0]["reasons"])

    def test_quote_digest_is_order_stable_and_changes_when_price_changes(self):
        first = normalize_offer(row(1, "0.10"))
        second = normalize_offer(row(2, "0.20"))
        complete = {"complete": True}
        forward = rank_offers([first, second], completeness=complete)
        backward = rank_offers([second, first], completeness=complete)
        changed = rank_offers([normalize_offer(row(1, "0.11")), second], completeness=complete)
        self.assertEqual(forward["digest"], backward["digest"])
        self.assertNotEqual(forward["digest"], changed["digest"])


if __name__ == "__main__":
    unittest.main()
