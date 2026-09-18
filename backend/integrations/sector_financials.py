"""Collect public issuer statements and dated valuations once, then join exact members.

These are *current Eastmoney membership samples*, not official index earnings or
historical index constituents. No macro-industry name matching or synthetic PE.
Provider fields follow the published data-centre tables; live schema/availability
is checked on every request and must be verified separately from fixture tests.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
import json
import re
import time
from urllib.parse import urlencode
from urllib.request import Request

from backend.core.config import Settings
from backend.core.trading_calendar import SHANGHAI, get_calendar, day
from backend.integrations.http_transport import get_bytes
from backend.storage.database import migrate
from backend.storage.research import number, utc_now
from backend.storage.sector_fundamentals import VERSION, archive_response, membership_identity, save_observation

API_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
SOURCE_ID = "financials.eastmoney_datacenter"
TABLES = {
    "income": ("RPT_DMSK_FN_INCOME", ("TOTAL_OPERATE_INCOME", "PARENT_NETPROFIT")),
    "cashflow": ("RPT_DMSK_FN_CASHFLOW", ("NETCASH_OPERATE",)),
    "valuation": ("RPT_VALUEANALYSIS_DET", ("PE_TTM",)),
}
PAGE_SIZE = 500
MAX_PAGES = 30
BUDGET_SECONDS = 180
LIMITATIONS = [
    "经营数据为当前东方财富成分公司同口径样本的累计报告期比较，不是单季环比或官方指数加权盈利。",
    "来源为东方财富财务数据转录，未逐份审计发行人原报告；原响应、报告期和公告日期均留存。",
    "成分公司之间可能存在关联交易或母子公司交叉纳入，简单合计不是行业合并财务报表。",
    "PE为有正PE成分的中位数，不是官方板块PE；亏损、缺失和样本变化单独披露。",
    "估值历史只累计系统实际观察过的相同成分样本，不把今天的成分倒推成历史行业。",
]


def report_periods(today: date) -> list[str]:
    """Latest *ended* quarter and its predecessor. Do not back off to flatter data."""
    quarters = sorted(date(year, month, last) for year in (today.year - 1, today.year)
                      for month, last in ((3, 31), (6, 30), (9, 30), (12, 31))
                      if date(year, month, last) < today)
    return [value.isoformat() for value in quarters[-2:]][::-1]


def previous_year(value: str) -> str:
    parsed = day(value)
    return parsed.replace(year=parsed.year - 1).isoformat()


def _date_field(value: object, name: str) -> str:
    if name == "NOTICE_DATE" and isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}", value):
        return datetime.fromisoformat(value).date().isoformat()
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}(?:[ T]00:00:00)?", value):
        raise ValueError(f"{name}不是日期或来源出现未约定时间精度")
    return day(value[:10]).isoformat()


def _decimal(value: object) -> str | None:
    if value in (None, "", "-", "--"):
        return None
    if isinstance(value, bool) or isinstance(value, float):
        raise ValueError("财务数值必须是来源精确小数，不能接收布尔或二进制浮点")
    return number(str(value))


def parse_rows(rows: list, kind: str, business_date: str, now: datetime) -> tuple[dict, dict]:
    """Reject identity/schema drift; preserve unavailable and unpublished observations."""
    expected = TABLES[kind][1]
    result, excluded = {}, {}
    seen = set()
    local_day = now.astimezone(SHANGHAI).date()
    for raw in rows:
        if not isinstance(raw, dict) or not isinstance(raw.get("SECURITY_CODE"), str):
            raise ValueError("财务行缺少来源证券身份")
        code = raw["SECURITY_CODE"]
        if not re.fullmatch(r"\d{6}", code) or code in seen:
            raise ValueError("来源证券代码无效或分页出现重复")
        seen.add(code)
        field = "TRADE_DATE" if kind == "valuation" else "REPORT_DATE"
        if _date_field(raw.get(field), field) != business_date:
            raise ValueError("来源业务日期与请求不符，不能混合报告期或交易日")
        if not all(name in raw for name in expected):
            raise ValueError(f"来源字段发生变化：{','.join(expected)}")
        published = None
        if kind != "valuation":
            try:
                published = _date_field(raw.get("NOTICE_DATE"), "NOTICE_DATE")
            except ValueError:
                excluded[code] = "公告日期缺失或精度不明"
                continue
            if published < business_date:
                excluded[code] = "公告早于业务日期"
                continue
            # Day-only publication becomes available at next local midnight.
            if day(published) >= local_day:
                excluded[code] = "公告尚未到达保守可用时点"
                continue
        try:
            values = {name: _decimal(raw[name]) for name in expected}
        except ValueError as exc:
            excluded[code] = f"数值字段无效：{exc}"
            continue
        if kind == "income" and values["TOTAL_OPERATE_INCOME"] is not None and Decimal(values["TOTAL_OPERATE_INCOME"]) < 0:
            excluded[code] = "营业总收入为负，须核查来源"
            continue
        result[code] = {"code": code, "date": business_date, "published_at": published, **values}
    return result, excluded


def _integer(value: object) -> int:
    if isinstance(value, bool) or not re.fullmatch(r"\d+", str(value)):
        raise ValueError("来源分页数量无效")
    return int(value)


def fetch_table(settings: Settings, kind: str, business_date: str, now: datetime, *,
                fetcher, deadline: float, clock=time.monotonic) -> dict:
    report, fields = TABLES[kind]
    date_field = "TRADE_DATE" if kind == "valuation" else "REPORT_DATE"
    columns = ["SECURITY_CODE", date_field, *fields]
    if kind != "valuation":
        columns.append("NOTICE_DATE")
    params = {"reportName": report, "columns": ",".join(columns), "pageSize": PAGE_SIZE,
              "sortColumns": "SECURITY_CODE", "sortTypes": "1", "source": "WEB", "client": "WEB",
              "filter": f"({date_field}='{business_date}')"}
    if kind != "valuation":
        params["filter"] += '(SECURITY_TYPE_CODE in ("058001001","058001008"))(TRADE_MARKET_CODE!="069001017")'
    all_rows, sources, totals = [], [], None
    for page in range(1, MAX_PAGES + 1):
        remaining = deadline - clock()
        if remaining <= 0:
            raise TimeoutError("经营估值采集时间预算耗尽，未完成部分不能算作成功")
        params["pageNumber"] = page
        url = API_URL + "?" + urlencode(params)
        body = fetcher(url, min(8.0, remaining))
        if not isinstance(body, bytes):
            raise ValueError("财务来源响应必须为原始字节")
        asset_id = archive_response(settings, url, body)
        sources.append({"url": url, "asset_id": asset_id})
        root = json.loads(body, parse_float=str)
        if not isinstance(root, dict) or root.get("success") is not True or not isinstance(root.get("result"), dict):
            code = root.get("code") if isinstance(root, dict) else None
            message = root.get("message") if isinstance(root, dict) else "响应不是对象"
            raise ValueError(f"财务来源{report}/{business_date}未返回成功结果；code={code!r} message={str(message)[:300]}")
        result = root["result"]
        count, pages = _integer(result.get("count")), _integer(result.get("pages"))
        if count > PAGE_SIZE * MAX_PAGES or pages > MAX_PAGES or pages != ((count + PAGE_SIZE - 1) // PAGE_SIZE):
            raise ValueError("财务来源分页不完整或超过采集上限")
        if totals is not None and totals != (count, pages):
            raise ValueError("财务数据采集中总数变化，不能发布拼接快照")
        totals = count, pages
        rows = result.get("data")
        expected = min(PAGE_SIZE, count - len(all_rows))
        if not isinstance(rows, list) or len(rows) != expected:
            raise ValueError("财务来源截断页，不能将部分公司当作全行业")
        all_rows.extend(rows)
        if page >= pages:
            break
    parsed, excluded = parse_rows(all_rows, kind, business_date, now)
    return {"status": "available", "rows": parsed, "excluded": excluded, "sources": sources,
            "source_count": len(all_rows), "error": None}


def _supported(member: dict) -> bool:
    code, market = str(member.get("stock_code", "")), str(member.get("market", ""))
    return bool(re.fullmatch(r"\d{6}", code)) and ((market == "1" and code.startswith("6"))
                                                    or (market == "0" and code.startswith(("0", "3"))))


def _sum(rows: dict, codes: list[str], field: str) -> Decimal:
    return sum((Decimal(rows[code][field]) for code in codes), Decimal(0))


def operating_sample(members: list[dict], reports: list[str], datasets: dict) -> dict:
    total = len(members)
    supported = sorted({member["stock_code"] for member in members if _supported(member)})
    output = {"status": "partial", "total_members": total, "supported_members": len(supported), "periods": []}
    for report in reports:
        previous = previous_year(report)
        point = {"report_date": report, "base_report_date": previous, "metrics": {}}
        for kind, names in (("income", ("revenue_yoy", "profit_yoy")), ("cashflow", ("operating_cashflow_yoy",))):
            current, base = datasets[(kind, report)], datasets[(kind, previous)]
            fields = TABLES[kind][1]
            # Revenue and profit share one sample, both on matching YTD report dates.
            common = [code for code in supported if code in current["rows"] and code in base["rows"]
                      and all(current["rows"][code][field] is not None and base["rows"][code][field] is not None for field in fields)]
            omissions = []
            for member in members:
                code = member["stock_code"]
                if code in common:
                    continue
                reason = ("暂未接入该市场" if not _supported(member) else current.get("error") or base.get("error")
                          or current["excluded"].get(code) or base["excluded"].get(code) or "当期/去年同期报表或字段缺失")
                omissions.append({"code": code, "reason": reason})
            for metric, field in zip(names, fields):
                current_sum = _sum(current["rows"], common, field) if common else None
                base_sum = _sum(base["rows"], common, field) if common else None
                growth = (current_sum / base_sum - 1) * 100 if base_sum is not None and base_sum > 0 else None
                public_dates = [dataset["rows"][code]["published_at"] for dataset in (current, base) for code in common]
                point["metrics"][metric] = {
                    "value": str(growth) if growth is not None else None, "unit": "%",
                    "current_sum": str(current_sum) if current_sum is not None else None,
                    "base_sum": str(base_sum) if base_sum is not None else None, "amount_unit": "CNY",
                    "delta": str(current_sum - base_sum) if common else None,
                    "covered": len(common), "total": total, "sample_codes": common,
                    "complete": total > 0 and len(common) == total,
                    "supported_total": len(supported), "unsupported_total": total - len(supported),
                    "supported_complete": bool(supported) and len(common) == len(supported),
                    "coverage_pct": str(Decimal(len(common)) / total * 100) if total else None,
                    "scope": "当前成分中已接入的沪深A股可比样本；complete仍以全部成分为分母",
                    "published_at": max(public_dates) if public_dates else None,
                    "excluded": omissions,
                    "baseline": "positive" if base_sum is not None and base_sum > 0 else "nonpositive" if base_sum is not None else "missing",
                }
        output["periods"].append(point)
    full = bool(total) and all(metric["complete"] for p in output["periods"] for metric in p["metrics"].values())
    any_value = any(metric["covered"] for p in output["periods"] for metric in p["metrics"].values())
    output["status"] = "available" if full else "partial" if any_value else "missing"
    return output


def valuation_sample(members: list[dict], dataset: dict, as_of: str) -> dict:
    codes = [member["stock_code"] for member in members if _supported(member)]
    rows = dataset["rows"]
    observed = [code for code in codes if code in rows and rows[code]["PE_TTM"] is not None]
    positive = sorted(code for code in observed if Decimal(rows[code]["PE_TTM"]) > 0)
    values = sorted(Decimal(rows[code]["PE_TTM"]) for code in positive)
    n = len(values)
    median = (values[n // 2] if n % 2 else (values[n // 2 - 1] + values[n // 2]) / 2) if n else None
    return {"status": "available" if len(observed) == len(members) and members else "partial" if observed else "missing",
            "as_of": as_of, "covered": len(observed), "total": len(members),
            "supported_total": len(codes), "unsupported_total": len(members) - len(codes),
            "supported_complete": bool(codes) and len(observed) == len(codes),
            "median_pe_ttm": str(median) if median is not None else None,
            "positive_count": len(positive), "nonpositive_count": len(observed) - len(positive),
            "positive_codes": positive, "error": dataset.get("error"),
            "excluded": [{"code": member["stock_code"], "reason": "暂未接入该市场" if not _supported(member) else dataset.get("error") or "缺少同日PE"}
                         for member in members if member["stock_code"] not in observed],
            "methodology": "当前成分正PE(TTM)中位数；非官方指数PE；不使用成分表中无日期的市值倒算"}


def collect_sector_fundamentals(settings: Settings, boards: list[dict], *, fetcher=None,
                                now: datetime | None = None, clock=time.monotonic, progress=None) -> dict:
    migrate(settings)
    now = now or datetime.now(SHANGHAI)
    if now.tzinfo is None:
        raise ValueError("采集时间必须带时区")
    now = now.astimezone(SHANGHAI)
    cutoff = get_calendar().latest_completed(now).isoformat()
    reports = report_periods(now.date())
    keys = [(kind, report) for kind in ("income", "cashflow")
            for report in sorted(set(reports + [previous_year(p) for p in reports]), reverse=True)]
    keys.append(("valuation", cutoff))
    if fetcher is None:
        def fetcher(url, timeout):
            request = Request(url, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://data.eastmoney.com/", "Accept": "application/json"})
            return get_bytes(request, timeout=timeout)
    valid, outcomes = [], []
    for board in boards:
        identity = membership_identity(board)
        initial = {"version": VERSION, "subject_key": f"hot_board:{board['source_id']}:{board['code']}",
                   "membership_hash": identity["hash"], "membership_as_of": identity["as_of"],
                   "total_members": identity["count"], "name": board["name"], "status": "collecting",
                   "operating": {"status": "not_collected", "periods": []},
                   "valuation": {"status": "not_collected"}, "sources": [], "errors": [], "limitations": LIMITATIONS}
        if not identity["valid"] or not identity["as_of"] or day(identity["as_of"]) < day(cutoff) or day(identity["as_of"]) > now.date():
            initial.update(status="blocked", errors=["当前成分快照缺失、更新失败或日期不匹配；不能用旧成分代表当前行业"])
        elif not any(_supported(member) for member in board["membership"]["members"]):
            initial.update(status="blocked", errors=["当前成分没有已接入的沪深A股；不替换市场或省略分母"])
        save_observation(settings, initial)
        if initial["status"] == "collecting":
            valid.append((board, initial))
        else:
            outcomes.append({"code": board["code"], "status": "blocked"})
    if not valid:
        return {"status": "not_collected", "total": len(boards), "available": 0, "items": outcomes,
                "message": "没有当前可核对的沪深A股成分，未请求财务来源"}
    datasets, errors, deadline = {}, [], clock() + BUDGET_SECONDS
    for kind, report in keys:
        if progress:
            progress({"stage": "financials", "dataset": kind, "business_date": report, "state": "running"})
        try:
            dataset = fetch_table(settings, kind, report, now, fetcher=fetcher, deadline=deadline, clock=clock)
        except Exception as exc:
            message = f"{kind}/{report}: {type(exc).__name__}: {str(exc)[:300]}"
            dataset = {"status": "failed", "rows": {}, "excluded": {}, "sources": [], "error": message}
            errors.append(message)
        datasets[(kind, report)] = dataset
        if progress:
            progress({"stage": "financials", "dataset": kind, "business_date": report,
                      "state": dataset["status"], "row_count": len(dataset["rows"]),
                      "error": dataset.get("error")})
    for board, initial in valid:
        members = board["membership"]["members"]
        operating = operating_sample(members, reports, datasets)
        valuation = valuation_sample(members, datasets[("valuation", cutoff)], cutoff)
        full = operating["status"] == valuation["status"] == "available"
        any_data = operating["status"] in ("available", "partial") or valuation["status"] in ("available", "partial")
        status = "available" if full else "partial" if any_data else "failed" if errors else "missing"
        payload = {**initial, "status": status, "operating": operating, "valuation": valuation,
                   "sources": [source for dataset in datasets.values() for source in dataset["sources"]],
                   "errors": errors, "valuation_history": []}
        save_observation(settings, payload)
        outcomes.append({"code": board["code"], "status": status})
    available = sum(item["status"] == "available" for item in outcomes)
    return {"status": "success" if available == len(boards) else "partial" if any(item["status"] in ("available", "partial") for item in outcomes) else "failed",
            "total": len(boards), "available": available, "items": outcomes, "errors": errors,
            "message": f"经营与同日估值样本完整 {available}/{len(boards)}；历史估值积累、行业催化与正式投资验证另行判断"}
