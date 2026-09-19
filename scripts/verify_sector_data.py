"""Run the production HTTP refresh and export bounded, source-exact diagnostic evidence.

Default: start this checkout's API with a NEW isolated database, then remove it.
--base-url: use an existing local API; without --read-only this refreshes its database.
No source implementation or fetcher is injected by this verifier.
"""
from __future__ import annotations

import argparse
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import re
import signal
import socket
import subprocess
import sys
import tempfile
import time
from urllib.error import HTTPError
from urllib.parse import urlencode, quote, urlsplit
from urllib.request import Request, urlopen
from uuid import uuid4
from zipfile import ZipFile, ZIP_DEFLATED

from scripts.import_sector_heat import COLLECTOR_VERSION
from backend.core.trading_calendar import get_calendar

ROOT = Path(__file__).resolve().parents[1]
PERIODS = {"short": 20, "medium": 60, "long": 120}
MAX_JSON_BYTES = 64 * 1024 * 1024
SENSITIVE = {"token", "access_token", "authorization", "password", "secret", "api_key", "apikey", "cookie"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def clean(value):
    """Avoid exporting local home paths or credentials in error messages/URLs."""
    if isinstance(value, dict):
        return {key: "[redacted]" if key.lower() in SENSITIVE else clean(item) for key, item in value.items()}
    if isinstance(value, list):
        return [clean(item) for item in value]
    if isinstance(value, str):
        value = value.replace(str(Path.home()), "<HOME>")
        return re.sub(r"(?i)(token|access_token|password|api_key|apikey|secret|authorization)=([^&\s]+)",
                      r"\1=[redacted]", value)
    return value


def local_base(value: str) -> str:
    parsed = urlsplit(value)
    if (parsed.scheme not in ("http", "https") or parsed.hostname not in ("localhost", "127.0.0.1", "::1")
            or parsed.username or parsed.password or parsed.query or parsed.fragment):
        raise ValueError("只接受无凭据的本机 HTTP(S) 服务地址；不要将本项目服务暴露到公网")
    return value.rstrip("/")


def request_json(base: str, path: str, *, post: bool = False, timeout: float = 30) -> dict:
    started = time.monotonic()
    result = {"path": path, "method": "POST" if post else "GET", "http_status": None,
              "requested_at": utc_now(), "payload": None, "error": None}
    request = Request(base + path, data=b"{}" if post else None,
                      headers={"Content-Type": "application/json", "Accept": "application/json"})
    try:
        try:
            response = urlopen(request, timeout=timeout)
        except HTTPError as exc:
            response = exc
        with response:
            result["http_status"] = response.status
            body = response.read(MAX_JSON_BYTES + 1)
        if len(body) > MAX_JSON_BYTES:
            raise ValueError("本地API响应超过64MiB，未将截断数据当作完整响应")
        try:
            result["payload"] = json.loads(body, parse_constant=lambda raw: f"[nonfinite:{raw}]")
        except (ValueError, UnicodeError):
            result["response_preview"] = body[:4000].decode("utf-8", "replace")
            raise ValueError("本地API未返回有效JSON")
        if result["http_status"] != 200:
            result["error"] = f"HTTP {result['http_status']}"
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
    result["elapsed_seconds"] = round(time.monotonic() - started, 3)
    return result


def payload(record: dict | None) -> dict:
    value = (record or {}).get("payload")
    return value if isinstance(value, dict) else {}


def worker_result(record: dict | None) -> dict:
    body = payload(record)
    value = body.get("refresh")
    if isinstance(value, dict):
        return value
    error = body.get("error", {})
    details = error.get("details", {}) if isinstance(error, dict) else {}
    value = details.get("refresh", {}) if isinstance(details, dict) else {}
    return value if isinstance(value, dict) else {}


def numeric(value) -> bool:
    if value is None or isinstance(value, bool):
        return False
    try:
        return Decimal(str(value)).is_finite()
    except (InvalidOperation, ValueError):
        return False


def rankable(period: dict) -> bool:
    return (period.get("strength", {}).get("eligible") is True
            and period.get("status") in ("strong", "neutral", "weak")
            and numeric(period.get("return_pct")))


def inspect_evaluation(evaluation: dict, refresh: dict) -> dict:
    """Never infer data completeness from HTTP 200 or the presence of a label."""
    items = [item for item in evaluation.get("items", []) if item.get("universe_type") == "hot_board"]
    cutoff = evaluation.get("price_cutoff")
    matrix, ranks = [], {key: [] for key in PERIODS}
    rank_errors = []
    for item in items:
        periods = {period["id"]: period for period in item.get("periods", []) if period.get("id") in PERIODS}
        fundamental = item.get("fundamentals", {})
        matrix.append({"code": item.get("code"), "name": item.get("name"), "source_id": item.get("source_id"),
                       "history_source_id": item.get("history_source_id"),
                       "history_source_code": item.get("history_source_code"),
                       "expected_as_of": cutoff, "actual_as_of": item.get("as_of"),
                       "observation_count": item.get("observation_count"),
                       "collection_error": item.get("collection_error"),
                       "periods": {key: {"required_observations": window + 1, **periods.get(key, {})}
                                   for key, window in PERIODS.items()},
                       "membership": item.get("membership", {}), "fundamentals": fundamental})
        for key, period in periods.items():
            if rankable(period):
                ranks[key].append({"code": item.get("code"), "return_pct": period["return_pct"],
                                   "rank": period["strength"].get("rank"),
                                   "sample_count": period["strength"].get("sample_count"),
                                   "as_of": period["strength"].get("as_of"),
                                   "observation_start": period.get("observation_start"),
                                   "observation_grid": period.get("observation_grid")})
    for key, entries in ranks.items():
        entries.sort(key=lambda row: (-Decimal(str(row["return_pct"])), str(row["code"])))
        for row in entries:
            if (not isinstance(row["rank"], int) or isinstance(row["rank"], bool)
                    or not 1 <= row["rank"] <= len(entries) or row["sample_count"] != len(entries)
                    or row["as_of"] != cutoff):
                rank_errors.append({"period": key, "code": row["code"], "error": "排名范围、样本量或截止日不匹配"})
        # Displayed returns are rounded: equal displayed values may have different raw ranks.
        for left, right in zip(entries, entries[1:]):
            if (numeric(left["rank"]) and numeric(right["rank"]) and
                    Decimal(str(left["return_pct"])) > Decimal(str(right["return_pct"]))
                    and Decimal(str(left["rank"])) >= Decimal(str(right["rank"]))):
                rank_errors.append({"period": key, "codes": [left["code"], right["code"]], "error": "排名与该周期收益方向不符"})
        grids = {(row["observation_start"], row["observation_grid"]) for row in entries}
        if len(grids) > 1:
            rank_errors.append({"period": key, "error": "同榜有效样本不在同一观察日期网格"})
    universe = evaluation.get("universe", {})
    expected = refresh.get("catalog_count", universe.get("catalog_count"))
    member_ready = lambda item: (item.get("membership", {}).get("status") == "ready"
                                and item["membership"].get("member_count", 0) > 0
                                and item["membership"]["member_count"] == len(item["membership"].get("members", []))
                                and bool(cutoff) and str(item["membership"].get("as_of", "")) >= cutoff)

    def operating_ready(item: dict) -> bool:
        context = item.get("fundamentals", {})
        reports = context.get("operating", {}).get("periods", [])
        required = ("revenue_yoy", "profit_yoy", "operating_cashflow_yoy")
        return (context.get("usable") is True and len(reports) >= 2 and
                all(report.get("metrics", {}).get(metric, {}).get("complete") is True
                    and numeric(report["metrics"][metric].get("current_sum"))
                    and numeric(report["metrics"][metric].get("base_sum"))
                    for report in reports for metric in required))

    def valuation_ready(item: dict) -> bool:
        context = item.get("fundamentals", {})
        value = context.get("valuation", {})
        return (context.get("usable") is True and value.get("status") == "available"
                and value.get("as_of") == cutoff and value.get("total", 0) > 0
                and value.get("covered") == value.get("total"))

    checks = {
        "nonempty_complete_industry_universe": bool(items) and expected == len(items)
            and universe.get("scope") == "verified_industry_all" and not universe.get("last_error"),
        "calendar_ready": evaluation.get("calendar_status") == "ready" and bool(cutoff),
        "native_current_prices_all_periods": bool(items) and all(
            item.get("history_source_id") == "sector_daily.ths"
            and item.get("history_source_code") == "bk_" + str(item.get("code"))
            and item.get("as_of") == cutoff and not item.get("collection_error") for item in items)
            and all(len(entries) == len(items) for entries in ranks.values()),
        "current_membership_complete": bool(items) and all(member_ready(item) for item in items),
        "operating_reports_complete": bool(items) and all(operating_ready(item) for item in items),
        "dated_valuation_complete": bool(items) and all(valuation_ready(item) for item in items),
        "ranking_integrity": bool(items) and not rank_errors and all(ranks.values()),
        "patched_collector_reported": refresh.get("collector_version") == COLLECTOR_VERSION,
    }
    valuation_histories = [item.get("fundamentals", {}).get("valuation", {}) for item in items]
    structural_gaps = {
        "long_valuation_history_complete": bool(items) and all(
            value.get("history_count", 0) >= 60 and value.get("history_span_days", 0) >= 365
            and numeric(value.get("percentile")) for value in valuation_histories),
        "catalyst_collection_complete": bool(items) and all(
            item.get("fundamentals", {}).get("catalysts", {}).get("status") == "available"
            and bool(item["fundamentals"]["catalysts"].get("observations")) for item in items),
    }
    return {"expected_as_of": cutoff, "industry_count": len(items), "catalog_count": expected,
            "core_checks": checks, "core_data_complete": all(checks.values()),
            "research_evidence_checks": structural_gaps,
            "all_missing_data_resolved": all(checks.values()) and all(structural_gaps.values()),
            "eligible_counts": {key: len(entries) for key, entries in ranks.items()},
            "rank_orders": ranks, "ranking_errors": rank_errors, "boards": matrix,
            "note": "周期排名可以真实相同；不以强行改变顺序验收。历史估值与催化未接入/不足不等于HTTP失败。"}



def verify_price_series(evaluation: dict, series: dict[str, dict]) -> dict:
    """Recompute displayed metrics from the application's stored /series endpoint."""
    calendar = get_calendar()
    results = []
    for item in evaluation.get("items", []):
        if item.get("universe_type") != "hot_board":
            continue
        code = str(item.get("code"))
        response = series.get(code, {})
        data = payload(response)
        errors, checks = [], {}
        try:
            if response.get("http_status") != 200:
                raise ValueError("未取得应用中的板块日线")
            if (data.get("code") != code or data.get("source_id") != item.get("source_id")
                    or data.get("history_source_id") != "sector_daily.ths"
                    or data.get("history_source_code") != "bk_" + code):
                raise ValueError("实际日线的原生身份不匹配")
            if evaluation.get("calendar_hash") != calendar.hash:
                raise ValueError("服务端和本地验收脚本交易日历不一致")
            cutoff = date.fromisoformat(evaluation["price_cutoff"])
            rows = data.get("rows", [])
            periods = {period["id"]: period for period in item.get("periods", [])}
            for key, window in PERIODS.items():
                sample = rows[-window - 1:]
                quality = calendar.validate_grid([row["date"] for row in sample], end=cutoff, minimum=window + 1)
                check = {"quality": quality, "metrics_match": False}
                checks[key] = check
                if quality["status"] != "ready":
                    continue
                prices = [Decimal(str(row["close"])) for row in sample]
                if any(not price.is_finite() or price <= 0 for price in prices):
                    raise ValueError("实际日线存在无效收盘价")
                peak, drawdown = prices[0], Decimal(0)
                for price in prices:
                    peak = max(peak, price)
                    drawdown = min(drawdown, price / peak - 1)
                values = {"return_pct": (prices[-1] / prices[0] - 1) * 100,
                          "ma_bias_pct": (prices[-1] / (sum(prices[1:]) / window) - 1) * 100,
                          "max_drawdown_pct": drawdown * 100}
                check["recomputed"] = {name: str(value) for name, value in values.items()}
                period = periods.get(key, {})
                # The API serializes decimal closes as display numbers and rounds metrics to 2dp.
                check["metrics_match"] = (rankable(period) and all(
                    numeric(period.get(name)) and abs(Decimal(str(period[name])) - value) <= Decimal("0.011")
                    for name, value in values.items()))
        except (KeyError, ValueError, TypeError, InvalidOperation, ArithmeticError) as exc:
            errors.append(f"{type(exc).__name__}: {exc}")
        results.append({"code": code, "periods": checks, "errors": errors,
                        "passed": not errors and len(checks) == 3 and all(
                            check["metrics_match"] for check in checks.values())})
    return {"passed": bool(results) and all(row["passed"] for row in results), "items": results,
            "note": "从应用实际日线复算价格、均线和回撤；不以分析接口返回的标签自证。"}


def source_metadata() -> dict:
    files = ["scripts/import_sector_heat.py", "scripts/refresh_market_data.py",
             "backend/integrations/sector_financials.py",
             "backend/integrations/sector_dataset_runner.py", "backend/storage/sector_heat.py",
             "scripts/verify_sector_data.py", "config/exchange-calendar.json", "uv.lock"]
    result = {"python": platform.python_version(), "platform": platform.system(),
              "expected_collector_version": COLLECTOR_VERSION,
              "file_sha256": {name: sha256((ROOT / name).read_bytes()).hexdigest()
                              for name in files if (ROOT / name).is_file()}}
    for name in ("fastapi", "uvicorn", "akshare"):
        try:
            result[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            result[name] = "not_installed"
    try:
        process = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True,
                                 capture_output=True, timeout=5, check=False)
        result["git_head"] = process.stdout.strip() if process.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        result["git_head"] = None
    return result


def export_assets(base: str, run: dict, refresh: dict, *, max_assets: int, max_bytes: int) -> tuple[list[dict], dict]:
    references = {}
    sources = list(run.get("attempts", []))
    for dataset in refresh.get("fundamentals", {}).get("datasets", []):
        sources.extend(dataset.get("sources", []))
    for row in sources:
        asset_id = row.get("raw_asset_id") or row.get("asset_id")
        host = urlsplit(row.get("url", "")).hostname or ""
        if asset_id and re.fullmatch(r"[a-f0-9]{64}", str(asset_id)) and (
            host == "10jqka.com.cn" or host.endswith(".10jqka.com.cn")
        ):
            references.setdefault(asset_id, row.get("url"))
    manifest, files, downloaded = [], {}, 0
    deadline = time.monotonic() + 120
    for position, (asset_id, url) in enumerate(references.items()):
        record = {"asset_id": asset_id, "url": url, "exported": False}
        if position >= max_assets or downloaded >= max_bytes or time.monotonic() >= deadline:
            record["reason"] = "raw_export_budget_exhausted"
            manifest.append(record)
            continue
        try:
            with urlopen(base + "/api/research/assets/" + asset_id, timeout=max(0.1, min(15, deadline - time.monotonic()))) as response:
                content = response.read(min(10 * 1024 * 1024, max_bytes - downloaded) + 1)
                expected_hash = response.headers.get("X-Content-SHA256")
            if len(content) > min(10 * 1024 * 1024, max_bytes - downloaded):
                raise ValueError("原文超过导出上限；未保存截断原文")
            downloaded += len(content)
            actual_hash = sha256(content).hexdigest()
            if actual_hash != expected_hash:
                raise ValueError("原文SHA256与服务端声明不匹配")
            name = f"raw/{actual_hash}.bin"
            files.setdefault(name, content)
            record.update(exported=True, file=name, bytes=len(content), sha256=actual_hash)
        except Exception as exc:
            record["reason"] = f"{type(exc).__name__}: {exc}"
        manifest.append(record)
    return manifest, files


def stop_server(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    if os.name == "posix":
        os.killpg(process.pid, signal.SIGTERM)
    else:
        process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
        process.wait(timeout=10)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", help="已有本机服务；省略时使用新建隔离库，不改动日常数据库")
    parser.add_argument("--read-only", action="store_true", help="仅导出已有服务结果，不发起刷新")
    parser.add_argument("--output", type=Path, help="新ZIP路径；默认在runtime生成带时间戳的文件，不覆盖旧包")
    parser.add_argument("--timeout", type=float, default=900)
    parser.add_argument("--max-assets", type=int, default=400)
    parser.add_argument("--max-raw-mib", type=int, default=64)
    args = parser.parse_args(argv)
    if args.read_only and not args.base_url:
        parser.error("--read-only 必须同时指定 --base-url")
    if not 610 <= args.timeout <= 900 or not 0 <= args.max_assets <= 1000 or not 0 <= args.max_raw_mib <= 256:
        parser.error("timeout须610–900秒，max-assets须0–1000，max-raw-mib须0–256")
    try:
        base = local_base(args.base_url) if args.base_url else None
    except ValueError as exc:
        parser.error(str(exc))
    suffix = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:6]
    output = args.output or ROOT / "runtime" / f"sector-verification-{suffix}.zip"
    if output.exists():
        parser.error("输出文件已存在；请使用新文件名，避免覆盖上一次诊断")
    output.parent.mkdir(parents=True, exist_ok=True)
    documents, binary_files, process = {}, {}, None
    metadata = {"version": "sector-live-verification-v1", "started_at": utc_now(),
                "mode": "existing_http" if base else "isolated_http", "read_only": args.read_only,
                "verifier_injects_fetcher": False, "source": source_metadata()}
    exit_code = 2
    with tempfile.TemporaryDirectory(prefix="fund-sector-verification-") as temporary:
        log_path = Path(temporary) / "server.log"
        try:
            if base is None:
                with socket.socket() as listener:
                    listener.bind(("127.0.0.1", 0))
                    port = listener.getsockname()[1]
                base = f"http://127.0.0.1:{port}"
                env = dict(os.environ, FUND_ADVICE_DATABASE_PATH=str(Path(temporary) / "verification.sqlite3"))
                with log_path.open("wb") as log:
                    process = subprocess.Popen(
                        [sys.executable, "-m", "uvicorn", "backend.api.main:app", "--host", "127.0.0.1", "--port", str(port)],
                        cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT,
                        start_new_session=os.name == "posix")
                ready_deadline = time.monotonic() + 30
                while time.monotonic() < ready_deadline and process.poll() is None:
                    health = request_json(base, "/health", timeout=1)
                    if health["http_status"] == 200:
                        break
                    time.sleep(0.25)
            documents["health.json"] = request_json(base, "/health")
            if documents["health.json"]["http_status"] != 200:
                raise RuntimeError("本地API未就绪；详见health.json和server.log")
            documents["before.json"] = request_json(base, "/api/sectors/opportunities")
            documents["before-status.json"] = request_json(base, "/api/sectors/collection-status")
            if not args.read_only:
                print("正在通过 /api/sectors/refresh 采集真实目录、日线、成分和财报；请等待本轮完成。", flush=True)
                documents["refresh.json"] = request_json(base, "/api/sectors/refresh", post=True, timeout=args.timeout)
            documents["evaluation.json"] = request_json(base, "/api/sectors/opportunities")
            documents["collection-status.json"] = request_json(base, "/api/sectors/collection-status")
            collection_list = request_json(base, "/api/research/collections?limit=20")
            status = payload(documents["collection-status.json"])
            run_id = status.get("run_id")
            run = next((row for row in payload(collection_list).get("items", [])
                        if row.get("run_id") == run_id and row.get("target") == "sectors"), {})
            documents["collection.json"] = {"request_error": collection_list.get("error"), "run": run}
            refresh = worker_result(documents.get("refresh.json"))
            if not refresh and args.read_only:
                terminal = run.get("result") or {}
                refresh = terminal if terminal.get("target") == "sectors" else terminal.get("details", {}).get("refresh", {})
            metadata["run_id"] = run_id
            metadata["new_run_observed"] = bool(run_id) and run_id != payload(documents["before-status.json"]).get("run_id")
            metadata["terminal_run_observed"] = status.get("state") in ("success", "partial", "failed", "timeout", "interrupted")
            evaluation = payload(documents["evaluation.json"])
            analysis = inspect_evaluation(evaluation, refresh)
            series = {}
            series_deadline = time.monotonic() + 120
            for item in evaluation.get("items", []):
                if item.get("universe_type") != "hot_board":
                    continue
                code = str(item.get("code"))
                query = urlencode({"source_id": item.get("source_id", ""), "universe_type": "hot_board"})
                path = f"/api/sectors/{quote(code, safe='')}/series?{query}"
                remaining = series_deadline - time.monotonic()
                series[code] = (request_json(base, path, timeout=min(10, remaining)) if remaining > 0 else
                                {"path": path, "http_status": None, "error": "日线导出120秒预算耗尽", "payload": None})
            documents["series.json"] = series
            documents["series-checks.json"] = verify_price_series(evaluation, series)
            analysis["core_checks"]["stored_series_recomputed"] = documents["series-checks.json"]["passed"]
            analysis["core_data_complete"] = all(analysis["core_checks"].values())
            analysis["all_missing_data_resolved"] &= analysis["core_data_complete"]
            # Old cache/read-only exports must not pass as a freshly verified collection.
            analysis["live_refresh_verified"] = (not args.read_only and metadata["new_run_observed"]
                                                  and metadata["terminal_run_observed"]
                                                  and documents.get("refresh.json", {}).get("http_status") == 200)
            analysis["all_missing_data_resolved"] &= analysis["live_refresh_verified"]
            documents["analysis.json"] = analysis
            manifest, binary_files = export_assets(base, run, refresh, max_assets=args.max_assets,
                                                   max_bytes=args.max_raw_mib * 1024 * 1024)
            documents["raw-manifest.json"] = {"assets": manifest, "asset_count": len(manifest),
                                              "complete": bool(manifest) and all(row["exported"] for row in manifest),
                                              "note": "仅导出本次板块任务的公开原响应；无原文、上限或120秒导出预算触发均不算原文核验完整。"}
            documents["final-status.json"] = request_json(base, "/api/sectors/collection-status")
            analysis["consistent_run_during_export"] = (bool(run_id) and
                payload(documents["final-status.json"]).get("run_id") == run_id
                and payload(documents["final-status.json"]).get("state") == status.get("state"))
            analysis["raw_evidence_export_complete"] = documents["raw-manifest.json"]["complete"]
            analysis["all_missing_data_resolved"] &= (analysis["raw_evidence_export_complete"]
                                                       and analysis["consistent_run_during_export"])
            exit_code = 0 if analysis["all_missing_data_resolved"] else 1
        except KeyboardInterrupt:
            metadata["error"] = "用户中断；导出已有诊断，未完成项不认定成功"
        except Exception as exc:
            metadata["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            if process is not None:
                try:
                    stop_server(process)
                except Exception as exc:
                    metadata["shutdown_error"] = str(exc)
            if log_path.exists():
                binary_files["server.log"] = clean(log_path.read_text(encoding="utf-8", errors="replace")).encode()
            metadata["finished_at"] = utc_now()
            metadata["exit_code"] = exit_code
            documents["metadata.json"] = metadata
            analysis = documents.get("analysis.json", {})
            checks = analysis.get("core_checks", {}) | analysis.get("research_evidence_checks", {})
            summary = "# 板块真实采集诊断\n\n" + f"模式：{metadata['mode']}；任务：{metadata.get('run_id')}\n\n"
            summary += f"期望截止日：{analysis.get('expected_as_of')}；行业数：{analysis.get('industry_count')}\n\n"
            summary += "\n".join(f"- {name}: {'通过' if value else '未通过/未取得'}" for name, value in checks.items())
            summary += "\n\n退出码0：完整验收；1：诊断导出成功但数据仍有缺项；2：运行未完成。\n"
            summary += "\n请发送整个ZIP；不需要发送数据库、持仓文件、.env或密钥。\n"
            if metadata.get("error"):
                summary += "\n运行异常：" + str(metadata["error"]) + "\n"
            binary_files["SUMMARY.md"] = clean(summary).encode()
            with ZipFile(output, "x", compression=ZIP_DEFLATED) as archive:
                for name, value in documents.items():
                    archive.writestr(name, json.dumps(clean(value), ensure_ascii=False, indent=2, allow_nan=False))
                for name, content in binary_files.items():
                    archive.writestr(name, content)
    print(f"诊断包：{output.resolve()}")
    print("退出码1表示还有数据缺口，不表示诊断文件生成失败。")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
