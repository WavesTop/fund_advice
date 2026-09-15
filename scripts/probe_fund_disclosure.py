#!/usr/bin/env python3
"""Bounded probes for fund disclosures, fees, relations and event indexes."""

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
DATASETS = ("holdings", "fee", "dividend-announcements", "dividend-cumulative",
            "dividend-events", "split-events", "fund-profile")
FEE_INDICATORS = (
    "交易状态", "申购与赎回金额", "交易确认日", "运作费用", "认购费率（前端）",
    "认购费率（后端）", "申购费率（前端）", "赎回费率",
)
SOURCE_ERRORS = {
    "missing_dependency", "network_timeout", "network_error", "upstream_failure",
    "invalid_response", "empty_response",
}


def _child_code():
    return r'''
import datetime as dt
from decimal import Decimal
import json, math, sys

def scalar(value):
    if value is None or bool(pd.isna(value)):
        return None
    if hasattr(value, "to_pydatetime"):
        value = value.to_pydatetime()
    if isinstance(value, (dt.datetime, dt.date)):
        return value.isoformat()
    if hasattr(value, "item"):
        try: value = value.item()
        except (TypeError, ValueError): pass
    if isinstance(value, float) and not math.isfinite(value): return None
    if isinstance(value, Decimal): return format(value, "f") if value.is_finite() else None
    if isinstance(value, (str, int, float, bool)): return value
    raise TypeError("unsupported scalar")

try:
    import akshare as ak
    import pandas as pd
    request=json.loads(sys.argv[1]); dataset=request["dataset"]; code=request["code"]
    source_evidence=None
    if dataset == "holdings":
        frame=ak.fund_portfolio_hold_em(symbol=code, date=request["year"])
    elif dataset == "dividend-events":
        frame=ak.fund_fh_em(year=request["year"])
        frame=frame[frame["基金代码"] == code]
    elif dataset == "split-events":
        frame=ak.fund_cf_em(year=request["year"])
        frame=frame[frame["基金代码"] == code]
    elif dataset == "fee":
        frame=ak.fund_fee_em(symbol=code, indicator=request["indicator"])
    elif dataset == "dividend-announcements":
        frame=ak.fund_announcement_dividend_em(symbol=code)
    elif dataset == "dividend-cumulative":
        prefix="sh" if code.startswith("5") else "sz"
        symbol=prefix+code; matches=[]
        for category,kind in (("ETF基金","ETF"),("LOF基金","LOF")):
            catalog=ak.fund_etf_category_sina(symbol=category)
            if "代码" not in catalog.columns or "名称" not in catalog.columns:
                raise ValueError("identity schema")
            for _,item in catalog[catalog["代码"] == symbol].iterrows():
                matches.append({"symbol":symbol,"name":item["名称"],"kind":kind})
        if len(matches) != 1 or not isinstance(matches[0]["name"],str) or not matches[0]["name"].strip():
            raise ValueError("identity mismatch")
        source_evidence={"identity":matches[0],"catalogs":["ETF基金","LOF基金"]}
        frame=ak.fund_etf_dividend_sina(symbol=prefix+code)
    else:
        frame=ak.fund_info_ths(symbol=code)
    print(json.dumps({"columns":[str(c) for c in frame.columns],
                      "dtypes":{str(c):str(t) for c,t in frame.dtypes.items()},
                      "rows":[[scalar(v) for v in row] for row in frame.itertuples(index=False,name=None)],
                      "source_evidence":source_evidence,
                      "akshare_version":getattr(ak,"__version__",None)},ensure_ascii=False,allow_nan=False))
except ImportError:
    print(json.dumps({"error":"missing_dependency"}))
except Exception as exc:
    module=type(exc).__module__.lower(); name=type(exc).__name__
    if name in ("TimeoutError","Timeout","ConnectTimeout","ReadTimeout"): category="network_timeout"
    elif any(x in module for x in ("requests","urllib","httpx","socket")): category="network_error"
    else: category="upstream_failure"
    print(json.dumps({"error":category}))
'''


def run_adapter(request, timeout):
    started = time.monotonic()
    try:
        result = subprocess.run(
            [sys.executable, "-c", _child_code(), json.dumps(request, ensure_ascii=False)],
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise ProbeError("network_timeout", "来源请求超过硬超时限制") from exc
    except (OSError, UnicodeError) as exc:
        raise ProbeError("upstream_failure", "来源探针子进程无法完成") from exc
    if result.returncode != 0:
        raise ProbeError("upstream_failure", "来源接口调用失败")
    try:
        payload = json.loads(result.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError) as exc:
        raise ProbeError("invalid_response", "来源返回无法解析") from exc
    if not isinstance(payload, dict):
        raise ProbeError("invalid_response", "来源返回结构无效")
    if "error" in payload:
        error = payload["error"]
        if not isinstance(error, str) or error not in SOURCE_ERRORS:
            raise ProbeError("invalid_response", "来源返回未知错误")
        raise ProbeError(error, "来源接口调用或解析失败")
    return payload, time.monotonic() - started


def _records(payload):
    columns, rows, dtypes = payload.get("columns"), payload.get("rows"), payload.get("dtypes")
    if (not isinstance(columns, list) or not columns or not all(isinstance(column, str) for column in columns)
            or len(set(columns)) != len(columns)):
        raise ProbeError("invalid_response", "来源列名无效")
    if (not isinstance(rows, list) or not isinstance(dtypes, dict) or set(dtypes) != set(columns)
            or not all(isinstance(value, str) for value in dtypes.values())):
        raise ProbeError("invalid_response", "来源表格结构无效")
    output = []
    for row in rows:
        if not isinstance(row, list) or len(row) != len(columns):
            raise ProbeError("invalid_response", "来源行结构无效")
        output.append(dict(zip(columns, row)))
    return columns, rows, dtypes, output


def _decimal(value, field, factor="1", *, positive=False):
    if value in (None, ""):
        return None
    try:
        number = Decimal(str(value))
        multiplier = Decimal(factor)
    except (InvalidOperation, ValueError) as exc:
        raise ProbeError("invalid_response", f"来源 {field} 不是有效数字") from exc
    if not number.is_finite() or number < 0 or positive and number <= 0:
        raise ProbeError("invalid_response", f"来源 {field} 超出允许范围")
    with localcontext() as context:
        context.prec = max(40, len(number.as_tuple().digits) + len(multiplier.as_tuple().digits) + 2)
        return format(number * multiplier, "f")


def _date(value, field):
    if not isinstance(value, str) or len(value) != 10:
        raise ProbeError("invalid_response", f"来源 {field} 不是日期")
    try:
        parsed = dt.date.fromisoformat(value)
    except ValueError as exc:
        raise ProbeError("invalid_response", f"来源 {field} 不是有效日期") from exc
    if parsed.isoformat() != value:
        raise ProbeError("invalid_response", f"来源 {field} 不是 YYYY-MM-DD 日期")
    return parsed.isoformat()


def _quarter(value):
    import re
    match = re.fullmatch(r"(\d{4})年([1-4])季度股票投资明细", str(value))
    if not match:
        raise ProbeError("invalid_response", "来源季度标签无法解析")
    year, quarter = int(match.group(1)), int(match.group(2))
    month_day = {1: "03-31", 2: "06-30", 3: "09-30", 4: "12-31"}[quarter]
    return f"{year:04d}-{month_day}", quarter


def normalize(request, payload):
    columns, raw_rows, dtypes, records = _records(payload)
    dataset, code = request["dataset"], request["code"]
    normalized, limitations = [], []
    if dataset == "holdings":
        required = {"股票代码", "股票名称", "占净值比例", "持股数", "持仓市值", "季度"}
        if not required.issubset(columns):
            raise ProbeError("invalid_response", "持仓来源缺少必需字段")
        seen, positions = set(), {}
        for row in records:
            period, quarter = _quarter(row["季度"])
            if period[:4] != request["year"]:
                raise ProbeError("invalid_response", "持仓来源返回年份与请求不一致")
            if row["股票代码"] in (None, "") or row["股票名称"] in (None, ""):
                raise ProbeError("invalid_response", "持仓来源证券身份为空")
            stock_code = str(row["股票代码"])
            if not stock_code.isascii() or not stock_code.isalnum():
                raise ProbeError("invalid_response", "持仓来源证券代码无效")
            key = (period, stock_code)
            if key in seen:
                raise ProbeError("invalid_response", "持仓来源同报告期包含重复证券")
            seen.add(key)
            positions[period] = positions.get(period, 0) + 1
            normalized.append({
                "fund_code": code, "report_period_end": period, "quarter": quarter,
                "position": positions[period],
                "security_code": stock_code, "security_namespace": "source_unverified",
                "security_name": str(row["股票名称"]),
                "weight_fraction": _decimal(row["占净值比例"], "占净值比例", "0.01"),
                "weight_denominator": "fund_net_assets", "shares": _decimal(row["持股数"], "持股数", "10000"),
                "market_value_cny": _decimal(row["持仓市值"], "持仓市值", "10000"),
                "scope": "source_table_scope_unverified",
                "published_at": None,
            })
        limitations = ["接口没有公告时间；报告期不能作为公开时间", "来源每期最多返回100只；达到或少于100只都不能自动证明披露完整",
                       "同一年度一次返回多个季度；占净值比例分母为基金净资产，禁止按返回行重归一化", "持股数由万股换算为股，持仓市值由万元换算为元",
                       "适配器输出不含可独立核对的基金身份；调用前须由基金目录验证，空结果不能证明基金或报告不存在"]
    elif dataset == "dividend-announcements":
        required = {"基金代码", "公告标题", "基金名称", "公告日期", "报告ID"}
        if not required.issubset(columns):
            raise ProbeError("invalid_response", "分红公告来源缺少必需字段")
        seen_reports = set()
        for row in records:
            if row["基金代码"] != code or not row["报告ID"]:
                raise ProbeError("invalid_response", "分红公告身份无效")
            if row["报告ID"] in seen_reports:
                raise ProbeError("invalid_response", "分红公告索引包含重复报告")
            seen_reports.add(row["报告ID"])
            normalized.append({"fund_code": code, "title": row["公告标题"], "fund_name": row["基金名称"],
                               "published_on": _date(row["公告日期"], "公告日期"), "report_id": row["报告ID"]})
        limitations = ["该接口是分红配送公告索引，不包含每份金额、登记日、除息日或支付日", "标题可能包含分红、拆分或规则公告，不能把每条索引都当成现金分红事件",
                       "事件规范化必须继续读取并保存对应管理人或交易所公告"]
    elif dataset == "dividend-cumulative":
        if not {"日期", "累计分红"}.issubset(columns):
            raise ProbeError("invalid_response", "累计分红来源缺少必需字段")
        parsed = []
        for row in records:
            day = _date(row["日期"], "日期")
            value = _decimal(row["累计分红"], "累计分红")
            if value is None:
                raise ProbeError("invalid_response", "累计分红为空")
            parsed.append((day, Decimal(value)))
        if len({day for day, _ in parsed}) != len(parsed):
            raise ProbeError("invalid_response", "累计分红包含重复日期")
        previous = None
        for day, cumulative in sorted(parsed):
            if previous is not None and cumulative < previous:
                raise ProbeError("invalid_response", "累计分红序列发生倒退")
            delta = None if previous is None else cumulative - previous
            candidate = format(delta, "f") if delta is not None and delta > 0 else None
            normalized.append({"fund_code": code, "source_date": day,
                               "cumulative_cash_per_unit": format(cumulative, "f"),
                               "candidate_cash_per_unit": candidate})
            previous = cumulative
        limitations = ["来源日期可能是分红或拆分调整日，不标为正式除息日", "首个累计值及非正增量不推导单次金额；正增量候选也只能交叉核对",
                       "来源没有登记日、支付日、币种、公告时间和修订标识"]
    elif dataset == "dividend-events":
        required = {"基金代码", "基金简称", "权益登记日", "除息日期", "分红", "分红发放日"}
        if not required.issubset(columns):
            raise ProbeError("invalid_response", "分红事件来源缺少必需字段")
        seen_events = set()
        for row in records:
            if row["基金代码"] != code:
                raise ProbeError("invalid_response", "分红事件身份不匹配")
            record_date = _date(row["权益登记日"], "权益登记日")
            if record_date[:4] != request["year"]:
                raise ProbeError("invalid_response", "分红事件年份与请求不一致")
            cash = _decimal(row["分红"], "分红", positive=True)
            if cash is None:
                raise ProbeError("invalid_response", "分红事件金额为空")
            ex_date = _date(row["除息日期"], "除息日期")
            payable_date = _date(row["分红发放日"], "分红发放日")
            if not record_date <= ex_date <= payable_date:
                raise ProbeError("invalid_response", "分红事件业务日期顺序无效")
            key = (record_date, ex_date, payable_date)
            if key in seen_events:
                raise ProbeError("invalid_response", "分红来源包含重复事件")
            seen_events.add(key)
            normalized.append({"fund_code": code, "fund_name": row["基金简称"],
                               "record_date": record_date,
                               "ex_date": ex_date, "payable_date": payable_date,
                               "candidate_cash_per_unit_cny": cash,
                               "published_at": None})
        limitations = ["结构化数值和业务日期只作事件发现与交叉核对；每份单位须由正式公告确认",
                       "来源没有公告日期、发布时间、原始宣告单位和修订关系，不能单独作为记账事件"]
    elif dataset == "split-events":
        required = {"基金代码", "基金简称", "拆分折算日", "拆分类型", "拆分折算"}
        if not required.issubset(columns):
            raise ProbeError("invalid_response", "拆分事件来源缺少必需字段")
        seen_events = set()
        for row in records:
            if row["基金代码"] != code or row["拆分类型"] != "份额分拆":
                raise ProbeError("invalid_response", "拆分事件身份或类型不匹配")
            split_date = _date(row["拆分折算日"], "拆分折算日")
            if split_date[:4] != request["year"]:
                raise ProbeError("invalid_response", "拆分事件年份与请求不一致")
            ratio = _decimal(row["拆分折算"], "拆分折算", positive=True)
            if ratio is None:
                raise ProbeError("invalid_response", "拆分事件比例为空")
            if split_date in seen_events:
                raise ProbeError("invalid_response", "拆分来源包含重复事件")
            seen_events.add(split_date)
            normalized.append({"fund_code": code, "fund_name": row["基金简称"],
                               "record_or_split_date": split_date,
                               "event_type": "share_split",
                               "candidate_post_units_per_pre_unit": ratio,
                               "ex_date": None, "published_at": None})
        limitations = ["拆分折算值只作候选方向；必须由安排公告确认每份拆后份额",
                       "拆分折算日不等于拆分除权日；来源没有公告时间、结果确认时间和修订关系"]
    elif dataset == "fee":
        normalized = records
        limitations = ["费率表是当前网页展示，没有有效期或历史版本", "天天基金优惠属于销售渠道价格，不能覆盖基金合同标准费率",
                       "条件文字保留原样；在官方文件核对端点、单位和固定费用前不能执行计费"]
    else:
        if not {"字段", "值"}.issubset(columns):
            raise ProbeError("invalid_response", "基金资料来源缺少必需字段")
        values = {}
        for row in records:
            field = str(row["字段"])
            if field in values:
                raise ProbeError("invalid_response", "基金资料来源包含重复字段")
            values[field] = row["值"]
        if values.get("基金代码") != code:
            raise ProbeError("invalid_response", "基金资料身份不匹配")
        normalized = [{"field": key, "value": value} for key, value in values.items()]
        limitations = ["当前资料可发现费用、类型和管理人，但不提供字段有效期", "基金名称中的‘联接’不能单独证明目标 ETF 代码",
                       "目标关系、最低比例及实际持有比例必须分别引用合同和对应定期报告"]
    result = {"adapter": {"columns": columns, "dtypes": dtypes, "rows": raw_rows},
              "normalized_rows": normalized, "row_count": len(normalized), "limitations": limitations}
    if dataset == "holdings":
        periods = {}
        for row in normalized:
            periods[row["report_period_end"]] = periods.get(row["report_period_end"], 0) + 1
        result["periods"] = [{"report_period_end": period, "row_count": count,
                              "cap_hit": count >= 100, "completeness": "unknown"}
                             for period, count in sorted(periods.items())]
        result["fund_identity_status"] = "unverified_by_holdings_source"
    return result


def parse_args(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", required=True, choices=DATASETS)
    parser.add_argument("--code", required=True)
    parser.add_argument("--year")
    parser.add_argument("--indicator", choices=FEE_INDICATORS)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)
    if len(args.code) != 6 or not args.code.isascii() or not args.code.isdigit():
        parser.error("--code 必须是六位 ASCII 数字")
    if args.dataset in ("holdings", "dividend-events", "split-events") and (
            not args.year or len(args.year) != 4 or not args.year.isascii() or not args.year.isdigit()
            or not 1900 <= int(args.year) <= dt.date.today().year + 1):
        parser.error(f"{args.dataset} 必须提供四位 --year")
    if args.dataset == "fee" and not args.indicator:
        parser.error("fee 必须提供 --indicator")
    if args.dataset not in ("holdings", "dividend-events", "split-events") and args.year:
        parser.error("只有 holdings/dividend-events/split-events 接受 --year")
    if args.dataset != "fee" and args.indicator:
        parser.error("只有 fee 接受 --indicator")
    if args.dataset == "dividend-cumulative" and not args.code.startswith(("5", "15", "16")):
        parser.error("dividend-cumulative 只接受待目录核验的沪深 ETF／LOF 代码范围")
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error("--timeout 必须是有限正数")
    return args


def main(argv=None):
    args = parse_args(argv)
    request = {"dataset": args.dataset, "code": args.code}
    if args.year: request["year"] = args.year
    if args.indicator: request["indicator"] = args.indicator
    started_at = dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")
    clock = time.monotonic()
    payload = None
    try:
        payload, elapsed = run_adapter(request, args.timeout)
        result = normalize(request, payload)
        report = {"status": "success" if result["row_count"] else "empty", "artifact_kind": "bounded_disclosure_probe",
                  "production_ready": False, "request": request, "started_at": started_at,
                  "elapsed_seconds": round(elapsed, 3), "python_version": sys.version.split()[0],
                  "akshare_version": payload.get("akshare_version") or installed_akshare_version(), **result, "error": None}
        if payload.get("source_evidence") is not None:
            report["source_evidence"] = payload["source_evidence"]
        try:
            path = write_report(args.output_dir, report)
        except OSError:
            print("错误[output_error]：探针报告无法写入输出目录", file=sys.stderr)
            return 2
        print(path)
        return 0 if report["status"] == "success" else 3
    except ProbeError as exc:
        report = {"status": "error", "artifact_kind": "bounded_disclosure_probe", "production_ready": False,
                  "request": request, "started_at": started_at, "elapsed_seconds": round(time.monotonic()-clock, 3),
                  "python_version": sys.version.split()[0], "akshare_version": installed_akshare_version(),
                  "adapter": _safe_adapter(payload), "normalized_rows": [], "row_count": 0, "limitations": [],
                  "error": {"code": exc.code, "message": exc.message}}
        try:
            path = write_report(args.output_dir, report)
        except OSError:
            print("错误[output_error]：探针报告无法写入输出目录", file=sys.stderr)
            return 2
        print(path)
        print(f"错误[{exc.code}]：{exc.message}", file=sys.stderr)
        return 2


def _safe_adapter(payload):
    try:
        columns, rows, dtypes, _ = _records(payload)
        return {"columns": columns, "dtypes": dtypes, "rows": rows}
    except (ProbeError, TypeError, AttributeError):
        return None


if __name__ == "__main__":
    raise SystemExit(main())
