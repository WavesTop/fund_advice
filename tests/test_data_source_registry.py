import copy
import json
from pathlib import Path
import tempfile
import unittest

from scripts.data_source_registry import (
    RegistryError,
    load_registry,
    replacement_preserves_quality,
    sources_for,
)


class DataSourceRegistryTests(unittest.TestCase):
    def test_checked_in_registry_is_valid_and_orders_automatic_quotes(self):
        registry = load_registry()
        self.assertEqual(registry["schema_version"], 1)
        self.assertEqual(registry["policy_version"], "d0-2026-09-20.4")
        sources = sources_for("exchange_daily", "candidate_fact")
        self.assertEqual([source["provider"] for source in sources],
                         ["sina", "tencent", "baostock"])
        self.assertTrue(all(source["automatic"] for source in sources))

    def test_discovery_and_authoritative_sources_are_kept_separate(self):
        actions = sources_for("corporate_actions")
        by_provider = {source["provider"]: source for source in actions}
        self.assertEqual(by_provider["eastmoney"]["decision"], "rejected")
        self.assertEqual(by_provider["fund_manager"]["decision"], "authoritative_evidence")
        self.assertEqual(by_provider["eastmoney"]["allowed_uses"], [])

    def test_catalog_has_manual_authoritative_source_without_adapter(self):
        catalogue = {source["id"]: source for source in sources_for("fund_catalog")}
        manager = catalogue["fund_catalog.manager_documents"]
        self.assertEqual(manager["decision"], "authoritative_evidence")
        self.assertEqual(manager["allowed_uses"], ["primary_evidence"])
        self.assertFalse(manager["automatic"])
        self.assertIsNone(manager["adapter"])

    def test_tonghuashun_registry_matches_only_implemented_capabilities(self):
        registry = load_registry()
        by_id = {source["id"]: source for source in registry["sources"]}
        daily = by_id["sector_daily.ths"]
        self.assertEqual((daily["dataset"], daily["decision"], daily["automatic"]),
                         ("sector_daily", "conditional", True))
        self.assertEqual(daily["adapter"], "scripts.import_sector_heat.fetch_ths_sector_daily")
        membership = by_id["sector_membership.ths"]
        self.assertEqual((membership["dataset"], membership["decision"], membership["automatic"]),
                         ("sector_membership", "conditional", True))
        self.assertEqual(membership["adapter"], "scripts.import_sector_heat.fetch_ths_visible_constituents")
        profile = by_id["fund_profile.ths"]
        self.assertEqual((profile["dataset"], profile["decision"], profile["automatic"]),
                         ("fund_profile", "discovery_only", False))
        self.assertEqual(profile["allowed_uses"], ["discovery", "cross_check"])
        self.assertNotIn("primary_evidence", profile["allowed_uses"])
        nav = by_id["fund_nav.ths"]
        self.assertEqual((nav["dataset"], nav["decision"], nav["automatic"]),
                         ("fund_nav", "conditional", True))
        self.assertEqual(nav["allowed_uses"], ["candidate_fact", "cross_check"])
        self.assertEqual(nav["adapter"], "scripts.fund_nav_ths.fetch_ths_fund_nav")
        cni_relation = by_id["fund_profile.ths+cni_index.official"]
        self.assertEqual((cni_relation["dataset"], cni_relation["decision"], cni_relation["automatic"]),
                         ("fund_relations", "conditional", True))
        self.assertEqual(cni_relation["allowed_uses"], ["discovery", "candidate_fact", "cross_check"])
        self.assertNotIn("primary_evidence", cni_relation["allowed_uses"])
        csi_relation = by_id["fund_profile.ths+csi_index.official"]
        self.assertEqual((csi_relation["dataset"], csi_relation["decision"], csi_relation["automatic"]),
                         ("fund_relations", "conditional", True))
        csi_index = by_id["index_daily.csi_official"]
        self.assertEqual((csi_index["dataset"], csi_index["decision"], csi_index["automatic"]),
                         ("index_daily", "authoritative_evidence", True))
        self.assertIn("primary_evidence", csi_index["allowed_uses"])
        sina_index = by_id["index_daily.sina"]
        self.assertEqual((sina_index["dataset"], sina_index["decision"], sina_index["automatic"]),
                         ("index_daily", "conditional", True))
        fundamentals = by_id["financials.ths_constituent_sample"]
        self.assertEqual(fundamentals["decision"], "not_verified")
        self.assertEqual(fundamentals["allowed_uses"], [])
        self.assertFalse(fundamentals["automatic"])
        self.assertIsNone(fundamentals["adapter"])

    def test_all_eastmoney_sources_are_retired_from_collection(self):
        registry = load_registry()
        retired = [source for source in registry["sources"] if source["provider"] == "eastmoney"]
        self.assertTrue(retired)
        for source in retired:
            self.assertEqual(source["decision"], "rejected")
            self.assertEqual(source["allowed_uses"], [])
            self.assertFalse(source["automatic"])
            self.assertIsNone(source["adapter"])
            self.assertIsNone(source["priority"])

    def test_rejected_sources_cannot_enter_automatic_selection(self):
        rejected = [source for source in sources_for("exchange_daily") if source["decision"] == "rejected"]
        self.assertEqual({source["provider"] for source in rejected}, {"ths", "netease", "eastmoney"})
        self.assertTrue(all(not source["automatic"] and source["priority"] is None for source in rejected))

    def test_invalid_automatic_retired_source_is_rejected(self):
        registry = load_registry()
        broken = copy.deepcopy(registry)
        source = next(item for item in broken["sources"] if item["provider"] == "eastmoney")
        source["automatic"], source["priority"], source["adapter"] = True, 1, "retired.adapter"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "registry.json"
            path.write_text(json.dumps(broken, ensure_ascii=False), encoding="utf-8")
            with self.assertRaises(RegistryError):
                load_registry(path)

    def test_retired_aliases_cannot_bypass_registry_guard(self):
        registry = load_registry()
        for field, value in (
            ("provider", "东财"),
            ("id", "exchange_daily.east-money"),
            ("adapter", "https://emdata.example/adapter"),
            ("adapter", "https://push2.example/adapter"),
            ("adapter", "https://datacenter-web.example/adapter"),
        ):
            with self.subTest(field=field, value=value):
                broken = copy.deepcopy(registry)
                source = next(item for item in broken["sources"] if item["id"] == "exchange_daily.sina")
                source[field] = value
                with tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / "registry.json"
                    path.write_text(json.dumps(broken, ensure_ascii=False), encoding="utf-8")
                    with self.assertRaises(RegistryError):
                        load_registry(path)

    def test_unknown_use_is_rejected(self):
        with self.assertRaises(RegistryError):
            sources_for("fund_nav", "production_truth")

    def test_candidate_sources_cannot_downgrade_primary_evidence(self):
        registry = load_registry()
        self.assertFalse(replacement_preserves_quality(
            registry, "fund_nav.manager_documents", "fund_nav.ths", dataset="fund_nav"
        ))
        self.assertTrue(replacement_preserves_quality(
            registry, "fund_nav.eastmoney", "fund_nav.ths", dataset="fund_nav"
        ))
        self.assertFalse(replacement_preserves_quality(
            registry, "index_daily.csi_official", "index_daily.sina", dataset="index_daily"
        ))


if __name__ == "__main__":
    unittest.main()
