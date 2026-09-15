import copy
import json
from pathlib import Path
import tempfile
import unittest

from scripts.data_source_registry import RegistryError, load_registry, sources_for


class DataSourceRegistryTests(unittest.TestCase):
    def test_checked_in_registry_is_valid_and_orders_automatic_quotes(self):
        registry = load_registry()
        self.assertEqual(registry["schema_version"], 1)
        self.assertEqual(registry["policy_version"], "d0-2026-09-13.1")
        sources = sources_for("exchange_daily", "candidate_fact")
        self.assertEqual([source["provider"] for source in sources],
                         ["sina", "tencent", "baostock", "eastmoney"])
        self.assertTrue(all(source["automatic"] for source in sources))

    def test_discovery_and_authoritative_sources_are_kept_separate(self):
        actions = sources_for("corporate_actions")
        by_provider = {source["provider"]: source for source in actions}
        self.assertEqual(by_provider["eastmoney"]["decision"], "discovery_only")
        self.assertEqual(by_provider["fund_manager"]["decision"], "authoritative_evidence")
        self.assertNotIn("primary_evidence", by_provider["eastmoney"]["allowed_uses"])

    def test_rejected_sources_cannot_enter_automatic_selection(self):
        rejected = [source for source in sources_for("exchange_daily") if source["decision"] == "rejected"]
        self.assertEqual({source["provider"] for source in rejected}, {"ths", "netease"})
        self.assertTrue(all(not source["automatic"] and source["priority"] is None for source in rejected))

    def test_invalid_automatic_discovery_source_is_rejected(self):
        registry = load_registry()
        broken = copy.deepcopy(registry)
        source = next(item for item in broken["sources"] if item["decision"] == "discovery_only")
        source["automatic"], source["priority"] = True, 1
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "registry.json"
            path.write_text(json.dumps(broken, ensure_ascii=False), encoding="utf-8")
            with self.assertRaises(RegistryError):
                load_registry(path)

    def test_unknown_use_is_rejected(self):
        with self.assertRaises(RegistryError):
            sources_for("fund_nav", "production_truth")


if __name__ == "__main__":
    unittest.main()
