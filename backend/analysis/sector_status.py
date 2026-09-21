"""Descriptive price states, not forecasts or investment recommendations."""
from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timedelta
from hashlib import sha256
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from backend.core.config import Settings
from backend.core.trading_calendar import CalendarUnavailable, TradingCalendar, get_calendar
from backend.storage.database import connection_scope
from backend.storage.related_market import current_associations, market_data_state
from backend.analysis.sector_evidence import (
    assess_opportunity, evidence_matches, industry_context, load_industry_evidence, valuation_context,
)
from backend.analysis.sector_strength import attach_strength, build_advantage_summary
from backend.analysis.research_view import attach_research_views

METHOD_VERSION = "price-state-v5-historical-display"
PERIODS = (
    ("short", "短期", "约 1 周至 1 个月", 20),
    ("medium", "中期", "约 1 至 3 个月", 60),
    ("long", "长期", "约 3 至 6 个月", 120),
)
# Initial display thresholds, not parameters selected for investment performance.
MOVE_THRESHOLD = Decimal("0.01")
RISK_DRAWDOWN = Decimal("-0.10")
STALE_DAYS = 10
MAX_GAP_DAYS = 14
LEGACY_SECTOR_SOURCE_MARKERS = ("east" + "money", "em" + "data", "push2" + "his", "datacenter" + "-web")
DATA_SUPPORT_VERSION = "sector-data-support-v1"


def _percent(value: Decimal) -> float:
    return float((value * 100).quantize(Decimal("0.01")))


def _contains_legacy_sector_source(value: object) -> bool:
    if isinstance(value, str):
        lowered = value.lower()
        return any(marker in lowered for marker in LEGACY_SECTOR_SOURCE_MARKERS) or ("东方" + "财富") in value
    if isinstance(value, Mapping):
        return any(_contains_legacy_sector_source(item) for item in value.values())
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return any(_contains_legacy_sector_source(item) for item in value)
    return False


def _current_collection_status(settings: Settings) -> dict[str, object]:
    from backend.storage.collection_runs import latest_sector_status

    status = latest_sector_status(settings)
    if _contains_legacy_sector_source(status.get("result")):
        return {**status, "state": "stale",
                "result": {"message": "最近一次持久化刷新状态来自已停用的板块来源，已从当前同花顺机会结果中忽略；请以当前同花顺快照和重新评估结果为准。"}}
    return status


def analyze_index(rows: Sequence[Mapping[str, object]], *, as_of: date, calendar: TradingCalendar | None = None) -> dict[str, object]:
    """Use only observations dated on/before as_of; never fill missing prices."""
    observations: list[tuple[date, object]] = []
    invalid_dates = False
    seen: set[date] = set()
    for row in rows:
        try:
            raw_date = str(row.get("date", ""))
            day = date.fromisoformat(raw_date)
            if day.isoformat() != raw_date:
                raise ValueError("invalid date format")
        except ValueError:
            invalid_dates = True
            continue
        if day > as_of:
            continue
        if day in seen:
            invalid_dates = True
        seen.add(day)
        observations.append((day, row.get("close")))
    observations.sort(key=lambda row: row[0])
    latest = observations[-1][0] if observations else None
    periods = []
    for period_id, name, period_range, window in PERIODS:
        result = {
            "id": period_id, "name": name, "range": period_range,
            "lookback_sessions": window, "status": "insufficient", "label": "数据不足",
            "reason": "尚未采集可用的指数日行情。", "return_pct": None,
            "ma_bias_pct": None, "max_drawdown_pct": None, "risk": "unknown",
        }
        periods.append(result)
        if invalid_dates:
            result["reason"] = "行情日期无效或重复，需核查数据后重新计算。"
            continue
        if latest is None:
            continue
        if (as_of - latest).days > STALE_DAYS:
            result.update(status="stale", label="数据过期", reason=f"最新行情为 {latest}，距计算日超过 {STALE_DAYS} 个自然日。")
            continue
        if len(observations) < window + 1:
            result["reason"] = f"已有 {len(observations)} 条日行情，需至少 {window + 1} 条才能观察 {window} 个交易日的变化。"
            continue
        sample = observations[-window - 1:]
        if any((right[0] - left[0]).days > MAX_GAP_DAYS for left, right in zip(sample, sample[1:])):
            result["reason"] = f"观察窗口内存在超过 {MAX_GAP_DAYS} 个自然日的行情间隔，需核查连续性。"
            continue
        if calendar is not None:
            try:
                quality = calendar.validate_grid([day.isoformat() for day, _ in sample], end=as_of, minimum=window + 1)
            except CalendarUnavailable as exc:
                quality = {"status": "blocked", "errors": [str(exc)]}
            result["calendar_quality"] = quality
            if quality["status"] != "ready":
                result["reason"] = f"交易日历校验未通过：{quality['errors']}；缺值未填补。"
                continue
        try:
            prices = [Decimal(str(row[1])) for row in sample]
            if any(not value.is_finite() or value <= 0 for value in prices):
                raise ValueError("non-positive or non-finite close")
        except (InvalidOperation, ValueError):
            result["reason"] = "观察窗口内存在缺失或无效收盘价，未填补数据。"
            continue
        change = prices[-1] / prices[0] - 1
        mean = sum(prices[1:]) / window
        bias = prices[-1] / mean - 1
        peak, drawdown = prices[0], Decimal(0)
        for value in prices:
            peak = max(peak, value)
            drawdown = min(drawdown, value / peak - 1)
        if change > MOVE_THRESHOLD and bias > 0:
            status, label, reason = "strong", "上涨走势", "区间上涨超过 1%，最新价格也在该窗口均线之上。"
        elif change < -MOVE_THRESHOLD and bias < 0:
            status, label, reason = "weak", "下跌走势", "区间下跌超过 1%，最新价格也在该窗口均线之下。"
        elif change > MOVE_THRESHOLD:
            status, label, reason = "neutral", "上涨后回落", "整个区间仍上涨超过 1%，但最新价格已回到窗口均线或其下方。"
        elif change < -MOVE_THRESHOLD:
            status, label, reason = "neutral", "下跌后修复", "整个区间仍下跌超过 1%，但最新价格已回到窗口均线或其上方。"
        else:
            status, label, reason = "neutral", "区间变化有限", "区间累计价格变化在 ±1% 内；并不表示期间波动很小。"
        result.update(status=status, label=label, reason=reason,
                      return_pct=_percent(change), ma_bias_pct=_percent(bias),
                      max_drawdown_pct=_percent(drawdown),
                      risk="elevated" if drawdown <= RISK_DRAWDOWN else "normal")
        result.update(observation_start=sample[0][0].isoformat(), observation_end=sample[-1][0].isoformat(),
                      observation_grid=sha256(",".join(day.isoformat() for day, _ in sample).encode()).hexdigest(),
                      _ranking_return=str(change))
    if calendar is not None and latest is not None and latest < as_of and not invalid_dates:
        historical = analyze_index(rows, as_of=latest, calendar=calendar)
        for period, past in zip(periods, historical["periods"]):
            if past["status"] in ("strong", "neutral", "weak"):
                period["historical"] = _historical_display(past, latest.isoformat())
    return {"as_of": latest.isoformat() if latest else None,
            "observation_count": len(observations), "periods": periods}


def _historical_display(period: dict, as_of: str) -> dict:
    # No rank or current-eligibility fields cross into the historical display contract.
    return {"available": True, "as_of": as_of, "label": period["label"],
            "return_pct": period["return_pct"], "ma_bias_pct": period["ma_bias_pct"],
            "max_drawdown_pct": period["max_drawdown_pct"],
            "observation_start": period.get("observation_start"),
            "observation_end": period.get("observation_end"),
            "note": "历史窗口已校验；不代表最新行情，不参与当前排名。"}


def summarize_data_support(items: list[dict]) -> dict[str, object]:
    """Expose the evidence denominator and material gaps behind the analysis."""
    boards = [item for item in items if item.get("universe_type") == "hot_board"]
    total = len(boards)
    ready_boards = [item for item in boards if item.get("membership", {}).get("status") == "ready"]
    members = [member for item in ready_boards for member in item["membership"].get("members", [])]

    def status(available: int, denominator: int) -> str:
        if available <= 0:
            return "not_collected"
        return "available" if denominator > 0 and available == denominator else "partial"

    price = {
        key: sum(any(period.get("id") == key and period.get("strength", {}).get("eligible") is True
                     for period in item.get("periods", [])) for item in boards)
        for key in ("short", "medium", "long")
    }
    operating_available = sum(
        item.get("fundamentals", {}).get("operating", {}).get("status") == "available" for item in boards
    )
    operating_partial = sum(
        item.get("fundamentals", {}).get("operating", {}).get("status") == "partial" for item in boards
    )
    valuation_current = sum(
        item.get("fundamentals", {}).get("valuation", {}).get("status") == "available"
        and item.get("fundamentals", {}).get("valuation", {}).get("as_of")
        and (item["fundamentals"]["valuation"].get("median_pe_ttm") is not None
             or item["fundamentals"]["valuation"].get("index_pe_ttm") is not None)
        for item in boards
    )
    valuation_history = sum(
        item.get("fundamentals", {}).get("valuation", {}).get("percentile") is not None for item in boards
    )
    catalyst_available = sum(
        item.get("fundamentals", {}).get("catalysts", {}).get("status") == "available" for item in boards
    )
    market_caps = sum(member.get("market_cap") is not None for member in members)
    weights = sum(member.get("weight") is not None for member in members)
    datasets = [
        {"id": "sector_price_history", "label": "板块日线与三周期窗口",
         "status": "available" if total and all(value == total for value in price.values()) else "partial" if any(price.values()) else "not_collected",
         "coverage": {**price, "total": total}, "required_for": ["short", "medium", "long"]},
        {"id": "current_constituents", "label": "当前行业成分",
         "status": status(len(ready_boards), total),
         "coverage": {"boards": len(ready_boards), "total_boards": total, "members": len(members)},
         "required_for": ["medium", "long", "valuation"]},
        {"id": "constituent_market_cap", "label": "成分流通市值",
         "status": status(market_caps, len(members)),
         "coverage": {"members": market_caps, "total_members": len(members)},
         "required_for": ["sample_quality"],
         "note": "流通市值不是同花顺官方行业权重。"},
        {"id": "official_constituent_weights", "label": "官方成分权重与历史成分",
         "status": status(weights, len(members)),
         "coverage": {"members": weights, "total_members": len(members)},
         "required_for": ["weighted_fundamentals", "historical_replay"]},
        {"id": "constituent_financials", "label": "成分收入、利润与经营现金流",
         "status": "available" if total and operating_available == total else "partial" if operating_available or operating_partial else "not_collected",
         "coverage": {"available_boards": operating_available, "partial_boards": operating_partial,
                      "total_boards": total}, "required_for": ["medium", "long"]},
        {"id": "constituent_quote_valuation", "label": "成分股带日期行情与原始估值字段",
         "status": "not_collected", "coverage": {"boards": 0, "total_boards": total},
         "required_for": ["valuation", "sample_quality"],
         "note": "普通成分页的“市盈率”尚未核验类型、业务日期和亏损处理。"},
        {"id": "dated_valuation", "label": "有明确口径和业务日期的估值",
         "status": status(valuation_current, total),
         "coverage": {"boards": valuation_current, "total_boards": total},
         "required_for": ["long", "pricing"]},
        {"id": "comparable_valuation_history", "label": "同成分同口径估值历史",
         "status": status(valuation_history, total),
         "coverage": {"boards": valuation_history, "total_boards": total},
         "required_for": ["valuation_percentile", "long"]},
        {"id": "market_breadth_and_flow", "label": "上涨家数、成交扩散与资金流口径",
         "status": "not_collected", "coverage": {"boards": 0, "total_boards": total},
         "required_for": ["short", "crowding"]},
        {"id": "benchmark_relative_return", "label": "相对宽基收益、波动与回撤",
         "status": "not_collected", "coverage": {"boards": 0, "total_boards": total},
         "required_for": ["short", "medium", "risk"]},
        {"id": "total_return_adjustments", "label": "分红拆分与总收益口径",
         "status": "not_collected", "coverage": {"boards": 0, "total_boards": total},
         "required_for": ["relative_return", "historical_replay"],
         "note": "当前板块日线为不复权价格序列。"},
        {"id": "point_in_time_catalysts", "label": "催化、预期与失效事件快照",
         "status": status(catalyst_available, total),
         "coverage": {"boards": catalyst_available, "total_boards": total},
         "required_for": ["short", "medium"]},
        {"id": "industry_specific_metrics", "label": "金融及行业专属经营指标",
         "status": "not_collected", "coverage": {"boards": 0, "total_boards": total},
         "required_for": ["medium", "long"]},
        {"id": "fund_exposure_and_cost", "label": "基金真实暴露、跟踪与成本",
         "status": "not_collected", "coverage": {"boards": 0, "total_boards": total},
         "required_for": ["fund_selection", "formal_recommendation"]},
    ]
    return {
        "version": DATA_SUPPORT_VERSION,
        "scope": "tonghuashun_industry_pool",
        "datasets": datasets,
        "blocking_gaps": [row["id"] for row in datasets if row["status"] != "available"],
        "formal_recommendation_ready": False,
        "note": "覆盖率说明底层资料是否存在及是否完整，不是投资评分；未采集项不会由价格走势代填。",
    }


def sector_opportunities(settings: Settings) -> dict[str, object]:
    from backend.storage.sector_heat import read_sector_heat

    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    calendar = get_calendar()
    calendar_error = None
    try:
        price_cutoff = calendar.latest_completed(now)
    except CalendarUnavailable as exc:
        # Keep old observations inspectable, but block every comparison below.
        price_cutoff, calendar_error = now.date(), str(exc)
    from backend.analysis.sector_assessment import context_from_bundle, evaluate_period
    from backend.storage.research import read_manifest, timestamp
    evidence = load_industry_evidence()
    heat = read_sector_heat(settings)
    # One read transaction keeps index identity, relations and prices consistent
    # while a concurrent import replaces the current projection.
    with connection_scope(settings) as connection:
        connection.execute("BEGIN")
        indexes = connection.execute(
            """SELECT i.code,i.name,i.source_id,i.updated_at,
                      m.outcome AS market_refresh_outcome,
                      m.attempted_at AS market_refresh_attempted_at,
                      (SELECT MAX(d.date) FROM market_index_daily d WHERE d.index_code=i.code) AS market_data_as_of
                 FROM market_index_projection i
                 LEFT JOIN index_market_refresh_state m ON m.index_code=i.code
                ORDER BY i.code"""
        ).fetchall()
        associations = current_associations(connection, generated_at=now.isoformat())
        items = []
        for index in indexes:
            rows = connection.execute("SELECT date,close FROM market_index_daily WHERE index_code=? ORDER BY date", (index["code"],)).fetchall()
            funds = [fund for fund in associations if fund["index_code"] == index["code"]]
            market_state = market_data_state(dict(index), generated_at=now.isoformat())
            item = {**dict(index), **market_state,
                    **analyze_index([dict(row) for row in rows], as_of=price_cutoff, calendar=calendar),
                    "funds": [dict(row) for row in funds], "universe_type": "tracked_index",
                    "kind": "参考指数", "heat_rank": None, "heat_value": None,
                    "collection_error": None if market_state["market_status"] == "ready"
                    else market_state["market_reason"]}
            if item["collection_error"]:
                for period in item["periods"]:
                    if period["status"] in ("strong", "neutral", "weak"):
                        period["historical"] = _historical_display(period, item["as_of"])
                    period.update(status="stale", label="行情待更新",
                                  reason=str(item["collection_error"]))
            items.append(item)
        subjects = [f"hot_board:{board['source_id']}:{board['code']}" for board in heat["items"]]
        subjects += [f"tracked_index:{index['source_id']}:{index['code']}" for index in indexes]
        manifest = read_manifest(connection, subjects, timestamp(now.isoformat()))
        connection.execute("COMMIT")
    boards = []
    for board in heat["items"]:
        rows = board.pop("rows")
        analyzed = analyze_index(rows, as_of=price_cutoff, calendar=calendar)
        if heat["universe"].get("last_error"):
            board["collection_error"] = heat["universe"]["last_error"]
        if board.get("collection_error"):
            for period in analyzed["periods"]:
                if period["status"] in ("strong", "neutral", "weak"):
                    period["historical"] = _historical_display(period, analyzed["as_of"])
                period.update(status="stale", label="行情待更新", reason="本次采集失败，旧行情仅供核对，不参与当前判断。")
        boards.append({**board, **analyzed})
    items = boards + items
    for item in items:
        if calendar_error:
            for period in item["periods"]:
                period.update(status="insufficient", label="日历待补齐", reason=calendar_error)
        context_code = item["code"] if evidence_matches(evidence["sectors"].get(item["code"]), item) else ""
        item["industry"] = industry_context(evidence, context_code, now)
        item["valuation"] = valuation_context(item["code"], now, subject=item)
        key = f"{item['universe_type']}:{item['source_id']}:{item['code']}"
        if item["universe_type"] == "hot_board":
            try:
                context = context_from_bundle(manifest["sector_fundamentals"].get(key), now, board=item, calendar=calendar)
            except CalendarUnavailable as exc:
                context = context_from_bundle(None, now, board=item)
                context["errors"].append(str(exc))
        else:
            # Local import avoids a module-initialization cycle with analyze_index.
            from backend.analysis.research_pipeline import assess_subject
            context = assess_subject(manifest, key)["evidence_context"]
        item["fundamentals"] = context
        for period in item["periods"]:
            period["opportunity"] = evaluate_period(period, context)
    comparison_dates = {}
    for universe in ("hot_board", "tracked_index"):
        days = [item["as_of"] for item in items if item["universe_type"] == universe
                and item.get("as_of") and not item.get("collection_error")
                and any(period["status"] in ("strong", "neutral", "weak") for period in item["periods"])]
        counts = Counter(days)
        comparison_dates[universe] = max(counts, key=lambda day: (counts[day], day)) if counts else None
    attach_strength(items, comparison_dates)
    for item in items:
        for period in item["periods"]:
            period.pop("_ranking_return", None)
    advantages = build_advantage_summary(items)
    attach_research_views(items, generated_at=now.isoformat(timespec="seconds"))
    coverage = []
    for universe in ("hot_board", "tracked_index"):
        group = [item for item in items if item["universe_type"] == universe]
        coverage.append({"universe_type": universe, "total": len(group),
                         "price_eligible": {key: sum(any(p["id"] == key and p["strength"]["eligible"] for p in item["periods"])
                                                     for item in group) for key in ("short", "medium", "long")},
                         "industry_current": sum(item["fundamentals"].get("usable", False) and item["fundamentals"]["operating"]["status"] == "available" for item in group),
                         "valuation_observations": sum(item["fundamentals"]["valuation"].get("median_pe_ttm") is not None or item["fundamentals"]["valuation"].get("index_pe_ttm") is not None for item in group),
                         "valuation_dated_current": sum(item["fundamentals"]["valuation"]["status"] == "available" and bool(item["fundamentals"]["valuation"].get("as_of")) for item in group),
                         "formal_recommendation_ready": False})
    return {"collection_status": _current_collection_status(settings),
            "method_version": "sector-horizons-v1", "price_method_version": METHOD_VERSION,
            "strength_method_version": "common-grid-quartile-v3", "coverage": coverage,
            "data_support": summarize_data_support(items),
            "price_cutoff": price_cutoff.isoformat(),
            "calendar_status": "ready" if calendar_error is None else "unsupported_range",
            "calendar_version": calendar.version, "calendar_hash": calendar.hash, "calendar_error": calendar_error,
            "evidence_version": evidence["version"], "generated_at": now.isoformat(timespec="seconds"),
            "universe": heat["universe"], "advantages": advantages,
            "comparison_as_of": comparison_dates, "turnover_as_of": heat["universe"].get("ranking_as_of"),
            "advantage_rule": "同截止日、同完整观测日期序列可比；原始收益排名前25%；趋势为上涨或可识别修复/回落；最多10个。非投资排名，仅在已核验沪深日历覆盖范围内计算。",
            "items": items}
