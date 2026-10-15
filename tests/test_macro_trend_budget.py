from types import SimpleNamespace
import unittest

from backend.analysis.macro_trend_budget import LPRSeries, macro_budget_target
from backend.analysis.trend_risk_budget import TrendBudgetPolicy, budget_statistics, risk_budget_target
from scripts.collect_lpr_announcements import parse_announcement
from scripts.research_macro_trend_budget import audit_macro_decision
from scripts.research_local_trend_models import prepare_local
from tests import test_local_trend_models as model_fixtures
from tests.test_trend_risk_budget import calendar_fixture


def fixture():
    calendar = calendar_fixture()
    funds, codes = model_fixtures.LocalTrendModelTests.funds(SimpleNamespace(calendar=calendar))
    return prepare_local(funds, codes, calendar)


def quote(day, one=4.25, five=4.85, published=None):
    return {"announcement_date": day, "displayed_published_at": f"{published or day}T09:15:00+08:00",
            "visible_date": max(day, published or day), "lpr_1y_pct": one, "lpr_5y_pct": five,
            "source_url": "https://www.pbc.gov.cn/example"}


class MacroTrendTests(unittest.TestCase):
    def test_release_lag_and_no_future_read(self):
        rows = [quote("2019-08-20"), quote("2019-11-20", 4.15, 4.80)]
        history = LPRSeries(rows)
        self.assertFalse(history.state("2019-11-20")["recent_easing"])
        self.assertTrue(history.state("2019-11-21")["recent_easing"])
        self.assertFalse(history.state("2019-11-21", lag_days=7)["recent_easing"])
        future = LPRSeries(rows + [quote("2020-02-20", 2.0, 3.0)])
        self.assertEqual(history.state("2019-11-21"), future.state("2019-11-21"))

    def test_late_publication_does_not_overwrite_newer_quote(self):
        history = LPRSeries([quote("2019-08-20", published="2019-12-01"), quote("2019-09-20", 4.20, 4.85)])
        self.assertEqual(history.state("2019-12-02")["current"]["announcement_date"], "2019-09-20")
        self.assertEqual(history.state("2019-09-20")["status"], "insufficient_history")

    def test_missing_prior_and_bad_metadata(self):
        history = LPRSeries([quote("2019-08-20")])
        self.assertEqual(history.state("2019-10-20")["status"], "insufficient_history")
        for rows in ([quote("2019-08-20", one=float("nan"))], [quote("2019-08-20")] * 2,
                     [quote("2019-08-19")], [quote("2019-08-20") | {"visible_date": "2019-08-19"}],
                     [quote("2019-08-20") | {"displayed_published_at": "2019-08-20T09:00:00"}]):
            with self.assertRaises(ValueError):
                LPRSeries(rows)
        with self.assertRaises(ValueError):
            history.state("2019-11-20", lag_days=0)

    def test_original_body_and_publication_parse(self):
        html = """<h2>2024年4月22日全国银行间同业拆借中心受权公布贷款市场报价利率（LPR）公告</h2>
        <p>2024-04-22 09:15:00</p><div class='content'>中国人民银行授权，2024年4月22日贷款市场报价利率（LPR）为：1年期LPR为3.45%，5年期以上LPR为3.95%。</div>"""
        row = parse_announcement(html.encode(), "https://www.pbc.gov.cn/example")
        self.assertEqual(row["lpr_1y_pct"], 3.45)
        self.assertEqual(row["displayed_published_at"], "2024-04-22T09:15:00+08:00")
        spaced = html.replace("2024年4月22日贷款", "2024 年 4 月 22 日贷款").replace("1年期LPR为", "1 年期 LPR 为 ")
        self.assertEqual(parse_announcement(spaced.encode(), "test")["lpr_1y_pct"], 3.45)
        combined = html.replace("（LPR）公告", "（LPR）及调整发布时间公告")
        self.assertEqual(parse_announcement(combined.encode(), "test")["lpr_5y_pct"], 3.95)
        with self.assertRaises(ValueError):
            parse_announcement(html.replace("09:15:00", "").encode(), "test")
        with self.assertRaises(ValueError):
            parse_announcement(html.replace("1年期LPR为3.45%", "1年期LPR为3.45%，1年期LPR为3.50%").encode(), "test")

    def test_overlay_and_controls_share_risk_engine(self):
        data = fixture()
        stats = budget_statistics(data, 120)
        state = {"signal_date": data["days"][120], "status": "ready", "recent_easing": True}
        for quality, confirmed, caution in ((.5, .5, .4), (-.5, .4, .3), (0., .4, .4)):
            current = stats | {"quality": quality}
            for name, desired in (("easing_vol10", .5), ("easing_confirmed_vol10", confirmed), ("easing_caution_vol10", caution)):
                targets, meta = macro_budget_target(data, name, 120, (), state, stats=current)
                self.assertAlmostEqual(meta["proposed_stock_budget"], desired)
                expected, _ = risk_budget_target(data, TrendBudgetPolicy("reference", "static40", .10), 120, (),
                                                 stats=current, stock_budget_override=desired)
                self.assertEqual(targets, expected)
        base, _ = macro_budget_target(data, "static40_vol10", 120, (), state, stats=stats)
        expected, _ = risk_budget_target(data, TrendBudgetPolicy("base", "static40", .10), 120, (), stats=stats)
        self.assertEqual(base, expected)
        for name, desired in (("static50_vol10", .5), ("static30_vol10", .3)):
            target, meta = macro_budget_target(data, name, 120, (), state, stats=stats)
            self.assertAlmostEqual(meta["proposed_stock_budget"], desired)
            expected, _ = risk_budget_target(data, TrendBudgetPolicy("reference", "static40", .10), 120, (),
                                             stats=stats, stock_budget_override=desired)
            self.assertEqual(target, expected)
        unknown = state | {"status": "insufficient_history"}
        _, meta = macro_budget_target(data, "easing_vol10", 120, (), unknown, stats=stats)
        self.assertEqual(meta["proposed_stock_budget"], .4)

    def test_override_invalid_and_signal_dates(self):
        data = fixture()
        stats = budget_statistics(data, 120)
        for desired in (float("nan"), -.1, .9):
            with self.assertRaises(ValueError):
                risk_budget_target(data, TrendBudgetPolicy("x"), 120, (), stats=stats, stock_budget_override=desired)
        with self.assertRaises(ValueError):
            macro_budget_target(data, "easing_vol10", 120, (), {"signal_date": "wrong"}, stats=stats)

    def test_independent_visibility_audit_rejects_tampering(self):
        rows = [quote("2019-08-20"), quote("2019-11-20", 4.15, 4.80)]
        state = LPRSeries(rows).state("2019-11-21")
        entry = {"signal_date": "2019-11-21", "macro": state, "quality": .5, "proposed_stock_budget": .5}
        audit_macro_decision(entry, rows, "easing_confirmed_vol10")
        for damaged in (entry | {"macro": state | {"recent_easing": False}},
                        entry | {"macro": state | {"change_percentage_points": float("nan")}},
                        entry | {"macro": state | {"current": state["current"] | {"announcement_date": "2020-01-20"}}},
                        entry | {"proposed_stock_budget": float("nan")},
                        entry | {"proposed_stock_budget": .6}):
            with self.assertRaises(ValueError):
                audit_macro_decision(damaged, rows, "easing_confirmed_vol10")


if __name__ == "__main__":
    unittest.main()
