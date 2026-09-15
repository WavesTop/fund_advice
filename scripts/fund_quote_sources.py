"""Direct, bounded public quote sources used by the time-series probe."""

import datetime as dt
from decimal import Decimal, InvalidOperation, localcontext
import json
import re

import requests

if __package__:
    from .probe_fund_source import ProbeError
else:
    from probe_fund_source import ProbeError


TENCENT_URL = "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get"
COLUMNS = ["日期", "开盘", "收盘", "最高", "最低", "成交量", "成交额"]
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}\Z")


def _requested_date(value, field):
    if not isinstance(value, str) or len(value) != 8 or not value.isascii() or not value.isdigit():
        raise ProbeError("invalid_request", f"{field} 必须是 YYYYMMDD 有效日期")
    try:
        parsed = dt.datetime.strptime(value, "%Y%m%d").date()
    except ValueError as exc:
        raise ProbeError("invalid_request", f"{field} 必须是 YYYYMMDD 有效日期") from exc
    if parsed.strftime("%Y%m%d") != value:
        raise ProbeError("invalid_request", f"{field} 必须是 YYYYMMDD 有效日期")
    return parsed


def _symbol(code):
    if not isinstance(code, str) or len(code) != 6 or not code.isascii() or not code.isdigit():
        raise ProbeError("invalid_request", "基金代码必须是保留前导零的六位 ASCII 数字")
    if code.startswith("5"):
        return "sh" + code
    if code.startswith(("15", "16")):
        return "sz" + code
    raise ProbeError("unsupported_symbol", "腾讯来源不支持该基金代码前缀")


def _decode_response(text):
    if not isinstance(text, str):
        raise ProbeError("invalid_response", "腾讯来源返回无法解析")
    body = text.strip()
    if not body.startswith("{"):
        before, separator, after = body.partition("=")
        if not separator or not before.strip():
            raise ProbeError("invalid_response", "腾讯来源返回无法解析")
        body = after.strip()
    if body.endswith(";"):
        body = body[:-1].rstrip()
    try:
        value = json.loads(body)
    except (TypeError, ValueError) as exc:
        raise ProbeError("invalid_response", "腾讯来源返回无法解析") from exc
    if not isinstance(value, dict):
        raise ProbeError("invalid_response", "腾讯来源返回结构无效")
    return value


def _date(value):
    if not isinstance(value, str) or not _DATE.fullmatch(value):
        raise ProbeError("invalid_response", "腾讯来源返回无效日期")
    try:
        parsed = dt.date.fromisoformat(value)
    except ValueError as exc:
        raise ProbeError("invalid_response", "腾讯来源返回无效日期") from exc
    if parsed.isoformat() != value:
        raise ProbeError("invalid_response", "腾讯来源返回无效日期")
    return parsed


def _number(value, field, *, positive=False):
    if value is None or value == "":
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise ProbeError("invalid_response", f"腾讯来源 {field} 不是有效数字")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ProbeError("invalid_response", f"腾讯来源 {field} 不是有效数字") from exc
    if not number.is_finite() or (number <= 0 if positive else number < 0):
        raise ProbeError("invalid_response", f"腾讯来源 {field} 超出允许范围")
    return number


def _amount_cny(value):
    number = _number(value, "成交额")
    if number is None:
        return None
    with localcontext() as context:
        context.prec = max(40, len(number.as_tuple().digits) + 5)
        return format(number * Decimal("10000"), "f")


def _selected_fields(raw):
    names = ("date", "open", "close", "high", "low", "volume", "turnover", "amount")
    return dict(zip(names, [*raw[:6], raw[7], raw[8]]))


def _parse_window(response, symbol, code, expected_kind, window_start, window_end):
    payload = _decode_response(response.text)
    if type(payload.get("code")) is not int or payload["code"] != 0:
        raise ProbeError("upstream_failure", "腾讯来源返回失败状态")
    data = payload.get("data")
    if not isinstance(data, dict) or not isinstance(data.get(symbol), dict):
        raise ProbeError("invalid_response", "腾讯来源返回缺少请求证券")
    node = data[symbol]
    qt = node.get("qt")
    if not isinstance(qt, dict) or not isinstance(qt.get(symbol), list) or len(qt[symbol]) < 62:
        raise ProbeError("invalid_response", "腾讯来源证券身份结构无效")
    identity = qt[symbol]
    if identity[2] != code or not isinstance(identity[1], str) or not identity[1].strip():
        raise ProbeError("invalid_response", "腾讯来源证券身份不匹配")
    if identity[61] not in ("ETF", "LOF") or (
        expected_kind is not None and identity[61] != expected_kind
    ):
        raise ProbeError("invalid_response", "腾讯来源证券类型不匹配")
    if "day" not in node or not isinstance(node["day"], list):
        raise ProbeError("invalid_response", "腾讯来源缺少未复权日线")

    parsed = []
    for raw in node["day"]:
        if not isinstance(raw, list) or len(raw) < 9:
            raise ProbeError("invalid_response", "腾讯来源日线行结构无效")
        day = _date(raw[0])
        for index, field in ((1, "开盘"), (2, "收盘"), (3, "最高"), (4, "最低")):
            _number(raw[index], field, positive=True)
        _number(raw[5], "成交量")
        _number(raw[8], "成交额")
        parsed.append((day, raw))
    returned = [item[0] for item in parsed]
    selected = [(day, raw) for day, raw in parsed if window_start <= day <= window_end]
    if any(any(raw[index] in (None, "") for index in range(1, 5)) for _, raw in selected):
        raise ProbeError("invalid_response", "腾讯来源日线价格为空")
    earliest = min(returned) if returned else None
    truncated = len(parsed) >= 640 and selected and earliest > window_start
    if truncated:
        raise ProbeError("truncated_response", "腾讯来源单次返回达到上限，无法证明窗口完整")
    status = "window_selected" if selected else "empty"
    subset = {
        "code": payload["code"],
        "symbol": symbol,
        "day": [_selected_fields(raw) for _, raw in selected],
    }
    subset.update({"identity_code": identity[2], "identity_name": identity[1],
                   "identity_kind": identity[61]})
    bounds = {
        "min_date": min(returned).isoformat() if returned else None,
        "max_date": max(returned).isoformat() if returned else None,
    }
    return selected, status, bounds, subset, len(parsed)


def fetch_tencent_daily(code, start_date, end_date, expected_kind=None):
    """Fetch unadjusted Tencent daily quotes, split by calendar year."""
    symbol = _symbol(code)
    if expected_kind not in (None, "ETF", "LOF"):
        raise ProbeError("invalid_request", "expected_kind 必须是 ETF 或 LOF")
    start = _requested_date(start_date, "start_date")
    end = _requested_date(end_date, "end_date")
    if start > end:
        raise ProbeError("invalid_request", "start_date 不能晚于 end_date")
    effective_end = min(end, dt.date.today())
    evidence = {"artifact_kind": "source_response", "provider": "tencent",
                "url": TENCENT_URL, "symbol": symbol, "windows": []}
    merged = {}
    if start <= effective_end:
        for year in range(start.year, effective_end.year + 1):
            window_start = max(start, dt.date(year, 1, 1))
            window_end = min(effective_end, dt.date(year, 12, 31))
            params = {
                "_var": f"kline_day{year}",
                "param": f"{symbol},day,{year}-01-01,{year}-12-31,640,",
            }
            response = requests.get(TENCENT_URL, params=params, timeout=12)
            response.raise_for_status()
            selected, status, bounds, subset, returned_count = _parse_window(
                response, symbol, code, expected_kind, window_start, window_end
            )
            evidence["windows"].append({
                "params": params,
                "requested_bounds": {"start_date": window_start.isoformat(), "end_date": window_end.isoformat()},
                "actual_returned_bounds": bounds,
                "returned_row_count": returned_count,
                "status": status,
                "response_subset": subset,
            })
            for day, raw in selected:
                retained = [*raw[:6], raw[7], raw[8]]
                previous = merged.get(day)
                if previous is not None and previous != retained:
                    raise ProbeError("duplicate_response", "腾讯来源同一日期返回冲突数据")
                merged[day] = retained
    rows = []
    for day in sorted(merged):
        raw = merged[day]
        rows.append([raw[0], raw[1], raw[2], raw[3], raw[4], raw[5], _amount_cny(raw[7])])
    return {
        "provider": "tencent",
        "columns": COLUMNS.copy(),
        "dtypes": {column: "object" for column in COLUMNS},
        "rows": rows,
        "quote_units": {"volume": "hands", "amount": "CNY"},
        "source_evidence": evidence,
    }


def fetch_baostock_daily(code, start_date, end_date, expected_kind=None):
    """Fetch unadjusted ETF daily quotes from the free BaoStock service."""
    if expected_kind != "ETF":
        raise ProbeError("unsupported_symbol", "BaoStock 来源只用于已验证的 ETF 日线")
    symbol = _symbol(code).replace("sh", "sh.", 1).replace("sz", "sz.", 1)
    start = _requested_date(start_date, "start_date")
    end = _requested_date(end_date, "end_date")
    if start > end:
        raise ProbeError("invalid_request", "start_date 不能晚于 end_date")

    try:
        import baostock as bs
    except ImportError as exc:
        raise ProbeError("missing_dependency", "未安装 BaoStock 来源依赖") from exc

    login = bs.login()
    if login.error_code != "0":
        raise ProbeError("upstream_failure", "BaoStock 登录失败")
    try:
        basic = bs.query_stock_basic(code=symbol)
        if basic.error_code != "0":
            raise ProbeError("upstream_failure", "BaoStock 证券身份查询失败")
        identities = []
        while basic.next():
            identities.append(dict(zip(basic.fields, basic.get_row_data())))
        if len(identities) != 1:
            raise ProbeError("invalid_response", "BaoStock 未返回唯一 ETF 身份")
        identity = identities[0]
        if identity.get("code") != symbol or not identity.get("code_name") or identity.get("type") != "5":
            raise ProbeError("invalid_response", "BaoStock 证券身份或类型不匹配")

        ipo_date = _date(identity.get("ipoDate"))
        out_date = _date(identity["outDate"]) if identity.get("outDate") else None
        effective_start = max(start, ipo_date)
        effective_end = min(end, dt.date.today(), out_date) if out_date else min(end, dt.date.today())
        fields = "date,code,open,high,low,close,volume,amount,adjustflag,tradestatus"
        result = bs.query_history_k_data_plus(
            symbol, fields, start_date=start.isoformat(), end_date=min(end, dt.date.today()).isoformat(),
            frequency="d", adjustflag="3",
        )
        if result.error_code != "0":
            raise ProbeError("upstream_failure", "BaoStock 日线查询失败")
        rows, returned = [], []
        while result.next():
            item = dict(zip(result.fields, result.get_row_data()))
            if item.get("code") != symbol or item.get("adjustflag") != "3":
                raise ProbeError("invalid_response", "BaoStock 返回证券或复权口径不匹配")
            day = _date(item.get("date"))
            for field in ("open", "high", "low", "close"):
                _number(item.get(field), field, positive=True)
            _number(item.get("volume"), "成交量")
            _number(item.get("amount"), "成交额")
            returned.append(day)
            rows.append([day.isoformat(), item["open"], item["close"], item["high"], item["low"],
                         item["volume"] or None, item["amount"] or None])
        if effective_start <= effective_end:
            if not returned and (effective_end - effective_start).days >= 14:
                raise ProbeError("truncated_response", "BaoStock 未返回有效上市区间内的日线，无法证明覆盖完整")
            if returned and (min(returned) - effective_start).days >= 14:
                raise ProbeError("truncated_response", "BaoStock 返回起点晚于请求区间，无法证明历史完整")
            if returned and (effective_end - max(returned)).days >= 14:
                raise ProbeError("truncated_response", "BaoStock 返回终点早于请求区间，无法证明历史完整")
        return {
            "provider": "baostock", "columns": COLUMNS.copy(),
            "dtypes": {column: "object" for column in COLUMNS}, "rows": rows,
            "quote_units": {"volume": "shares", "amount": "CNY"},
            "source_evidence": {
                "artifact_kind": "source_response", "provider": "baostock", "symbol": symbol,
                "identity": {"code": symbol, "name": identity["code_name"], "kind": "ETF"},
                "requested_bounds": {"start_date": start.isoformat(), "end_date": end.isoformat()},
                "actual_returned_bounds": {
                    "min_date": min(returned).isoformat() if returned else None,
                    "max_date": max(returned).isoformat() if returned else None,
                },
                "returned_row_count": len(rows), "adjustment": "none",
            },
        }
    finally:
        bs.logout()
