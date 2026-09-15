"""Cross-sectional price comparisons with an explicit common observation date."""
from __future__ import annotations
from collections import Counter
from collections.abc import Mapping

ADVANTAGE_LIMIT = 5


def build_advantage_summary(items: list[dict]) -> list[dict]:
    """Select explainable leaders after ranking; never turn relative rank into a forecast."""
    summaries = []
    names = {"short": ("短期", "未来1周至1个月重点观察"),
             "medium": ("中期", "未来1至3个月重点观察"),
             "long": ("长期", "未来3至6个月重点观察")}
    for period_id, (name, horizon) in names.items():
        candidates = []
        eligible_count = 0
        for item in items:
            if item.get("universe_type") != "hot_board":
                continue
            period = next((value for value in item["periods"] if value["id"] == period_id), None)
            if not period or not period.get("strength", {}).get("eligible"):
                continue
            eligible_count += 1
            strength = period["strength"]
            if (strength.get("percentile") or 0) < 75 or period["status"] not in ("strong", "neutral"):
                continue
            if period["status"] == "strong":
                tier = "high_volatility_leader" if period["risk"] == "elevated" else "trend_leader"
                label = "高波动领先" if period["risk"] == "elevated" else "趋势领先"
                reason = "区间收益为正、价格位于窗口均线上方，且相对强度进入前25%。"
            elif (period["return_pct"] or 0) <= 0 < (period["ma_bias_pct"] or 0):
                tier, label = "relative_recovery", "回撤中修复"
                reason = "区间仍有回撤，但价格已回到窗口均线上方，且相对表现进入前25%。"
            elif (period["return_pct"] or 0) > 0 >= (period["ma_bias_pct"] or 0):
                tier, label = "pullback_after_lead", "领先后回落"
                reason = "区间累计收益仍领先，但价格已落到窗口均线下方，优势正在减弱。"
            else:
                continue
            candidates.append({
                "code": item["code"], "name": item["name"], "kind": item.get("kind"),
                "label": label, "tier": tier, "reason": reason,
                "strength_rank": strength["rank"], "strength_percentile": strength["percentile"],
                "sample_count": strength["sample_count"], "return_pct": period["return_pct"],
                "ma_bias_pct": period["ma_bias_pct"], "max_drawdown_pct": period["max_drawdown_pct"],
                "risk": period["risk"], "heat_rank": item.get("heat_rank"),
                "industry_status": item.get("industry", {}).get("status", "missing"),
                "valuation_status": item.get("valuation", {}).get("status", "missing"),
            })
        candidates.sort(key=lambda value: (value["strength_rank"], value["code"]))
        selected = candidates[:ADVANTAGE_LIMIT]
        summaries.append({
            "id": period_id, "name": name, "horizon": horizon,
            "eligible_count": eligible_count, "candidate_count": len(selected), "items": selected,
            "comparison_as_of": next((period["strength"]["as_of"] for item in items
                                      if item.get("universe_type") == "hot_board"
                                      for period in item["periods"] if period["id"] == period_id
                                      and period.get("strength", {}).get("eligible")), None),
            "message": (f"从同日可比较的 {eligible_count} 个板块中筛出 {len(selected)} 个走势相对占优板块。"
                        if selected else f"当前 {eligible_count} 个可比较板块中，没有同时满足方向与相对强度条件的板块。"),
        })
    return summaries


def attach_strength(items: list[dict], ranking_as_of: str | Mapping[str, str | None] | None) -> None:
    """Rank within each universe; filtering in the UI must not change these ranks.

    The percentile describes observed return, never the chance of a future gain.
    Equal returns receive equal ranks and a midpoint percentile.
    """
    for period_id in ("short", "medium", "long"):
        for universe in ("hot_board", "tracked_index"):
            comparison_day = ranking_as_of.get(universe) if isinstance(ranking_as_of, Mapping) else ranking_as_of
            group = [item for item in items if item["universe_type"] == universe]
            entries = [(item, next(p for p in item["periods"] if p["id"] == period_id)) for item in group]
            eligible = []
            for item, period in entries:
                reason = ""
                if period["status"] in ("stale", "insufficient") or period["return_pct"] is None:
                    reason = period["reason"]
                elif item.get("collection_error"):
                    reason = "本次行情采集失败，保留旧数据供核对，不参与本次排名。"
                elif not comparison_day or item["as_of"] != comparison_day:
                    reason = "行情日期与本次比较日期不一致，暂不参与排名。"
                period["strength"] = {"rank": None, "percentile": None, "sample_count": 0, "tied_count": 0,
                                      "as_of": comparison_day, "eligible": not reason, "reason": reason}
                if not reason:
                    eligible.append(period)
            starts = Counter(p.get("observation_start") for p in eligible if p.get("observation_start"))
            common_start = max(starts, key=lambda day: (starts[day], day)) if starts else None
            aligned = []
            for period in eligible:
                period["strength"]["observation_start"] = common_start
                if not common_start or period.get("observation_start") != common_start:
                    period["strength"].update(eligible=False, reason="观察起点与同榜多数板块不一致，历史可能缺行，暂不参与排名。")
                else:
                    aligned.append(period)
            eligible = aligned
            count = len(eligible)
            values = [p["return_pct"] for p in eligible]
            for _, period in entries:
                period["strength"]["sample_count"] = count
            for period in eligible:
                value = period["return_pct"]
                below = sum(other < value for other in values)
                equal = sum(other == value for other in values)
                period["strength"].update(
                    rank=1 + sum(other > value for other in values),
                    tied_count=equal,
                    percentile=round(100 * (below + (equal - 1) / 2) / (count - 1), 1) if count > 1 else None,
                    reason="只有一个可比较样本，不能计算相对强度分位。" if count == 1 else "",
                )
