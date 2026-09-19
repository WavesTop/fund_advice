"""Real-fund universe selection and an auditable integrated strategy backtest."""
from __future__ import annotations

from collections import Counter
from decimal import Decimal, localcontext

from backend.analysis.simulation import SimulationCosts, simulate_strategy
from backend.analysis.strategy import ObservedSessionCalendar, StrategyPolicy
from backend.core.config import Settings
from backend.storage.database import connection_scope, migrate
from backend.storage.related_market import current_associations
from backend.storage.research import digest, utc_now
from backend.storage.timeseries import get_timeseries


VERSION = "integrated-fund-strategy-v2"
DATE_INTERSECTION_AUDIT_VERSION = "observed-source-date-intersection-v1"


def _number(value: Decimal) -> str:
    if not value.is_finite():
        raise ArithmeticError("历史验证出现非有限数值")
    rendered = format(value, "f")
    rendered = rendered.rstrip("0").rstrip(".") if "." in rendered else rendered
    return rendered if rendered and rendered != "-0" else "0"


def _rows(series: dict) -> list[dict[str, str]]:
    if series.get("kind") == "price":
        return [{"date": str(row["date"]), "close": str(row["close"])}
                for row in series.get("rows", []) if row.get("close") is not None]
    if series.get("kind") == "nav":
        return [{"date": str(row["date"]), "close": str(row["unit_nav"])}
                for row in series.get("rows", []) if row.get("unit_nav") is not None]
    return []


def eligible_fund_universe(settings: Settings) -> dict:
    """Use only current verified fund/index links and real stored price or NAV rows."""
    migrate(settings)
    generated_at = utc_now()
    with connection_scope(settings) as connection:
        associations = current_associations(connection, generated_at=generated_at)
    candidates, excluded = [], []
    for relation in associations:
        if relation.get("relation_status") != "linked":
            excluded.append({"code": relation["code"], "reason": relation.get("relation_reason") or "基金关系未通过当前核验"})
            continue
        series = get_timeseries(settings, str(relation["code"]))
        rows = _rows(series)
        if len(rows) < StrategyPolicy().long_window + 2:
            excluded.append({"code": relation["code"], "reason": f"真实价格或净值仅有{len(rows)}条，无法运行长期观察"})
            continue
        candidates.append({
            "code": relation["code"], "name": relation["name"],
            "index_code": relation["index_code"], "index_name": relation["index_name"],
            "relation_source_id": relation["relation_source_id"],
            "relation_evidence_url": relation["evidence_url"],
            "relation_verified_at": relation["verified_at"],
            "series_kind": series["kind"], "series_source_id": series["source_id"],
            "series_policy_version": series["policy_version"], "series_updated_at": series["updated_at"],
            "rows": rows, "observation_count": len(rows), "first_date": rows[0]["date"],
            "last_date": rows[-1]["date"],
        })
    # One actual product represents one tracked index in v1. A duplicate share is
    # not diversification. Prefer the longest usable history, then stable code.
    selected = []
    for index_code in sorted({str(item["index_code"]) for item in candidates}):
        group = [item for item in candidates if item["index_code"] == index_code]
        group.sort(key=lambda item: (-int(item["observation_count"]), str(item["code"])))
        selected.append(group[0])
        excluded.extend({"code": item["code"], "reason": f"与{group[0]['code']}跟踪同一指数，首版不重复配置风险敞口"}
                        for item in group[1:])
    return {"version": VERSION, "generated_at": generated_at, "items": selected,
            "excluded": excluded, "selection": "one_longest_history_fund_per_verified_index",
            "selection_temporality": "retrospective_current_snapshot"}


def _combine_curves(results: list[dict]) -> list[dict[str, str]]:
    dates = [point["date"] for point in results[0]["equity_curve"]]
    combined, peak = [], Decimal(0)
    initial = sum((Decimal(result["initial_equity"]) for result in results), Decimal(0))
    for index, value_date in enumerate(dates):
        points = [result["equity_curve"][index] for result in results]
        if any(
            point["date"] != value_date
            or Decimal(point["equity"]) != Decimal(point["cash"]) + Decimal(point["market_value"])
            for point in points
        ):
            raise ArithmeticError("分账户曲线日期或资产恒等式不成立")
        cash = sum((Decimal(point["cash"]) for point in points), Decimal(0))
        market_value = sum((Decimal(point["market_value"]) for point in points), Decimal(0))
        # Decimal applies its context after each addition.  Summing each
        # sleeve's equity and separately summing cash/market value can differ
        # in the final digit solely because those additions associate in a
        # different order.  Define the aggregate ledger identity directly.
        equity = cash + market_value
        if cash < 0 or market_value < 0 or equity != cash + market_value:
            raise ArithmeticError("综合策略资产恒等式不成立")
        peak = max(peak, equity)
        combined.append({"date": value_date, "equity": _number(equity), "cash": _number(cash),
                         "market_value": _number(market_value),
                         "return_pct": _number((equity / initial - 1) * 100),
                         "drawdown_pct": _number((equity / peak - 1) * 100)})
    return combined


def _benchmark(rows_by_code: dict[str, list[dict[str, str]]], allocations: dict[str, Decimal],
               fee_bps: Decimal) -> tuple[list[dict[str, str]], Decimal]:
    dates = [row["date"] for row in next(iter(rows_by_code.values()))]
    fee_rate = fee_bps / Decimal("10000")
    holdings = {}
    initial_fees = Decimal(0)
    for code, rows in rows_by_code.items():
        price = Decimal(rows[0]["close"])
        gross = allocations[code] / (Decimal(1) + fee_rate)
        # Fully invest the sleeve.  Repeating allocation-gross-gross*rate can
        # create a negative Decimal tail solely from division rounding.  The
        # residual allocation-gross is the exact, auditable first-day fee and
        # cash is defined as zero by the benchmark protocol.
        fee = allocations[code] - gross
        initial_fees += fee
        holdings[code] = {"units": gross / price, "cash": Decimal(0)}
    initial = sum(allocations.values(), Decimal(0))
    curve, peak = [], Decimal(0)
    for index, value_date in enumerate(dates):
        cash = sum((value["cash"] for value in holdings.values()), Decimal(0))
        market = sum((holdings[code]["units"] * Decimal(rows[index]["close"])
                      for code, rows in rows_by_code.items()), Decimal(0))
        equity = cash + market
        peak = max(peak, equity)
        curve.append({"date": value_date, "equity": _number(equity), "cash": _number(cash),
                      "market_value": _number(market),
                      "return_pct": _number((equity / initial - 1) * 100),
                      "drawdown_pct": _number((equity / peak - 1) * 100)})
    return curve, initial_fees


def _periods(curve: list[dict], trades: list[dict]) -> list[dict]:
    result = []
    for year in sorted({point["date"][:4] for point in curve}):
        points = [point for point in curve if point["date"].startswith(year)]
        start, end = Decimal(points[0]["equity"]), Decimal(points[-1]["equity"])
        year_trades = [trade for trade in trades if str(trade["execution_date"]).startswith(year)]
        result.append({"year": year, "start_date": points[0]["date"], "end_date": points[-1]["date"],
                       "return_pct": _number((end / start - 1) * 100), "trade_count": len(year_trades),
                       "actions": dict(Counter(str(trade["action"]) for trade in year_trades))})
    return result


def _date_intersection_audit(items: list[dict], common_dates: list[str]) -> dict:
    """Describe every date removed by the source intersection without inferring sessions."""
    date_sets = {
        str(item["code"]): {str(row["date"]) for row in item["rows"]}
        for item in items
    }
    coverage_start = max(min(values) for values in date_sets.values())
    coverage_end = min(max(values) for values in date_sets.values())
    covered = {
        code: {value for value in values if coverage_start <= value <= coverage_end}
        for code, values in date_sets.items()
    }
    union_dates = sorted(set().union(*covered.values())) if coverage_start <= coverage_end else []
    derived_common = sorted(set.intersection(*covered.values())) if coverage_start <= coverage_end else []
    if derived_common != common_dates:
        raise ArithmeticError("基金日期交集与共同覆盖区间不一致")
    excluded_dates = sorted(set(union_dates) - set(common_dates))
    excluded_set = set(excluded_dates)
    by_code = {str(item["code"]): item for item in items}
    sources = []
    for code in sorted(covered):
        observed = covered[code]
        missing_dates = sorted(excluded_set - observed)
        excluded_by_intersection = sorted(observed - set(common_dates))
        sources.append({
            "code": code,
            "series_source_id": by_code[code].get("series_source_id"),
            "observations_in_coverage": len(observed),
            "missing_observations": len(missing_dates),
            "missing_dates": missing_dates,
            "excluded_by_intersection_observations": len(excluded_by_intersection),
            "excluded_by_intersection_dates": excluded_by_intersection,
        })
    return {
        "version": DATE_INTERSECTION_AUDIT_VERSION,
        "basis": "union_of_observed_source_dates_within_shared_coverage",
        "coverage_start": coverage_start,
        "coverage_end": coverage_end,
        "source_count": len(sources),
        "union_observations": len(union_dates),
        "common_observations": len(common_dates),
        "intersection_excluded_observations": len(excluded_dates),
        "intersection_excluded_dates": excluded_dates,
        "sources": sources,
        "limitations": [
            "仅把共同覆盖区间内至少一个来源实际记录的日期作为候选日期，不推定交易日。",
            "所有来源在同一天同时缺失时，来源日期交集审计无法识别该日期。",
        ],
    }


def _historical_strategy_report(settings: Settings, *, initial_cash: str = "100000",
                                buy_fee_bps: str = "10", sell_fee_bps: str = "10") -> dict:
    universe = eligible_fund_universe(settings)
    items = universe["items"]
    if not items:
        return {"version": VERSION, "status": "insufficient", "reason": "没有关系与历史序列均通过核验的真实基金",
                "universe": universe, "curves": [], "trades": [], "diagnostics": ["eligible_universe_empty"]}
    total = Decimal(initial_cash)
    if not total.is_finite() or total <= 0:
        raise ValueError("初始资金必须为正数")
    common_dates = sorted(set.intersection(*(set(row["date"] for row in item["rows"]) for item in items)))
    date_intersection_audit = _date_intersection_audit(items, common_dates)
    if len(common_dates) < StrategyPolicy().long_window + 2:
        return {"version": VERSION, "status": "insufficient", "reason": "基金共同真实观察日期不足",
                "universe": universe, "date_intersection_audit": date_intersection_audit,
                "curves": [], "trades": [],
                "diagnostics": [f"common_observations:{len(common_dates)}"]}
    rows_by_code = {str(item["code"]): [row for row in item["rows"] if row["date"] in set(common_dates)] for item in items}
    calendar = ObservedSessionCalendar(common_dates, source="intersection_of_stored_real_fund_series")
    costs = SimulationCosts(buy_fee_bps=Decimal(buy_fee_bps), sell_fee_bps=Decimal(sell_fee_bps))
    allocations, remaining = {}, total
    equal = total / len(items)
    for index, item in enumerate(items):
        value = remaining if index == len(items) - 1 else equal
        allocations[str(item["code"])] = value
        remaining -= value
    sleeve_results = []
    for item in items:
        code = str(item["code"])
        result = simulate_strategy(rows_by_code[code], calendar=calendar,
                                   initial_cash=allocations[code], costs=costs)
        result["code"], result["name"] = code, item["name"]
        sleeve_results.append(result)
    curve = _combine_curves(sleeve_results)
    benchmark, benchmark_initial_fee = _benchmark(rows_by_code, allocations, costs.buy_fee_bps)
    trades = sorted([{**trade, "code": result["code"], "name": result["name"]}
                     for result in sleeve_results for trade in result["trades"]],
                    key=lambda row: (str(row["execution_date"]), str(row["code"])))
    action_counts = Counter()
    execution_counts = Counter()
    for result in sleeve_results:
        action_counts.update(result["action_counts"])
        execution_counts.update(result["executed_action_counts"])
    initial_equity, final_equity = Decimal(curve[0]["equity"]), Decimal(curve[-1]["equity"])
    benchmark_final = Decimal(benchmark[-1]["equity"])
    action_gaps = [action for action in ("BUY", "ADD", "REDUCE", "EXIT") if execution_counts[action] == 0]
    years = max(Decimal(len(common_dates)) / Decimal("244"), Decimal("1"))
    trades_per_fund_year = Decimal(len(trades)) / Decimal(len(items)) / years
    diagnostics = []
    if date_intersection_audit["intersection_excluded_observations"]:
        diagnostics.append("source_date_gap")
    if not trades:
        diagnostics.append("strategy_never_trades")
    diagnostics.extend(f"missing_executed_action:{action}" for action in action_gaps)
    if trades_per_fund_year > Decimal("30"):
        diagnostics.append("turnover_review_required")
    if any(Decimal(point["equity"]) <= 0 or Decimal(point["cash"]) < 0 for point in curve):
        diagnostics.append("invalid_account_balance")
    # Report fees in the same deterministic flattened trade order exposed to
    # validators and users; regrouping by sleeve can round the final Decimal
    # digit differently under the active context.
    total_fees = sum((Decimal(str(trade["fee"])) for trade in trades), Decimal(0))
    return {
        "version": VERSION, "status": "completed" if not diagnostics else "review_required",
        "strategy_version": StrategyPolicy().version, "generated_at": utc_now(),
        "start_date": common_dates[0], "end_date": common_dates[-1],
        "calendar_basis": calendar.version, "calendar_document": calendar.document,
        "protocol": {"mode": "historical_replay", "execution": "next_observed_session_close",
                     "allocation": "equal_fixed_fund_sleeves", "initial_cash": initial_cash,
                     "buy_fee_bps": buy_fee_bps, "sell_fee_bps": sell_fee_bps,
                     "decimal_precision": 40,
                     "benchmark": "same_funds_equal_weight_buy_and_hold"},
        "universe": {key: value for key, value in universe.items() if key not in {"items", "generated_at"}} | {
            "items": [{key: value for key, value in item.items() if key != "rows"} for item in items]},
        "date_intersection_audit": date_intersection_audit,
        "input_identity": {"hash": digest({"common_rows": rows_by_code,
                                            "date_intersection_audit": date_intersection_audit}),
                           "common_observations": len(common_dates)},
        "curves": [
            {"id": "strategy", "name": "综合策略", "points": curve},
            {"id": "benchmark", "name": "同基金等权持有基准", "points": benchmark},
            *[{"id": f"fund:{result['code']}", "name": f"{result['name']}策略分账户",
               "points": result["equity_curve"]} for result in sleeve_results],
        ],
        "metrics": {"initial_equity": _number(initial_equity), "final_equity": _number(final_equity),
                    "total_return_pct": _number((final_equity / initial_equity - 1) * 100),
                    "benchmark_return_pct": _number((benchmark_final / initial_equity - 1) * 100),
                    "excess_return_pct": _number((final_equity / initial_equity - benchmark_final / initial_equity) * 100),
                    "max_drawdown_pct": min((point["drawdown_pct"] for point in curve), key=Decimal),
                    "total_fees": _number(total_fees), "trade_count": len(trades),
                    "trades_per_fund_year": _number(trades_per_fund_year),
                    "benchmark_initial_fee": _number(benchmark_initial_fee)},
        "action_summary": {action: action_counts[action] for action in ("BUY","ADD","HOLD","REDUCE","EXIT","WAIT")},
        "executed_action_summary": {action: execution_counts[action] for action in ("BUY","ADD","REDUCE","EXIT")},
        "periods": _periods(curve, trades), "trades": trades, "diagnostics": diagnostics,
        "limitations": [
            "首版仅包含关系已核验且本地有足量真实历史序列的被动基金。",
            "基金池按回放运行时的当前核验关系和全历史长度选择，存在生存与选择偏差；收益仅用于验证规则行为，不是无偏业绩估计。",
            "多年回放按各基金共同存在的真实观察日期推进，无法识别所有来源同时缺失的日期。",
            "场外基金按下一共同观察日净值近似执行，尚未模拟真实申购确认时差、限购和分红。",
            "单位净值不是含现金分红再投资的总收益序列，跨基金收益比较仍有口径限制。",
            "历史模拟只验证策略行为和账本一致性，不保证未来收益。",
        ],
    }


def historical_strategy_report(settings: Settings, *, initial_cash: str = "100000",
                               buy_fee_bps: str = "10", sell_fee_bps: str = "10") -> dict:
    """Build the complete report with a fixed, versioned decimal context."""
    with localcontext() as context:
        context.prec = 40
        return _historical_strategy_report(
            settings,
            initial_cash=initial_cash,
            buy_fee_bps=buy_fee_bps,
            sell_fee_bps=sell_fee_bps,
        )
