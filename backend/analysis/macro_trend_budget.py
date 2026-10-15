"""Dated LPR information joined to the existing price/risk budget rules."""
from datetime import date, datetime, timedelta
import math

from backend.analysis.trend_risk_budget import TrendBudgetPolicy, risk_budget_target

POLICIES = ("static40_vol10", "quality_vol10", "easing_vol10", "easing_confirmed_vol10", "easing_caution_vol10",
            "static50_vol10", "static30_vol10")


class LPRSeries:
    def __init__(self, rows):
        self.rows = []
        months = set()
        for source in rows:
            row = dict(source)
            announced = date.fromisoformat(row["announcement_date"])
            published = datetime.fromisoformat(row["displayed_published_at"])
            if published.utcoffset() != timedelta(hours=8):
                raise ValueError("LPR publication clock must have its Chinese local offset")
            visible = max(announced, published.date())
            if row["visible_date"] != visible.isoformat():
                raise ValueError("LPR visible date contradicts its announcement/publication")
            if announced < date(2019, 8, 20) or announced.strftime("%Y-%m") in months:
                raise ValueError("Pre-reform or duplicate monthly LPR quote")
            months.add(announced.strftime("%Y-%m"))
            values = [float(row[key]) for key in ("lpr_1y_pct", "lpr_5y_pct")]
            if any(not math.isfinite(v) or not 0 < v < 20 for v in values):
                raise ValueError("Invalid LPR level")
            row.update(announced=announced, visible=visible, level=sum(values) / 2)
            self.rows.append(row)
        self.rows.sort(key=lambda r: r["announced"])
        if not self.rows:
            raise ValueError("LPR history is empty")

    def state(self, asof, *, lag_days=1):
        if isinstance(lag_days, bool) or not isinstance(lag_days, int) or lag_days < 1:
            raise ValueError("LPR lag must be positive natural days")
        day = date.fromisoformat(asof)
        prior_day = day - timedelta(days=90)

        def latest(cutoff):
            return next((r for r in reversed(self.rows) if r["visible"] + timedelta(days=lag_days) <= cutoff), None)

        current, prior = latest(day), latest(prior_day)
        result = {"signal_date": asof, "lag_days": lag_days, "lookback_natural_days": 90,
                  "status": "ready" if current is not None and prior is not None else "insufficient_history",
                  "recent_easing": False, "change_percentage_points": None}
        for key, row in (("current", current), ("prior", prior)):
            result[key] = None if row is None else {"announcement_date": row["announcement_date"],
                                                   "visible_date": row["visible_date"], "level_pct": row["level"],
                                                   "source_url": row["source_url"]}
        if result["status"] == "ready":
            result["change_percentage_points"] = current["level"] - prior["level"]
            result["recent_easing"] = result["change_percentage_points"] < -1e-10
        return result


def macro_budget_target(data, name, index, held, state, *, stats):
    if name not in POLICIES or state["signal_date"] != data["days"][index]:
        raise ValueError("Unknown macro policy or mismatched signal date")
    stock_budget = .4
    driver = "static40"
    if name in ("static50_vol10", "static30_vol10"):
        stock_budget = .5 if name == "static50_vol10" else .3
    elif name == "quality_vol10":
        driver = "quality"
        stock_budget = .35 + .25 * stats["quality"]
    elif state["status"] == "ready" and state["recent_easing"]:
        if name == "easing_vol10" or (name == "easing_confirmed_vol10" and stats["quality"] > 0):
            stock_budget = .5
        elif name == "easing_caution_vol10" and stats["quality"] < 0:
            stock_budget = .3
    targets, meta = risk_budget_target(data, TrendBudgetPolicy(name, driver, .10), index, held,
                                       stats=stats, stock_budget_override=stock_budget)
    return targets, meta | {"macro": state}
