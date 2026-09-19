"""Independent, deterministic acceptance checks for integrated strategy reports."""
from __future__ import annotations

from collections import Counter
from datetime import date, datetime, timezone
from decimal import Decimal, DecimalException, InvalidOperation, localcontext
import re
from typing import Any

from backend.analysis.simulation import SimulationCosts, simulate_strategy
from backend.analysis.strategy import (
    ObservedSessionCalendar,
    StrategyPolicy,
    evaluate_strategy,
    policy_document,
)
from backend.storage.research import digest


VERSION = "strategy-validation-v5"
TRADE_ACTIONS = ("BUY", "ADD", "REDUCE", "EXIT")
DECISION_ACTIONS = ("BUY", "ADD", "HOLD", "REDUCE", "EXIT", "WAIT")
TRADING_SESSIONS_PER_YEAR = Decimal("244")
HIGH_TRADES_PER_FUND_YEAR = Decimal("30")
HIGH_ANNUAL_NOTIONAL_TURNOVER_PCT = Decimal("200")
DATE_INTERSECTION_AUDIT_VERSION = "observed-source-date-intersection-v1"
LOW_AVERAGE_EXPOSURE = Decimal("0.20")
SHORT_HOLDING_OBSERVATIONS = Decimal("20")
COST_SENSITIVITY_RETURN_GAP_PCT = Decimal("5")
RISK_QUALITY_VERSION = "strategy-risk-quality-v1"


def _number(value: Decimal) -> str:
    return "0" if value == 0 else format(value, "f")


def _error(errors: list[dict[str, Any]], code: str, path: str, message: str) -> None:
    normalized_path = re.sub(r"\[\d+\]", "[]", path)
    group = f"{code}:{normalized_path}"
    for item in errors:
        if item.get("_group") == group:
            item["occurrences"] += 1
            examples = item.setdefault("example_paths", [item["path"]])
            if len(examples) < 3:
                examples.append(path)
            return
    errors.append({"code": code, "path": path, "message": message, "occurrences": 1, "_group": group})


def _review(reviews: list[dict[str, str]], code: str, message: str) -> None:
    if not any(item["code"] == code for item in reviews):
        reviews.append({"code": code, "message": message})


def _decimal(value: object, path: str, errors: list[dict[str, Any]]) -> Decimal | None:
    if not isinstance(value, str) or not value:
        _error(errors, "decimal_string_required", path, "数值必须是十进制字符串")
        return None
    try:
        parsed = Decimal(value)
    except InvalidOperation:
        _error(errors, "invalid_decimal", path, "数值不是有效十进制数")
        return None
    if not parsed.is_finite():
        _error(errors, "non_finite_decimal", path, "数值必须有限，不能为NaN或无穷大")
        return None
    return parsed


def _day(value: object, path: str, errors: list[dict[str, Any]]) -> str | None:
    if not isinstance(value, str):
        _error(errors, "date_string_required", path, "日期必须是YYYY-MM-DD字符串")
        return None
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        _error(errors, "invalid_date", path, "日期必须是有效的YYYY-MM-DD")
        return None
    if parsed.isoformat() != value:
        _error(errors, "invalid_date", path, "日期必须是有效的YYYY-MM-DD")
        return None
    return value


def _count(value: object, path: str, errors: list[dict[str, Any]]) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        _error(errors, "invalid_count", path, "计数必须是非负整数")
        return None
    return value


def _date_list(value: object, path: str, errors: list[dict[str, Any]]) -> list[str]:
    if not isinstance(value, list):
        _error(errors, "date_list_required", path, "日期明细必须是数组")
        return []
    dates: list[str] = []
    for index, raw in enumerate(value):
        parsed = _day(raw, f"{path}[{index}]", errors)
        if parsed is not None:
            dates.append(parsed)
    if len(dates) != len(set(dates)):
        _error(errors, "duplicate_audit_dates", path, "日期交集审计明细必须唯一")
    if dates != sorted(dates):
        _error(errors, "unordered_audit_dates", path, "日期交集审计明细必须严格递增")
    return dates


def _validate_date_intersection_audit(
    raw: object,
    *,
    strategy_dates: list[str],
    common_count: int | None,
    universe_items: list,
    errors: list[dict[str, Any]],
    reviews: list[dict[str, str]],
) -> dict[str, Any] | None:
    """Validate the optional v1 audit while accepting reports created before it existed."""
    if raw is None:
        return None
    path = "date_intersection_audit"
    if not isinstance(raw, dict):
        _error(errors, "date_intersection_audit_invalid", path, "来源日期交集审计必须是对象")
        return None
    if raw.get("version") != DATE_INTERSECTION_AUDIT_VERSION:
        _error(errors, "date_intersection_audit_version_unsupported", f"{path}.version",
               "来源日期交集审计版本不受支持")
    expected_basis = "union_of_observed_source_dates_within_shared_coverage"
    if raw.get("basis") != expected_basis:
        _error(errors, "date_intersection_audit_basis_invalid", f"{path}.basis",
               "来源日期交集审计必须以共同覆盖区间内的实际观测日期并集为基准")

    coverage_start = _day(raw.get("coverage_start"), f"{path}.coverage_start", errors)
    coverage_end = _day(raw.get("coverage_end"), f"{path}.coverage_end", errors)
    if coverage_start is not None and coverage_end is not None and coverage_start > coverage_end:
        _error(errors, "date_intersection_coverage_invalid", path, "来源共同覆盖区间起止日期无效")

    excluded_dates = _date_list(raw.get("intersection_excluded_dates"),
                                f"{path}.intersection_excluded_dates", errors)
    excluded_set = set(excluded_dates)
    excluded_count = _count(raw.get("intersection_excluded_observations"),
                            f"{path}.intersection_excluded_observations", errors)
    if excluded_count is not None and excluded_count != len(excluded_dates):
        _error(errors, "date_intersection_count_mismatch",
               f"{path}.intersection_excluded_observations",
               "被日期交集排除的观察日数量与明细不一致")
    audit_common_count = _count(raw.get("common_observations"),
                                f"{path}.common_observations", errors)
    if audit_common_count is not None and (
            audit_common_count != len(strategy_dates)
            or common_count is not None and audit_common_count != common_count):
        _error(errors, "date_intersection_common_count_mismatch", f"{path}.common_observations",
               "日期交集审计的共同观察日数量与曲线不一致")
    union_count = _count(raw.get("union_observations"), f"{path}.union_observations", errors)
    if union_count is not None and union_count != len(strategy_dates) + len(excluded_dates):
        _error(errors, "date_intersection_union_count_mismatch", f"{path}.union_observations",
               "实际观测日期并集数量不能由共同日期与被排除日期复算")
    if excluded_set.intersection(strategy_dates):
        _error(errors, "date_intersection_dates_overlap", f"{path}.intersection_excluded_dates",
               "被交集排除的日期不能出现在共同曲线中")
    if coverage_start is not None and coverage_end is not None and any(
            value < coverage_start or value > coverage_end for value in excluded_dates):
        _error(errors, "date_intersection_date_outside_coverage",
               f"{path}.intersection_excluded_dates", "被交集排除的日期必须位于共同覆盖区间")
    if coverage_start is not None and coverage_end is not None and any(
            value < coverage_start or value > coverage_end for value in strategy_dates):
        _error(errors, "date_intersection_common_date_outside_coverage", path,
               "共同曲线日期必须位于来源共同覆盖区间")

    universe_by_code = {
        str(item.get("code")): item
        for item in universe_items
        if isinstance(item, dict) and isinstance(item.get("code"), str) and item.get("code")
    }
    if universe_by_code:
        first_dates = [_day(item.get("first_date"), f"universe.items[{code}].first_date", errors)
                       for code, item in universe_by_code.items()]
        last_dates = [_day(item.get("last_date"), f"universe.items[{code}].last_date", errors)
                      for code, item in universe_by_code.items()]
        if all(first_dates) and coverage_start != max(str(value) for value in first_dates):
            _error(errors, "date_intersection_coverage_mismatch", f"{path}.coverage_start",
                   "共同覆盖起点不能由各来源首个观察日复算")
        if all(last_dates) and coverage_end != min(str(value) for value in last_dates):
            _error(errors, "date_intersection_coverage_mismatch", f"{path}.coverage_end",
                   "共同覆盖终点不能由各来源最后观察日复算")

    raw_sources = raw.get("sources")
    if not isinstance(raw_sources, list):
        _error(errors, "date_intersection_sources_invalid", f"{path}.sources", "来源日期统计必须是数组")
        raw_sources = []
    source_count = _count(raw.get("source_count"), f"{path}.source_count", errors)
    if source_count is not None and source_count != len(raw_sources):
        _error(errors, "date_intersection_source_count_mismatch", f"{path}.source_count",
               "来源数量与来源日期统计明细不一致")

    normalized_sources: list[dict[str, Any]] = []
    source_codes: list[str] = []
    classifications = {value: {"missing": 0, "observed": 0} for value in excluded_dates}
    has_source_gap = False
    for index, source in enumerate(raw_sources):
        source_path = f"{path}.sources[{index}]"
        if not isinstance(source, dict):
            _error(errors, "date_intersection_source_invalid", source_path, "来源日期统计必须是对象")
            continue
        code = source.get("code")
        if not isinstance(code, str) or not code:
            _error(errors, "date_intersection_source_code_invalid", f"{source_path}.code",
                   "来源日期统计必须引用基金代码")
            code = ""
        else:
            source_codes.append(code)
            if universe_by_code and code not in universe_by_code:
                _error(errors, "date_intersection_source_unknown", f"{source_path}.code",
                       "来源日期统计引用了基金池外代码")
            elif (code in universe_by_code
                  and source.get("series_source_id") != universe_by_code[code].get("series_source_id")):
                _error(errors, "date_intersection_source_identity_mismatch",
                       f"{source_path}.series_source_id", "来源日期统计的数据源身份与基金池不一致")
        missing_dates = _date_list(source.get("missing_dates"), f"{source_path}.missing_dates", errors)
        observed_excluded = _date_list(source.get("excluded_by_intersection_dates"),
                                       f"{source_path}.excluded_by_intersection_dates", errors)
        missing_count = _count(source.get("missing_observations"),
                               f"{source_path}.missing_observations", errors)
        observed_excluded_count = _count(source.get("excluded_by_intersection_observations"),
                                         f"{source_path}.excluded_by_intersection_observations", errors)
        observations = _count(source.get("observations_in_coverage"),
                              f"{source_path}.observations_in_coverage", errors)
        if missing_count is not None and missing_count != len(missing_dates):
            _error(errors, "date_intersection_count_mismatch", f"{source_path}.missing_observations",
                   "来源缺失观察日数量与明细不一致")
        if observed_excluded_count is not None and observed_excluded_count != len(observed_excluded):
            _error(errors, "date_intersection_count_mismatch",
                   f"{source_path}.excluded_by_intersection_observations",
                   "来源被交集排除的实际观察日数量与明细不一致")
        if observations is not None and observations != len(strategy_dates) + len(observed_excluded):
            _error(errors, "date_intersection_source_observation_mismatch",
                   f"{source_path}.observations_in_coverage",
                   "来源覆盖区间观察数不能由共同日期与该来源被排除日期复算")
        if set(missing_dates).intersection(observed_excluded):
            _error(errors, "date_intersection_source_classification_overlap", source_path,
                   "同一来源不能把日期同时标记为缺失和已观测")
        if set(missing_dates).union(observed_excluded) != excluded_set:
            _error(errors, "date_intersection_source_classification_mismatch", source_path,
                   "每个来源必须逐一分类全部被交集排除的日期")
        for value in missing_dates:
            if value in classifications:
                classifications[value]["missing"] += 1
        for value in observed_excluded:
            if value in classifications:
                classifications[value]["observed"] += 1
        has_source_gap = has_source_gap or bool(missing_dates)
        normalized_sources.append({
            "code": code,
            "series_source_id": source.get("series_source_id"),
            "observations_in_coverage": observations,
            "missing_observations": missing_count,
            "missing_dates": missing_dates,
            "excluded_by_intersection_observations": observed_excluded_count,
            "excluded_by_intersection_dates": observed_excluded,
        })
    if len(source_codes) != len(set(source_codes)):
        _error(errors, "date_intersection_source_duplicate", f"{path}.sources",
               "每个基金来源只能出现一次")
    if universe_by_code and set(source_codes) != set(universe_by_code):
        _error(errors, "date_intersection_sources_mismatch", f"{path}.sources",
               "来源日期统计必须覆盖基金池中的全部来源")
    for value, counts in classifications.items():
        if counts["missing"] == 0 or counts["observed"] == 0:
            _error(errors, "date_intersection_exclusion_not_reproducible",
                   f"{path}.intersection_excluded_dates",
                   "每个被交集排除的日期都必须由至少一个来源观测且至少一个来源缺失")
            break
    if has_source_gap:
        _review(reviews, "source_date_gap",
                f"共同覆盖区间内有{len(excluded_dates)}个日期仅被部分来源观测，已从回测交集中排除")
    return {
        "version": raw.get("version"),
        "basis": raw.get("basis"),
        "coverage_start": coverage_start,
        "coverage_end": coverage_end,
        "source_count": source_count,
        "union_observations": union_count,
        "common_observations": audit_common_count,
        "intersection_excluded_observations": excluded_count,
        "intersection_excluded_dates": excluded_dates,
        "sources": normalized_sources,
    }


def _validate_observed_date_audit(
    raw: object,
    *,
    report_version: object,
    strategy_dates: list[str],
    universe_items: list,
    errors: list[dict[str, Any]],
    reviews: list[dict[str, str]],
) -> dict[str, Any] | None:
    """Ensure period-end weekend disclosures never become execution sessions."""

    for index, value in enumerate(strategy_dates):
        if date.fromisoformat(value).weekday() >= 5:
            _error(errors, "non_session_date_in_curve", f"curves.strategy.points[{index}].date",
                   "周末披露日不能成为策略成交观察日")
    if raw is None:
        if report_version in {"integrated-fund-strategy-v3", "integrated-fund-strategy-v4",
                              "integrated-fund-strategy-v5"}:
            _error(errors, "observed_date_audit_missing", "observed_date_audit",
                   "v3历史报告必须说明被排除的非交易披露日")
        return None
    if not isinstance(raw, dict):
        _error(errors, "observed_date_audit_invalid", "observed_date_audit", "观察日期审计必须是对象")
        return None
    path = "observed_date_audit"
    excluded = _date_list(raw.get("excluded_non_session_dates"),
                          f"{path}.excluded_non_session_dates", errors)
    count = _count(raw.get("excluded_non_session_observations"),
                   f"{path}.excluded_non_session_observations", errors)
    if count is not None and count != len(excluded):
        _error(errors, "observed_date_audit_count_mismatch",
               f"{path}.excluded_non_session_observations", "非交易披露日数量与明细不一致")
    if any(date.fromisoformat(value).weekday() < 5 for value in excluded):
        _error(errors, "observed_date_exclusion_invalid", f"{path}.excluded_non_session_dates",
               "周末过滤审计不能把工作日直接归类为非交易日")
    if set(excluded).intersection(strategy_dates):
        _error(errors, "excluded_non_session_still_in_curve", f"{path}.excluded_non_session_dates",
               "已排除的非交易披露日仍出现在策略曲线中")
    expected = sorted({
        str(value)
        for item in universe_items if isinstance(item, dict)
        for value in item.get("excluded_non_session_dates", [])
    })
    if expected != excluded:
        _error(errors, "observed_date_audit_not_reproducible", f"{path}.excluded_non_session_dates",
               "非交易披露日不能由基金池来源明细复算")
    if excluded:
        _review(reviews, "non_session_disclosures_excluded",
                f"已排除{len(excluded)}个周末期末披露点；仍需完整官方历史交易日历复核节假日")
    return {
        "policy": raw.get("policy"),
        "excluded_non_session_observations": count,
        "excluded_non_session_dates": excluded,
    }


def _validate_investment_eligibility(
    raw: object,
    *,
    report_version: object,
    universe: dict[str, Any],
    universe_items: list,
    protocol: dict[str, Any],
    errors: list[dict[str, Any]],
    reviews: list[dict[str, str]],
) -> dict[str, Any] | None:
    """Reproduce the hard gates separating a mechanics replay from investability."""

    if raw is None:
        if report_version in {"integrated-fund-strategy-v3", "integrated-fund-strategy-v4",
                              "integrated-fund-strategy-v5"}:
            _error(errors, "investment_eligibility_missing", "investment_eligibility",
                   "v3历史报告必须给出真实资金资格门槛")
        return None
    if not isinstance(raw, dict):
        _error(errors, "investment_eligibility_invalid", "investment_eligibility",
               "真实资金资格必须是对象")
        return None
    status = raw.get("status")
    enabled = raw.get("investment_operations_enabled")
    blockers = raw.get("blockers")
    if status not in {"ready", "blocked"}:
        _error(errors, "investment_eligibility_status_invalid", "investment_eligibility.status",
               "真实资金资格状态必须为ready或blocked")
    if type(enabled) is not bool:
        _error(errors, "investment_operations_flag_invalid",
               "investment_eligibility.investment_operations_enabled", "真实资金操作开关必须是布尔值")
    if (status == "ready") != (enabled is True):
        _error(errors, "investment_eligibility_state_mismatch", "investment_eligibility",
               "资格状态与真实资金操作开关不一致")
    if (not isinstance(blockers, list) or any(not isinstance(value, str) or not value for value in blockers)
            or blockers != sorted(set(blockers))):
        _error(errors, "investment_eligibility_blockers_invalid", "investment_eligibility.blockers",
               "资格阻断项必须是有序且唯一的非空字符串数组")
        blockers = []

    expected: set[str] = set()
    for index, item in enumerate(universe_items):
        if not isinstance(item, dict):
            continue
        fields = (("catalog_eligibility", "data_eligibility", "relation_eligibility")
                  if report_version in {"integrated-fund-strategy-v4", "integrated-fund-strategy-v5"}
                  else ("data_eligibility", "relation_eligibility"))
        for field in fields:
            eligibility = item.get(field)
            if not isinstance(eligibility, dict):
                if report_version in {"integrated-fund-strategy-v3", "integrated-fund-strategy-v4",
                                      "integrated-fund-strategy-v5"}:
                    _error(errors, "source_eligibility_missing",
                           f"universe.items[{index}].{field}",
                           "v3历史报告必须逐只提供目录、序列与跟踪关系的数据用途资格")
                continue
            reasons = eligibility.get("performance_blockers")
            if not isinstance(reasons, list) or any(not isinstance(reason, str) for reason in reasons):
                _error(errors, "source_eligibility_invalid",
                       f"universe.items[{index}].{field}.performance_blockers",
                       "数据用途资格的业绩阻断项必须是字符串数组")
                continue
            expected.update(str(reason) for reason in reasons)
    if report_version == "integrated-fund-strategy-v5":
        # v5 is the first report schema emitted after the global investment
        # gate became fail-closed.  These capabilities do not yet have
        # immutable evidence objects in the report, so a current v5 producer
        # must carry the same explicit blockers as strategy_readiness.
        expected.update({
            "immutable_source_evidence_chain_missing",
            "point_in_time_universe_missing",
            "point_in_time_observation_revisions_missing",
            "verified_historical_trading_calendar_missing",
            "stable_investable_universe_policy_missing",
            "sector_research_signal_not_integrated",
            "risk_free_cash_return_series_missing",
            "shared_live_portfolio_engine_not_used",
            "atomic_pretrade_revalidation_missing",
            "product_specific_execution_terms_missing",
            "walk_forward_holdout_not_completed",
        })
    if universe.get("selection_temporality") != "point_in_time":
        expected.add("point_in_time_universe_missing")
    if protocol.get("point_in_time_observations") is not True:
        expected.add("point_in_time_observation_revisions_missing")
    if protocol.get("portfolio_engine") != "shared_live_portfolio_engine":
        expected.add("shared_live_portfolio_engine_not_used")
    if protocol.get("execution_terms") != "product_specific_versioned_terms":
        expected.add("product_specific_execution_terms_missing")
    if protocol.get("validation_mode") != "walk_forward_holdout":
        expected.add("walk_forward_holdout_not_completed")
    if set(blockers) != expected:
        _error(errors, "investment_eligibility_not_reproducible", "investment_eligibility.blockers",
               "真实资金资格阻断项不能由来源、基金池和回放协议复算")
    if blockers:
        _review(reviews, "investment_validation_not_eligible",
                "当前回放只完成规则行为核验，数据时点、总收益、真实执行或样本外门槛尚未满足")
    return {"status": status, "investment_operations_enabled": enabled, "blockers": blockers}


def _timestamp_date(value: object, path: str, errors: list[dict[str, Any]]) -> date | None:
    if not isinstance(value, str):
        _error(errors, "timestamp_required", path, "生成时间必须是含时区的字符串")
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        _error(errors, "timestamp_invalid", path, "生成时间必须是有效的含时区时间戳")
        return None
    if parsed.tzinfo is None:
        _error(errors, "timestamp_timezone_missing", path, "生成时间必须包含时区")
        return None
    return parsed.astimezone(timezone.utc).date()


def _period_stats(
    points: list[dict[str, Any]], *, baseline: dict[str, Any] | None = None,
) -> dict[str, str | int]:
    baseline = baseline or points[0]
    start, end = baseline["equity"], points[-1]["equity"]
    peak, drawdown = baseline["equity"], Decimal(0)
    for point in points:
        peak = max(peak, point["equity"])
        drawdown = min(drawdown, point["equity"] / peak - 1)
    return {
        "start_date": points[0]["date"],
        "end_date": points[-1]["date"],
        "return_base_date": baseline["date"],
        "observations": len(points),
        "return_pct": _number((end / start - 1) * 100),
        "max_drawdown_pct": _number(drawdown * 100),
    }


def _curve(
    curve: object,
    *,
    path: str,
    baseline: Decimal | None,
    errors: list[dict[str, Any]],
) -> tuple[list[str], list[dict[str, Any]], dict[str, Any] | None]:
    if not isinstance(curve, dict) or not isinstance(curve.get("points"), list) or not curve["points"]:
        _error(errors, "curve_missing", path, "收益曲线必须包含非空points")
        return [], [], None
    dates: list[str] = []
    parsed: list[dict[str, Any]] = []
    peak: Decimal | None = None
    for index, raw in enumerate(curve["points"]):
        point_path = f"{path}.points[{index}]"
        if not isinstance(raw, dict):
            _error(errors, "curve_point_invalid", point_path, "曲线点必须是对象")
            continue
        value_date = _day(raw.get("date"), f"{point_path}.date", errors)
        cash = _decimal(raw.get("cash"), f"{point_path}.cash", errors)
        market = _decimal(raw.get("market_value"), f"{point_path}.market_value", errors)
        equity = _decimal(raw.get("equity"), f"{point_path}.equity", errors)
        reported_return = _decimal(raw.get("return_pct"), f"{point_path}.return_pct", errors)
        reported_drawdown = _decimal(raw.get("drawdown_pct"), f"{point_path}.drawdown_pct", errors)
        price = _decimal(raw.get("price"), f"{point_path}.price", errors) if "price" in raw else None
        units = _decimal(raw.get("units"), f"{point_path}.units", errors) if "units" in raw else None
        if value_date is not None:
            dates.append(value_date)
        if None in (value_date, cash, market, equity, reported_return, reported_drawdown):
            continue
        assert isinstance(value_date, str) and isinstance(cash, Decimal) and isinstance(market, Decimal)
        assert isinstance(equity, Decimal) and isinstance(reported_return, Decimal)
        assert isinstance(reported_drawdown, Decimal)
        if cash < 0:
            _error(errors, "negative_cash", f"{point_path}.cash", "确认现金不能小于零")
        if market < 0:
            _error(errors, "negative_market_value", f"{point_path}.market_value", "持仓市值不能小于零")
        if equity < 0:
            _error(errors, "negative_equity", f"{point_path}.equity", "账户资产不能小于零")
        if equity != cash + market:
            _error(errors, "equity_identity_mismatch", point_path, "equity必须精确等于cash加market_value")
        if price is not None and price <= 0:
            _error(errors, "nonpositive_curve_price", f"{point_path}.price", "曲线价格必须大于零")
        if units is not None and units < 0:
            _error(errors, "negative_curve_units", f"{point_path}.units", "曲线份额不能小于零")
        if price is not None and units is not None and market != price * units:
            _error(errors, "market_value_identity_mismatch", point_path,
                   "分账户market_value必须精确等于price乘units")
        if equity <= 0:
            _error(errors, "nonpositive_equity", f"{point_path}.equity", "收益曲线资产必须大于零")
        peak = equity if peak is None else max(peak, equity)
        if peak > 0:
            expected_drawdown = (equity / peak - 1) * 100
            if reported_drawdown != expected_drawdown:
                _error(errors, "drawdown_not_reproducible", f"{point_path}.drawdown_pct",
                       "回撤不能由本曲线资产精确复算")
        if baseline is not None and baseline > 0:
            expected_return = (equity / baseline - 1) * 100
            if reported_return != expected_return:
                _error(errors, "return_not_reproducible", f"{point_path}.return_pct",
                       "收益率不能由初始资产精确复算")
        parsed.append({"date": value_date, "cash": cash, "market_value": market, "equity": equity,
                       "return_pct": reported_return, "drawdown_pct": reported_drawdown,
                       "price": price, "units": units})
    if len(dates) != len(set(dates)):
        _error(errors, "duplicate_curve_dates", path, "曲线日期必须唯一")
    if dates != sorted(dates):
        _error(errors, "unordered_curve_dates", path, "曲线日期必须严格递增")
    if len(parsed) != len(curve["points"]) or any(point["equity"] <= 0 for point in parsed):
        return dates, parsed, None
    summary = _period_stats(parsed)
    total_return = ((parsed[-1]["equity"] / baseline - 1) * 100
                    if baseline is not None and baseline > 0 else Decimal(str(summary["return_pct"])))
    return dates, parsed, {
        "start_date": str(summary["start_date"]), "end_date": str(summary["end_date"]),
        "observations": summary["observations"], "total_return_pct": _number(total_return),
        "max_drawdown_pct": str(summary["max_drawdown_pct"]),
        "initial_equity": _number(parsed[0]["equity"]), "final_equity": _number(parsed[-1]["equity"]),
    }


def _compare_metric(report: dict, field: str, expected: Decimal | None,
                    errors: list[dict[str, Any]]) -> None:
    actual = _decimal(report.get("metrics", {}).get(field), f"metrics.{field}", errors)
    if actual is not None and expected is not None and actual != expected:
        _error(errors, "metric_not_reproducible", f"metrics.{field}", "汇总指标不能由曲线或交易精确复算")


def _independent_risk_quality(
    points: list[dict[str, Any]], errors: list[dict[str, Any]],
) -> dict[str, str | int | None] | None:
    """Recompute risk metrics solely from consecutive observed equity points."""
    if len(points) < 2:
        _error(errors, "risk_quality_observations_insufficient", "risk_quality",
               "风险质量至少需要两个资产观察点")
        return None
    returns = [
        points[index]["equity"] / points[index - 1]["equity"] - Decimal(1)
        for index in range(1, len(points))
    ]
    if any(not value.is_finite() or value <= Decimal(-1) for value in returns):
        _error(errors, "risk_quality_return_invalid", "curves.strategy",
               "相邻观察点收益率必须有限且大于-100%")
        return None
    count = Decimal(len(returns))
    mean = sum(returns, Decimal(0)) / count
    variance = sum(((value - mean) ** 2 for value in returns), Decimal(0)) / count
    volatility = variance.sqrt()
    annual_root = TRADING_SESSIONS_PER_YEAR.sqrt()
    annualized_volatility = volatility * annual_root
    downside_variance = sum(
        ((min(value, Decimal(0))) ** 2 for value in returns), Decimal(0),
    ) / count
    downside_deviation = downside_variance.sqrt()
    annualized_return = (
        (points[-1]["equity"] / points[0]["equity"])
        ** (TRADING_SESSIONS_PER_YEAR / count)
        - Decimal(1)
    )
    if not annualized_return.is_finite() or not annualized_volatility.is_finite():
        _error(errors, "risk_quality_non_finite", "risk_quality",
               "风险质量年化计算必须为有限数")
        return None

    peak = points[0]["equity"]
    max_drawdown = Decimal(0)
    current_duration = 0
    max_duration = 0
    for point in points:
        equity = point["equity"]
        if equity >= peak:
            peak = equity
            current_duration = 0
        else:
            current_duration += 1
            max_duration = max(max_duration, current_duration)
            max_drawdown = min(max_drawdown, equity / peak - Decimal(1))

    sharpe = mean / volatility * annual_root if volatility > 0 else None
    sortino = mean / downside_deviation * annual_root if downside_deviation > 0 else None
    calmar = annualized_return / abs(max_drawdown) if max_drawdown < 0 else None
    values = (sharpe, sortino, calmar)
    if any(value is not None and not value.is_finite() for value in values):
        _error(errors, "risk_quality_non_finite", "risk_quality",
               "风险质量比率必须为有限数")
        return None
    positive = Decimal(sum(value > 0 for value in returns)) / count
    return {
        "version": RISK_QUALITY_VERSION,
        "return_observations": len(returns),
        "observations_per_year": _number(TRADING_SESSIONS_PER_YEAR),
        "risk_free_return_assumption_pct": "0",
        "minimum_acceptable_return_assumption_pct": "0",
        "annualized_return_pct": _number(annualized_return * 100),
        "annualized_volatility_pct": _number(annualized_volatility * 100),
        "sharpe_ratio": _number(sharpe) if sharpe is not None else None,
        "sortino_ratio": _number(sortino) if sortino is not None else None,
        "calmar_ratio": _number(calmar) if calmar is not None else None,
        "max_drawdown_pct": _number(max_drawdown * 100),
        "max_drawdown_duration_observations": max_duration,
        "positive_return_observation_pct": _number(positive * 100),
        "worst_observation_return_pct": _number(min(returns) * 100),
    }


def _validate_risk_quality(
    raw: object, expected: dict[str, str | int | None] | None,
    errors: list[dict[str, Any]],
) -> dict[str, str | int | None] | None:
    if expected is None:
        return None
    if not isinstance(raw, dict):
        _error(errors, "risk_quality_missing", "risk_quality",
               "v5报告必须提供可独立复算的风险质量指标")
        return expected
    if raw.get("version") != expected["version"]:
        _error(errors, "risk_quality_not_reproducible", "risk_quality.version",
               "风险质量版本与独立验收器不一致")
    for field in ("return_observations", "max_drawdown_duration_observations"):
        actual_count = _count(raw.get(field), f"risk_quality.{field}", errors)
        if actual_count is not None and actual_count != expected[field]:
            _error(errors, "risk_quality_not_reproducible", f"risk_quality.{field}",
                   "风险质量指标不能由策略资产曲线独立复算")
    for field in (
        "observations_per_year", "risk_free_return_assumption_pct",
        "minimum_acceptable_return_assumption_pct", "annualized_return_pct",
        "annualized_volatility_pct", "max_drawdown_pct",
        "positive_return_observation_pct", "worst_observation_return_pct",
    ):
        actual = _decimal(raw.get(field), f"risk_quality.{field}", errors)
        if actual is not None and actual != Decimal(str(expected[field])):
            _error(errors, "risk_quality_not_reproducible", f"risk_quality.{field}",
                   "风险质量指标不能由策略资产曲线独立复算")
    for field in ("sharpe_ratio", "sortino_ratio", "calmar_ratio"):
        expected_value = expected[field]
        if expected_value is None:
            if raw.get(field) is not None:
                _error(errors, "risk_quality_not_reproducible", f"risk_quality.{field}",
                       "分母为零时风险调整比率必须为空")
            continue
        actual = _decimal(raw.get(field), f"risk_quality.{field}", errors)
        if actual is not None and actual != Decimal(str(expected_value)):
            _error(errors, "risk_quality_not_reproducible", f"risk_quality.{field}",
                   "风险质量比率不能由策略资产曲线独立复算")
    return expected


def _v3_report(report: dict[str, Any]) -> bool:
    return report.get("version") in {
        "integrated-fund-strategy-v3", "integrated-fund-strategy-v4",
        "integrated-fund-strategy-v5",
    }


def _v4_report(report: dict[str, Any]) -> bool:
    return report.get("version") in {"integrated-fund-strategy-v4", "integrated-fund-strategy-v5"}


def _v5_report(report: dict[str, Any]) -> bool:
    return report.get("version") == "integrated-fund-strategy-v5"


def _strategy_policy_for_replay(
    report: dict[str, Any], protocol: dict[str, Any], errors: list[dict[str, Any]],
) -> StrategyPolicy | None:
    """Accept only the exact versioned policy implemented by this validator."""
    if not _v4_report(report):
        return None
    policy = StrategyPolicy()
    raw = protocol.get("strategy_policy")
    if raw != policy_document(policy):
        _error(
            errors,
            "strategy_policy_not_reproducible",
            "protocol.strategy_policy",
            "v4报告必须保存与验收器一致的完整策略参数，不能只声明版本号",
        )
        return None
    if report.get("strategy_version") != policy.version:
        _error(
            errors,
            "strategy_version_mismatch",
            "strategy_version",
            "报告策略版本必须与完整策略参数一致",
        )
        return None
    return policy


def _replay_strategy_decisions(
    *,
    policy: StrategyPolicy,
    strategy_dates: list[str],
    parsed_curves: dict[str, list[dict[str, Any]]],
    trade_records: dict[tuple[str, str], dict[str, Any]],
    universe_items: list,
    errors: list[dict[str, Any]],
) -> Counter[str]:
    """Recompute every signal from serialized prices and post-trade balances.

    This deliberately calls the strategy evaluator again instead of trusting
    decision totals or the signal metadata attached to trades.  It therefore
    catches changed thresholds, targets, cooldown state and missing trades
    even when the resulting account ledger is otherwise self-consistent.
    """
    counts: Counter[str] = Counter()
    if not strategy_dates:
        return counts
    calendar = ObservedSessionCalendar(strategy_dates, source="independent_validation_replay")
    signal_records: dict[tuple[str, str], dict[str, Any]] = {}
    for record in trade_records.values():
        code = str(record["code"])
        signal_date = record.get("signal_date")
        if not isinstance(signal_date, str):
            continue
        key = (code, signal_date)
        if key in signal_records:
            _error(errors, "duplicate_trade_signal", record["path"],
                   "同一基金同一信号日只能产生一笔下一观察日成交")
            continue
        signal_records[key] = record

    expected_codes = {
        str(item.get("code")) for item in universe_items
        if isinstance(item, dict) and isinstance(item.get("code"), str) and item.get("code")
    }
    curve_codes = {key.removeprefix("fund:") for key in parsed_curves if key.startswith("fund:")}
    if curve_codes != expected_codes:
        _error(errors, "fund_curve_universe_mismatch", "curves",
               "v4报告必须为基金池中的每只基金提供且只提供一条分账户曲线")

    for code in sorted(curve_codes):
        points = parsed_curves.get(f"fund:{code}", [])
        if len(points) != len(strategy_dates) or any(
                point.get("price") is None or point.get("units") is None for point in points):
            continue
        last_trade_date: str | None = None
        rows = [{"date": point["date"], "close": _number(point["price"])} for point in points]
        for index, point in enumerate(points):
            value_date = str(point["date"])
            executed = trade_records.get((code, value_date))
            if executed is not None:
                last_trade_date = value_date
            current_weight = point["market_value"] / point["equity"]
            visible_start = max(0, index - policy.long_window)
            decision = evaluate_strategy(
                rows[visible_start:index + 1],
                as_of=value_date,
                current_weight=current_weight,
                calendar=calendar,
                last_trade_date=last_trade_date,
                policy=policy,
            )
            action = str(decision["action"])
            counts[action] += 1
            reported = signal_records.get((code, value_date))
            should_execute = action in TRADE_ACTIONS and index + 1 < len(points)
            if should_execute and reported is None:
                _error(errors, "strategy_trade_missing", f"curves.fund:{code}.points[{index}]",
                       "独立重放产生交易信号，但报告缺少下一观察日成交")
                continue
            if not should_execute and reported is not None:
                _error(errors, "unexpected_strategy_trade", reported["path"],
                       "报告成交不能由独立重放的策略信号产生")
                continue
            if reported is None:
                continue
            if reported.get("execution_date") != points[index + 1]["date"]:
                _error(errors, "strategy_execution_date_mismatch", reported["path"],
                       "独立重放要求成交发生在信号后的下一观察日")
            if reported.get("signal_action") != action:
                _error(errors, "strategy_signal_action_mismatch", reported["path"],
                       "成交记录的信号动作与独立策略重放不一致")
            if reported["values"].get("target_weight") != Decimal(str(decision["target_weight"])):
                _error(errors, "strategy_target_weight_mismatch", reported["path"],
                       "成交目标仓位与独立策略重放不一致")
    return counts


def _independent_exposure_evidence(
    strategy: list[dict[str, Any]], benchmark: list[dict[str, Any]],
) -> tuple[list[dict[str, Decimal | str]], dict[str, int | str]] | None:
    """Rebuild exposure evidence without trusting the report's derived fields."""
    if not strategy or len(strategy) != len(benchmark):
        return None
    if [point["date"] for point in strategy] != [point["date"] for point in benchmark]:
        return None
    exposures: list[Decimal] = []
    for point in strategy:
        if point["equity"] <= 0:
            return None
        exposure = point["market_value"] / point["equity"]
        if not Decimal(0) <= exposure <= Decimal(1):
            return None
        exposures.append(exposure)

    initial = strategy[0]["equity"]
    equity, peak = initial, initial
    curve: list[dict[str, Decimal | str]] = []
    for index, (strategy_point, benchmark_point) in enumerate(zip(strategy, benchmark)):
        benchmark_daily_return = Decimal(0)
        if index:
            previous_benchmark = benchmark[index - 1]["equity"]
            if previous_benchmark <= 0:
                return None
            benchmark_daily_return = benchmark_point["equity"] / previous_benchmark - 1
            equity *= Decimal(1) + exposures[index - 1] * benchmark_daily_return
        market = equity * exposures[index]
        cash = equity - market
        # The producer serializes the ledger identity as canonical equity.
        # Reapply that same reported-account invariant after Decimal rounding;
        # otherwise a one-unit tail can compound into false tamper findings on
        # long histories even though cash + market remains exact.
        equity = cash + market
        peak = max(peak, equity)
        curve.append({
            "date": strategy_point["date"],
            "equity": equity,
            "cash": cash,
            "market_value": market,
            "strategy_exposure": exposures[index],
            "strategy_exposure_pct": exposures[index] * 100,
            "benchmark_daily_return_pct": benchmark_daily_return * 100,
            "return_pct": (equity / initial - 1) * 100,
            "drawdown_pct": (equity / peak - 1) * 100,
        })

    ordered = sorted(exposures)
    middle = len(ordered) // 2
    median = (ordered[middle] if len(ordered) % 2
              else (ordered[middle - 1] + ordered[middle]) / 2)
    average = sum(ordered, Decimal(0)) / Decimal(len(ordered))
    active_returns = [
        strategy[index]["equity"] / strategy[index - 1]["equity"] - 1
        for index in range(1, len(strategy))
        if exposures[index - 1] > 0
    ]
    positive_active = sum(value > 0 for value in active_returns)
    return curve, {
        "observation_count": len(ordered),
        "average_exposure": _number(average),
        "average_exposure_pct": _number(average * 100),
        "median_exposure": _number(median),
        "median_exposure_pct": _number(median * 100),
        "full_cash_observation_days": sum(value == 0 for value in ordered),
        "below_10_percent_exposure_days": sum(value < Decimal("0.10") for value in ordered),
        "invested_observation_days": sum(value > 0 for value in ordered),
        "active_exposure_return_observations": len(active_returns),
        "positive_active_exposure_return_observations": positive_active,
        "positive_active_exposure_return_pct": (
            _number(Decimal(positive_active) / Decimal(len(active_returns)) * 100)
            if active_returns else None
        ),
    }


def _independent_sleeve_exposure_evidence(
    strategy: list[dict[str, Any]],
    parsed_curves: dict[str, list[dict[str, Any]]],
    universe_items: list,
    errors: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, int | str]] | None:
    """Rebuild v5 exposure attribution from fund-level lagged holdings.

    This implementation intentionally does not call the report producer's
    economic helper.  Fund returns come from each serialized sleeve's price;
    weights come from that sleeve's previous-observation market value divided
    by aggregate strategy equity.  The residual cash return is fixed at zero.
    """

    if not strategy:
        return None
    codes = sorted({
        str(item.get("code")) for item in universe_items
        if isinstance(item, dict) and isinstance(item.get("code"), str) and item.get("code")
    })
    curve_codes = sorted(
        curve_id.removeprefix("fund:")
        for curve_id in parsed_curves if curve_id.startswith("fund:")
    )
    if not codes or curve_codes != codes:
        _error(errors, "exposure_sleeve_universe_mismatch", "curves",
               "逐基金同敞口对照必须使用基金池中全部且仅这些分账户")
        return None
    sleeves = {code: parsed_curves.get(f"fund:{code}", []) for code in codes}
    strategy_dates = [point["date"] for point in strategy]
    for code, points in sleeves.items():
        if (len(points) != len(strategy)
                or [point["date"] for point in points] != strategy_dates
                or any(point.get("price") is None or point["price"] <= 0 for point in points)):
            _error(errors, "exposure_sleeve_evidence_invalid", f"curves.fund:{code}",
                   "逐基金同敞口对照要求日期对齐且逐日提供正的基金价格")
            return None

    exposures: list[Decimal] = []
    for index, aggregate in enumerate(strategy):
        sleeve_cash = sum((sleeves[code][index]["cash"] for code in codes), Decimal(0))
        sleeve_market = sum(
            (sleeves[code][index]["market_value"] for code in codes), Decimal(0),
        )
        if aggregate["cash"] != sleeve_cash or aggregate["market_value"] != sleeve_market:
            _error(errors, "strategy_sleeve_aggregate_mismatch",
                   f"curves.strategy.points[{index}]",
                   "综合策略的现金和市值不能由全部基金分账户精确汇总")
            return None
        if aggregate["equity"] <= 0:
            return None
        exposure = aggregate["market_value"] / aggregate["equity"]
        if not Decimal(0) <= exposure <= Decimal(1):
            return None
        exposures.append(exposure)

    initial = strategy[0]["equity"]
    equity, peak = initial, initial
    curve: list[dict[str, Any]] = []
    for index, aggregate in enumerate(strategy):
        matched_daily_return = Decimal(0)
        contributions: list[dict[str, Decimal | str]] = []
        if index:
            previous_strategy_equity = strategy[index - 1]["equity"]
            for code in codes:
                previous = sleeves[code][index - 1]
                current = sleeves[code][index]
                asset_daily_return = current["price"] / previous["price"] - 1
                prior_strategy_weight = previous["market_value"] / previous_strategy_equity
                contribution = prior_strategy_weight * asset_daily_return
                matched_daily_return += contribution
                contributions.append({
                    "code": code,
                    "prior_strategy_weight": prior_strategy_weight,
                    "asset_daily_return_pct": asset_daily_return * 100,
                    "contribution_pct": contribution * 100,
                })
            equity *= 1 + matched_daily_return
            if equity <= 0:
                _error(errors, "exposure_matched_nonpositive", "curves.exposure_matched",
                       "逐基金同敞口对照产生非正资产")
                return None
        else:
            contributions = [{
                "code": code,
                "prior_strategy_weight": Decimal(0),
                "asset_daily_return_pct": Decimal(0),
                "contribution_pct": Decimal(0),
            } for code in codes]

        exposure = exposures[index]
        market = equity * exposure
        cash = equity - market
        equity = cash + market
        peak = max(peak, equity)
        curve.append({
            "date": aggregate["date"],
            "equity": equity,
            "cash": cash,
            "market_value": market,
            "strategy_exposure": exposure,
            "strategy_exposure_pct": exposure * 100,
            "matched_daily_return_pct": matched_daily_return * 100,
            "cash_daily_return_pct": Decimal(0),
            "return_pct": (equity / initial - 1) * 100,
            "drawdown_pct": (equity / peak - 1) * 100,
            "sleeve_contributions": contributions,
        })

    ordered = sorted(exposures)
    middle = len(ordered) // 2
    median = (ordered[middle] if len(ordered) % 2
              else (ordered[middle - 1] + ordered[middle]) / 2)
    average = sum(ordered, Decimal(0)) / Decimal(len(ordered))
    active_returns = [
        strategy[index]["equity"] / strategy[index - 1]["equity"] - 1
        for index in range(1, len(strategy))
        if exposures[index - 1] > 0
    ]
    positive_active = sum(value > 0 for value in active_returns)
    return curve, {
        "observation_count": len(ordered),
        "average_exposure": _number(average),
        "average_exposure_pct": _number(average * 100),
        "median_exposure": _number(median),
        "median_exposure_pct": _number(median * 100),
        "full_cash_observation_days": sum(value == 0 for value in ordered),
        "below_10_percent_exposure_days": sum(value < Decimal("0.10") for value in ordered),
        "invested_observation_days": sum(value > 0 for value in ordered),
        "active_exposure_return_observations": len(active_returns),
        "positive_active_exposure_return_observations": positive_active,
        "positive_active_exposure_return_pct": (
            _number(Decimal(positive_active) / Decimal(len(active_returns)) * 100)
            if active_returns else None
        ),
    }


def _independent_holding_cycles(
    trades: list[object], observation_dates: list[str],
) -> dict[str, Any] | None:
    date_indexes = {value: index for index, value in enumerate(observation_dates)}
    opened: dict[str, tuple[str, int]] = {}
    completed: list[dict[str, Any]] = []
    previous_execution_index = -1
    for raw in trades:
        if not isinstance(raw, dict):
            return None
        code, action, execution = raw.get("code"), raw.get("action"), raw.get("execution_date")
        if (not isinstance(code, str) or not code or action not in TRADE_ACTIONS
                or not isinstance(execution, str) or execution not in date_indexes):
            return None
        execution_index = date_indexes[execution]
        if execution_index < previous_execution_index:
            return None
        previous_execution_index = execution_index
        if action == "BUY":
            if code in opened:
                return None
            opened[code] = (execution, execution_index)
        elif action in {"ADD", "REDUCE"}:
            if code not in opened:
                return None
        else:
            if code not in opened:
                return None
            entry_date, entry_index = opened.pop(code)
            completed.append({
                "code": code,
                "entry_date": entry_date,
                "exit_date": execution,
                "holding_observations": execution_index - entry_index,
            })
    durations = sorted(Decimal(item["holding_observations"]) for item in completed)
    median: str | None = None
    if durations:
        middle = len(durations) // 2
        median_value = (durations[middle] if len(durations) % 2
                        else (durations[middle - 1] + durations[middle]) / 2)
        median = _number(median_value)
    return {
        "trade_count": len(trades),
        "completed_cycle_count": len(completed),
        "open_cycle_count": len(opened),
        "median_holding_observation_days": median,
        "cycles_lte_10_observation_days": sum(value <= 10 for value in durations),
        "cycles_lte_20_observation_days": sum(value <= 20 for value in durations),
        "completed_cycles": completed,
        "open_cycles": [
            {"code": code, "entry_date": opened[code][0]} for code in sorted(opened)
        ],
    }


def _compare_summary(
    raw: object, expected: dict[str, Any], *, path: str, code: str,
    errors: list[dict[str, Any]],
) -> None:
    if not isinstance(raw, dict):
        _error(errors, code, path, "经济性摘要必须是可独立复算的对象")
        return
    for field, expected_value in expected.items():
        field_path = f"{path}.{field}"
        actual = raw.get(field)
        if isinstance(expected_value, str) and field not in {"entry_date", "exit_date", "code"}:
            parsed = _decimal(actual, field_path, errors)
            if parsed is not None and parsed != Decimal(expected_value):
                _error(errors, code, field_path, "经济性摘要不能由原始曲线或成交记录复算")
        elif actual != expected_value:
            _error(errors, code, field_path, "经济性摘要不能由原始曲线或成交记录复算")


def _validate_economic_evidence(
    report: dict[str, Any], *, parsed_curves: dict[str, list[dict[str, Any]]],
    raw_curves_by_id: dict[object, object], strategy_dates: list[str], trades: list[object],
    protocol: dict[str, Any], metrics: dict[str, Any], errors: list[dict[str, Any]],
    reviews: list[dict[str, str]], strategy_policy: StrategyPolicy | None,
) -> dict[str, Any] | None:
    """Validate v3 economic claims and return independently derived evidence."""
    if not _v3_report(report):
        return None
    strategy = parsed_curves.get("strategy", [])
    benchmark = parsed_curves.get("benchmark", [])
    universe = report.get("universe") if isinstance(report.get("universe"), dict) else {}
    universe_items = universe.get("items") if isinstance(universe.get("items"), list) else []
    if _v5_report(report):
        benchmark_methods = protocol.get("benchmarks")
        expected_method = "same_prior_observation_fund_sleeve_exposure_no_cost"
        if (not isinstance(benchmark_methods, list)
                or expected_method not in benchmark_methods
                or "same_daily_exposure_equal_weight_no_cost" in benchmark_methods):
            _error(errors, "exposure_benchmark_protocol_mismatch", "protocol.benchmarks",
                   "v5报告必须声明逐基金上一观察日敞口的无成本对照方法")
        exposure_evidence = _independent_sleeve_exposure_evidence(
            strategy, parsed_curves, universe_items, errors,
        )
    else:
        exposure_evidence = _independent_exposure_evidence(strategy, benchmark)
    if exposure_evidence is None:
        return None
    expected_curve, expected_exposure = exposure_evidence

    reported_curve = raw_curves_by_id.get("exposure_matched")
    reported_points = (reported_curve.get("points") if isinstance(reported_curve, dict) else None)
    if isinstance(reported_points, list) and len(reported_points) == len(expected_curve):
        numeric_fields = (
            ("equity", "cash", "market_value", "strategy_exposure",
             "strategy_exposure_pct", "matched_daily_return_pct", "cash_daily_return_pct",
             "return_pct", "drawdown_pct")
            if _v5_report(report)
            else ("equity", "cash", "market_value", "strategy_exposure",
                  "strategy_exposure_pct", "benchmark_daily_return_pct",
                  "return_pct", "drawdown_pct")
        )
        for index, (actual, expected) in enumerate(zip(reported_points, expected_curve)):
            path = f"curves.exposure_matched.points[{index}]"
            if not isinstance(actual, dict) or actual.get("date") != expected["date"]:
                _error(errors, "exposure_matched_not_reproducible", path,
                       "同敞口曲线日期不能由策略与基准曲线复算")
                continue
            for field in numeric_fields:
                value = _decimal(actual.get(field), f"{path}.{field}", errors)
                if value is not None and value != expected[field]:
                    _error(errors, "exposure_matched_not_reproducible", f"{path}.{field}",
                           "同敞口曲线不能由前一日策略敞口与当日基准收益复算")
            if _v5_report(report):
                actual_contributions = actual.get("sleeve_contributions")
                expected_contributions = expected["sleeve_contributions"]
                if (not isinstance(actual_contributions, list)
                        or len(actual_contributions) != len(expected_contributions)):
                    _error(errors, "exposure_sleeve_contributions_not_reproducible",
                           f"{path}.sleeve_contributions",
                           "逐基金收益贡献必须覆盖基金池中的全部分账户")
                    continue
                for sleeve_index, (actual_sleeve, expected_sleeve) in enumerate(
                        zip(actual_contributions, expected_contributions)):
                    sleeve_path = f"{path}.sleeve_contributions[{sleeve_index}]"
                    if (not isinstance(actual_sleeve, dict)
                            or actual_sleeve.get("code") != expected_sleeve["code"]):
                        _error(errors, "exposure_sleeve_contributions_not_reproducible",
                               sleeve_path, "逐基金收益贡献的基金身份或顺序不能独立复算")
                        continue
                    for field in ("prior_strategy_weight", "asset_daily_return_pct",
                                  "contribution_pct"):
                        value = _decimal(actual_sleeve.get(field), f"{sleeve_path}.{field}", errors)
                        if value is not None and value != expected_sleeve[field]:
                            _error(errors, "exposure_sleeve_contributions_not_reproducible",
                                   f"{sleeve_path}.{field}",
                                   "逐基金收益贡献不能由上一观察日市值和下一期基金收益复算")
    else:
        _error(errors, "exposure_matched_not_reproducible", "curves.exposure_matched",
               "同敞口曲线长度必须与策略曲线一致")

    cash_points = parsed_curves.get("cash", [])
    protocol_initial = _decimal(protocol.get("initial_cash"), "protocol.initial_cash", errors)
    if len(cash_points) == len(strategy_dates) and protocol_initial is not None:
        for index, point in enumerate(cash_points):
            if (point["date"] != strategy_dates[index] or point["cash"] != protocol_initial
                    or point["market_value"] != 0 or point["equity"] != protocol_initial
                    or point["return_pct"] != 0 or point["drawdown_pct"] != 0):
                _error(errors, "cash_curve_not_constant", f"curves.cash.points[{index}]",
                       "现金对照必须逐日保持初始资产、零持仓、零收益和零回撤")
    else:
        _error(errors, "cash_curve_not_constant", "curves.cash",
               "现金对照曲线长度必须与策略曲线一致")

    _compare_summary(report.get("exposure_summary"), expected_exposure,
                     path="exposure_summary", code="exposure_summary_not_reproducible", errors=errors)
    holding = _independent_holding_cycles(trades, strategy_dates)
    if holding is not None:
        _compare_summary(report.get("holding_cycles"), holding, path="holding_cycles",
                         code="holding_cycles_not_reproducible", errors=errors)

    exposure_return = expected_curve[-1]["return_pct"]
    strategy_return = ((strategy[-1]["equity"] / strategy[0]["equity"] - 1) * 100
                       if strategy else None)
    actual_exposure_return = _decimal(metrics.get("exposure_matched_return_pct"),
                                      "metrics.exposure_matched_return_pct", errors)
    if actual_exposure_return is not None and actual_exposure_return != exposure_return:
        _error(errors, "metric_not_reproducible", "metrics.exposure_matched_return_pct",
               "同敞口收益不能由同敞口曲线复算")
    timing_excess = strategy_return - exposure_return if strategy_return is not None else None
    actual_timing_excess = _decimal(metrics.get("exposure_timing_excess_pct"),
                                    "metrics.exposure_timing_excess_pct", errors)
    if actual_timing_excess is not None and timing_excess is not None and actual_timing_excess != timing_excess:
        _error(errors, "metric_not_reproducible", "metrics.exposure_timing_excess_pct",
               "敞口择时差额不能由策略收益与同敞口收益复算")

    if Decimal(str(expected_exposure["average_exposure"])) < LOW_AVERAGE_EXPOSURE:
        _review(reviews, "low_market_exposure", "策略平均市场敞口低于20%，与满仓基准的差额主要受现金仓位影响")
    if strategy_return is not None and strategy_return < exposure_return:
        _review(reviews, "underperformed_exposure_matched",
                "策略收益低于同敞口无成本对照，交易成本或选时执行没有增加净收益")
    if holding is not None and holding["completed_cycle_count"]:
        median = holding["median_holding_observation_days"]
        if median is not None and Decimal(str(median)) <= SHORT_HOLDING_OBSERVATIONS:
            _review(reviews, "short_holding_period", "已完成持仓周期的中位数不超过20个观察日，需复核短持有成本")

    cost_stress = _validate_cost_stress(
        report.get("cost_stress"), protocol=protocol, metrics=metrics, errors=errors,
        reviews=reviews, strategy_dates=strategy_dates, parsed_curves=parsed_curves,
        universe_items=universe_items,
        strategy_policy=strategy_policy if _v5_report(report) else None,
    )
    attribution = _validate_economic_attribution(
        report.get("economic_attribution"), exposure_return=exposure_return,
        strategy_return=strategy_return, cost_stress=cost_stress,
        errors=errors, reviews=reviews,
    )
    return {
        "exposure_summary": expected_exposure,
        "holding_cycles": holding,
        "exposure_matched_return_pct": _number(Decimal(str(exposure_return))),
        "exposure_timing_excess_pct": (_number(timing_excess) if timing_excess is not None else None),
        "cost_stress": cost_stress,
        "economic_attribution": attribution,
    }


def _validate_cost_stress(
    raw: object, *, protocol: dict[str, Any], metrics: dict[str, Any],
    errors: list[dict[str, Any]], reviews: list[dict[str, str]],
    strategy_dates: list[str], parsed_curves: dict[str, list[dict[str, Any]]],
    universe_items: list, strategy_policy: StrategyPolicy | None,
) -> list[dict[str, Any]]:
    if not isinstance(raw, list) or not raw:
        _error(errors, "cost_stress_missing", "cost_stress", "v3报告必须提供成本压力场景")
        return []
    requested_buy = _decimal(protocol.get("buy_fee_bps"), "protocol.buy_fee_bps", errors)
    requested_sell = _decimal(protocol.get("sell_fee_bps"), "protocol.sell_fee_bps", errors)
    initial = _decimal(protocol.get("initial_cash"), "protocol.initial_cash", errors)
    main_final = _decimal(metrics.get("final_equity"), "metrics.final_equity", errors)
    main_return = _decimal(metrics.get("total_return_pct"), "metrics.total_return_pct", errors)
    main_fees = _decimal(metrics.get("total_fees"), "metrics.total_fees", errors)
    main_trades = metrics.get("trade_count")
    scenarios: list[dict[str, Any]] = []
    pairs: set[tuple[Decimal, Decimal]] = set()
    requested_count = 0
    replay_inputs: dict[str, tuple[list[dict[str, str]], Decimal]] = {}
    if strategy_policy is not None and strategy_dates:
        expected_codes = sorted(
            str(item.get("code")) for item in universe_items
            if isinstance(item, dict) and isinstance(item.get("code"), str) and item.get("code")
        )
        for code in expected_codes:
            points = parsed_curves.get(f"fund:{code}", [])
            if (len(points) != len(strategy_dates)
                    or any(point.get("price") is None or point.get("units") is None for point in points)
                    or not points or points[0]["units"] != 0 or points[0]["market_value"] != 0):
                _error(errors, "cost_stress_replay_input_invalid", f"curves.fund:{code}",
                       "成本压力独立重放要求完整价格、零初始持仓和全部共同观察日")
                replay_inputs = {}
                break
            replay_inputs[code] = (
                [{"date": str(point["date"]), "close": _number(point["price"])}
                 for point in points],
                points[0]["equity"],
            )
        if expected_codes and set(replay_inputs) != set(expected_codes):
            replay_inputs = {}

    replay_calendar = (
        ObservedSessionCalendar(strategy_dates, source="independent_cost_stress_replay")
        if replay_inputs else None
    )
    for index, item in enumerate(raw):
        path = f"cost_stress[{index}]"
        if not isinstance(item, dict):
            _error(errors, "cost_stress_scenario_invalid", path, "成本压力场景必须是对象")
            continue
        buy = _decimal(item.get("buy_fee_bps"), f"{path}.buy_fee_bps", errors)
        sell = _decimal(item.get("sell_fee_bps"), f"{path}.sell_fee_bps", errors)
        final = _decimal(item.get("final_equity"), f"{path}.final_equity", errors)
        return_pct = _decimal(item.get("total_return_pct"), f"{path}.total_return_pct", errors)
        fees = _decimal(item.get("total_fees"), f"{path}.total_fees", errors)
        trade_count = _count(item.get("trade_count"), f"{path}.trade_count", errors)
        requested = item.get("is_requested_scenario")
        if not isinstance(requested, bool):
            _error(errors, "cost_stress_requested_flag_invalid", f"{path}.is_requested_scenario",
                   "成本压力场景必须明确是否为请求费率")
            requested = False
        if None in (buy, sell, final, return_pct, fees, trade_count):
            continue
        assert buy is not None and sell is not None and final is not None and return_pct is not None
        assert fees is not None and trade_count is not None
        if buy < 0 or sell < 0 or fees < 0 or final <= 0:
            _error(errors, "cost_stress_value_invalid", path, "成本、费用必须非负且期末资产必须为正")
        pair = (buy, sell)
        if pair in pairs:
            _error(errors, "cost_stress_duplicate_scenario", path, "成本压力场景费率不能重复")
        pairs.add(pair)
        if initial is not None and return_pct != (final / initial - 1) * 100:
            _error(errors, "cost_stress_return_not_reproducible", f"{path}.total_return_pct",
                   "成本压力收益不能由初始与期末资产复算")
        matches_requested = buy == requested_buy and sell == requested_sell
        if requested != matches_requested:
            _error(errors, "cost_stress_requested_scenario_mismatch", f"{path}.is_requested_scenario",
                   "请求费率标记与回放协议不一致")
        if requested:
            requested_count += 1
            if ((main_final is not None and final != main_final)
                    or (main_return is not None and return_pct != main_return)
                    or (main_fees is not None and fees != main_fees)
                    or (isinstance(main_trades, int) and trade_count != main_trades)):
                _error(errors, "cost_stress_requested_result_mismatch", path,
                       "请求费率压力场景必须与主报告结果完全一致")
        if replay_calendar is not None and strategy_policy is not None:
            replayed = []
            for code in sorted(replay_inputs):
                rows, allocation = replay_inputs[code]
                simulation = simulate_strategy(
                    rows, calendar=replay_calendar, initial_cash=allocation,
                    costs=SimulationCosts(buy_fee_bps=buy, sell_fee_bps=sell),
                    policy=strategy_policy,
                )
                replayed.append((code, simulation))
            replay_final = sum(
                (Decimal(str(simulation["final_equity"])) for _, simulation in replayed),
                Decimal(0),
            )
            replay_trades = sorted(
                [(code, trade) for code, simulation in replayed
                 for trade in simulation["trades"]],
                key=lambda value: (str(value[1]["execution_date"]), value[0]),
            )
            replay_fees = sum(
                (Decimal(str(trade["fee"])) for _, trade in replay_trades), Decimal(0),
            )
            if final != replay_final or fees != replay_fees or trade_count != len(replay_trades):
                _error(errors, "cost_stress_not_independently_reproducible", path,
                       "成本压力场景不能由基金价格、策略参数和费率独立重放")
        scenarios.append({
            "buy_fee_bps": _number(buy), "sell_fee_bps": _number(sell),
            "is_requested_scenario": requested, "final_equity": _number(final),
            "total_return_pct": _number(return_pct), "total_fees": _number(fees),
            "trade_count": trade_count,
        })
    if requested_count != 1:
        _error(errors, "cost_stress_requested_scenario_count", "cost_stress",
               "成本压力场景必须且只能有一个请求费率场景")
    required_pairs = {(Decimal(0), Decimal(0)), (Decimal(50), Decimal(50)), (Decimal(100), Decimal(100))}
    if not required_pairs.issubset(pairs):
        _error(errors, "cost_stress_standard_scenarios_missing", "cost_stress",
               "成本压力必须覆盖双边0、50和100基点")
    symmetric = {Decimal(item["buy_fee_bps"]): Decimal(item["total_return_pct"])
                 for item in scenarios if item["buy_fee_bps"] == item["sell_fee_bps"]}
    for lower, higher in ((Decimal(0), Decimal(50)), (Decimal(50), Decimal(100))):
        if lower in symmetric and higher in symmetric and symmetric[higher] > symmetric[lower]:
            _error(errors, "cost_stress_monotonicity_broken", "cost_stress",
                   "相同成交路径下提高双边费率不应改善净收益")
    if Decimal(0) in symmetric and Decimal(100) in symmetric:
        gap = symmetric[Decimal(0)] - symmetric[Decimal(100)]
        if gap >= COST_SENSITIVITY_RETURN_GAP_PCT:
            _review(reviews, "cost_sensitive", "双边100基点相对零费率拖累至少5个百分点，策略对真实成本敏感")
    return scenarios


def _validate_economic_attribution(
    raw: object, *, exposure_return: Decimal, strategy_return: Decimal | None,
    cost_stress: list[dict[str, Any]], errors: list[dict[str, Any]],
    reviews: list[dict[str, str]],
) -> dict[str, str] | None:
    """Reproduce the additive return attribution from independent evidence."""
    if not isinstance(raw, dict) or strategy_return is None:
        _error(errors, "economic_attribution_missing", "economic_attribution",
               "v3报告必须提供可复算的收益归因")
        return None
    zero_cost = next(
        (item for item in cost_stress
         if Decimal(item["buy_fee_bps"]) == 0 and Decimal(item["sell_fee_bps"]) == 0),
        None,
    )
    if zero_cost is None:
        return None
    zero_cost_return = Decimal(zero_cost["total_return_pct"])
    expected = {
        "cash_return_pct": Decimal(0),
        "market_exposure_return_pct": exposure_return,
        "signal_and_execution_before_cost_pct": zero_cost_return - exposure_return,
        "modeled_transaction_cost_drag_pct": strategy_return - zero_cost_return,
        "strategy_total_return_pct": strategy_return,
    }
    for field, expected_value in expected.items():
        value = _decimal(raw.get(field), f"economic_attribution.{field}", errors)
        if value is not None and value != expected_value:
            _error(errors, "economic_attribution_not_reproducible",
                   f"economic_attribution.{field}",
                   "收益归因不能由同敞口曲线、零费率场景和主策略收益复算")
    if (expected["market_exposure_return_pct"]
            + expected["signal_and_execution_before_cost_pct"]
            + expected["modeled_transaction_cost_drag_pct"]
            != expected["strategy_total_return_pct"]):
        _error(errors, "economic_attribution_identity_failed", "economic_attribution",
               "收益归因加总不等于策略总收益")
    if expected["signal_and_execution_before_cost_pct"] < 0:
        _review(reviews, "signal_execution_negative_before_cost",
                "即使不计交易费用，策略信号与执行仍跑输同敞口对照")
    return {field: _number(value) for field, value in expected.items()}


def _validate_strategy_report(report: object) -> dict[str, Any]:
    """Validate a historical_strategy_report result without writing any state."""
    errors: list[dict[str, Any]] = []
    reviews: list[dict[str, str]] = []
    if not isinstance(report, dict):
        _error(errors, "report_invalid", "$", "策略报告必须是对象")
        return _result({}, errors, reviews, {}, {}, [], [])

    source_status = report.get("status")
    if source_status == "insufficient":
        _error(errors, "source_report_insufficient", "status", str(report.get("reason") or "历史报告资料不足"))
    elif source_status not in {"completed", "review_required"}:
        _error(errors, "source_status_invalid", "status", "历史报告状态不可验收")

    metrics = report.get("metrics") if isinstance(report.get("metrics"), dict) else {}
    protocol = report.get("protocol") if isinstance(report.get("protocol"), dict) else {}
    initial = _decimal(metrics.get("initial_equity"), "metrics.initial_equity", errors)
    protocol_initial = _decimal(protocol.get("initial_cash"), "protocol.initial_cash", errors)
    protocol_fees: dict[str, Decimal | None] = {}
    for field in ("buy_fee_bps", "sell_fee_bps"):
        value = _decimal(protocol.get(field), f"protocol.{field}", errors)
        protocol_fees[field] = value
        if value is not None and not Decimal(0) <= value < Decimal("10000"):
            _error(errors, "fee_basis_points_invalid", f"protocol.{field}", "交易费率基点必须在0到10000之间")
    strategy_policy = _strategy_policy_for_replay(report, protocol, errors)
    if initial is not None and initial <= 0:
        _error(errors, "initial_equity_invalid", "metrics.initial_equity", "初始资产必须大于零")
    if initial is not None and protocol_initial is not None and initial != protocol_initial:
        _error(errors, "initial_cash_mismatch", "protocol.initial_cash", "协议初始资金与策略初始资产不一致")

    raw_curves = report.get("curves")
    if not isinstance(raw_curves, list):
        _error(errors, "curves_invalid", "curves", "curves必须是数组")
        raw_curves = []
    ids = [curve.get("id") for curve in raw_curves if isinstance(curve, dict)]
    if len(ids) != len(set(ids)):
        _error(errors, "duplicate_curve_ids", "curves", "曲线身份必须唯一")
    by_id = {curve.get("id"): curve for curve in raw_curves if isinstance(curve, dict)}
    required_curves = ["strategy", "benchmark"]
    if _v3_report(report):
        required_curves.extend(("exposure_matched", "cash"))
    for required in required_curves:
        if required not in by_id:
            _error(errors, "required_curve_missing", "curves", f"缺少{required}曲线")

    curve_summaries: dict[str, dict[str, Any]] = {}
    parsed_curves: dict[str, list[dict[str, Any]]] = {}
    curve_dates: dict[str, list[str]] = {}
    curve_paths: dict[str, str] = {}
    for index, curve in enumerate(raw_curves):
        if not isinstance(curve, dict):
            _error(errors, "curve_invalid", f"curves[{index}]", "曲线必须是对象")
            continue
        curve_id = curve.get("id")
        if not isinstance(curve_id, str) or not curve_id:
            _error(errors, "curve_id_invalid", f"curves[{index}].id", "曲线必须有稳定身份")
            continue
        baseline = protocol_initial if curve_id in {"strategy", "benchmark"} else None
        if baseline is None and isinstance(curve.get("points"), list) and curve["points"]:
            first = curve["points"][0]
            if isinstance(first, dict):
                baseline = _decimal(first.get("equity"), f"curves[{index}].points[0].equity_baseline", errors)
        dates, parsed, summary = _curve(curve, path=f"curves[{index}]", baseline=baseline, errors=errors)
        curve_dates[curve_id], parsed_curves[curve_id] = dates, parsed
        curve_paths[curve_id] = f"curves[{index}]"
        if curve_id.startswith("fund:"):
            for point_index, point in enumerate(parsed):
                if point.get("price") is None or point.get("units") is None:
                    _error(
                        errors,
                        "fund_curve_state_missing",
                        f"curves[{index}].points[{point_index}]",
                        "基金分账户曲线必须逐日提供price和units以复核成交账本",
                    )
        if summary is not None:
            curve_summaries[curve_id] = summary

    strategy_dates = curve_dates.get("strategy", [])
    for curve_id, dates in curve_dates.items():
        if curve_id != "strategy" and dates != strategy_dates:
            _error(errors, "curve_dates_not_aligned", f"curves.{curve_id}", "所有收益曲线必须使用同一组观察日期")

    input_identity = report.get("input_identity") if isinstance(report.get("input_identity"), dict) else {}
    common_count = _count(input_identity.get("common_observations"), "input_identity.common_observations", errors)
    minimum = StrategyPolicy().long_window + 2
    if common_count is not None:
        if common_count != len(strategy_dates):
            _error(errors, "common_observation_count_mismatch", "input_identity.common_observations",
                   "共同观察日数量与策略曲线不一致")
        if common_count < minimum:
            _error(errors, "insufficient_common_observations", "input_identity.common_observations",
                   f"共同真实观察日少于长期窗口所需的{minimum}日")
        if common_count < 244:
            _review(reviews, "less_than_one_trading_year", "共同观察日不足一个常用交易年，长期结论仍需继续前瞻观察")
    identity_hash = input_identity.get("hash")
    if not isinstance(identity_hash, str) or len(identity_hash) != 64 or any(c not in "0123456789abcdef" for c in identity_hash):
        _error(errors, "input_identity_invalid", "input_identity.hash", "输入身份必须是64位小写SHA-256")

    if strategy_dates:
        if report.get("start_date") != strategy_dates[0] or report.get("end_date") != strategy_dates[-1]:
            _error(errors, "report_range_mismatch", "start_date/end_date", "报告范围与实际共同曲线范围不一致")
        span_days = (date.fromisoformat(strategy_dates[-1]) - date.fromisoformat(strategy_dates[0])).days
        if span_days < 365:
            _review(reviews, "calendar_span_under_one_year", "共同观察日期跨度不足一年，市场环境覆盖有限")
    else:
        span_days = 0
    generated_date = _timestamp_date(report.get("generated_at"), "generated_at", errors)
    lag_days = None
    if strategy_dates and generated_date is not None:
        lag_days = (generated_date - date.fromisoformat(strategy_dates[-1])).days
        if lag_days < 0:
            _error(errors, "curve_after_report_generation", "end_date", "曲线结束日晚于报告生成日")
        elif lag_days > 7:
            _review(reviews, "common_history_stale", "共同历史序列距离报告生成日超过7个自然日，需补齐较慢的数据源")

    calendar_document = report.get("calendar_document") if isinstance(report.get("calendar_document"), dict) else {}
    if strategy_dates:
        for field, expected in (("coverage_start", strategy_dates[0]), ("coverage_end", strategy_dates[-1]),
                                ("session_count", len(strategy_dates))):
            if calendar_document.get(field) != expected:
                _error(errors, "calendar_document_mismatch", f"calendar_document.{field}",
                       "日历文档与共同观察曲线不一致")

    universe = report.get("universe") if isinstance(report.get("universe"), dict) else {}
    universe_items = universe.get("items") if isinstance(universe.get("items"), list) else []
    date_intersection_audit = _validate_date_intersection_audit(
        report.get("date_intersection_audit"),
        strategy_dates=strategy_dates,
        common_count=common_count,
        universe_items=universe_items,
        errors=errors,
        reviews=reviews,
    )
    observed_date_audit = _validate_observed_date_audit(
        report.get("observed_date_audit"),
        report_version=report.get("version"),
        strategy_dates=strategy_dates,
        universe_items=universe_items,
        errors=errors,
        reviews=reviews,
    )
    investment_eligibility = _validate_investment_eligibility(
        report.get("investment_eligibility"),
        report_version=report.get("version"),
        universe=universe,
        universe_items=universe_items,
        protocol=protocol,
        errors=errors,
        reviews=reviews,
    )

    strategy_points = parsed_curves.get("strategy", [])
    benchmark_points = parsed_curves.get("benchmark", [])
    if _v5_report(report):
        try:
            expected_risk_quality = _independent_risk_quality(strategy_points, errors)
        except DecimalException:
            _error(errors, "risk_quality_non_finite", "risk_quality",
                   "风险质量年化计算超出有限Decimal范围")
            expected_risk_quality = None
        risk_quality = _validate_risk_quality(
            report.get("risk_quality"), expected_risk_quality, errors,
        )
    else:
        risk_quality = None
    strategy_summary = curve_summaries.get("strategy")
    benchmark_summary = curve_summaries.get("benchmark")
    if strategy_summary and initial is not None:
        if Decimal(strategy_summary["initial_equity"]) != initial:
            _error(errors, "strategy_initial_equity_mismatch", "metrics.initial_equity", "策略曲线起点与初始资产不一致")
        strategy_return = (Decimal(strategy_summary["final_equity"]) / initial - 1) * 100
        strategy_drawdown = Decimal(strategy_summary["max_drawdown_pct"])
        _compare_metric(report, "final_equity", Decimal(strategy_summary["final_equity"]), errors)
        _compare_metric(report, "total_return_pct", strategy_return, errors)
        _compare_metric(report, "max_drawdown_pct", strategy_drawdown, errors)
    else:
        strategy_return = strategy_drawdown = None
    if benchmark_summary and initial is not None:
        benchmark_return = (Decimal(benchmark_summary["final_equity"]) / initial - 1) * 100
        benchmark_drawdown = Decimal(benchmark_summary["max_drawdown_pct"])
        _compare_metric(report, "benchmark_return_pct", benchmark_return, errors)
    else:
        benchmark_return = benchmark_drawdown = None
    if strategy_return is not None and benchmark_return is not None:
        _compare_metric(report, "excess_return_pct", strategy_return - benchmark_return, errors)
        if strategy_return < 0:
            _review(reviews, "negative_strategy_return", "策略长期收益为负；结构仍可复算，但策略表现需要复盘")
        if strategy_return < benchmark_return:
            _review(reviews, "strategy_underperformed_benchmark", "策略收益低于同基金等权持有基准")
        if strategy_drawdown is not None and benchmark_drawdown is not None and strategy_drawdown < benchmark_drawdown:
            _review(reviews, "strategy_drawdown_worse_than_benchmark", "策略最大回撤深于同基金等权持有基准")

    trades_raw = report.get("trades")
    if not isinstance(trades_raw, list):
        _error(errors, "trades_invalid", "trades", "trades必须是数组")
        trades_raw = []
    actual_actions: Counter[str] = Counter()
    total_fees, total_gross = Decimal(0), Decimal(0)
    trade_keys: set[tuple[str, str]] = set()
    trade_records: dict[tuple[str, str], dict[str, Any]] = {}
    strategy_date_index = {value: index for index, value in enumerate(strategy_dates)}
    for index, trade in enumerate(trades_raw):
        path = f"trades[{index}]"
        if not isinstance(trade, dict):
            _error(errors, "trade_invalid", path, "成交必须是对象")
            continue
        action = trade.get("action")
        signal_action = trade.get("signal_action")
        if action not in TRADE_ACTIONS:
            _error(errors, "trade_action_invalid", f"{path}.action", "成交动作不受支持")
        else:
            actual_actions[str(action)] += 1
        if _v4_report(report) and signal_action not in TRADE_ACTIONS:
            _error(errors, "trade_signal_action_invalid", f"{path}.signal_action",
                   "v4成交必须保存产生该成交的策略信号动作")
        signal = _day(trade.get("signal_date"), f"{path}.signal_date", errors)
        execution = _day(trade.get("execution_date"), f"{path}.execution_date", errors)
        if signal is not None and execution is not None:
            if signal >= execution:
                _error(errors, "trade_not_after_signal", path, "execution_date必须晚于signal_date")
            signal_index = strategy_date_index.get(signal)
            execution_index = strategy_date_index.get(execution)
            if strategy_dates and (signal_index is None or execution_index is None):
                _error(errors, "trade_date_outside_curve", path, "信号和成交日期必须属于共同观察日")
            elif signal_index is not None and execution_index != signal_index + 1:
                _error(errors, "trade_not_next_observation", path,
                       "成交日必须恰好是信号日后的下一曲线观察日")
        code = trade.get("code")
        trade_key: tuple[str, str] | None = None
        duplicate_trade = False
        if not isinstance(code, str) or not code:
            _error(errors, "trade_code_invalid", f"{path}.code", "成交必须引用基金代码")
        elif execution is not None:
            trade_key = (code, execution)
            if trade_key in trade_keys:
                _error(errors, "duplicate_trade_execution", path, "同一基金同一观察日只能有一笔策略成交")
                duplicate_trade = True
            trade_keys.add(trade_key)
        parsed_numbers = {field: _decimal(trade.get(field), f"{path}.{field}", errors)
                          for field in ("price", "target_weight", "units_delta", "units_after", "gross_amount",
                                        "fee", "cash_after", "equity_after")}
        if all(value is not None for value in parsed_numbers.values()):
            values = {field: value for field, value in parsed_numbers.items() if value is not None}
            if any(values[field] < 0 for field in ("units_after", "gross_amount", "fee", "cash_after", "equity_after")):
                _error(errors, "negative_trade_balance", path, "成交后的份额、金额、费用、现金和资产不能为负")
            if values["price"] <= 0 or values["equity_after"] <= 0 or values["gross_amount"] <= 0:
                _error(errors, "nonpositive_trade_value", path, "成交价、成交额和成交后资产必须大于零")
            if not Decimal(0) <= values["target_weight"] <= Decimal(1):
                _error(errors, "target_weight_invalid", f"{path}.target_weight", "目标仓位必须在0到1之间")
            if action in {"BUY", "ADD"} and values["units_delta"] <= 0:
                _error(errors, "trade_direction_mismatch", f"{path}.units_delta", "买入或加仓的份额变化必须大于零")
            if action in {"REDUCE", "EXIT"} and values["units_delta"] >= 0:
                _error(errors, "trade_direction_mismatch", f"{path}.units_delta", "减仓或清仓的份额变化必须小于零")
            expected_gross = abs(values["units_delta"]) * values["price"]
            if values["gross_amount"] != expected_gross:
                _error(errors, "trade_gross_units_mismatch", f"{path}.gross_amount",
                       "成交额必须精确等于份额变化绝对值乘成交价")
            fee_field = ("buy_fee_bps" if action in {"BUY", "ADD"}
                         else "sell_fee_bps" if action in {"REDUCE", "EXIT"} else None)
            fee_bps = protocol_fees.get(fee_field) if fee_field is not None else None
            if fee_bps is not None:
                expected_fee = values["gross_amount"] * (fee_bps / Decimal("10000"))
                if values["fee"] != expected_fee:
                    _error(errors, "trade_fee_mismatch", f"{path}.fee",
                           "成交费用必须由成交额和协议费率精确复算")
            if values["equity_after"] != values["cash_after"] + values["units_after"] * values["price"]:
                _error(errors, "trade_equity_identity_mismatch", path, "成交后资产恒等式不成立")
            if values["equity_after"] > 0:
                post_weight = values["units_after"] * values["price"] / values["equity_after"]
                if abs(post_weight - values["target_weight"]) > Decimal("1e-36"):
                    _error(errors, "trade_target_weight_not_reached", f"{path}.target_weight",
                           "成交后的持仓市值占资产比例必须与目标仓位一致")
            if action == "EXIT" and values["units_after"] != 0:
                _error(errors, "exit_units_not_zero", f"{path}.units_after", "清仓成交后的份额必须精确为零")
            if trade_key is not None and not duplicate_trade:
                trade_records[trade_key] = {
                    "path": path,
                    "code": code,
                    "action": action,
                    "signal_action": signal_action,
                    "signal_date": signal,
                    "execution_date": execution,
                    "values": values,
                }
            total_fees += values["fee"]
            total_gross += values["gross_amount"]

    # Reconcile every reported trade against its independently serialized fund
    # sleeve, then ensure no sleeve cash/unit transition exists without a
    # matching trade.  Price movement may change market value and equity, but
    # cannot change cash or units by itself.
    for trade_key, record in trade_records.items():
        code, execution = trade_key
        curve_id = f"fund:{code}"
        points = parsed_curves.get(curve_id)
        curve_path = curve_paths.get(curve_id, f"curves.{curve_id}")
        if points is None:
            _error(errors, "trade_fund_curve_missing", record["path"],
                   "每笔成交都必须有对应的fund:{code}分账户曲线")
            continue
        if len(points) != len(strategy_dates):
            continue
        point_by_date = {point["date"]: (index, point) for index, point in enumerate(points)}
        located = point_by_date.get(execution)
        if located is None:
            _error(errors, "trade_execution_curve_point_missing", record["path"],
                   "基金分账户曲线缺少成交观察日")
            continue
        point_index, current = located
        if point_index == 0:
            _error(errors, "trade_previous_curve_point_missing", record["path"],
                   "成交观察日前必须有可复核的分账户状态")
            continue
        previous = points[point_index - 1]
        values = record["values"]
        point_path = f"{curve_path}.points[{point_index}]"
        if current.get("price") is not None and values["price"] != current["price"]:
            _error(errors, "trade_price_curve_mismatch", record["path"],
                   "成交价必须等于基金分账户在执行日的价格")
        post_fields = (("cash_after", "cash"), ("units_after", "units"),
                       ("equity_after", "equity"))
        for trade_field, curve_field in post_fields:
            curve_value = current.get(curve_field)
            if curve_value is not None and values[trade_field] != curve_value:
                _error(errors, "trade_post_state_curve_mismatch", record["path"],
                       f"成交后{trade_field}必须等于执行日分账户{curve_field}")
        previous_units = previous.get("units")
        if previous_units is not None and values["units_after"] != previous_units + values["units_delta"]:
            _error(errors, "trade_units_transition_mismatch", point_path,
                   "执行日份额必须由上一观察日份额加本次份额变化精确得到")
        if record["action"] in {"BUY", "ADD"}:
            expected_cash = previous["cash"] - values["gross_amount"] - values["fee"]
        elif record["action"] in {"REDUCE", "EXIT"}:
            expected_cash = previous["cash"] + values["gross_amount"] - values["fee"]
        else:
            expected_cash = None
        if expected_cash is not None and values["cash_after"] != expected_cash:
            _error(errors, "trade_cash_transition_mismatch", point_path,
                   "执行日现金必须由上一观察日现金、成交额和费用精确得到")

    for curve_id, points in parsed_curves.items():
        if not curve_id.startswith("fund:") or len(points) != len(strategy_dates):
            continue
        code = curve_id.removeprefix("fund:")
        curve_path = curve_paths.get(curve_id, f"curves.{curve_id}")
        for point_index in range(1, len(points)):
            previous, current = points[point_index - 1], points[point_index]
            if previous.get("units") is None or current.get("units") is None:
                continue
            if (code, current["date"]) not in trade_records and (
                    current["cash"] != previous["cash"] or current["units"] != previous["units"]):
                _error(errors, "unreported_fund_state_change", f"{curve_path}.points[{point_index}]",
                       "无成交的相邻观察日之间现金和份额必须保持不变")

    metric_trade_count = _count(metrics.get("trade_count"), "metrics.trade_count", errors)
    if metric_trade_count is not None and metric_trade_count != len(trades_raw):
        _error(errors, "trade_count_mismatch", "metrics.trade_count", "成交总数与成交明细不一致")
    _compare_metric(report, "total_fees", total_fees, errors)
    if "benchmark_initial_fee" in metrics:
        benchmark_fee = _decimal(metrics.get("benchmark_initial_fee"), "metrics.benchmark_initial_fee", errors)
        if benchmark_fee is not None and benchmark_fee < 0:
            _error(errors, "benchmark_fee_negative", "metrics.benchmark_initial_fee", "基准初始费用不能小于零")

    executed = report.get("executed_action_summary")
    if not isinstance(executed, dict):
        _error(errors, "executed_action_summary_invalid", "executed_action_summary", "成交动作汇总必须是对象")
        executed = {}
    for action in TRADE_ACTIONS:
        count = _count(executed.get(action), f"executed_action_summary.{action}", errors)
        if count is not None and count != actual_actions[action]:
            _error(errors, "executed_action_count_mismatch", f"executed_action_summary.{action}",
                   "成交动作汇总与成交明细不一致")
        if actual_actions[action] == 0:
            _review(reviews, f"missing_executed_{action.lower()}", f"长周期中没有实际执行{action}动作")
    if not trades_raw:
        _review(reviews, "strategy_never_trades", "策略在整个验证区间没有任何成交")
    elif len(actual_actions) == 1:
        _review(reviews, "single_executed_action", "策略长期只执行一种成交动作，买入到退出闭环不完整")

    action_summary = report.get("action_summary")
    if not isinstance(action_summary, dict):
        _error(errors, "action_summary_invalid", "action_summary", "决策动作汇总必须是对象")
        action_summary = {}
    action_total = 0
    for action in DECISION_ACTIONS:
        count = _count(action_summary.get(action), f"action_summary.{action}", errors)
        if count is not None:
            action_total += count
        if action in {"HOLD", "WAIT"} and count == 0:
            _review(reviews, f"missing_{action.lower()}_decision", f"长周期没有{action}决策，需核对状态机是否过度交易")
    if strategy_policy is not None:
        try:
            replayed_actions = _replay_strategy_decisions(
                policy=strategy_policy,
                strategy_dates=strategy_dates,
                parsed_curves=parsed_curves,
                trade_records=trade_records,
                universe_items=universe_items,
                errors=errors,
            )
        except (ArithmeticError, KeyError, TypeError, ValueError) as exc:
            _error(errors, "strategy_replay_failed", "curves", f"独立策略重放失败：{exc}")
            replayed_actions = Counter()
        for action in DECISION_ACTIONS:
            reported = action_summary.get(action)
            if isinstance(reported, int) and not isinstance(reported, bool) and reported >= 0:
                if reported != replayed_actions[action]:
                    _error(errors, "decision_action_not_reproducible", f"action_summary.{action}",
                           "决策动作数量不能由分账户价格和策略参数独立重放")
    if universe.get("selection_temporality") == "retrospective_current_snapshot":
        _review(
            reviews,
            "universe_not_point_in_time",
            "基金池由当前核验关系和当前全历史长度回溯选择，存在生存与选择偏差；收益仅可用于规则行为验证",
        )
    if common_count is not None and universe_items and action_total != common_count * len(universe_items):
        _error(errors, "decision_count_mismatch", "action_summary", "决策总数与基金数乘共同观察日数量不一致")

    years = max(Decimal(common_count or 0) / TRADING_SESSIONS_PER_YEAR, Decimal(1))
    fund_count = max(len(universe_items), 1)
    trades_per_fund_year = Decimal(len(trades_raw)) / Decimal(fund_count) / years
    _compare_metric(report, "trades_per_fund_year", trades_per_fund_year, errors)
    average_equity = (sum((point["equity"] for point in strategy_points), Decimal(0)) / len(strategy_points)
                      if strategy_points else None)
    notional_turnover_pct = total_gross / average_equity * 100 if average_equity and average_equity > 0 else None
    annualized_turnover_pct = notional_turnover_pct / years if notional_turnover_pct is not None else None
    if trades_per_fund_year > HIGH_TRADES_PER_FUND_YEAR:
        _review(reviews, "high_trade_frequency", "每基金年度成交次数超过30次，需核对费用和过度交易")
    if annualized_turnover_pct is not None and annualized_turnover_pct > HIGH_ANNUAL_NOTIONAL_TURNOVER_PCT:
        _review(reviews, "high_notional_turnover", "年化成交额超过平均资产的200%，需复盘换手成本")

    economic = _validate_economic_evidence(
        report,
        parsed_curves=parsed_curves,
        raw_curves_by_id=by_id,
        strategy_dates=strategy_dates,
        trades=trades_raw,
        protocol=protocol,
        metrics=metrics,
        errors=errors,
        reviews=reviews,
        strategy_policy=strategy_policy,
    )

    annual: list[dict[str, Any]] = []
    segments: list[dict[str, Any]] = []
    if strategy_points and len(strategy_points) == len(benchmark_points):
        for year in sorted({point["date"][:4] for point in strategy_points}):
            indexes = [index for index, point in enumerate(strategy_points)
                       if point["date"].startswith(year)]
            strategy_year = [strategy_points[index] for index in indexes]
            benchmark_year = [benchmark_points[index] for index in indexes]
            first_index = indexes[0]
            strategy_baseline = strategy_points[first_index - 1] if first_index else strategy_year[0]
            benchmark_baseline = benchmark_points[first_index - 1] if first_index else benchmark_year[0]
            left = _period_stats(strategy_year, baseline=strategy_baseline)
            right = _period_stats(benchmark_year, baseline=benchmark_baseline)
            annual.append({"year": year, "strategy": left, "benchmark": right,
                           "excess_return_pct": _number(Decimal(str(left["return_pct"]))
                                                        - Decimal(str(right["return_pct"])))})
        if len(strategy_points) >= 2:
            midpoint = len(strategy_points) // 2
            for label, indexes in (("earlier", slice(0, midpoint + 1)), ("later", slice(midpoint, None))):
                left, right = _period_stats(strategy_points[indexes]), _period_stats(benchmark_points[indexes])
                segments.append({"segment": label, "strategy": left, "benchmark": right,
                                 "excess_return_pct": _number(Decimal(str(left["return_pct"]))
                                                              - Decimal(str(right["return_pct"])))})
    if len(annual) < 2:
        _review(reviews, "fewer_than_two_calendar_years", "观察范围未覆盖两个自然年，跨市场环境证据仍有限")

    coverage = {
        "common_observations": common_count,
        "minimum_required": minimum,
        "start_date": strategy_dates[0] if strategy_dates else None,
        "end_date": strategy_dates[-1] if strategy_dates else None,
        "calendar_span_days": span_days,
        "report_generated_date": generated_date.isoformat() if generated_date is not None else None,
        "latest_observation_lag_days": lag_days,
        "fund_count": len(universe_items),
        "curve_count": len(raw_curves),
        "date_intersection_audit": date_intersection_audit,
        "observed_date_audit": observed_date_audit,
        "investment_eligibility": investment_eligibility,
    }
    action_validation = {
        "decision_counts": {action: action_summary.get(action) for action in DECISION_ACTIONS},
        "executed_counts": {action: actual_actions[action] for action in TRADE_ACTIONS},
        "trade_count": len(trades_raw),
        "trades_per_fund_year": _number(trades_per_fund_year),
        "notional_turnover_pct": _number(notional_turnover_pct) if notional_turnover_pct is not None else None,
        "annualized_notional_turnover_pct": (_number(annualized_turnover_pct)
                                             if annualized_turnover_pct is not None else None),
    }
    comparison = {
        "strategy_return_pct": _number(strategy_return) if strategy_return is not None else None,
        "benchmark_return_pct": _number(benchmark_return) if benchmark_return is not None else None,
        "excess_return_pct": (_number(strategy_return - benchmark_return)
                              if strategy_return is not None and benchmark_return is not None else None),
        "strategy_max_drawdown_pct": _number(strategy_drawdown) if strategy_drawdown is not None else None,
        "benchmark_max_drawdown_pct": _number(benchmark_drawdown) if benchmark_drawdown is not None else None,
        "drawdown_difference_pct": (_number(strategy_drawdown - benchmark_drawdown)
                                    if strategy_drawdown is not None and benchmark_drawdown is not None else None),
    }
    return _result(report, errors, reviews, coverage, action_validation, annual, segments,
                   curve_summaries=curve_summaries, comparison=comparison, economic=economic,
                   risk_quality=risk_quality)


def _result(
    report: dict[str, Any],
    errors: list[dict[str, Any]],
    reviews: list[dict[str, str]],
    coverage: dict[str, Any],
    actions: dict[str, Any],
    annual: list[dict[str, Any]],
    segments: list[dict[str, Any]],
    *,
    curve_summaries: dict[str, Any] | None = None,
    comparison: dict[str, Any] | None = None,
    economic: dict[str, Any] | None = None,
    risk_quality: dict[str, Any] | None = None,
) -> dict[str, Any]:
    status = "blocked" if errors else "review_required" if reviews else "passed"
    result = {
        "version": VERSION,
        "status": status,
        "source": {
            "report_version": report.get("version"),
            "report_status": report.get("status"),
            "strategy_version": report.get("strategy_version"),
            "input_hash": (report.get("input_identity", {}).get("hash")
                           if isinstance(report.get("input_identity"), dict) else None),
        },
        "coverage": coverage,
        "checks": {
            "data_integrity": "blocked" if errors else "passed",
            "history_coverage": "review_required" if any(
                item["code"] in {"less_than_one_trading_year", "calendar_span_under_one_year",
                                 "fewer_than_two_calendar_years", "common_history_stale",
                                 "source_date_gap"} for item in reviews
            ) else "passed",
            "action_coverage": "review_required" if any(
                item["code"].startswith(("missing_", "strategy_never", "single_")) for item in reviews
            ) else "passed",
            "performance": "review_required" if any(
                item["code"] in {"negative_strategy_return", "strategy_underperformed_benchmark",
                                 "strategy_drawdown_worse_than_benchmark", "high_trade_frequency",
                                 "high_notional_turnover", "universe_not_point_in_time",
                                 "low_market_exposure", "underperformed_exposure_matched",
                                 "short_holding_period", "cost_sensitive",
                                 "signal_execution_negative_before_cost"} for item in reviews
            ) else "passed",
            "investment_readiness": "blocked" if (
                errors
                or isinstance(coverage.get("investment_eligibility"), dict)
                and coverage["investment_eligibility"].get("status") == "blocked"
            ) else "review_required" if any(
                item["code"] == "investment_validation_not_eligible" for item in reviews
            ) else "passed",
        },
        "blocking_errors": [{key: value for key, value in item.items() if key != "_group"} for item in errors],
        "review_findings": reviews,
        "curve_summaries": curve_summaries or {},
        "actions": actions,
        "performance": {"comparison": comparison or {}, "calendar_years": annual,
                        "time_segments": segments},
        "economic": economic,
        "risk_quality": risk_quality,
        "limitations": [
            "来源日期交集审计只能识别至少一个来源记录过的日期；所有来源同日缺失仍无法识别。",
            "负收益、落后基准和高换手属于策略表现复盘，不会被伪装成数据结构错误。",
            "历史回放通过不代表未来收益，也不替代真实费用、限购、停牌及确认时差验证。",
            "真实资金资格必须同时满足来源用途、总收益、历史时点、产品执行规则和样本外验证门槛。",
            "逐基金同敞口归因保留基金选择并固定实际敞口路径；剩余项仍有路径交互，不能解释为因果alpha。",
            "风险调整指标按244观察日年化，Sharpe的现金收益与Sortino的最低可接受收益均按0计算。",
        ],
    }
    result["validation_hash"] = digest({key: value for key, value in result.items() if key != "validation_hash"})
    return result


def validate_strategy_report(report: object) -> dict[str, Any]:
    """Validate with fixed precision so acceptance hashes are reproducible."""
    precision = 28
    if isinstance(report, dict):
        protocol = report.get("protocol")
        configured = protocol.get("decimal_precision") if isinstance(protocol, dict) else None
        if isinstance(configured, int) and not isinstance(configured, bool) and 28 <= configured <= 100:
            precision = configured
        elif str(report.get("strategy_version", "")).endswith("-v2"):
            precision = 40
    with localcontext() as context:
        context.prec = precision
        return _validate_strategy_report(report)
