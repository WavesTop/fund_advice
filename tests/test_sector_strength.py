import unittest

from backend.analysis.sector_strength import attach_strength, build_advantage_summary


def board(code, returns, *, day="2026-09-14", start="2026-08-17", state="strong", error=None, universe="hot_board"):
    return {"code": code, "as_of": day, "collection_error": error, "universe_type": universe,
            "periods": [{"id": key, "return_pct": value, "status": state, "reason": "数据不足",
                         "observation_start": start} for key, value in zip(("short", "medium", "long"), returns)]}


class SectorStrengthTests(unittest.TestCase):
    def test_each_horizon_uses_its_own_return_ranking(self):
        items = [board("a", [3, -4, 6]), board("b", [-1, 7, 2]), board("c", [1, 1, -1])]
        attach_strength(items, "2026-09-14")
        self.assertEqual([p["strength"]["rank"] for p in items[0]["periods"]], [1, 3, 1])
        self.assertEqual([p["strength"]["percentile"] for p in items[0]["periods"]], [100, 0, 100])

    def test_ties_share_rank_and_midpoint_percentile(self):
        items = [board("a", [2] * 3), board("b", [2] * 3), board("c", [-2] * 3)]
        attach_strength(items, "2026-09-14")
        self.assertEqual([i["periods"][0]["strength"]["rank"] for i in items], [1, 1, 3])
        self.assertEqual([i["periods"][0]["strength"]["percentile"] for i in items], [75, 75, 0])
        self.assertEqual(items[0]["periods"][0]["strength"]["tied_count"], 2)

    def test_stale_failed_and_misaligned_rows_are_not_promoted(self):
        items = [board("ok", [1] * 3), board("old", [99] * 3, day="2026-09-11"),
                 board("missing", [None] * 3, state="insufficient"),
                 board("failed", [99] * 3, error="下载失败")]
        attach_strength(items, "2026-09-14")
        self.assertEqual(items[0]["periods"][0]["strength"]["sample_count"], 1)
        self.assertIsNone(items[0]["periods"][0]["strength"]["percentile"])
        self.assertTrue(all(not i["periods"][0]["strength"]["eligible"] for i in items[1:]))

    def test_common_start_prevents_comparing_different_intervals(self):
        items = [board("a", [1] * 3), board("b", [2] * 3), board("gap", [99] * 3, start="2026-08-10")]
        attach_strength(items, "2026-09-14")
        self.assertFalse(items[2]["periods"][0]["strength"]["eligible"])
        self.assertEqual(items[1]["periods"][0]["strength"]["rank"], 1)
        self.assertEqual(items[1]["periods"][0]["strength"]["sample_count"], 2)

    def test_reference_indexes_do_not_change_heat_universe_rank(self):
        items = [board("a", [-5] * 3), board("b", [-3] * 3), board("reference", [99] * 3, universe="tracked_index")]
        attach_strength(items, "2026-09-14")
        self.assertEqual(items[1]["periods"][0]["strength"]["rank"], 1)
        self.assertEqual(items[1]["periods"][0]["strength"]["sample_count"], 2)
        self.assertEqual(items[1]["periods"][0]["return_pct"], -3)

    def test_advantage_summary_requires_top_quartile_and_nonweak_structure(self):
        items = [board("leader", [8, 8, 8]), board("repair", [-2, -2, -2], state="neutral"),
                 board("weak", [-3, -3, -3], state="weak"), board("laggard", [-8, -8, -8], state="neutral")]
        for item in items:
            item.update(name=item["code"], kind="行业", heat_rank=1, industry={}, valuation={})
            for period in item["periods"]:
                period.update(ma_bias_pct=2 if item["code"] == "repair" else 1,
                              max_drawdown_pct=-5, risk="normal")
        attach_strength(items, "2026-09-14")
        summaries = build_advantage_summary(items)
        self.assertEqual([row["name"] for row in summaries[0]["items"]], ["leader"])
        self.assertEqual(summaries[0]["items"][0]["label"], "趋势领先")

    def test_advantage_summary_labels_high_volatility_and_pullback_without_forecast(self):
        items = [board("volatile", [8, 8, 8]), board("pullback", [6, 6, 6], state="neutral"),
                 board("other", [1, 1, 1]), board("low", [0, 0, 0]), board("last", [-2, -2, -2])]
        for item in items:
            item.update(name=item["code"], kind="概念", heat_rank=1, industry={}, valuation={})
            for period in item["periods"]:
                period.update(ma_bias_pct=-1 if item["code"] == "pullback" else 2,
                              max_drawdown_pct=-15 if item["code"] == "volatile" else -5,
                              risk="elevated" if item["code"] == "volatile" else "normal")
        attach_strength(items, "2026-09-14")
        labels = [row["label"] for row in build_advantage_summary(items)[0]["items"]]
        self.assertEqual(labels, ["高波动领先", "领先后回落"])


if __name__ == "__main__":
    unittest.main()
