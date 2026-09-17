"""Same-benchmark passive fund comparison without arbitrary composite scores.

A Pareto shortlist is an experimental product comparison, NOT a buy instruction.
Annual fees are compared as terms; NAV returns already embed ongoing expenses.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from decimal import Decimal
from statistics import median
import json

from backend.core.trading_calendar import TradingCalendar, day


def blocked_subjects(manifest: dict) -> set[str]:
    return ({row["subject_key"] for row in manifest.get("excluded", []) if row.get("kind") == "quality"}
            | {json.loads(key)[0] for key in manifest.get("source_conflicts", [])})


def _points(facts: list[dict], subject: str, kind: str) -> list[dict]:
    rows = [fact for fact in facts if fact["subject_key"] == subject and fact["dataset_kind"] == "series"
            and fact["series_kind"] == kind and fact["semantic_status"] == "verified"]
    if len({row["source_id"] for row in rows}) > 1 or len({row["effective_date"] for row in rows}) != len(rows):
        raise ValueError("同对象序列存在来源或版本冲突，不能拼接择优")
    return sorted(rows, key=lambda row: row["effective_date"])


def _tracking(fund: list[dict], benchmark: list[dict]) -> dict:
    if [row["effective_date"] for row in fund] != [row["effective_date"] for row in benchmark]:
        raise ValueError("基金与基准必须使用完全相同的日期序列")
    f = [Decimal(row["value"]) for row in fund]
    b = [Decimal(row["value"]) for row in benchmark]
    active = [(f[i] / f[i-1] - b[i] / b[i-1]) for i in range(1, len(f))]
    if len(active) < 2:
        raise ValueError("跟踪误差至少需要两段收益")
    mean = sum(active) / len(active)
    variance = sum((value - mean) ** 2 for value in active) / (len(active) - 1)
    difference = f[-1] / f[0] - b[-1] / b[0]
    return {"tracking_difference_pct": str(difference * 100),
            "tracking_error_annualized_pct": str(variance.sqrt() * Decimal(252).sqrt() * 100),
            "observations": len(active), "annualization_convention": 252,
            "start": fund[0]["effective_date"], "end": fund[-1]["effective_date"]}


def compare_passive_funds(manifest: dict, benchmark_key: str, *, lookback: int = 60) -> dict:
    if isinstance(lookback, bool) or not isinstance(lookback, int) or not 20 <= lookback <= 252:
        raise ValueError("基金比较窗口必须为20至252个交易日；不是投资效果门槛")
    facts = manifest["facts"]
    calendar = TradingCalendar(manifest["calendar"])
    cutoff = datetime.fromisoformat(manifest["cutoff"].replace("Z", "+00:00"))
    end = calendar.latest_completed(cutoff)
    output = {"benchmark_key": benchmark_key, "lookback_sessions": lookback, "items": [],
              "status": "insufficient", "recommendation_status": "unvalidated",
              "cost_comparison": "annual_terms_only_not_investor_total_cost",
              "limitations": ["只比较同指数、同类别的被动产品；候选不代表投资观点已通过验证。",
                              "未接入共享费用试算、交易执行和个人约束；不输出金额或买卖指令。",
                              "净值收益已含持续费用，未再扣一次年费；折溢价不是未来收益。"]}
    blocked = blocked_subjects(manifest)
    try:
        if benchmark_key in blocked:
            raise ValueError("基准存在被隔离版本或来源冲突，禁止回退旧值进行比较")
        benchmark = [row for row in _points(facts, benchmark_key, "total_return") if day(row["effective_date"]) <= end][-lookback-1:]
        quality = calendar.validate_grid([row["effective_date"] for row in benchmark], end=end, minimum=lookback + 1)
        if quality["status"] != "ready":
            raise ValueError("基准总收益数据不完整或非最新完整交易日")
    except ValueError as exc:
        output["benchmark_error"] = str(exc)
        benchmark = []
    profiles = defaultdict(list)
    for fact in facts:
        if fact["dataset_kind"] == "fund_profile":
            profiles[fact["subject_key"]].append(fact)
    comparable = []
    for subject, versions in sorted(profiles.items()):
        latest_date = max(row["effective_date"] for row in versions)
        latest = [row for row in versions if row["effective_date"] == latest_date]
        if len(latest) != 1:
            output["items"].append({"subject_key": subject, "status": "insufficient", "reasons": ["基金身份或费率存在多来源冲突"]})
            continue
        profile = latest[0]
        reasons = ["基金存在被隔离版本或来源冲突，禁止悄悄退回旧状态"] if subject in blocked else []
        item = {"subject_key": subject, "code": profile["fund_code"], "name": profile["name"],
                "portfolio_id": profile["portfolio_id"], "category": profile["category"],
                "profile_revision_id": profile["revision_id"], "status": "insufficient", "reasons": reasons}
        output["items"].append(item)
        if profile["benchmark_key"] != benchmark_key:
            item.update(status="excluded", reasons=["跟踪基准不同，不放入同一比较组"])
            continue
        if profile["category"] not in ("passive_etf", "passive_otc"):
            item.update(status="excluded", reasons=["主动基金归因或ETF联接穿透尚未验收，不用被动比较代替"])
            continue
        if profile["semantic_status"] != "verified":
            reasons.append("基金身份/基准/费率尚未核验")
        if profile["subscription_status"] != "open":
            reasons.append("交易状态非明确可用，限购不能视为无限申购")
        if day(profile["effective_date"]) < end:
            reasons.append("基金交易状态未覆盖最新完整交易日，旧开放状态不能当作当前可用")
        if profile["annual_fee_bps"] is None:
            reasons.append("持续费率未知，不能记为零")
        if not benchmark:
            reasons.append(output["benchmark_error"])
        if reasons:
            continue
        try:
            fund = [row for row in _points(facts, subject, "total_return") if day(row["effective_date"]) <= end][-lookback-1:]
            quality = calendar.validate_grid([row["effective_date"] for row in fund], end=end, minimum=lookback + 1)
            if quality["status"] != "ready":
                raise ValueError("基金总收益数据缺失、日期不齐或过期")
            metrics = _tracking(fund, benchmark)
            item["input_revision_ids"] = [profile["revision_id"], *[row["revision_id"] for row in fund], *[row["revision_id"] for row in benchmark]]
            costs = [abs(Decimal(metrics["tracking_difference_pct"])), Decimal(metrics["tracking_error_annualized_pct"]), Decimal(profile["annual_fee_bps"])]
            metrics["annual_fee_bps"] = profile["annual_fee_bps"]
            if profile["category"] == "passive_etf":
                price = [row for row in _points(facts, subject, "price") if day(row["effective_date"]) <= end][-20:]
                nav = [row for row in _points(facts, subject, "nav") if day(row["effective_date"]) == end]
                if (calendar.validate_grid([row["effective_date"] for row in price], end=end, minimum=20)["status"] != "ready"
                        or len(nav) != 1 or any(row["amount"] is None for row in price)):
                    raise ValueError("ETF缺少同日价格/净值或完整20日成交额")
                premium = Decimal(price[-1]["value"]) / Decimal(nav[0]["value"]) - 1
                turnover = median(Decimal(row["amount"]) for row in price)
                if turnover <= 0:
                    raise ValueError("ETF缺少有效成交活跃度")
                metrics.update(premium_pct=str(premium * 100), median_amount_20=str(turnover))
                costs.extend([abs(premium), -turnover])
                item["input_revision_ids"].extend(row["revision_id"] for row in price + nav)
            item.update(status="comparable", metrics=metrics)
            comparable.append((item, costs))
        except ValueError as exc:
            reasons.append(str(exc))
    for item, costs in comparable:
        dominating = [other["code"] for other, factors in comparable if other["category"] == item["category"]
                      and all(a <= b for a, b in zip(factors, costs)) and any(a < b for a, b in zip(factors, costs))]
        item.update(status="dominated" if dominating else "research_candidate", dominated_by=dominating,
                    reasons=["同组至少一个产品在全部已比较维度不差且至少一项更好"] if dominating else ["同组未被全面优于的产品；存在取舍，未生成综合分数"])
    output["status"] = "experimental_comparison" if comparable else "insufficient"
    portfolios = defaultdict(list)
    for item in output["items"]:
        if item.get("portfolio_id"):
            portfolios[item["portfolio_id"]].append(item["code"])
    output["share_class_groups"] = dict(portfolios)
    return output
