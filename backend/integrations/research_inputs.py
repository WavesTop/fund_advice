"""Archive today's local projections as today's knowledge, never backdate history.

Captured JSON is labelled a local projection, not the upstream response body.
Reviewed operating/profile/TR batches use the separate explicit ingest interface.
"""
from __future__ import annotations
from calendar import monthrange
from datetime import datetime
from decimal import Decimal
import json
from pathlib import Path

from backend.core.config import Settings
from backend.core.trading_calendar import get_calendar, day
from backend.storage.database import connection_scope, migrate
from backend.storage.research import append_facts, canonical, freeze_snapshot, utc_now

ROOT = Path(__file__).resolve().parents[2]
MEDIA_TYPE = "application/vnd.fund-advice.local-projection+json"


def capture_current_inputs(settings: Settings, subjects: list[str] | None = None) -> dict:
    """Freeze a consistent projection read, then retain versioned facts per source."""
    migrate(settings)
    calendar = get_calendar()
    end = calendar.latest_completed(datetime.fromisoformat(utc_now().replace("Z", "+00:00")))
    groups, gaps = [], []
    with connection_scope(settings) as connection:
        connection.execute("BEGIN")
        indexes = [dict(row) for row in connection.execute("SELECT * FROM market_index_projection")]
        for identity in indexes:
            key = f"tracked_index:{identity['source_id']}:{identity['code']}"
            rows = [dict(row) for row in connection.execute("SELECT * FROM market_index_daily WHERE index_code=? ORDER BY date", (identity["code"],))]
            url = "https://www.csindex.com.cn/" if identity["source_id"] == "index_daily.csi_official" else "https://quotes.sina.cn/"
            groups.append((key, identity["source_id"], "price", rows, "verified", url))
        for item in connection.execute("SELECT * FROM sector_heat_member"):
            key = f"hot_board:{item['source_id']}:{item['code']}"
            history_source = item["history_source_id"] or item["source_id"]
            semantics = "verified" if history_source == item["source_id"] and not item["collection_error"] else "unverified"
            if semantics != "verified":
                gaps.append({"subject_key": key, "reason": "跨来源参考行情或本轮采集失败，不作为投资输入核验通过"})
            groups.append((key, history_source, "price", json.loads(item["rows_json"]), semantics,
                           "https://d.10jqka.com.cn/" if history_source == "sector_daily.ths" else "https://push2his.eastmoney.com/"))
        for item in connection.execute("SELECT * FROM fund_timeseries_projection"):
            table = "fund_price_daily" if item["kind"] == "price" else "fund_nav_daily"
            rows = [dict(row) for row in connection.execute(f"SELECT * FROM {table} WHERE code=? ORDER BY date", (item["code"],))]
            groups.append((f"fund:catalog:{item['code']}", item["source_id"], item["kind"], rows, "verified", "https://fund.eastmoney.com/" if "eastmoney" in item["source_id"] else "https://github.com/WavesTop/fund_advice"))
        connection.execute("COMMIT")
    if subjects is None:
        subjects = sorted({group[0] for group in groups})
    if not subjects or len(subjects) > 200:
        raise ValueError("捕获需1至200个明确对象；空库先采集，超过范围须显式选择，不静默截断")
    subjects = sorted(set(subjects))
    batches, new_revisions, observations = [], 0, 0
    for subject, source, kind, rows, semantics, url in groups:
        if subject not in subjects:
            continue
        facts = []
        for row in rows:
            try:
                point_day = day(row["date"])
                if point_day > end:
                    raise ValueError("未结算或未来日期")
                if not calendar.is_session(point_day):
                    raise ValueError("非交易日")
                value = row.get("close") if kind == "price" else row.get("unit_nav")
                if value is None:
                    raise ValueError("没有单位净值；不采用累计净值替代")
                facts.append({"kind": "series", "subject_key": subject, "source_id": source,
                    "effective_date": row["date"], "publication_precision": "unknown", "series_kind": kind,
                    "value": str(value), "amount": str(row["amount"]) if row.get("amount") is not None else None,
                    "currency": "CNY", "basis": "unadjusted_price" if kind == "price" else "unit_nav",
                    "semantic_status": semantics, "parser_version": "current-projection-capture-v1"})
            except ValueError as exc:
                gaps.append({"subject_key": subject, "date": row.get("date"), "reason": str(exc)})
        if facts:
            result = append_facts(settings, facts, source_url=url, raw_body=canonical({"fidelity": "local_projection_not_original_response", "source_id": source, "subject_key": subject, "rows": rows}).encode(), media_type=MEDIA_TYPE)
            batches.append(result["raw_asset_id"])
            new_revisions += result["new_revision_count"]
            observations += result["observation_count"]
    # Legacy evidence is observed NOW, with its original publication metadata retained.
    document = json.loads((ROOT / "config/sector-industry-evidence.json").read_text())
    for code, industry in document["sectors"].items():
        identity = industry["subject"]
        subject = f"{identity['universe_type']}:{identity['source_id']}:{code}"
        if subject not in subjects:
            continue
        for metric in industry["metrics"]:
            for observation in metric["observations"]:
                year, month = map(int, observation["period"].split("-"))
                precision = observation["published_time_precision"]
                published = observation["published_at"][:10] if precision == "day" else observation["published_at"]
                fact = {"kind": "numeric", "subject_key": subject, "source_id": "industry.nbs.legacy-reviewed",
                    "effective_date": f"{year:04}-{month:02}-{monthrange(year, month)[1]:02}",
                    "published_at": published, "publication_precision": precision,
                    "metric": metric["id"], "value": str(Decimal(str(observation["value"]))), "unit": observation["unit"],
                    "scope": industry["scope"], "methodology": "cumulative_industry_yoy_not_index_earnings",
                    "semantic_status": "background", "parser_version": "legacy-evidence-capture-v1"}
                if day(fact["effective_date"]) > end:
                    gaps.append({"subject_key": subject, "reason": "行业旧配置的业务日期晚于本轮截止"})
                    continue
                result = append_facts(settings, [fact], source_url=observation["source_url"],
                                      raw_body=canonical({"fidelity": "legacy_reviewed_configuration_not_original_release", "industry": industry}).encode(), media_type=MEDIA_TYPE)
                new_revisions += result["new_revision_count"]
                observations += 1
                batches.append(result["raw_asset_id"])
    valuations = json.loads((ROOT / "config/sector-valuation-evidence.json").read_text())
    for code, valuation in valuations["indexes"].items():
        subject = f"{valuation['subject']['universe_type']}:{valuation['subject']['source_id']}:{code}"
        if subject not in subjects:
            continue
        gaps.append({"subject_key": subject, "reason": "旧估值缺业务日期或官方完整计算口径，未晋升为已核验PE历史"})
        # Preserve the original observed document even though it cannot enter ranking.
        if valuation.get("as_of") and day(valuation["as_of"]) <= end:
            for metric in valuation["metrics"]:
                fact = {"kind": "numeric", "subject_key": subject, "source_id": "valuation.legacy-observation",
                    "effective_date": valuation["as_of"], "metric": "source_field_" + metric["source_field"],
                    "value": str(metric["value"]), "unit": metric["unit"], "scope": "index_constituents",
                    "methodology": metric["semantic_status"], "semantic_status": "unverified"}
                result = append_facts(settings, [fact], source_url=valuation["source_url"], raw_body=canonical(valuation).encode(), media_type=MEDIA_TYPE)
                new_revisions += result["new_revision_count"]
                observations += 1
                batches.append(result["raw_asset_id"])
    snapshot = freeze_snapshot(settings, subjects, purpose="current-research")
    return {"snapshot_id": snapshot["snapshot_id"], "subjects": subjects,
            "new_revision_count": new_revisions, "observation_count": observations,
            "raw_asset_ids": sorted(set(batches)), "gaps": gaps,
            "history_mode": "system_as_of_only", "fidelity": "local_projection_not_original_response"}
