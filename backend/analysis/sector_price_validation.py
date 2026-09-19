"""Retrospective price-label diagnostics using the exact current advantage screen.

This deliberately does not relax validation.py's system-as-of/total-return
contract. It does not simulate tradable funds, reinvestment or strategy NAV.
"""
from __future__ import annotations

from collections import Counter
from datetime import date
from decimal import Decimal
from hashlib import sha256
import json
from statistics import median

from backend.analysis.sector_status import PERIODS, analyze_index
from backend.analysis.sector_strength import attach_strength, build_advantage_summary
from backend.core.trading_calendar import TradingCalendar


def digest(value: object) -> str:
    return sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def signals_at(boards: list[dict], signal_date: str, calendar: TradingCalendar) -> dict:
    """Only the prefix known by the signal date enters any ranking calculation."""
    items = []
    for board in boards:
        prefix = [row for row in board["rows"] if row["date"] <= signal_date]
        items.append({"code": board["code"], "name": board["name"], "kind": "行业",
                      "universe_type": "hot_board", "source_id": "sector_daily.ths",
                      **analyze_index(prefix, as_of=date.fromisoformat(signal_date), calendar=calendar)})
    attach_strength(items, {"hot_board": signal_date, "tracked_index": None})
    return {row["id"]: row for row in build_advantage_summary(items)}


def _pct(value: Decimal) -> float:
    return float(value * 100)


def _drawdown(path: list[Decimal]) -> float:
    peak, worst = path[0], Decimal(0)
    for value in path:
        peak = max(peak, value)
        worst = min(worst, value / peak - 1)
    return _pct(worst)


def label_case(boards: list[dict], selected_codes: list[str], dates: list[str]) -> dict:
    """Missing outcomes block the whole scheduled comparison, never drop losers."""
    paths, missing = {}, []
    for board in boards:
        rows = [row for row in board["rows"] if dates[0] <= row["date"] <= dates[-1]]
        if [row["date"] for row in rows] != dates:
            missing.append(board["code"])
            continue
        try:
            prices = [Decimal(str(row["close"])) for row in rows]
            if any(not value.is_finite() or value <= 0 for value in prices):
                raise ValueError("invalid close")
            paths[board["code"]] = [value / prices[0] for value in prices]
        except (ValueError, ArithmeticError):
            missing.append(board["code"])
    if missing:
        return {"status": "outcome_incomplete", "missing_codes": missing}
    if not selected_codes:
        return {"status": "no_selection"}
    if len(selected_codes) != len(set(selected_codes)) or not set(selected_codes) <= paths.keys():
        raise ValueError("Selected codes must be unique and belong to the frozen pool")

    def basket(codes: list[str]) -> list[Decimal]:
        return [sum(paths[code][i] for code in codes) / len(codes) for i in range(len(dates))]

    selected, control = basket(selected_codes), basket(list(paths))
    selected_return, control_return = selected[-1] - 1, control[-1] - 1
    member_returns = [{"code": code, "return_pct": _pct(paths[code][-1] - 1)} for code in selected_codes]
    return {"status": "matured", "selected_return_pct": _pct(selected_return),
            "control_return_pct": _pct(control_return),
            "excess_return_pp": _pct(selected_return - control_return),
            "selected_max_drawdown_pct": _drawdown(selected),
            "control_max_drawdown_pct": _drawdown(control),
            "selected_member_returns": member_returns}


def summarize(cases: list[dict], costs: list[int]) -> dict:
    mature = [row for row in cases if row["status"] == "matured"]
    def mean(key: str) -> float | None:
        return sum(row[key] for row in mature) / len(mature) if mature else None

    avg = mean("selected_return_pct")
    return {
        "scheduled_batches": len(cases), "status_counts": dict(Counter(row["status"] for row in cases)),
        "matured_batches": len(mature), "selected_observations": sum(len(row["selected_codes"]) for row in mature),
        "first_signal_date": mature[0]["signal_date"] if mature else None,
        "last_signal_date": mature[-1]["signal_date"] if mature else None,
        "mean_selected_return_pct": avg, "mean_control_return_pct": mean("control_return_pct"),
        "mean_excess_return_pp": mean("excess_return_pp"),
        "median_excess_return_pp": median(row["excess_return_pp"] for row in mature) if mature else None,
        "positive_batches": sum(row["selected_return_pct"] > 0 for row in mature),
        "outperforming_batches": sum(row["excess_return_pp"] > 0 for row in mature),
        "positive_batch_ratio_pct": 100 * sum(row["selected_return_pct"] > 0 for row in mature) / len(mature) if mature else None,
        "outperforming_batch_ratio_pct": 100 * sum(row["excess_return_pp"] > 0 for row in mature) / len(mature) if mature else None,
        "worst_batch_return_pct": min((row["selected_return_pct"] for row in mature), default=None),
        "worst_within_batch_drawdown_pct": min((row["selected_max_drawdown_pct"] for row in mature), default=None),
        "cost_stress": [{"round_trip_bps_of_initial_value": cost,
                         "mean_selected_after_haircut_pct": avg - cost / 100 if avg is not None else None,
                         "positive_batches_after_haircut": sum(row["selected_return_pct"] > cost / 100 for row in mature)}
                        for cost in costs],
        "confidence_interval": None,
        "uncertainty": "批次和行业仍相关；重叠样本不当独立样本，历史占比不作未来胜率。",
    }


def run_diagnostic(snapshot: dict, protocol: dict) -> dict:
    expected = {
        "version": "sector-price-validation-v1",
        "signal_rule": "existing_analyze_index_attach_strength_build_advantage_summary",
        "entry_policy": "next_session_close",
        "exit_policy": "entry_plus_horizon_sessions_close",
        "primary_schedule": "first_possible_signal_then_stride_horizon_plus_one",
        "secondary_schedule": "every_session_overlapping_descriptive_only",
        "coverage_policy": "full_frozen_pool_required_for_signal_and_outcome",
        "control": "same_date_equal_weight_all_frozen_industries",
        "selected_weighting": "equal_at_entry_hold_units_no_rebalance",
        "no_selection_policy": "record_without_zero_return_or_cash_simulation",
        "parameter_search": "none",
        "return_basis": "unadjusted_index_price_not_fund_total_return",
        "interpretation": "retrospective_diagnostic_not_unseen_oos",
        "investment_operations_enabled": False,
    }
    if any(protocol.get(key) != value for key, value in expected.items()):
        raise ValueError("协议与当前实现不一致，不能只更改说明而不更改计算")
    boards = snapshot["boards"]
    if not boards or len({b["code"] for b in boards}) != len(boards):
        raise ValueError("冻结行业池为空或代码重复")
    if any(b.get("source_id") != "sector_daily.ths" or not b["rows"] for b in boards):
        raise ValueError("只接受有真实价格的同花顺原生行业池")
    if protocol["horizons"] != {key: size for key, _, _, size in PERIODS}:
        raise ValueError("观察期限必须与固定验证协议一致")
    calendar = TradingCalendar(snapshot["calendar"])
    first = min(row["date"] for b in boards for row in b["rows"])
    # The dataset cutoff is recorded at export, never selected using outcomes.
    last = snapshot["as_of"]
    sessions = [d.isoformat() for d in calendar.sessions(date.fromisoformat(first), date.fromisoformat(last))]
    for b in boards:
        dates = [r["date"] for r in b["rows"]]
        if dates != sorted(set(dates)) or any(d > last for d in dates):
            raise ValueError("冻结输入日期必须有序、唯一，且不晚于截止日")
    cases = {key: [] for key in protocol["horizons"]}
    for index in range(min(protocol["horizons"].values()), len(sessions)):
        signal_date = sessions[index]
        selections = signals_at(boards, signal_date, calendar)
        for key, horizon in protocol["horizons"].items():
            if index < horizon:
                continue
            selection = selections[key]
            selected = [row["code"] for row in selection["items"]]
            case = {"signal_date": signal_date, "selected_codes": selected,
                    "selected_names": [row["name"] for row in selection["items"]],
                    "eligible_count": selection["eligible_count"], "pool_count": len(boards),
                    "primary_schedule": (index - horizon) % (horizon + 1) == 0,
                    "entry_date": sessions[index + 1] if index + 1 < len(sessions) else None,
                    "exit_date": sessions[index + 1 + horizon] if index + 1 + horizon < len(sessions) else None}
            if selection["eligible_count"] != len(boards):
                case["status"] = "signal_incomplete"
            elif not selected:
                case["status"] = "no_selection"
            elif index + 1 + horizon >= len(sessions):
                case["status"] = "not_matured"
            else:
                case.update(label_case(boards, selected, sessions[index + 1:index + horizon + 2]))
            cases[key].append(case)
    costs = protocol["cost_stress_bps_of_initial_value"]
    midpoint = sessions[len(sessions) // 2]
    return {
        "version": protocol["version"], "input_hash": digest(snapshot), "protocol_hash": digest(protocol),
        "interpretation": protocol["interpretation"], "investment_effectiveness": "not_established",
        "investment_operations_enabled": False,
        "data": {"start": first, "end": last, "sessions": len(sessions), "boards": len(boards),
                 "rows": sum(len(b["rows"]) for b in boards), "chronological_midpoint": midpoint},
        "periods": {key: {
            "lookback_sessions": horizon, "forward_sessions": horizon,
            "primary": summarize([r for r in cases[key] if r["primary_schedule"]], costs),
            "overlapping_secondary": summarize(cases[key], costs),
            "earlier_signals": summarize([r for r in cases[key] if r["signal_date"] < midpoint], costs),
            "later_signals": summarize([r for r in cases[key] if r["signal_date"] >= midpoint], costs),
            "cases": cases[key],
        } for key, horizon in protocol["horizons"].items()},
        "limitations": [
            "使用今日冻结的行业池和目前来源返回的历史；缺少当时完整行业目录及价格修订版本，存在存续及修订偏差。",
            "信号仅读取截至信号日的行情，但不能证明这些版本当时已由本系统取得；不是严格历史可知或未见样本外验证。",
            "对照是同花顺行业等权价格篮子，非可投资宽基；没有相对沪深300等宽基的结论。",
            "结果为次日收盘到固定期限的价格标签；不是基金总收益或可成交组合净值，不年化、不跨批次复利。",
            "成本仅按初始金额扣除固定基点做压力测试，不替代基金真实费率、滑点或执行服务。",
            "主表批次持有区间不重叠，但仍可能相关；逐日补充表重叠，不能据其数量宣称统计显著。",
            "本轮仅检验优势榜价格筛选；历史经营、估值、事件及基金映射不足，不能认证完整投资判断。",
        ],
    }
