"""Independent, deterministic acceptance checks for integrated strategy reports."""
from __future__ import annotations

from collections import Counter
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation, localcontext
import re
from typing import Any

from backend.analysis.strategy import StrategyPolicy
from backend.storage.research import digest


VERSION = "strategy-validation-v2"
TRADE_ACTIONS = ("BUY", "ADD", "REDUCE", "EXIT")
DECISION_ACTIONS = ("BUY", "ADD", "HOLD", "REDUCE", "EXIT", "WAIT")
TRADING_SESSIONS_PER_YEAR = Decimal("244")
HIGH_TRADES_PER_FUND_YEAR = Decimal("30")
HIGH_ANNUAL_NOTIONAL_TURNOVER_PCT = Decimal("200")
DATE_INTERSECTION_AUDIT_VERSION = "observed-source-date-intersection-v1"


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


def _period_stats(points: list[dict[str, Any]]) -> dict[str, str | int]:
    start, end = points[0]["equity"], points[-1]["equity"]
    peak, drawdown = points[0]["equity"], Decimal(0)
    for point in points:
        peak = max(peak, point["equity"])
        drawdown = min(drawdown, point["equity"] / peak - 1)
    return {
        "start_date": points[0]["date"],
        "end_date": points[-1]["date"],
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
    for required in ("strategy", "benchmark"):
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

    strategy_points = parsed_curves.get("strategy", [])
    benchmark_points = parsed_curves.get("benchmark", [])
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
        if action not in TRADE_ACTIONS:
            _error(errors, "trade_action_invalid", f"{path}.action", "成交动作不受支持")
        else:
            actual_actions[str(action)] += 1
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
            if action == "EXIT" and values["units_after"] != 0:
                _error(errors, "exit_units_not_zero", f"{path}.units_after", "清仓成交后的份额必须精确为零")
            if trade_key is not None and not duplicate_trade:
                trade_records[trade_key] = {
                    "path": path,
                    "action": action,
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

    annual: list[dict[str, Any]] = []
    segments: list[dict[str, Any]] = []
    if strategy_points and len(strategy_points) == len(benchmark_points):
        for year in sorted({point["date"][:4] for point in strategy_points}):
            strategy_year = [point for point in strategy_points if point["date"].startswith(year)]
            benchmark_year = [point for point in benchmark_points if point["date"].startswith(year)]
            left, right = _period_stats(strategy_year), _period_stats(benchmark_year)
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
                   curve_summaries=curve_summaries, comparison=comparison)


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
                                 "high_notional_turnover", "universe_not_point_in_time"} for item in reviews
            ) else "passed",
        },
        "blocking_errors": [{key: value for key, value in item.items() if key != "_group"} for item in errors],
        "review_findings": reviews,
        "curve_summaries": curve_summaries or {},
        "actions": actions,
        "performance": {"comparison": comparison or {}, "calendar_years": annual,
                        "time_segments": segments},
        "limitations": [
            "来源日期交集审计只能识别至少一个来源记录过的日期；所有来源同日缺失仍无法识别。",
            "负收益、落后基准和高换手属于策略表现复盘，不会被伪装成数据结构错误。",
            "历史回放通过不代表未来收益，也不替代真实费用、限购、停牌及确认时差验证。",
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
