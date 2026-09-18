"""Horizon-specific evidence judgments, distinct from permission to recommend a fund.

A short-horizon price description does not require a fictitious earnings forecast.
Conversely, price strength cannot fill the medium/long earnings and valuation gaps.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from backend.core.trading_calendar import SHANGHAI, day, get_calendar
from backend.storage.sector_fundamentals import membership_identity

VERSION = "sector-horizons-v1"
MAX_REPORT_AGE_DAYS = 185
MIN_VALUATION_OBSERVATIONS = 60
MIN_VALUATION_SPAN_DAYS = 365
SPECIALIST_FINANCIAL_NAMES = ("银行", "证券", "保险", "多元金融")
CATALYST_REQUIREMENTS = {
    "贵金属": ["金银价格及其币种/日期", "实际利率与汇率变化", "成分公司的产销量、单位成本与套期保值披露"],
    "半导体": ["存储/晶圆价格与订单", "产能利用率与库存", "资本开支及产品结构变化"],
    "房地产开发": ["销售回款与交付", "债务到期及融资成本", "存货减值与经营现金流"],
}


def _value(metric: dict, field: str = "value") -> Decimal | None:
    raw = metric.get(field)
    return Decimal(str(raw)) if raw is not None else None


def context_from_bundle(bundle: dict | None, now: datetime, *, board: dict | None = None, calendar=None) -> dict:
    name = board.get("name", "") if board else bundle.get("name", "") if bundle else ""
    catalysts = CATALYST_REQUIREMENTS.get(name, ["可核对日期的行业需求、价格、订单及重要公告"])
    empty = {"version": VERSION, "scope": "当前东方财富成分样本（非官方指数加权）", "status": "not_collected",
             "observed_at": None, "membership_as_of": None, "total_members": 0,
             "operating": {"status": "not_collected", "periods": []},
             "valuation": {"status": "not_collected", "percentile": None, "history_count": 0, "history_span_days": 0},
             "sources": [], "errors": ["尚未采集该板块的成分报表及同日估值；点击重新评估启动采集"],
             "limitations": [], "catalysts": {"status": "not_collected", "required": catalysts},
             "specialist_financial": any(word in name for word in SPECIALIST_FINANCIAL_NAMES)}
    if not bundle:
        return empty
    import copy
    context = {**empty, **copy.deepcopy({key: bundle[key] for key in
        ("status", "observed_at", "membership_as_of", "total_members", "operating", "valuation", "sources", "errors", "limitations") if key in bundle})}
    context["observation_id"] = bundle.get("observation_id")
    context["membership_hash"] = bundle.get("membership_hash")
    problem = None
    if context["status"] == "collecting":
        problem = "本轮经营估值采集尚未完成（服务中断后也不会自动改为成功）"
    elif context["status"] in ("failed", "blocked"):
        problem = "本轮经营估值采集未通过；旧结果不冒充本轮更新"
    cutoff_day = (calendar or get_calendar()).latest_completed(now)
    if not context.get("membership_as_of") or day(context["membership_as_of"]) < cutoff_day or day(context["membership_as_of"]) > now.astimezone(SHANGHAI).date():
        problem = "成分快照日期不是当前可核对日期，不能以陈旧成分解释当前行业"
        context["status"] = "stale"
    if board is not None:
        identity = membership_identity(board)
        if (not identity["valid"] or identity["hash"] != bundle.get("membership_hash")
                or not identity["as_of"] or day(identity["as_of"]) < cutoff_day):
            problem = "成分身份变化或本轮成分更新失败，旧样本不再代表当前板块"
            context["status"] = "membership_mismatch"
    periods = context["operating"].get("periods", [])
    local_day = now.astimezone(SHANGHAI).date()
    if periods and ((local_day - day(periods[0]["report_date"])).days > MAX_REPORT_AGE_DAYS
                    or day(periods[0]["report_date"]) > local_day):
        problem = "经营报告期已过期或在未来，不能继续当作当前经营证据"
        context["status"] = "stale"
    context["usable"] = problem is None
    if problem:
        context["errors"] = [problem, *context["errors"]]
        context["operating"]["status"] = context["status"]
    valuation = context["valuation"]
    history = bundle.get("valuation_history", [])
    valuation["history_count"] = len(history)
    valuation["history_span_days"] = (day(history[-1]["as_of"]) - day(history[0]["as_of"])).days if history else 0
    valuation["percentile"] = None
    if not context["usable"]:
        valuation["status"] = context["status"]
    try:
        if context["usable"] and valuation.get("as_of") != (calendar or get_calendar()).latest_completed(now).isoformat():
            valuation["status"] = "stale" if valuation.get("as_of") else valuation["status"]
    except ValueError:
        valuation["status"] = "stale"
    if (context["usable"] and valuation["status"] == "available" and valuation.get("median_pe_ttm") is not None
            and len(history) >= MIN_VALUATION_OBSERVATIONS and valuation["history_span_days"] >= MIN_VALUATION_SPAN_DAYS):
        value = Decimal(valuation["median_pe_ttm"])
        valuation["percentile"] = str((sum(Decimal(row["value"]) < value for row in history)
                                      + Decimal(sum(Decimal(row["value"]) == value for row in history)) / 2) / len(history) * 100)
    return context


def context_from_index(result: dict, now: datetime) -> dict:
    """Bridge already-ingested PIT index facts, never a name-matched macro statistic."""
    operating = result.get("operating", {})
    rows, sources = {}, []
    for name, observation in operating.items():
        for sample in observation.get("observations", [observation]):
            period = sample["report_date"]
            rows.setdefault(period, {"report_date": period, "base_report_date": None, "metrics": {}})["metrics"][name] = {
                "value": sample["value"], "covered": 1, "total": 1,
                "complete": not observation["gaps"], "published_at": sample.get("published_at"),
                "current_sum": None, "base_sum": None, "sample_codes": [], "excluded": [], "unit": "%",
            }
            if sample.get("source_url"):
                sources.append({"url": sample["source_url"], "asset_id": sample.get("raw_asset_id", "")})
    valuation = result.get("valuation", {})
    if valuation.get("source_url"):
        sources.append({"url": valuation["source_url"], "asset_id": valuation.get("raw_asset_id", "")})
    return {"version": VERSION, "scope": "已入库且通过时点核验的指数范围事实", "status": "blocked" if result.get("quality_blocked") else "available" if operating else "not_collected",
            "usable": not result.get("quality_blocked", False), "observed_at": None, "membership_as_of": None, "total_members": 0,
            "operating": {"status": "blocked" if result.get("quality_blocked") else "available" if operating else "not_collected", "periods": sorted(rows.values(), key=lambda row: row["report_date"], reverse=True)},
            "valuation": {"status": "blocked" if result.get("quality_blocked") else "available" if not valuation.get("gaps") and valuation.get("value") is not None else "missing",
                          "as_of": valuation.get("as_of"), "median_pe_ttm": None, "index_pe_ttm": valuation.get("value"),
                          "percentile": None if result.get("quality_blocked") else valuation.get("percentile"), "history_count": valuation.get("sample_count", 0),
                          "history_span_days": valuation.get("span_days", 0)},
            "sources": sources, "errors": result.get("operating_gaps", []), "limitations": ["行业背景不能代替指数范围事实"],
            "specialist_financial": False, "catalysts": {"status": "not_collected", "required": ["行业与公司事件的原文及可用时点"]}}


def evaluate_period(period: dict, context: dict) -> dict:
    key = period["id"]
    supports, challenges, missing = [], [], []
    price_ready = period["status"] in ("strong", "neutral", "weak")
    if price_ready:
        supports.append(f"过去{period.get('lookback_sessions', '')}个交易日价格变化{period.get('return_pct')}%；{period.get('label', '')}。这是历史走势，不是预期回报。")
        if period.get("max_drawdown_pct") is not None:
            challenges.append(f"观察窗口最大回撤{period['max_drawdown_pct']}%；窗口最大回撤不等于当前回撤。")
    else:
        missing.append(period.get("reason") or "行情或交易日期不足")
    historical = period.get("historical", {})
    if not price_ready and historical.get("available"):
        supports.append(f"历史截至{historical['as_of']}，窗口价格变化{historical['return_pct']}%；仅作历史描述，当前不可比较。")
    operating = context.get("operating", {})
    periods = operating.get("periods", []) if context.get("usable", False) else []
    latest = periods[0]["metrics"] if periods else {}
    latest_date = periods[0]["report_date"] if periods else None
    metric_names = {"revenue_yoy": "营业总收入同比", "profit_yoy": "归母净利润同比", "operating_cashflow_yoy": "经营现金流同比"}
    for name, title in metric_names.items():
        row = latest.get(name, {})
        if row.get("covered"):
            description = f"{latest_date} {title}：{row['value']}%" if row.get("value") is not None else f"{latest_date} {title}：基期非正/缺失，不计算增长率；当期金额{row.get('current_sum')}元"
            description += f"；同口径样本{row['covered']}/{row['total']}。"
            if "supported_total" in row:
                description += f"已接入范围{row['supported_total']}只，范围外{row.get('unsupported_total', 0)}只；不等同全板块完整。"
            value = _value(row)
            (challenges if value is not None and value < 0 else supports).append(description)
        if not row.get("complete"):
            missing.append(f"{title}尚未取得完整可比样本" if row else f"{title}：{operating.get('status', 'not_collected')}")
    valuation = context.get("valuation", {})
    if valuation.get("median_pe_ttm") is not None:
        (supports if context.get("usable") and valuation.get("status") == "available" else challenges).append(f"{valuation.get('as_of')}正PE成分中位数{valuation['median_pe_ttm']}倍，正PE {valuation.get('positive_count', 0)}/{valuation.get('total', 0)}，非正PE {valuation.get('nonpositive_count', 0)}；不是官方板块PE。")
    if valuation.get("index_pe_ttm") is not None:
        (supports if context.get("usable") and valuation.get("status") == "available" else challenges).append(f"已入库指数PE {valuation['index_pe_ttm']}倍，业务日期{valuation.get('as_of')}。")
    if valuation.get("percentile") is None:
        missing.append(f"估值历史未满足同样本60次且跨度365日：已有{valuation.get('history_count', 0)}次；不能判定便宜或昂贵")
    else:
        supports.append(f"同口径估值历史位置{valuation['percentile']}%；这是描述分位，不是价格回归概率。")
    if valuation.get("status") not in ("available",):
        missing.append(f"估值状态：{valuation.get('status', 'not_collected')}，不能当作当前完整估值")
    missing.extend(context.get("errors", []))
    missing.append("催化/行业专属资料待采集：" + "、".join(context.get("catalysts", {}).get("required", [])))
    revenue, profit, cash = (latest.get(name, {}) for name in metric_names)
    income_complete = revenue.get("complete") and profit.get("complete")
    income_negative = any(_value(metric) is not None and _value(metric) < 0 for metric in (revenue, profit))
    loss = _value(profit, "current_sum") is not None and _value(profit, "current_sum") < 0
    cash_negative = _value(cash, "current_sum") is not None and _value(cash, "current_sum") < 0
    specialist = context.get("specialist_financial", False)
    if specialist:
        challenges.append("金融行业现金流含融资/存贷款等特殊业务，不采用一般实业的经营现金流正负规则。")
    if key == "short":
        if not price_ready:
            state, label = "insufficient", "短期行情待核验"
        elif period.get("max_drawdown_pct") is not None and period["max_drawdown_pct"] <= -10:
            state, label = "risk", f"{period.get('label', '走势观察')}·波动较大"
        else:
            state = "risk" if period["status"] == "weak" else "watch" if period["status"] == "strong" else "conflict"
            label = period.get("label", "短期走势已计算")
        summary = f"短期结论：{label}。先评价价格方向与回撤；经营/估值缺项不抹去已知走势，但尚无事件和预期差验证，不能推导买入结论。"
        conditions = ["核对" + "、".join(context.get("catalysts", {}).get("required", [])) + "，再检验价格是否已提前反映。"]
    elif key == "medium":
        if income_negative or loss:
            state, label = "risk", "经营承压" if income_complete else "已披露样本承压"
        elif income_complete and all(_value(metric) is not None and _value(metric) > 0 for metric in (revenue, profit)):
            state, label = ("conflict", "经营增长，走势分歧") if not price_ready or period["status"] == "weak" else ("watch", "经营增长与走势相符")
        elif income_complete:
            state, label = "conflict", "经营增长待确认"
        else:
            state, label = "insufficient", "中期业绩待确认"
        summary = f"中期结论：{label}。" + (f"使用{latest_date}同一成分样本收入和归母利润，而不是把近60日涨幅当作业绩。" if latest_date else "尚无当前成分的可比收入与利润，保留走势信息但不判定经营方向。")
        conditions = ["跟踪下一次报告的收入、归母利润及订单兑现；保持当期/去年同期样本一致，核查低基数和修订。"]
    else:
        sustained = len(periods) >= 2 and all(
            p["metrics"].get(metric, {}).get("complete") and _value(p["metrics"][metric]) is not None and _value(p["metrics"][metric]) > 0
            for p in periods[:2] for metric in ("revenue_yoy", "profit_yoy"))
        cash_supported = len(periods) >= 2 and all(p["metrics"].get("operating_cashflow_yoy", {}).get("complete")
            and _value(p["metrics"]["operating_cashflow_yoy"], "current_sum") is not None
            and _value(p["metrics"]["operating_cashflow_yoy"], "current_sum") > 0 for p in periods[:2])
        if loss or income_negative or (cash_negative and not specialist):
            state, label = "risk", "盈利或现金流承压" if profit.get("complete") and cash.get("complete") else "披露样本存在长期风险"
        elif specialist:
            state, label = "insufficient", "金融行业专属指标待补"
            missing.append("须接入资本充足率、资产质量或偿付能力等适用指标，不能套用实业现金流模型")
        elif sustained and cash_supported and valuation.get("status") == "available" and valuation.get("percentile") is not None and not valuation.get("nonpositive_count"):
            state, label = "watch", "经营现金流有支撑"
        elif sustained and cash_supported:
            state, label = "insufficient", "经营改善，定价待验证"
        elif income_complete:
            state, label = "insufficient", "长期现金流与持续性待验证"
        else:
            state, label = "insufficient", "长期依据待补齐"
        summary = f"长期结论：{label}。不能仅凭120日价格排名推导长期价值；累计季报相互重叠，不当作独立季度增长。"
        conditions = ["取得可持续利润、经营现金流和同口径估值历史，结合行业专属指标验证当前价格是否已透支增长。"]
    if key == "short":
        missing = ([period.get("reason") or "行情待核验"] if not price_ready else []) + [
            "事件验证未完成：" + "、".join(context.get("catalysts", {}).get("required", []))]
    elif key == "medium":
        missing = [gap for gap in missing if not gap.startswith(("经营现金流同比", "估值历史", "估值状态"))]
    return {"status": state, "label": label, "summary": summary,
            "supports": supports, "challenges": challenges + context.get("limitations", []),
            "conditions": conditions, "missing": list(dict.fromkeys(missing)),
            "method_version": VERSION, "recommendation_status": "not_evaluated",
            "basis": {"short": "price_and_risk", "medium": "same_member_earnings", "long": "cashflow_and_valuation"}[key]}
