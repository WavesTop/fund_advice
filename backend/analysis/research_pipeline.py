"""Deterministic, snapshot-only evidence assessment and passive fund comparison."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from hashlib import sha256
import json
from pathlib import Path

from backend.analysis.fund_selection import compare_passive_funds, _points, blocked_subjects
from backend.analysis.sector_status import analyze_index
from backend.analysis.sector_assessment import context_from_bundle, context_from_index, evaluate_period
from backend.core.config import Settings
from backend.core.errors import AppError
from backend.core.trading_calendar import TradingCalendar, CalendarUnavailable, day, SHANGHAI
from backend.storage.database import connection_scope
from backend.storage.research import canonical, digest, get_snapshot, utc_now

ALGORITHM_VERSION = "snapshot-research-v2"
# Observability requirements, not fitted parameters or investment success thresholds.
RULES = {"version": ALGORITHM_VERSION, "fund_lookback_sessions": 60,
         "valuation_min_samples": 60, "valuation_min_span_days": 365,
         "industry_max_age_days": 150, "valuation_max_age_days": 10}


def implementation_hash() -> str:
    root = Path(__file__).resolve().parents[2]
    paths = ["backend/analysis/research_pipeline.py", "backend/analysis/fund_selection.py",
             "backend/analysis/sector_status.py", "backend/analysis/total_return.py",
             "backend/core/trading_calendar.py", "backend/storage/research.py", "backend/analysis/validation.py",
             "backend/analysis/sector_assessment.py", "backend/storage/sector_fundamentals.py"]
    return sha256(b"".join(path.encode() + b"\0" + (root / path).read_bytes() for path in paths)).hexdigest()


def _numeric(facts: list[dict], subject: str, metric: str, cutoff_day) -> tuple[list[dict], list[str]]:
    rows = [fact for fact in facts if fact["subject_key"] == subject and fact["dataset_kind"] == "numeric"
            and fact["metric"] == metric and fact["scope"] == "index_constituents" and fact["semantic_status"] == "verified"]
    conventions = {(row["source_id"], row["methodology"], row["unit"]) for row in rows}
    if len(conventions) > 1 or len({row["effective_date"] for row in rows}) != len(rows):
        return [], [f"{metric}: 来源、计算口径或日期存在冲突"]
    rows.sort(key=lambda row: row["effective_date"])
    if not rows:
        return [], [f"{metric}: 缺少指数成分范围内已核验资料；行业背景不能替代"]
    if day(rows[-1]["effective_date"]) > cutoff_day:
        return [], [f"{metric}: 业务日期在未来"]
    return rows, []


def assess_subject(manifest: dict, subject: str) -> dict:
    facts = manifest["facts"]
    now = datetime.fromisoformat(manifest["cutoff"].replace("Z", "+00:00")).astimezone(SHANGHAI)
    calendar = TradingCalendar(manifest["calendar"])
    result = {"subject_key": subject, "operating": {}, "valuation": {}, "periods": [],
              "recommendation_status": "unvalidated", "background_revision_ids": [f["revision_id"] for f in facts if f["subject_key"] == subject and f.get("semantic_status") == "background"]}
    operating_gaps = ["存在质量隔离或来源冲突，不能用剩余旧期数掩盖本轮缺口"] if subject in blocked_subjects(manifest) else []
    for metric in ("revenue_yoy", "profit_yoy", "operating_cashflow_yoy"):
        rows, errors = _numeric(facts, subject, metric, now.date())
        if rows and (now.date() - day(rows[-1]["effective_date"])).days > RULES["industry_max_age_days"]:
            errors.append(f"{metric}: 经营资料报告期过旧")
        if rows and rows[-1]["unit"] != "%":
            errors.append(f"{metric}: 单位不是百分点口径")
        if len(rows) < 2:
            errors.append(f"{metric}: 不足两个可知报告期")
        operating_gaps.extend(errors)
        if rows:
            result["operating"][metric] = {"value": rows[-1]["value"], "report_date": rows[-1]["effective_date"],
                "revision_id": rows[-1]["revision_id"], "published_at": rows[-1]["published_at"],
                "change_pp": str(Decimal(rows[-1]["value"]) - Decimal(rows[-2]["value"])) if len(rows) > 1 and not errors else None,
                "qualification": "累计同比增速差，不是单月环比", "gaps": errors,
                "observations": [{"value": row["value"], "report_date": row["effective_date"],
                                  "published_at": row["published_at"], "source_url": row["source_url"],
                                  "raw_asset_id": row["raw_asset_id"]} for row in rows[-2:]]}
    valuation_gaps = []
    # Do not choose whichever of PE or PB happens to give a more favourable percentile.
    rows, errors = _numeric(facts, subject, "pe_ttm", now.date())
    valuation_gaps.extend(errors)
    if rows:
        latest = rows[-1]
        if latest["unit"] != "times" or Decimal(latest["value"]) <= 0:
            valuation_gaps.append("PE口径无效或亏损，不能解释为低估值")
        if (now.date() - day(latest["effective_date"])).days > RULES["valuation_max_age_days"]:
            valuation_gaps.append("估值业务日期过旧")
        comparable = [row for row in rows if row["unit"] == "times" and Decimal(row["value"]) > 0]
        if len(comparable) != len(rows):
            valuation_gaps.append("历史存在非正PE，亏损期处理尚未通过；不静默删除后计算分位")
        span = (day(comparable[-1]["effective_date"]) - day(comparable[0]["effective_date"])).days if comparable else 0
        if len(comparable) < RULES["valuation_min_samples"] or span < RULES["valuation_min_span_days"]:
            valuation_gaps.append("缺少同口径估值历史：至少60个观测且跨度365日；这不是有效性门槛")
        percentile = None
        if not valuation_gaps:
            current = Decimal(latest["value"])
            lower = sum(Decimal(row["value"]) < current for row in comparable)
            tied = sum(Decimal(row["value"]) == current for row in comparable)
            percentile = str((Decimal(lower) + Decimal(tied) / 2) / len(comparable) * 100)
        result["valuation"] = {"metric": "pe_ttm", "value": latest["value"], "as_of": latest["effective_date"],
            "percentile": percentile, "sample_count": len(comparable), "nonpositive_history_count": len(rows) - len(comparable), "span_days": span,
            "input_revision_ids": [row["revision_id"] for row in comparable], "gaps": valuation_gaps,
            "source_url": latest["source_url"], "raw_asset_id": latest["raw_asset_id"],
            "interpretation": "样本内描述分位，不是上涨概率或估值回归保证"}
    else:
        result["valuation"] = {"percentile": None, "gaps": valuation_gaps}
    try:
        end = calendar.latest_completed(now)
        prices = _points(facts, subject, "price")
        analysis = analyze_index([{"date": row["effective_date"], "close": row["value"]} for row in prices], as_of=end, calendar=calendar)
    except (ValueError, CalendarUnavailable) as exc:
        analysis = {"periods": [{"id": key, "status": "insufficient", "reason": str(exc)} for key in ("short", "medium", "long")]}
    result["operating_gaps"] = operating_gaps
    result["quality_blocked"] = subject in blocked_subjects(manifest)
    if subject.startswith("hot_board:"):
        context = context_from_bundle(manifest.get("sector_fundamentals", {}).get(subject), now, calendar=calendar)
    else:
        context = context_from_index(result, now)
    result["evidence_context"] = context
    for period in analysis["periods"]:
        judgment = evaluate_period(period, context)
        result["periods"].append({"id": period["id"], "state": judgment["status"], "reason": judgment["summary"],
            "label": judgment["label"], "price": period, "gaps": judgment["missing"], "assessment": judgment,
            "recommendation_status": "unvalidated"})
    return result


def evaluate_manifest(manifest: dict) -> dict:
    subjects = [key for key in manifest["subjects"] if not key.startswith("fund:")]
    output = {"version": ALGORITHM_VERSION, "rules": RULES, "cutoff": manifest["cutoff"],
              "source_conflicts": manifest["source_conflicts"], "subjects": [], "fund_comparisons": [],
              "investment_validation": "not_validated", "operation_status": "unavailable"}
    for subject in subjects:
        output["subjects"].append(assess_subject(manifest, subject))
        if subject.startswith("tracked_index:"):
            try:
                output["fund_comparisons"].append(compare_passive_funds(manifest, subject, lookback=RULES["fund_lookback_sessions"]))
            except CalendarUnavailable as exc:
                output["fund_comparisons"].append({"benchmark_key": subject, "status": "insufficient", "reason": str(exc), "items": []})
    return output


def run_snapshot(settings: Settings, snapshot_id: str) -> dict:
    snapshot = get_snapshot(settings, snapshot_id)
    output = evaluate_manifest(snapshot["manifest"])
    code_hash = implementation_hash()
    run_id = digest([snapshot_id, code_hash, RULES])
    with connection_scope(settings) as connection:
        connection.execute("INSERT OR IGNORE INTO analysis_run VALUES(?,?,?,?,?,?,?)",
                           (run_id, snapshot_id, ALGORITHM_VERSION, code_hash, utc_now(), digest(output), canonical(output)))
    return get_run(settings, run_id)


def get_run(settings: Settings, run_id: str) -> dict:
    from backend.storage.database import migrate
    migrate(settings)
    with connection_scope(settings) as connection:
        row = connection.execute("SELECT * FROM analysis_run WHERE run_id=?", (run_id,)).fetchone()
    if row is None:
        raise AppError("research_run_not_found", "研究运行不存在", 404)
    result = dict(row)
    result["output"] = json.loads(result.pop("output_json"))
    if digest(result["output"]) != result["output_hash"]:
        raise AppError("research_run_integrity_failed", "研究结果校验失败", 500)
    return result


def replay_run(settings: Settings, run_id: str) -> dict:
    original = get_run(settings, run_id)
    if original["implementation_hash"] != implementation_hash():
        raise AppError("research_implementation_changed", "当前代码版本不同，不能假装按原算法复算", 409)
    snapshot = get_snapshot(settings, original["snapshot_id"])
    recomputed = evaluate_manifest(snapshot["manifest"])
    return {"run_id": run_id, "snapshot_id": original["snapshot_id"],
            "matches": digest(recomputed) == original["output_hash"], "output_hash": digest(recomputed)}
