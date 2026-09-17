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
from backend.storage.related_market import current_associations
from backend.analysis.sector_evidence import (
    assess_opportunity, evidence_matches, industry_context, load_industry_evidence, valuation_context,
)
from backend.analysis.sector_strength import attach_strength, build_advantage_summary
from backend.analysis.research_view import attach_research_views

METHOD_VERSION = "price-state-v4-calendar"
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


def _percent(value: Decimal) -> float:
    return float((value * 100).quantize(Decimal("0.01")))


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
    return {"as_of": latest.isoformat() if latest else None,
            "observation_count": len(observations), "periods": periods}


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
        indexes = connection.execute("SELECT code,name,source_id,updated_at FROM market_index_projection ORDER BY code").fetchall()
        associations = current_associations(connection, generated_at=now.isoformat())
        items = []
        for index in indexes:
            rows = connection.execute("SELECT date,close FROM market_index_daily WHERE index_code=? ORDER BY date", (index["code"],)).fetchall()
            funds = [fund for fund in associations if fund["index_code"] == index["code"]]
            item = {**dict(index), **analyze_index([dict(row) for row in rows], as_of=price_cutoff, calendar=calendar),
                    "funds": [dict(row) for row in funds], "universe_type": "tracked_index",
                    "kind": "参考指数", "heat_rank": None, "heat_value": None, "collection_error": None}
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
    return {"method_version": "sector-horizons-v1", "price_method_version": METHOD_VERSION,
            "strength_method_version": "common-grid-quartile-v3", "coverage": coverage,
            "price_cutoff": price_cutoff.isoformat(),
            "calendar_status": "ready" if calendar_error is None else "unsupported_range",
            "calendar_version": calendar.version, "calendar_hash": calendar.hash, "calendar_error": calendar_error,
            "evidence_version": evidence["version"], "generated_at": now.isoformat(timespec="seconds"),
            "universe": heat["universe"], "advantages": advantages,
            "comparison_as_of": comparison_dates, "turnover_as_of": heat["universe"].get("ranking_as_of"),
            "advantage_rule": "同截止日、同完整观测日期序列可比；原始收益排名前25%；趋势为上涨或可识别修复/回落；最多5个。非投资排名，仅在已核验沪深日历覆盖范围内计算。",
            "items": items}
