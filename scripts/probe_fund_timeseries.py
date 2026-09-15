#!/usr/bin/env python3
"""Bounded, single-call AKShare time-series probe (not a production adapter)."""

import argparse
import datetime as dt
from decimal import Decimal, InvalidOperation, localcontext
import json
import math
import subprocess
import sys
import time
from pathlib import Path

if __package__:
    from .probe_fund_source import ProbeError, installed_akshare_version, write_report
else:
    from probe_fund_source import ProbeError, installed_akshare_version, write_report


DEFAULT_OUTPUT = Path("runtime/probes")
DATASETS = (
    "unit-nav", "cumulative-nav", "exchange-nav", "etf-daily", "lof-daily", "calendar"
)
QUOTE_DATASETS = ("etf-daily", "lof-daily")
SOURCE_ERRORS = {
    "missing_dependency": "未找到来源接口所需依赖，请先同步项目环境",
    "network_timeout": "来源网络请求超时",
    "network_error": "无法连接来源，请检查网络、代理或来源可用性",
    "upstream_failure": "来源调用或解析失败，需核验接口返回结构",
    "invalid_response": "来源返回结构、身份或字段无效",
    "truncated_response": "来源返回的历史窗口可能被截断",
    "unsupported_symbol": "行情探针不支持此代码范围",
    "duplicate_response": "来源在同一日期返回冲突数据",
}
INTERFACES = {
    "unit-nav": "akshare.fund_open_fund_info_em",
    "cumulative-nav": "akshare.fund_open_fund_info_em",
    "exchange-nav": "akshare.fund_etf_fund_info_em",
    "etf-daily": "akshare.fund_etf_hist_em",
    "lof-daily": "akshare.fund_lof_hist_em",
    "calendar": "akshare.tool_trade_date_hist_sina",
}
LIMITATIONS = {
    "unit-nav": ["来源接口不接受日期参数；探针获取成立以来全量数据后在本地筛选"],
    "cumulative-nav": ["来源接口不接受日期参数；探针获取成立以来全量数据后在本地筛选"],
    "exchange-nav": ["来源内部按每页 20 条抓取；来源空数据当前可能以拼接 ValueError 失败，不能视为空结果"],
    "etf-daily": ["行情为未复权日线；AKShare 已将来源数字转换为数值，无法恢复网页原始小数精度"],
    "lof-daily": ["行情为未复权日线；AKShare 已将来源数字转换为数值，无法恢复网页原始小数精度"],
    "calendar": ["AKShare 适配器会插入 1992-05-04；此日历不是逐市场或境外市场日历"],
}
SERIES_LIMITATIONS = [
    "AKShare 已将来源数字转换为数值，报告不声称恢复来源原始小数精度",
    "来源不提供可验证的发布时间，published_at 为空",
    "探针不提供历史修订版本或 as-of 快照语义",
]


def market_symbol(code):
    if code.startswith("5"):
        return "sh" + code
    if code.startswith(("15", "16")):
        return "sz" + code
    raise ProbeError("unsupported_symbol", "行情探针仅支持已约定的沪深 ETF／LOF 代码范围")


def source_interface(request):
    provider = request.get("provider", "eastmoney")
    if provider == "sina" and request["dataset"] in QUOTE_DATASETS:
        return "akshare.fund_etf_hist_sina"
    if provider == "tencent":
        return "fund_quote_sources.fetch_tencent_daily"
    if provider == "baostock":
        return "fund_quote_sources.fetch_baostock_daily"
    return INTERFACES[request["dataset"]]


def sina_identity(catalog, symbol, dataset):
    if "代码" not in catalog.columns or "名称" not in catalog.columns:
        raise ProbeError("invalid_response", "新浪基金分类目录缺少身份字段")
    matches = catalog[catalog["代码"] == symbol]
    if len(matches) != 1 or not isinstance(matches.iloc[0]["名称"], str) or not matches.iloc[0]["名称"].strip():
        raise ProbeError("invalid_response", "来源未返回与所选 ETF／LOF 类型一致的身份，需核验代码或历史身份资料")
    return {"symbol": symbol, "name": matches.iloc[0]["名称"],
            "kind": "ETF" if dataset == "etf-daily" else "LOF",
            "interface": "akshare.fund_etf_category_sina",
            "params": {"symbol": "ETF基金" if dataset == "etf-daily" else "LOF基金"}}


def _child_code():
    return r'''
import datetime as dt
from decimal import Decimal
import json
import math
import sys

def scalar(value):
    if value is None or bool(pd.isna(value)):
        return None
    if hasattr(value, "to_pydatetime"):
        value = value.to_pydatetime()
    if isinstance(value, (dt.datetime, dt.date)):
        return value.isoformat()
    if hasattr(value, "item"):
        try:
            value = value.item()
        except (ValueError, TypeError):
            pass
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Decimal):
        return format(value, "f") if value.is_finite() else None
    raise TypeError("unsupported dataframe scalar")

try:
    import akshare as ak
    import pandas as pd
    request = json.loads(sys.argv[1])
    sys.path.insert(0, sys.argv[2])
    dataset = request["dataset"]
    code = request.get("code")
    provider = request.get("provider", "eastmoney")
    identity = None
    start, end = request["start_date"], request["end_date"]
    if provider == "tencent":
        from fund_quote_sources import fetch_tencent_daily
        print(json.dumps(fetch_tencent_daily(code, start, end, expected_kind="ETF" if dataset == "etf-daily" else "LOF"), ensure_ascii=False, allow_nan=False))
        raise SystemExit(0)
    elif provider == "baostock":
        from fund_quote_sources import fetch_baostock_daily
        print(json.dumps(fetch_baostock_daily(code, start, end, expected_kind="ETF" if dataset == "etf-daily" else "LOF"), ensure_ascii=False, allow_nan=False))
        raise SystemExit(0)
    elif provider == "sina" and dataset in ("etf-daily", "lof-daily"):
        from probe_fund_timeseries import market_symbol, sina_identity
        symbol = market_symbol(code)
        catalog = ak.fund_etf_category_sina(symbol="ETF基金" if dataset == "etf-daily" else "LOF基金")
        identity = sina_identity(catalog, symbol, dataset)
        frame = ak.fund_etf_hist_sina(symbol=symbol)
    elif dataset in ("unit-nav", "cumulative-nav"):
        indicator = "单位净值走势" if dataset == "unit-nav" else "累计净值走势"
        frame = ak.fund_open_fund_info_em(symbol=code, indicator=indicator, period="成立来")
    elif dataset == "exchange-nav":
        frame = ak.fund_etf_fund_info_em(fund=code, start_date=start, end_date=end)
    elif dataset == "etf-daily":
        frame = ak.fund_etf_hist_em(symbol=code, period="daily", start_date=start, end_date=end, adjust="")
    elif dataset == "lof-daily":
        frame = ak.fund_lof_hist_em(symbol=code, period="daily", start_date=start, end_date=end, adjust="")
    else:
        frame = ak.tool_trade_date_hist_sina()
    columns = [str(c) for c in frame.columns]
    rows = [[scalar(value) for value in row] for row in frame.itertuples(index=False, name=None)]
    dtypes = {str(column): str(dtype) for column, dtype in frame.dtypes.items()}
    print(json.dumps({"columns": columns, "dtypes": dtypes, "rows": rows,
                      "provider": provider,
                      "source_evidence": {"artifact_kind": "adapter_output", "symbol": market_symbol(code),
                          "identity": identity,
                          "source_url": "https://finance.sina.com.cn/realstock/company/" + market_symbol(code) + "/hisdata_klc2/klc_kl.js"} if provider == "sina" and dataset in ("etf-daily", "lof-daily") else None,
                      "akshare_version": getattr(ak, "__version__", None)}, ensure_ascii=False))
except ImportError:
    print(json.dumps({"error": "missing_dependency"}))
except Exception as exc:
    from probe_fund_source import ProbeError
    if isinstance(exc, ProbeError):
        print(json.dumps({"error": exc.code if exc.code in ("invalid_response", "truncated_response", "unsupported_symbol", "duplicate_response") else "upstream_failure"}))
        raise SystemExit(0)
    module = type(exc).__module__.lower()
    name = type(exc).__name__
    if name in ("TimeoutError", "Timeout", "ConnectTimeout", "ReadTimeout"):
        category = "network_timeout"
    elif any(part in module for part in ("requests", "urllib", "httpx", "socket")):
        category = "network_error"
    else:
        category = "upstream_failure"
    print(json.dumps({"error": category}))
'''


def run_adapter(request, timeout):
    started = time.monotonic()
    try:
        completed = subprocess.run(
            [sys.executable, "-c", _child_code(), json.dumps(request), str(Path(__file__).resolve().parent)],
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise ProbeError("timeout", "来源请求超过硬超时限制") from exc
    except (OSError, UnicodeError) as exc:
        raise ProbeError("subprocess_error", "来源探针子进程无法安全完成") from exc
    if completed.returncode != 0:
        raise ProbeError("upstream_failure", "来源接口调用失败")
    try:
        payload = json.loads(completed.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        raise ProbeError("invalid_response", "来源返回无法解析")
    if not isinstance(payload, dict):
        raise ProbeError("invalid_response", "来源返回无法解析")
    error = payload.get("error")
    if "error" in payload:
        if not isinstance(error, str) or error not in SOURCE_ERRORS:
            raise ProbeError("invalid_response", "来源返回无法识别的错误类别")
        raise ProbeError(error, SOURCE_ERRORS[error])
    return payload, time.monotonic() - started


def parse_date(value, field="date"):
    if not isinstance(value, str) or len(value) != 10:
        raise ProbeError("invalid_response", f"来源 {field} 不是日期")
    try:
        date = dt.date.fromisoformat(value)
        if date.isoformat() != value:
            raise ValueError
        return date
    except ValueError as exc:
        raise ProbeError("invalid_response", f"来源 {field} 不是有效日期") from exc


def parse_requested_date(value):
    return dt.datetime.strptime(value, "%Y%m%d").date()


def decimal_string(value, field, *, nonnegative=False, positive=False, factor=None):
    if value is None or value == "":
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ProbeError("invalid_response", f"来源 {field} 不是有效数字") from exc
    if not number.is_finite() or (positive and number <= 0) or (nonnegative and number < 0):
        raise ProbeError("invalid_response", f"来源 {field} 超出允许范围")
    if factor is not None:
        with localcontext() as context:
            context.prec = max(40, len(number.as_tuple().digits) + len(factor.as_tuple().digits))
            number *= factor
    return format(number, "f")


def _table(payload):
    columns, rows, dtypes = payload.get("columns"), payload.get("rows"), payload.get("dtypes")
    if not isinstance(columns, list) or not all(isinstance(c, str) for c in columns):
        raise ProbeError("invalid_response", "来源列名无效")
    if len(set(columns)) != len(columns) or not isinstance(rows, list) or not isinstance(dtypes, dict):
        raise ProbeError("invalid_response", "来源表格结构无效")
    if any(not isinstance(key, str) or not isinstance(value, str) for key, value in dtypes.items()):
        raise ProbeError("invalid_response", "来源字段类型信息无效")
    output = []
    for values in rows:
        if not isinstance(values, list) or len(values) != len(columns):
            raise ProbeError("invalid_response", "来源行结构无效")
        if any(not (value is None or isinstance(value, (str, bool, int)) or
                   isinstance(value, float) and math.isfinite(value)) for value in values):
            raise ProbeError("invalid_response", "来源行包含不可序列化值")
        output.append(dict(zip(columns, values)))
    return columns, dtypes, rows, output


def _require(row, names):
    missing = [name for name in names if name not in row]
    if missing:
        raise ProbeError("missing_columns", "来源结果缺少必需列：" + "、".join(missing))


def normalize(dataset, payload, start_date, end_date):
    columns, dtypes, raw_rows, records = _table(payload)
    start, end = parse_requested_date(start_date), parse_requested_date(end_date)
    sina_quote = dataset in QUOTE_DATASETS and payload.get("provider") == "sina"
    quote_fields = dict(zip(("open", "high", "low", "close"),
                           ("open", "high", "low", "close") if sina_quote else ("开盘", "最高", "最低", "收盘")))
    date_column = "trade_date" if dataset == "calendar" else ("净值日期" if dataset.endswith("nav") else "date" if sina_quote else "日期")
    required = [date_column]
    if dataset in ("unit-nav", "exchange-nav"):
        required.append("单位净值")
        if dataset == "exchange-nav":
            required.append("累计净值")
    elif dataset == "cumulative-nav":
        required.append("累计净值")
    elif dataset in ("etf-daily", "lof-daily"):
        required.extend(quote_fields.values())
    missing = [name for name in required if name not in columns]
    if not records and not columns:
        return {
            "adapter": {"columns": columns, "dtypes": dtypes, "rows": raw_rows},
            "source": {"row_count": 0, "min_date": None, "max_date": None},
            "selected": {"row_count": 0, "min_date": None, "max_date": None},
            "normalization": {"mapping": {}, "units": {}}, "normalized_rows": [],
        }
    if missing:
        raise ProbeError("missing_columns", "来源结果缺少必需列：" + "、".join(missing))
    dated = [(parse_date(row.get(date_column)), row) for row in records]
    if len({date for date, _ in dated}) != len(dated):
        raise ProbeError("duplicate_response", "来源结果包含重复日期")
    selected = sorted(((date, row) for date, row in dated if start <= date <= end), key=lambda item: item[0])
    normalized, mapping, units = [], {}, {}
    for date, row in selected:
        if dataset in ("unit-nav", "cumulative-nav", "exchange-nav"):
            nav_col = "单位净值" if dataset != "cumulative-nav" else "累计净值"
            nav_key = "unit_nav" if dataset != "cumulative-nav" else "cumulative_nav"
            _require(row, [nav_col])
            item = {"date": date.isoformat(), nav_key: decimal_string(row[nav_col], nav_col, nonnegative=True),
                    "published_at": None}
            if item[nav_key] is None:
                raise ProbeError("invalid_response", f"来源 {nav_col} 为空")
            if "日增长率" in row:
                item["return_fraction"] = decimal_string(row["日增长率"], "日增长率", factor=Decimal("0.01"))
            if dataset == "exchange-nav":
                item["cumulative_nav"] = decimal_string(row["累计净值"], "累计净值", nonnegative=True)
            normalized.append(item)
            mapping = {"date": date_column, nav_key: nav_col, "return_fraction": "日增长率（若存在）"}
            units = {nav_key: "unverified_currency_per_share", "return_fraction": "fraction"}
            if dataset == "exchange-nav":
                mapping["cumulative_nav"] = "累计净值"
                units["cumulative_nav"] = "unverified_currency_per_share"
        elif dataset in ("etf-daily", "lof-daily"):
            item = {"date": date.isoformat()}
            for key, source in quote_fields.items():
                item[key] = decimal_string(row[source], source, positive=True)
                if item[key] is None:
                    raise ProbeError("invalid_response", f"来源 {source} 为空")
            if Decimal(item["low"]) > min(Decimal(item["open"]), Decimal(item["close"])) or \
                    Decimal(item["high"]) < max(Decimal(item["open"]), Decimal(item["close"])) or \
                    Decimal(item["low"]) > Decimal(item["high"]):
                raise ProbeError("invalid_response", "来源 OHLC 高低价关系无效")
            volume_col, amount_col = ("volume", "amount") if sina_quote else ("成交量", "成交额")
            quote_units = payload.get("quote_units") or {
                "volume": "shares" if sina_quote else "hands", "amount": "CNY"
            }
            if quote_units.get("volume") not in ("shares", "hands") or quote_units.get("amount") != "CNY":
                raise ProbeError("invalid_response", "来源未声明受支持的行情单位")
            volume_factor = Decimal(1 if quote_units["volume"] == "shares" else 100)
            item["volume_shares"] = decimal_string(row.get(volume_col), volume_col, nonnegative=True,
                                                    factor=volume_factor)
            item["amount_cny"] = decimal_string(row.get(amount_col), amount_col, nonnegative=True)
            item.update({"adjustment": "none", "published_at": None})
            normalized.append(item)
            mapping = {"date": date_column, **quote_fields, "volume_shares": volume_col, "amount_cny": amount_col}
            units = {"prices": "CNY_per_share", "volume_shares": "shares (source shares)" if quote_units["volume"] == "shares" else "shares (source hands x 100)", "amount_cny": "CNY"}
        else:
            normalized.append({"date": date.isoformat(), "calendar_scope": "mainland_exchange_reference"})
            mapping, units = {"date": date_column}, {"date": "calendar_date"}
    source_dates = [date for date, _ in dated]
    selected_dates = [date for date, _ in selected]
    bounds = lambda dates: {"min_date": min(dates).isoformat(), "max_date": max(dates).isoformat()} if dates else {"min_date": None, "max_date": None}
    return {
        "adapter": {"columns": columns, "dtypes": dtypes, "rows": raw_rows},
        "source": {"row_count": len(records), **bounds(source_dates),
                   "missing_value_counts": {column: sum(row[column] in (None, "") for row in records)
                                            for column in columns}},
        "selected": {"row_count": len(normalized), **bounds(selected_dates)},
        "normalization": {"mapping": mapping, "units": units},
        "normalized_rows": normalized,
    }


def _utc_now():
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def make_report(request, payload, elapsed, started_at):
    result = normalize(request["dataset"], payload, request["start_date"], request["end_date"])
    return {
        "status": "success" if result["normalized_rows"] else "empty",
        "artifact_kind": "bounded_adapter_probe", "production_ready": False,
        "request": request, "interface": source_interface(request), "provider": request.get("provider", "eastmoney"),
        "params": adapter_params(request), "python_version": sys.version.split()[0],
        "akshare_version": payload.get("akshare_version") or installed_akshare_version(),
        "started_at": started_at, "finished_at": _utc_now(), "elapsed_seconds": round(elapsed, 3),
        **result, "source_evidence": payload.get("source_evidence"),
        "limitations": report_limitations(request["dataset"], request.get("provider", "eastmoney")), "error": None,
    }


def adapter_params(request):
    dataset, code = request["dataset"], request.get("code")
    if request.get("provider") == "sina" and dataset in QUOTE_DATASETS:
        return {"symbol": market_symbol(code)}
    if request.get("provider") == "tencent":
        return {"code": code, "start_date": request["start_date"], "end_date": request["end_date"],
                "expected_kind": "ETF" if dataset == "etf-daily" else "LOF", "adjust": ""}
    if request.get("provider") == "baostock":
        return {"code": code, "start_date": request["start_date"], "end_date": request["end_date"],
                "expected_kind": "ETF" if dataset == "etf-daily" else "LOF", "adjust": ""}
    if dataset in ("unit-nav", "cumulative-nav"):
        return {"symbol": code, "indicator": "单位净值走势" if dataset == "unit-nav" else "累计净值走势", "period": "成立来"}
    if dataset == "exchange-nav":
        return {"fund": code, "start_date": request["start_date"], "end_date": request["end_date"]}
    if dataset in ("etf-daily", "lof-daily"):
        return {"symbol": code, "period": "daily", "start_date": request["start_date"], "end_date": request["end_date"], "adjust": ""}
    return {}


def report_limitations(dataset, provider="eastmoney"):
    if provider == "sina" and dataset in QUOTE_DATASETS:
        return ["取来源全部历史后本地筛选；实际首尾日期不等于完整上市期保证",
                "新浪与腾讯部分历史 OHLC 存在差异，不可假设逐点一致或静默拼接",
                "成交量按已核对的份额单位原值保留；接口文档的手单位与实际样本不符",
                "历史成交额字段可能缺失，保留为空；不复权口径经分红／拆分样本核对"] + SERIES_LIMITATIONS
    if provider == "tencent":
        return ["按年请求，每次最多 640 条并按本地窗口筛选；覆盖情况见 source_evidence",
                "新浪与腾讯部分历史 OHLC 存在差异，不可假设逐点一致或静默拼接",
                "原始成交量为手、成交额为万元；来源存在舍入，换算结果不代表逐份精确成交量",
                "仅接受不复权 day 数据，不自动改用 qfqday／hfqday",
                "来源不提供可验证的发布时间或历史修订版本"]
    if provider == "baostock":
        return ["免费登录接口，无服务可用性保证；当前只把证券类型 5 作为已验证 ETF",
                "历史日线明确请求不复权口径；成交量为份、成交额为元",
                "实测未覆盖 LOF，因此不进入 LOF 自动回退链",
                "来源不提供可验证的发布时间或历史修订版本"]
    return LIMITATIONS[dataset] + ([] if dataset == "calendar" else SERIES_LIMITATIONS)


def safe_adapter(payload):
    try:
        columns, dtypes, rows, _ = _table(payload)
    except (AttributeError, ProbeError):
        return None
    return {"columns": columns, "dtypes": dtypes, "rows": rows}


def parse_args(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", required=True, choices=DATASETS)
    parser.add_argument("--code")
    parser.add_argument("--provider", choices=("auto", "sina", "tencent", "baostock", "eastmoney"), default="auto")
    parser.add_argument("--start-date", required=True)
    parser.add_argument("--end-date", required=True)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)
    if args.dataset == "calendar" and args.code is not None:
        parser.error("--calendar 不接受 --code")
    if args.dataset != "calendar" and (not args.code or len(args.code) != 6 or not args.code.isascii() or not args.code.isdigit()):
        parser.error("--code 必须是保留前导零的六位 ASCII 数字")
    if args.dataset == "calendar" and args.provider not in ("auto", "sina"):
        parser.error("日历来源为新浪，请使用 --provider auto/sina")
    if args.dataset not in (*QUOTE_DATASETS, "calendar") and args.provider in ("sina", "tencent", "baostock"):
        parser.error("--provider sina/tencent/baostock 只适用于 ETF／LOF 日行情")
    if args.dataset == "lof-daily" and args.provider == "baostock":
        parser.error("BaoStock 当前只用于已验证的 ETF 日线")
    if args.dataset in QUOTE_DATASETS and args.provider != "eastmoney":
        try:
            market_symbol(args.code)
        except ProbeError as exc:
            parser.error(exc.message)
    for name in ("start_date", "end_date"):
        value = getattr(args, name)
        try:
            parsed = dt.datetime.strptime(value, "%Y%m%d").date()
        except ValueError:
            parser.error(f"--{name.replace('_', '-')} 必须是 YYYYMMDD 有效日期")
        if parsed.strftime("%Y%m%d") != value:
            parser.error(f"--{name.replace('_', '-')} 必须是 YYYYMMDD 有效日期")
    if args.start_date > args.end_date:
        parser.error("--start-date 不能晚于 --end-date")
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error("--timeout 必须是有限正数")
    return args


def main(argv=None):
    args = parse_args(argv)
    request = {"dataset": args.dataset, "code": args.code, "start_date": args.start_date, "end_date": args.end_date}
    started_at, clock = _utc_now(), time.monotonic()
    payload = None
    providers = (("sina", "tencent", "baostock", "eastmoney") if args.dataset == "etf-daily" else
                 ("sina", "tencent", "eastmoney")) if args.provider == "auto" and args.dataset in QUOTE_DATASETS else (
        ("sina" if args.dataset == "calendar" else "eastmoney") if args.provider == "auto" else args.provider,)
    attempts = []
    try:
        for index, provider in enumerate(providers):
            request["provider"] = provider
            payload = None
            try:
                payload, elapsed = run_adapter(request, args.timeout)
                report = make_report(request, payload, elapsed, started_at)
                attempts.append({"provider": provider, "status": report["status"], "error": None})
                if report["status"] == "empty" and index + 1 < len(providers):
                    continue
                break
            except ProbeError as exc:
                attempts.append({"provider": provider, "status": "error", "error": {"code": exc.code, "message": exc.message}})
                if index + 1 == len(providers) or exc.code not in (
                    "network_error", "network_timeout", "timeout", "upstream_failure", "truncated_response"
                ):
                    raise
        report.update({"requested_provider": args.provider, "attempts": attempts,
                       "elapsed_seconds": round(time.monotonic() - clock, 3)})
        path = write_report(args.output_dir, report)
        print(path)
        return 0 if report["status"] == "success" else 3
    except ProbeError as exc:
        report = {
            "status": "error", "artifact_kind": "bounded_adapter_probe", "production_ready": False,
            "request": request, "interface": source_interface(request), "params": adapter_params(request),
            "provider": request["provider"], "requested_provider": args.provider, "attempts": attempts,
            "python_version": sys.version.split()[0], "akshare_version": installed_akshare_version(),
            "started_at": started_at, "finished_at": _utc_now(),
            "elapsed_seconds": round(time.monotonic() - clock, 3), "adapter": safe_adapter(payload),
            "source": None, "selected": None, "normalization": None, "normalized_rows": [],
            "limitations": report_limitations(args.dataset, request["provider"]), "error": {"code": exc.code, "message": exc.message},
        }
        try:
            print(write_report(args.output_dir, report))
        except OSError:
            print("错误[output_error]：探针报告无法写入输出目录", file=sys.stderr)
        print(f"错误[{exc.code}]：{exc.message}", file=sys.stderr)
        return 2
    except OSError:
        print("错误[output_error]：探针报告无法写入输出目录", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
