"""Pure contracts: do not require network, a user's database, or future prices."""
import copy
import unittest
from backend.analysis.research_view import attach_research_views, build_research_view, fund_associations

NOW = "2026-09-15T20:00:00+08:00"


def sample():
    return {"code": "IDX001", "name": "测试指数", "source_id": "fixture.index",
            "universe_type": "tracked_index", "collection_error": None,
            "funds": [{"code": "000001", "name": "测试基金", "relation_type": "tracked_index",
                       "relation_source_id": "fixture.prospectus", "evidence_url": "https://example.org/prospectus",
                       "verified_at": "2026-09-15T10:00:00Z"}],
            "periods": [{"id": "short", "status": "strong", "strength": {"eligible": True},
                         "opportunity": {"status": "watch", "missing": ["缺少催化证据"]}}]}


class ResearchViewTests(unittest.TestCase):
    def test_explicit_relation_keeps_identity_and_provenance(self):
        fund = fund_associations(sample(), generated_at=NOW)[0]
        self.assertEqual(fund["code"], "000001")
        self.assertEqual(fund["status"], "linked")
        self.assertEqual(fund["source_id"], "fixture.prospectus")
        self.assertIn("不代表", fund["limitations"][0])

    def test_named_board_never_infers_fund_relation(self):
        item = sample()
        item["universe_type"] = "hot_board"
        self.assertEqual(fund_associations(item, generated_at=NOW), [])

    def test_missing_provenance_withholds_not_defaults(self):
        for field in ("relation_type", "relation_source_id", "evidence_url", "verified_at"):
            with self.subTest(field=field):
                item = sample()
                del item["funds"][0][field]
                self.assertEqual(fund_associations(item, generated_at=NOW)[0]["status"], "withheld")

    def test_future_or_naive_timestamp_is_withheld(self):
        for timestamp in ("2026-09-16T00:00:00Z", "2026-09-15T10:00:00", "bad", None):
            with self.subTest(timestamp=timestamp):
                item = sample()
                item["funds"][0]["verified_at"] = timestamp
                self.assertEqual(fund_associations(item, generated_at=NOW)[0]["status"], "withheld")

    def test_equivalent_timezone_is_allowed(self):
        item = sample()
        item["funds"][0]["verified_at"] = "2026-09-15T20:00:00+08:00"
        self.assertEqual(fund_associations(item, generated_at=NOW)[0]["status"], "linked")

    def test_unsafe_source_is_not_renderable(self):
        for url in ("javascript:alert(1)", "//example.org/a", "file:///secret", "https://name:pass@example.org/a", "https://[bad"):
            with self.subTest(url=url):
                item = sample()
                item["funds"][0]["evidence_url"] = url
                fund = fund_associations(item, generated_at=NOW)[0]
                self.assertEqual(fund["status"], "withheld")
                self.assertIsNone(fund["evidence_url"])

    def test_invalid_fund_identity_is_not_replaced(self):
        for code in ("123", "０００００１", None, "abc123"):
            with self.subTest(code=code):
                item = sample()
                item["funds"][0]["code"] = code
                self.assertEqual(fund_associations(item, generated_at=NOW), [])

    def test_current_screen_does_not_enable_recommendation(self):
        item = sample()
        item["periods"][0]["opportunity"]["missing"] = []
        view = build_research_view(item, generated_at=NOW)
        self.assertEqual(view["periods"][0]["recommendation_status"], "not_evaluated")
        self.assertEqual(view["operation_status"], "unavailable")
        self.assertNotIn("amount", view)
        self.assertNotIn("score", view)

    def test_failed_collection_suspends_comparison_keeps_counter_evidence(self):
        item = sample()
        item["collection_error"] = "network failed"
        item["periods"][0]["opportunity"]["status"] = "risk"
        state = build_research_view(item, generated_at=NOW)["periods"][0]
        self.assertFalse(state["comparison_available"])
        self.assertFalse(state["market_available"])
        self.assertEqual(state["evidence_state"], "risk")
        self.assertEqual(state["gaps"][-1], "缺少催化证据")

    def test_stale_insufficient_prices_not_comparable(self):
        for status in ("stale", "insufficient"):
            with self.subTest(status=status):
                item = sample()
                item["periods"][0]["status"] = status
                state = build_research_view(item, generated_at=NOW)["periods"][0]
                self.assertFalse(state["comparison_available"])

    def test_price_availability_does_not_imply_relative_comparability(self):
        item = sample()
        item["periods"][0]["strength"] = {"eligible": False}
        state = build_research_view(item, generated_at=NOW)["periods"][0]
        self.assertTrue(state["market_available"])
        self.assertFalse(state["comparison_available"])

    def test_conflict_is_not_averaged_away(self):
        item = sample()
        item["periods"][0]["opportunity"]["status"] = "conflict"
        self.assertEqual(build_research_view(item, generated_at=NOW)["periods"][0]["evidence_state"], "conflict")

    def test_same_code_different_sources_have_distinct_keys(self):
        left, right = sample(), sample()
        right["source_id"] = "other.index"
        self.assertNotEqual(build_research_view(left, generated_at=NOW)["subject_key"], build_research_view(right, generated_at=NOW)["subject_key"])

    def test_builder_does_not_mutate_source(self):
        item = sample()
        before = copy.deepcopy(item)
        build_research_view(item, generated_at=NOW)
        self.assertEqual(item, before)

    def test_attachment_is_additive(self):
        items = [sample()]
        before = copy.deepcopy(items[0]["funds"])
        attach_research_views(items, generated_at=NOW)
        self.assertEqual(items[0]["funds"], before)
        self.assertEqual(items[0]["research"]["version"], "research-view-v1")

    def test_invalid_run_timestamp_rejected(self):
        with self.assertRaises(ValueError):
            build_research_view(sample(), generated_at="2026-09-15")

    def test_unknown_state_degrades_to_insufficient(self):
        item = sample()
        item["periods"][0]["opportunity"]["status"] = "buy"
        self.assertEqual(build_research_view(item, generated_at=NOW)["periods"][0]["evidence_state"], "insufficient")


if __name__ == '__main__':
    unittest.main()
