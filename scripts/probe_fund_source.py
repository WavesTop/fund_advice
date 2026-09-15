#!/usr/bin/env python3
"""Small, deliberately isolated AKShare fund catalogue probe."""

import argparse
import datetime as dt
import importlib.metadata
import json
import math
import secrets
import subprocess
import sys
import time
from pathlib import Path


DEFAULT_SAMPLES = Path("tests/fixtures/data_sources/fund_probe_samples.json")
DEFAULT_OUTPUT = Path("runtime/probes")
MIN_SAMPLES = 16
SOURCE_ERRORS = {
    "missing_dependency": "未找到来源接口所需依赖，请先同步项目环境",
    "network_timeout": "来源网络请求超时",
    "network_error": "无法连接来源，请检查网络、代理或来源可用性",
    "upstream_failure": "来源调用或解析失败，需核验接口返回结构",
}


class ProbeError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


def load_samples(path=DEFAULT_SAMPLES):
    try:
        with Path(path).open(encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as exc:
        raise ProbeError("invalid_samples", "样本清单无法读取或不是有效 JSON") from exc
    if (
        not isinstance(data, dict)
        or type(data.get("schema_version")) is not int
        or data["schema_version"] != 1
        or not isinstance(data.get("samples"), list)
    ):
        raise ProbeError("invalid_samples", "样本清单须使用 schema_version=1 并包含 samples 数组")
    samples = data["samples"]
    if len(samples) < MIN_SAMPLES:
        raise ProbeError("invalid_samples", f"样本清单至少需要 {MIN_SAMPLES} 项")
    seen = set()
    for item in samples:
        if not isinstance(item, dict):
            raise ProbeError("invalid_samples", "样本项必须是对象")
        code = item.get("code")
        if not isinstance(code, str) or len(code) != 6 or not code.isascii() or not code.isdigit():
            raise ProbeError("invalid_samples", "样本代码必须是保留前导零的六位数字")
        if code in seen:
            raise ProbeError("duplicate_samples", "样本代码不能重复")
        seen.add(code)
        if not isinstance(item.get("name"), str) or not item["name"].strip():
            raise ProbeError("invalid_samples", "样本名称不能为空")
        for field in ("tags", "evidence_urls"):
            value = item.get(field)
            if not isinstance(value, list) or not value or any(
                not isinstance(v, str) or not v.strip() for v in value
            ):
                raise ProbeError("invalid_samples", f"样本 {field} 必须是非空字符串数组")
    return data, samples


def _child_code():
    return r'''
import json
try:
    import akshare as ak
    frame = ak.fund_name_em()
    columns = [str(c) for c in frame.columns]
    code_col = "基金代码" if "基金代码" in columns else None
    name_col = "基金简称" if "基金简称" in columns else None
    rows = []
    if code_col and name_col:
        for _, row in frame.iterrows():
            rows.append({
                "code": row[code_col] if isinstance(row[code_col], str) else None,
                "name": row[name_col] if isinstance(row[name_col], str) else None,
                "fund_type": row.get("基金类型") if isinstance(row.get("基金类型"), str) else None,
            })
    print(json.dumps({"columns": columns, "rows": rows, "akshare_version": getattr(ak, "__version__", None)}, ensure_ascii=False))
except ImportError:
    print(json.dumps({"error": "missing_dependency"}))
except Exception as exc:
    module = type(exc).__module__.lower()
    if type(exc).__name__ in ("TimeoutError", "Timeout", "ConnectTimeout", "ReadTimeout"):
        category = "network_timeout"
    else:
        category = "network_error" if any(x in module for x in ("requests", "urllib", "httpx", "socket")) else "upstream_failure"
    print(json.dumps({"error": category}))
'''


def run_catalog(timeout):
    started = time.monotonic()
    try:
        completed = subprocess.run(
            [sys.executable, "-c", _child_code()],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise ProbeError("timeout", "来源请求超过硬超时限制") from exc
    except OSError as exc:
        raise ProbeError("subprocess_error", "来源探针子进程无法启动") from exc
    except UnicodeError as exc:
        raise ProbeError("invalid_response", "来源返回编码无法解析") from exc
    if completed.returncode != 0:
        raise ProbeError("upstream_failure", "来源接口调用失败")
    try:
        result = json.loads(completed.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        raise ProbeError("invalid_response", "来源返回无法解析")
    if not isinstance(result, dict):
        raise ProbeError("invalid_response", "来源返回无法解析")
    if result.get("error"):
        code = result["error"]
        if not isinstance(code, str) or code not in SOURCE_ERRORS:
            raise ProbeError("invalid_response", "来源返回无法识别的错误类别")
        raise ProbeError(code, SOURCE_ERRORS[code])
    return result, time.monotonic() - started


def write_report(output_dir, report):
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = Path(output_dir) / f"{stamp}-{secrets.token_hex(4)}"
    target.mkdir(parents=True, exist_ok=False)
    with (target / "report.json").open("w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
        f.write("\n")
    return target


def installed_akshare_version():
    try:
        return importlib.metadata.version("akshare")
    except importlib.metadata.PackageNotFoundError:
        return None


def build_report(samples, result, elapsed, started_at):
    if not isinstance(result, dict):
        raise ProbeError("invalid_response", "来源返回结构无效")
    columns = result.get("columns") or []
    rows = result.get("rows") or []
    if not isinstance(columns, list) or not isinstance(rows, list):
        raise ProbeError("invalid_response", "来源返回结构无效")
    if columns != [str(c) for c in columns]:
        raise ProbeError("invalid_response", "来源返回列名无效")
    if not columns or "基金代码" not in columns:
        raise ProbeError("missing_columns", "来源结果缺少基金代码列")
    if "基金简称" not in columns:
        raise ProbeError("missing_columns", "来源结果缺少基金名称列")
    if "基金类型" not in columns:
        raise ProbeError("missing_columns", "来源结果缺少基金类型列")
    if not rows:
        raise ProbeError("empty_response", "来源返回空结果")
    actual = {}
    fund_types = {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("code"), str) or not isinstance(row.get("name"), str):
            raise ProbeError("invalid_response", "来源返回行结构无效")
        code = row["code"].strip()
        if len(code) != 6 or not code.isascii() or not code.isdigit():
            raise ProbeError("invalid_response", "来源返回包含格式错误的基金代码")
        if code in actual:
            raise ProbeError("duplicate_response", "来源返回包含重复基金代码")
        actual[code] = row["name"].strip()
        if not actual[code]:
            raise ProbeError("invalid_response", "来源返回包含空基金名称")
        if not isinstance(row.get("fund_type"), str) or not row["fund_type"].strip():
            raise ProbeError("invalid_response", "来源返回包含空基金类型")
        fund_types[code] = row["fund_type"].strip()
    wanted = {item["code"]: item for item in samples}
    matched = [
        {"code": code, "expected_name": item["name"], "actual_name": actual[code],
         "source_fund_type": fund_types[code],
         "name_matches_exactly": item["name"] == actual[code]}
        for code, item in wanted.items() if code in actual
    ]
    missing = [code for code in wanted if code not in actual]
    if not matched:
        raise ProbeError("no_matches", "来源结果未匹配任何样本")
    return {
        "status": "success",
        "interface": "akshare.fund_name_em",
        "artifact_kind": "adapter_output_summary",
        "started_at": started_at,
        "elapsed_seconds": round(elapsed, 3),
        "python_version": sys.version.split()[0],
        "akshare_version": installed_akshare_version(),
        "row_count": len(rows),
        "columns": columns,
        "request_params": {},
        "source_url": "https://fund.eastmoney.com/js/fundcode_search.js",
        "pagination": "来源一次返回当前目录，接口无分页参数；不保证包含历史终止基金",
        "limitations": ["名称、类型仅是目录标签，不证明产品归属、份额关系或场内身份",
                        "来源缺少币种、成立日、终止日、公开时点；代码缺失不能判定基金不存在"],
        "adapter_samples": [{"code": item["code"], "name": actual[item["code"]],
                             "fund_type": fund_types[item["code"]]} for item in matched],
        "matched_samples": matched,
        "missing_sample_codes": missing,
        "error": None,
    }


def main(argv=None):
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--list-samples", action="store_true")
    mode.add_argument("--catalog", action="store_true")
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--samples", type=Path, default=DEFAULT_SAMPLES)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error("--timeout 必须是正数")
    started_at = None
    probe_started = None
    try:
        data, samples = load_samples(args.samples)
        if args.list_samples:
            print(json.dumps(data, ensure_ascii=False, indent=2))
            return 0
        if not args.catalog:
            parser.error("默认不会联网；请显式指定 --catalog")
        started_at = dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")
        probe_started = time.monotonic()
        result, elapsed = run_catalog(args.timeout)
        report = build_report(samples, result, elapsed, started_at)
        path = write_report(args.output_dir, report)
        print(path)
        return 0
    except ProbeError as exc:
        if args.catalog and started_at:
            failure = {
                "status": "error", "interface": "akshare.fund_name_em",
                "artifact_kind": "adapter_output_summary", "started_at": started_at,
                "elapsed_seconds": round(time.monotonic() - probe_started, 3), "python_version": sys.version.split()[0],
                "akshare_version": installed_akshare_version(), "row_count": None, "columns": [],
                "matched_samples": [], "missing_sample_codes": [],
                "error": {"code": exc.code, "message": exc.message},
            }
            try:
                print(write_report(args.output_dir, failure))
            except OSError:
                print("错误[output_error]：探针结果无法写入输出目录", file=sys.stderr)
        print(f"错误[{exc.code}]：{exc.message}", file=sys.stderr)
        return 2
    except OSError:
        print("错误[output_error]：探针结果无法写入输出目录", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
