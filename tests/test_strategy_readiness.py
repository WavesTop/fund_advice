import unittest

from backend.analysis.strategy_readiness import (
    aggregate_execution_readiness,
    assess_catalog_source,
    assess_relation_source,
    assess_strategy_series,
    investment_validation_readiness,
)


class StrategyReadinessTests(unittest.TestCase):
    def test_candidate_fact_cannot_drive_execution_or_performance(self):
        result = assess_strategy_series(kind="nav", source_id="fund_nav.eastmoney")
        self.assertFalse(result["eligible_for_signal"])
        self.assertFalse(result["eligible_for_execution"])
        self.assertFalse(result["eligible_for_performance"])
        self.assertIn("source_not_primary_evidence", result["signal_blockers"])
        self.assertIn("verified_total_return_missing", result["performance_blockers"])

    def test_primary_nav_can_support_price_risk_but_not_total_return_claim(self):
        result = assess_strategy_series(kind="nav", source_id="fund_nav.manager_documents")
        self.assertTrue(result["eligible_for_signal"])
        self.assertTrue(result["eligible_for_execution"])
        self.assertFalse(result["eligible_for_performance"])
        self.assertEqual(result["return_basis"], "unit_nav_without_verified_distributions")

    def test_relation_requires_registered_primary_evidence(self):
        legacy = assess_relation_source(source_id="fund_profile.eastmoney+csi_index.official")
        primary = assess_relation_source(source_id="fund_relations.manager_documents")
        self.assertFalse(legacy["eligible_for_execution"])
        self.assertIn("relation_source_not_registered", legacy["signal_blockers"])
        self.assertTrue(primary["eligible_for_execution"])

    def test_primary_source_cannot_be_reused_for_the_wrong_dataset(self):
        for source_id in (
            "fund_relations.manager_documents",
            "corporate_actions.manager_documents",
            "materials.official_publishers",
        ):
            with self.subTest(source_id=source_id):
                result = assess_strategy_series(kind="nav", source_id=source_id)
                self.assertFalse(result["eligible_for_execution"])
                self.assertIn("source_dataset_mismatch", result["signal_blockers"])

    def test_catalog_identity_requires_registered_primary_evidence(self):
        retired = assess_catalog_source(source_id="fund_catalog.eastmoney")
        primary = assess_catalog_source(source_id="fund_catalog.manager_documents")
        self.assertFalse(retired["eligible_for_execution"])
        self.assertIn("catalog_source_not_primary_evidence", retired["signal_blockers"])
        self.assertTrue(primary["eligible_for_execution"])

    def test_unknown_source_fails_closed_and_aggregates_codes(self):
        eligibility = assess_strategy_series(kind="price", source_id="unknown")
        item = {
            "code": "510001",
            "catalog_eligibility": assess_catalog_source(source_id="fund_catalog.manager_documents"),
            "data_eligibility": eligibility,
            "relation_eligibility": assess_relation_source(
                source_id="fund_relations.manager_documents",
            ),
        }
        current = aggregate_execution_readiness([item])
        historical = investment_validation_readiness([item])
        self.assertEqual(current["status"], "blocked")
        self.assertEqual(current["blocked_fund_codes"], ["510001"])
        self.assertEqual(historical["status"], "blocked")
        self.assertFalse(historical["investment_operations_enabled"])
        self.assertIn("immutable_source_evidence_chain_missing", historical["blockers"])
        self.assertIn("point_in_time_universe_missing", historical["blockers"])
        self.assertIn("verified_historical_trading_calendar_missing", historical["blockers"])
        self.assertIn("stable_investable_universe_policy_missing", historical["blockers"])
        self.assertIn("sector_research_signal_not_integrated", historical["blockers"])
        self.assertIn("risk_free_cash_return_series_missing", historical["blockers"])
        self.assertIn("atomic_pretrade_revalidation_missing", historical["blockers"])

    def test_registry_labels_alone_never_unlock_investment_operations(self):
        item = {
            "code": "510001",
            "catalog_eligibility": assess_catalog_source(
                source_id="fund_catalog.manager_documents",
            ),
            "data_eligibility": assess_strategy_series(
                kind="nav", source_id="fund_nav.manager_documents",
            ),
            "relation_eligibility": assess_relation_source(
                source_id="fund_relations.manager_documents",
            ),
        }
        self.assertTrue(item["catalog_eligibility"]["eligible_for_execution"])
        self.assertTrue(item["data_eligibility"]["eligible_for_execution"])
        self.assertTrue(item["relation_eligibility"]["eligible_for_execution"])
        result = investment_validation_readiness([item])
        self.assertEqual(result["status"], "blocked")
        self.assertFalse(result["investment_operations_enabled"])
        self.assertIn("immutable_source_evidence_chain_missing", result["blockers"])


if __name__ == "__main__":
    unittest.main()
