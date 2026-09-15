#!/usr/bin/env python3
"""Apply reviewed changes with exact anchors; removed after verification."""
from __future__ import annotations
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
changes: dict[str, str] = {}


def read(path: str) -> str:
    return changes[path] if path in changes else (ROOT / path).read_text(encoding="utf-8")


def replace(path: str, old: str, new: str, count: int = 1) -> None:
    text = read(path)
    if text.count(old) != count:
        raise RuntimeError(f"Patch anchor changed in {path}: expected {count}, found {text.count(old)}")
    changes[path] = text.replace(old, new)


def new_file(path: str, content: str) -> None:
    if (ROOT / path).exists():
        raise RuntimeError(f"Refusing to overwrite new file: {path}")
    changes[path] = content


def record_results() -> None:
    path = ROOT / "docs/investment-analysis-plan.md"
    text = path.read_text(encoding="utf-8")
    rows = [("锁定依赖安装", "INSTALL_RESULT"), ("修改前端文件的 Prettier", "FORMAT_RESULT"),
            ("后端全量 unittest", "BACKEND_RESULT"), ("前端 TypeScript／构建", "BUILD_RESULT"), ("前端 Vitest", "FRONTEND_RESULT")]
    report = "\n".join(f"| {label} | {os.environ.get(key, 'not_run')} |" for label, key in rows)
    text = text.replace("<!-- MARKET_VERIFICATION_RESULTS -->", report)
    failed = []
    for key, logfile in (("BACKEND_RESULT", "/tmp/market-backend.log"), ("BUILD_RESULT", "/tmp/market-build.log"), ("FRONTEND_RESULT", "/tmp/market-frontend.log")):
        file = Path(logfile)
        if os.environ.get(key) != "success" and file.exists():
            failed.append(f"\n{key} 失败摘要（待修正）：\n```text\n" + "\n".join(file.read_text(errors="replace").splitlines()[-60:]) + "\n```\n")
    text = text.replace("<!-- MARKET_VERIFICATION_ERRORS -->", "".join(failed))
    path.write_text(text, encoding="utf-8")
    (ROOT / "scripts/apply_market_consistency_patch.py").unlink(missing_ok=True)


if "--record-results" in sys.argv:
    record_results()
    raise SystemExit(0)

# Snapshot dimensions belong to the selected universe, not an earlier top-100 run.
replace("backend/storage/sector_heat.py", '    if not members or len(members) > 100 or len({item["code"] for item in members}) != len(members):', '''    limit = 20000 if universe_scope == "verified_industry_all" else 100
    if (universe_scope not in ("verified_industry_all", "hot_board_top100")
            or not isinstance(requested_count, int) or isinstance(requested_count, bool)
            or not 0 < requested_count <= limit
            or not members or len(members) > limit
            or len({item["code"] for item in members}) != len(members)):''')
replace("backend/storage/sector_heat.py", '                   catalog_count=excluded.catalog_count,collected_count=excluded.collected_count,', '                   requested_count=excluded.requested_count,\n                   catalog_count=excluded.catalog_count,collected_count=excluded.collected_count,')
replace("backend/storage/sector_heat.py", '        item.update(universe_type="hot_board", funds=[], use_scope="market_context_only",', '''        item["history_relation"] = ("proxy_not_equivalent" if item.get("history_source_id") == "sector_daily.ths"
                                    else "native_source")
        item.update(universe_type="hot_board", funds=[], use_scope="market_context_only",''')
replace("scripts/import_sector_heat.py", '    if progress:\n        progress(f"完整目录', '''    if not members:
        message = "本次未取得可核对的行业行情身份，保留旧快照；不视为空行业池。"
        record_sector_heat_failure(settings, attempted_at=attempted_at, error=message)
        raise RuntimeError(message)
    if progress:
        progress(f"完整目录''')
replace("scripts/import_sector_heat.py", '            if industry_only or not rows or fetched[-1]["date"] >= rows[-1]["date"]:', '''            if rows and fetched[-1]["date"] < rows[-1]["date"]:
                raise ValueError("返回行情早于已保存行情，拒绝回退当前序列")
            if not rows or fetched[-1]["date"] >= rows[-1]["date"]:''')

# Validate all numeric rows before the existing atomic replacement.
replace("backend/storage/timeseries.py", '        result.append(item)\n    if not result:', '''        for field in fields:
            value = item[field]
            if value is not None:
                number = Decimal(value)
                if number < 0 or (field not in ("volume", "amount") and number == 0):
                    raise ValueError(f"{field}超出允许范围")
        if kind == "price":
            opening, high, low, close = (Decimal(str(item[field])) for field in ("open", "high", "low", "close"))
            if not low <= min(opening, close) <= max(opening, close) <= high:
                raise ValueError("OHLC大小关系无效")
        result.append(item)
    if not result:''')
replace("backend/storage/timeseries.py", 'def get_timeseries(settings: Settings, code: str) -> dict[str, object]:', 'def get_timeseries(settings: Settings, code: str, kind: str | None = None) -> dict[str, object]:')
replace("backend/storage/timeseries.py", '''    get_fund(settings, code)
    with connection_scope(settings) as connection:
        projection = connection.execute(
            "SELECT kind, source_id, policy_version, updated_at FROM fund_timeseries_projection WHERE code = ? ORDER BY CASE kind WHEN 'price' THEN 0 ELSE 1 END LIMIT 1", (code,)
        ).fetchone()''', '''    get_fund(settings, code)
    if kind not in (None, "price", "nav"):
        raise AppError("invalid_series_kind", "序列类型必须为price或nav", 422)
    with connection_scope(settings) as connection:
        connection.execute("BEGIN")
        projection = connection.execute(
            "SELECT kind, source_id, policy_version, updated_at FROM fund_timeseries_projection WHERE code = ? AND (? IS NULL OR kind = ?) ORDER BY CASE kind WHEN 'price' THEN 0 ELSE 1 END LIMIT 1", (code, kind, kind)
        ).fetchone()''')

new_file("backend/storage/migrations/011_relation_verification.sql", '''-- Verification history, not inferred business-effective dates. Existing evidence is retained.
CREATE TABLE fund_relation_verification (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fund_code TEXT NOT NULL REFERENCES fund_catalog_projection(code),
    checked_at TEXT NOT NULL,
    outcome TEXT NOT NULL CHECK (outcome IN ('verified', 'unresolved', 'failed')),
    index_code TEXT,
    error TEXT,
    CHECK ((outcome = 'verified' AND index_code IS NOT NULL) OR outcome <> 'verified')
);
CREATE INDEX idx_relation_verification_latest ON fund_relation_verification(fund_code, id DESC);
''')
new_file("backend/core/relation_policy.py", '''"""Current relationship provenance checks; these are not investment approval."""
from __future__ import annotations
from collections.abc import Mapping
from datetime import datetime
from urllib.parse import urlsplit


def timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo is not None else None
    except ValueError:
        return None


def public_source(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        url = urlsplit(value)
        return (url.scheme in ("http", "https") and bool(url.hostname)
                and not url.username and not url.password)
    except ValueError:
        return False


def relation_problems(fund: Mapping[str, object], *, generated_at: str) -> list[str]:
    now = timestamp(generated_at)
    if now is None:
        raise ValueError("generated_at must include a timezone")
    problems: list[str] = []
    if fund.get("relation_type") != "tracked_index":
        problems.append("未提供明确的跟踪指数关系。")
    if not fund.get("relation_source_id") or not public_source(fund.get("evidence_url")):
        problems.append("缺少可核对的基金—指数关系来源。")
    verified = timestamp(fund.get("verified_at"))
    if verified is None:
        problems.append("关系核验时间缺失或无时区。")
    elif verified > now:
        problems.append("关系核验时间晚于本次读取时间。")
    if fund.get("relation_status") not in (None, "linked"):
        problems.append(str(fund.get("relation_reason") or "当前关系未通过核验。"))
    return problems
''')

p = "backend/analysis/research_view.py"
text = read(p)
start, end = text.index('def _timestamp('), text.index('def fund_associations(')
text = text[:start] + text[end:]
text = text.replace('from datetime import datetime\nfrom urllib.parse import urlsplit\n', 'from backend.core.relation_policy import public_source as _public_source, timestamp as _timestamp, relation_problems\n')
start, end = text.index('        verified_at = _timestamp('), text.index('        result.append({')
text = text[:start] + '        problems = relation_problems(fund, generated_at=generated_at)\n' + text[end:]
changes[p] = text

p = "backend/storage/related_market.py"
replace(p, 'from backend.storage.timeseries import _clean_rows', 'from backend.storage.timeseries import _clean_rows, get_fund\nfrom backend.core.relation_policy import relation_problems, timestamp')
replace(p, '    normalized = _clean_rows("price", rows)\n    migrate(settings)\n    now = _now()', '''    normalized = _clean_rows("price", rows)
    now = _now()
    problems = relation_problems({"relation_type": "tracked_index", "relation_source_id": relation_source_id,
                                  "evidence_url": evidence_url, "verified_at": now}, generated_at=now)
    if problems or not all(isinstance(value, str) and value.strip() for value in (index_code, index_name, source_id)):
        raise ValueError("关联指数身份或来源无效：" + "；".join(problems))
    get_fund(settings, fund_code)
    migrate(settings)''')
replace(p, '            connection.execute("COMMIT")', '''            connection.execute(
                "INSERT INTO fund_relation_verification(fund_code,checked_at,outcome,index_code) VALUES (?,?,'verified',?)",
                (fund_code, now, index_code),
            )
            connection.execute("COMMIT")''')
text = read(p)
text = text[:text.index('def get_related_market(')] + '''def record_relation_check(settings: Settings, fund_code: str, *, outcome: str, error: str) -> None:
    if outcome not in ("unresolved", "failed"):
        raise ValueError("关系检查状态无效")
    get_fund(settings, fund_code)
    with connection_scope(settings) as connection:
        connection.execute(
            "INSERT INTO fund_relation_verification(fund_code,checked_at,outcome,error) VALUES (?,?,?,?)",
            (fund_code, _now(), outcome, error[:1000]),
        )


def current_associations(connection, *, generated_at: str) -> list[dict[str, object]]:
    rows = [dict(row) for row in connection.execute(
        """SELECT f.code,f.name,r.index_code,r.relation_type,r.source_id AS relation_source_id,
                  r.evidence_url,r.verified_at,i.name AS index_name,i.source_id AS market_source_id,
                  i.updated_at AS market_updated_at,v.outcome,v.index_code AS checked_index_code,
                  v.checked_at,v.error AS check_error
           FROM fund_market_relation r JOIN fund_catalog_projection f ON f.code=r.fund_code
           JOIN market_index_projection i ON i.code=r.index_code
           LEFT JOIN fund_relation_verification v ON v.id=(
               SELECT MAX(id) FROM fund_relation_verification WHERE fund_code=r.fund_code)
           ORDER BY f.code,r.index_code,r.relation_type""")]
    now = timestamp(generated_at)
    if now is None:
        raise ValueError("generated_at must include a timezone")
    for row in rows:
        reason = ""
        checked = timestamp(row["checked_at"])
        if row["outcome"] is not None and (checked is None or checked > now):
            reason = "最近关系检查时间无效或晚于本次读取，暂不开放。"
        elif row["outcome"] in ("unresolved", "failed"):
            reason = "最近一次关系核验未完成；保留旧证据，不作为当前已核验关联。"
        elif row["outcome"] == "verified" and row["checked_index_code"] != row["index_code"]:
            row["relation_status"] = "superseded"
            reason = "后续核验已记录其他跟踪指数；旧证据保留，不回填业务生效日。"
        elif row["outcome"] is None and sum(
                other["code"] == row["code"] and other["relation_type"] == "tracked_index" for other in rows) > 1:
            reason = "存在多条历史跟踪关系且缺少本次核验，不能默认选择其中一条。"
        if reason:
            row.setdefault("relation_status", "withheld")
            row["relation_reason"] = reason
        problems = relation_problems(row, generated_at=generated_at)
        if problems:
            row.setdefault("relation_status", "withheld")
        else:
            row["relation_status"] = "linked"
        row["limitations"] = problems
        row["relation_reason"] = "；".join(problems)
        row["effective_from"] = row["effective_to"] = None
    return rows


def get_related_markets(settings: Settings, fund_code: str) -> list[dict[str, object]]:
    get_fund(settings, fund_code)
    with connection_scope(settings) as connection:
        connection.execute("BEGIN")
        relations = [row for row in current_associations(connection, generated_at=_now()) if row["code"] == fund_code]
        result = []
        for relation in relations:
            rows = []
            if relation["relation_status"] == "linked":
                rows = [dict(row) for row in connection.execute(
                    "SELECT date,open,high,low,close,volume,amount FROM market_index_daily WHERE index_code=? ORDER BY date",
                    (relation["index_code"],),
                )]
            result.append({**relation, "code": relation["index_code"], "name": relation["index_name"],
                           "source_id": relation["market_source_id"], "updated_at": relation["market_updated_at"],
                           "kind": "index", "rows": rows})
        return result


def get_related_market(settings: Settings, fund_code: str) -> dict[str, object] | None:
    linked = [row for row in get_related_markets(settings, fund_code) if row["relation_status"] == "linked"]
    return linked[0] if len(linked) == 1 else None
'''
changes[p] = text

p = "scripts/import_related_market.py"
replace(p, 'from backend.storage.related_market import import_related_index', 'from backend.storage.related_market import import_related_index, record_relation_check\nfrom backend.storage.timeseries import get_fund')
replace(p, '''    relation = resolve_related_relation(fund_code, timeout, opener, catalogue_cache)
    if relation is None:
        return None
    rows, source = _fetch(relation, timeout, opener)
    import_related_index(settings, fund_code=fund_code, index_code=relation["index_code"], index_name=relation["index_name"], rows=rows,
                         source_id=source, relation_source_id=relation["relation_source_id"], evidence_url=relation["evidence_url"])''', '''    get_fund(settings, fund_code)
    try:
        relation = resolve_related_relation(fund_code, timeout, opener, catalogue_cache)
        if relation is None:
            record_relation_check(settings, fund_code, outcome="unresolved", error="未取得唯一且可核对的跟踪标的，旧关系待核验。")
            return None
        rows, source = _fetch(relation, timeout, opener)
        import_related_index(settings, fund_code=fund_code, index_code=relation["index_code"], index_name=relation["index_name"], rows=rows,
                             source_id=source, relation_source_id=relation["relation_source_id"], evidence_url=relation["evidence_url"])
    except Exception as exc:
        record_relation_check(settings, fund_code, outcome="failed", error=f"{type(exc).__name__}: {exc}")
        raise''')

# Compare each universe at its own price observation date.
p = "backend/analysis/sector_strength.py"
replace(p, 'from collections import Counter', 'from collections import Counter\nfrom collections.abc import Mapping')
replace(p, 'def attach_strength(items: list[dict], ranking_as_of: str | None) -> None:', 'def attach_strength(items: list[dict], ranking_as_of: str | Mapping[str, str | None] | None) -> None:')
replace(p, '            group = [item for item in items if item["universe_type"] == universe]', '            comparison_day = ranking_as_of.get(universe) if isinstance(ranking_as_of, Mapping) else ranking_as_of\n            group = [item for item in items if item["universe_type"] == universe]')
replace(p, 'elif not ranking_as_of or item["as_of"] != ranking_as_of:', 'elif not comparison_day or item["as_of"] != comparison_day:')
replace(p, '"as_of": ranking_as_of, "eligible": not reason', '"as_of": comparison_day, "eligible": not reason')
replace(p, '            "eligible_count": eligible_count, "candidate_count": len(selected), "items": selected,', '''            "eligible_count": eligible_count, "candidate_count": len(selected), "items": selected,
            "comparison_as_of": next((period["strength"]["as_of"] for item in items
                                      if item.get("universe_type") == "hot_board"
                                      for period in item["periods"] if period["id"] == period_id
                                      and period.get("strength", {}).get("eligible")), None),''')
p = "backend/analysis/sector_status.py"
replace(p, 'from backend.storage.database import connection_scope', 'from backend.storage.database import connection_scope\nfrom backend.storage.related_market import current_associations')
replace(p, '        items = []\n        for index in indexes:', '        associations = current_associations(connection, generated_at=now.isoformat())\n        items = []\n        for index in indexes:')
text = read(p)
start, end = text.index('            funds = connection.execute('), text.index('            item = {**dict(index)')
text = text[:start] + '            funds = [fund for fund in associations if fund["index_code"] == index["code"]]\n' + text[end:]
text = text.replace('        boards.append({**board, **analyze_index(rows, as_of=now.date())})', '''        analyzed = analyze_index(rows, as_of=now.date())
        if board.get("collection_error"):
            for period in analyzed["periods"]:
                period.update(status="stale", label="行情待更新", reason="本次采集失败，旧行情仅供核对，不参与当前判断。")
        boards.append({**board, **analyzed})''')
start, end = text.index('    hot_dates = '), text.index('    advantages = build_advantage_summary(items)')
text = text[:start] + '''    comparison_dates = {}
    for universe in ("hot_board", "tracked_index"):
        days = [item["as_of"] for item in items if item["universe_type"] == universe
                and item.get("as_of") and not item.get("collection_error")
                and any(period["status"] in ("strong", "neutral", "weak") for period in item["periods"])]
        counts = Counter(days)
        comparison_dates[universe] = max(counts, key=lambda day: (counts[day], day)) if counts else None
    attach_strength(items, comparison_dates)
''' + text[end:]
text = text.replace('            "universe": heat["universe"], "advantages": advantages,', '            "universe": heat["universe"], "advantages": advantages,\n            "comparison_as_of": comparison_dates, "turnover_as_of": heat["universe"].get("ranking_as_of"),')
changes[p] = text

new_file("backend/storage/market_series.py", '''"""Source-exact current series; a cross-provider proxy is not an equivalent index."""
from __future__ import annotations
from backend.core.config import Settings
from backend.core.errors import AppError
from backend.storage.database import connection_scope, migrate
from backend.storage.timeseries import _date, _clean_rows
import json


def market_series(settings: Settings, code: str, *, source_id: str, universe_type: str,
                  start: str | None = None, end: str | None = None) -> dict[str, object]:
    try:
        if start is not None:
            start = _date(start)
        if end is not None:
            end = _date(end)
        if start and end and start > end:
            raise ValueError("起始日期晚于截止日期")
    except ValueError as exc:
        raise AppError("invalid_series_range", str(exc), 422) from exc
    migrate(settings)
    with connection_scope(settings) as connection:
        connection.execute("BEGIN")
        if universe_type == "hot_board":
            identity = connection.execute("SELECT * FROM sector_heat_member WHERE code=? AND source_id=?", (code, source_id)).fetchone()
            if identity is None:
                raise AppError("sector_not_found", "未找到该来源下的板块", 404)
            item = dict(identity)
            rows = json.loads(item["rows_json"])
            history_source = item.get("history_source_id") or source_id
            history_code = item.get("history_source_code") or code
            error = item.get("collection_error")
        elif universe_type == "tracked_index":
            identity = connection.execute("SELECT * FROM market_index_projection WHERE code=? AND source_id=?", (code, source_id)).fetchone()
            if identity is None:
                raise AppError("sector_not_found", "未找到该来源下的指数", 404)
            item = dict(identity)
            rows = [dict(row) for row in connection.execute(
                "SELECT date,open,high,low,close,volume,amount FROM market_index_daily WHERE index_code=? ORDER BY date", (code,))]
            history_source, history_code, error = source_id, code, None
        else:
            raise AppError("invalid_universe", "研究范围无效", 422)
        try:
            normalized = _clean_rows("price", rows) if rows else []
        except ValueError as exc:
            raise AppError("market_series_invalid", "已存储行情存在无效值，暂停绘图", 422, {"reason": str(exc)}) from exc
        selected = [row for row in normalized if (not start or row["date"] >= start) and (not end or row["date"] <= end)]
        proxy = history_source != source_id
        return {"code": code, "name": item["name"], "source_id": source_id, "universe_type": universe_type,
                "history_source_id": history_source, "history_source_code": history_code,
                "history_relation": "proxy_not_equivalent" if proxy else "native_source",
                "updated_at": item["updated_at"], "collection_error": error,
                "as_of": normalized[-1]["date"] if normalized else None, "kind": "price", "rows": selected,
                "limitations": ["跨源同名仅作参考行情，不认定成分、权重或编制方法等价。"] if proxy else []}
''')

p = "backend/api/main.py"
replace(p, 'from threading import Lock', 'from threading import Lock\nfrom typing import Literal')
replace(p, 'from backend.storage.related_market import get_related_market', 'from backend.storage.related_market import get_related_markets\nfrom backend.storage.market_series import market_series')
text = read(p)
start, end = text.index('    @app.get("/api/funds/{code}")'), text.index('    @app.post("/api/funds/{code}/refresh")')
text = text[:start] + '''    def detail(code: str) -> dict[str, object]:
        identity = get_fund(resolved, code)
        options = [get_timeseries(resolved, code, kind) for kind in ("price", "nav")]
        options = [series for series in options if series["kind"] is not None]
        relations = get_related_markets(resolved, code)
        linked = [relation for relation in relations if relation["relation_status"] == "linked"]
        return {"fund": identity, "series": options[0] if options else get_timeseries(resolved, code),
                "series_options": options, "related_markets": relations,
                "related_market": linked[0] if len(linked) == 1 else None}

    @app.get("/api/funds/{code}")
    def fund(code: str) -> dict[str, object]:
        return detail(code)

    @app.get("/api/funds/{code}/series")
    def fund_series(code: str, kind: Literal["price", "nav"] | None = None) -> dict[str, object]:
        return get_timeseries(resolved, code, kind)

    @app.get("/api/sectors/{code}/series")
    def sector_series(code: str, source_id: str, universe_type: Literal["hot_board", "tracked_index"],
                      start: str | None = None, end: str | None = None) -> dict[str, object]:
        return market_series(resolved, code, source_id=source_id, universe_type=universe_type, start=start, end=end)

''' + text[end:]
start, end = text.index('            try:\n                refresh_fund_timeseries'), text.index('        finally:\n            lock.release()')
text = text[:start] + '''            stages = {}
            for name, refresh in (("fund_series", refresh_fund_timeseries), ("related_market", refresh_related_market)):
                try:
                    result = refresh(resolved, code)
                    stages[name] = {"status": "updated" if result is not None else "unresolved",
                                    "message": "更新成功" if result is not None else "未取得唯一的跟踪关系，旧关系待核验。"}
                except Exception as exc:
                    stages[name] = {"status": "failed", "message": str(exc)[:1000] or type(exc).__name__}
            updated = sum(stage["status"] == "updated" for stage in stages.values())
            refresh_state = {"status": "success" if updated == 2 else "partial" if updated else "failed", "stages": stages}
            if not updated:
                raise AppError("fund_refresh_failed", "本次更新未取得可发布数据，旧资料已保留；关系状态以最近核验为准", 502, refresh_state)
            return {**detail(code), "refresh": refresh_state}
''' + text[end:]
text = text.replace('        lock = _refresh_lock(code)', '        get_fund(resolved, code)\n        lock = _refresh_lock(code)')
changes[p] = text

# Real-page integration: preserve demo routes and existing interfaces.
p = "frontend/src/features/market/index.tsx"
replace(p, "import { researchHref, safeReturnPath } from '../advice/research-model';", "import { researchHref, safeReturnPath, sourceHref } from '../advice/research-model';\nimport { commonDates, alignRows, rangeDates } from './series-alignment';")
replace(p, '  related_market?: RelatedMarket | null;\n};', '''  related_market?: RelatedMarket | null;
  related_markets?: RelatedMarket[];
  series_options?: RealFundSeriesResponse[];
  refresh?: { status: 'success' | 'partial' | 'failed'; stages: Record<string, { status: string; message: string }> };
};''')
replace(p, "  kind: 'index';\n  source_id: string;\n  rows: RealPriceRow[];", '''  kind: 'index';
  source_id: string;
  relation_status?: 'linked' | 'withheld' | 'superseded';
  relation_source_id?: string;
  verified_at?: string;
  evidence_url?: string;
  relation_reason?: string;
  updated_at?: string;
  rows: RealPriceRow[];''')
replace(p, 'function RealFundSeries({ series, code, refreshing, refreshError, onRefresh }: {', 'function RealFundSeries({ series, code, refreshing, refreshError, onRefresh, dateAxis }: {')
replace(p, '  onRefresh: () => void;\n}) {', '  onRefresh: () => void;\n  dateAxis: string[];\n}) {')
replace(p, '  const dates = series.rows.map((row) => row.date);', '  const dates = dateAxis;')
replace(p, "  const priceRows = isPrice ? series.rows as RealPriceRow[] : [];\n  const navRows = !isPrice ? series.rows as RealNavRow[] : [];", "  const priceRows = isPrice ? alignRows(series.rows as RealPriceRow[], dates) : [];\n  const navRows = !isPrice ? alignRows(series.rows as RealNavRow[], dates) : [];")
text = read(p)
start, end = text.index('function RealFundSeries('), text.index('export function FundsPage()')
block = text[start:end]
for field in ('open', 'close', 'low', 'high', 'volume', 'unit_nav'):
    block = block.replace(f'numberValue(row.{field})', f'numberValue(row?.{field} ?? null)')
block = block.replace('row.accumulated_nav ?? row.cumulative_nav', 'row?.accumulated_nav ?? row?.cumulative_nav')
block = block.replace('    animation: false,', "    animation: false,\n    legend: { top: 0 },")
block = block.replace('`行情截至 ${dates[dates.length - 1]}', '`行情截至 ${series.rows.at(-1)?.date}')
block = block.replace('实际区间 {dates[0]} 至 {dates[dates.length - 1]} · {dates.length} 条记录', '源数据区间 {series.rows[0].date} 至 {series.rows.at(-1)?.date} · {series.rows.length} 条记录')
block = block.replace('height={390} />', 'height={390} linkGroup={`fund-market-${code}`} />')
block = block.replace('function RelatedMarketPanel({ market }: { market: RelatedMarket | null | undefined }) {\n  if (!market?.rows.length) return null;', '''function RelatedMarketPanel({ market, dateAxis, fundCode }: { market: RelatedMarket | null | undefined; dateAxis: string[]; fundCode: string }) {
  if (!market) return null;
  if (market.relation_status !== 'linked') return <Panel title="关联待核验"><p>{market.relation_reason || '关联来源或核验状态不足，不绘制默认指数。'}</p></Panel>;
  if (!market.rows.length) return <Panel title="关联指数行情"><p>关系已记录，但该指数尚无可用日线。</p></Panel>;''')
block = block.replace('  const dates = market.rows.map((row) => row.date);', '  const dates = dateAxis;\n  const aligned = alignRows(market.rows, dates);')
block = block.replace('data: market.rows.map((row)', 'data: aligned.map((row)')
block = block.replace("    series: [{ name: '真实指数 K 线'", "    dataZoom: [{ type: 'inside', start: 0, end: 100 }, { type: 'slider', bottom: 5, height: 20 }],\n    series: [{ name: '真实指数 K 线'")
block = block.replace('    <p className="muted">已存储的基金—指数关系', '''    <p className="muted">行情截至 {market.rows.at(-1)?.date} · 关系核验于 {market.verified_at || '未提供'} · 关系来源 {market.relation_source_id || '未提供'} {sourceHref(market.evidence_url) && <a href={sourceHref(market.evidence_url)!} target="_blank" rel="noreferrer">核对证据</a>}</p>
    <p className="muted">已存储的基金—指数关系''')
block = block.replace('height={340} />', 'height={340} linkGroup={`fund-market-${fundCode}`} />')
changes[p] = text[:start] + block + text[end:]
replace(p, "  const [relatedMarket, setRelatedMarket] = useState<RelatedMarket | null>(null);", '''  const [relatedMarket, setRelatedMarket] = useState<RelatedMarket | null>(null);
  const [relatedMarkets, setRelatedMarkets] = useState<RelatedMarket[]>([]);
  const [seriesOptions, setSeriesOptions] = useState<RealFundSeriesResponse[]>([]);
  const [seriesKind, setSeriesKind] = useState<'price' | 'nav' | null>(null);
  const [chartRange, setChartRange] = useState('all');
  const [chartStart, setChartStart] = useState('');
  const [chartEnd, setChartEnd] = useState('');
  const selectedSeries = seriesOptions.find((option) => option.kind === seriesKind) ?? series;
  const dates = useMemo(() => commonDates([selectedSeries?.rows ?? [], relatedMarket?.relation_status === 'linked' ? relatedMarket.rows : []]), [selectedSeries, relatedMarket]);
  const dateAxis = useMemo(() => rangeDates(dates, chartRange, chartStart, chartEnd), [dates, chartRange, chartStart, chartEnd]);''')
replace(p, "          setFund(data.fund); setSeries(data.series ?? null); setRelatedMarket(data.related_market ?? null); setStatus('ready');", '''          setFund(data.fund); setSeries(data.series ?? null); setRelatedMarket(data.related_market ?? null);
          setSeriesOptions(data.series_options ?? (data.series?.kind ? [data.series] : []));
          setRelatedMarkets(data.related_markets ?? []); setStatus('ready');''')
replace(p, '        setRelatedMarket(data.related_market ?? null);\n      })', '''        setRelatedMarket(data.related_market ?? null);
        setRelatedMarkets(data.related_markets ?? []);
        setSeriesOptions(data.series_options ?? (data.series?.kind ? [data.series] : []));
        if (data.refresh?.status === 'partial') {
          setRefreshError('本次部分更新成功。' + Object.entries(data.refresh.stages).map(([name, stage]) => `${name === 'fund_series' ? '基金序列' : '关联指数'}：${stage.message}`).join('；'));
        }
      })''')
replace(p, '''    <RealFundSeries code={fund.code} series={series} refreshing={refreshing} refreshError={refreshError} onRefresh={refresh} />
    <RelatedMarketPanel market={relatedMarket} />''', '''    {seriesOptions.length > 1 && <Tabs value={selectedSeries?.kind ?? 'nav'} onChange={(value) => setSeriesKind(value as 'price' | 'nav')} items={seriesOptions.map((option) => ({ value: option.kind!, label: option.kind === 'price' ? '交易价格' : '基金净值' }))} />}
    {!!dates.length && <Panel title="共同查看区间" subtitle="同步日期与缩放，不等于同口径收益对比；未接入交易日历，无法识别双方共同缺失的交易日。">
      <Tabs value={chartRange} onChange={setChartRange} items={[{ value: 'month', label: '近 1 月' }, { value: 'quarter', label: '近 3 月' }, { value: 'half', label: '近 6 月' }, { value: 'year', label: '近 1 年' }, { value: 'all', label: '全部' }, { value: 'custom', label: '自定义' }]} />
      {chartRange === 'custom' && <div className="toolbar"><label>起始日期<input aria-label="图表起始日期" type="date" min={dates[0]} max={dates.at(-1)} value={chartStart} onChange={(event) => setChartStart(event.target.value)} /></label><label>截止日期<input aria-label="图表截止日期" type="date" min={dates[0]} max={dates.at(-1)} value={chartEnd} onChange={(event) => setChartEnd(event.target.value)} /></label></div>}
      {!dateAxis.length && <p role="alert">请选择有效且有数据的日期区间。</p>}
    </Panel>}
    <RealFundSeries code={fund.code} series={selectedSeries} refreshing={refreshing} refreshError={refreshError} onRefresh={refresh} dateAxis={dateAxis} />
    <RelatedMarketPanel market={relatedMarket} dateAxis={dateAxis} fundCode={fund.code} />
    {relatedMarkets.some((relation) => relation.relation_status !== 'linked') && <Panel title="历史关联与核验缺项">{relatedMarkets.filter((relation) => relation.relation_status !== 'linked').map((relation) => <p key={`${relation.code}-${relation.relation_status}`}>{relation.name} · {relation.code}：{relation.relation_reason || '尚未通过当前核验。'}</p>)}</Panel>}''')

new_file("frontend/src/features/market/series-alignment.ts", '''/** Align observations by source date, never by row position or invented prices. */
export function commonDates(groups: ReadonlyArray<ReadonlyArray<{ date: string }>>): string[] {
  return [...new Set(groups.flatMap((rows) => rows.map((row) => row.date)))].sort();
}

export function alignRows<T extends { date: string }>(rows: readonly T[], dates: readonly string[]): Array<T | null> {
  const byDate = new Map(rows.map((row) => [row.date, row]));
  if (byDate.size !== rows.length) throw new Error('序列包含重复日期');
  return dates.map((date) => byDate.get(date) ?? null);
}

export function rangeDates(dates: readonly string[], range: string, start = '', end = ''): string[] {
  if (!dates.length) return [];
  if (range === 'custom') {
    if (!start || !end || start > end || start < dates[0] || end > dates[dates.length - 1]) return [];
    return dates.filter((date) => date >= start && date <= end);
  }
  const days = ({ month: 31, quarter: 93, half: 186, year: 366 } as Record<string, number>)[range];
  if (!days) return [...dates];
  const cutoff = Date.parse(dates[dates.length - 1] + 'T00:00:00Z') - days * 86400000;
  return dates.filter((date) => Date.parse(date + 'T00:00:00Z') >= cutoff);
}
''')
p = "frontend/src/shared/Chart.tsx"
replace(p, 'export function Chart({', 'const linkedCharts = new Map<string, number>();\n\nexport function Chart({')
replace(p, '  height = 300,\n}: {', '  height = 300,\n  linkGroup,\n}: {')
replace(p, '  height?: number;\n}) {', '  height?: number;\n  linkGroup?: string;\n}) {')
replace(p, '  }, []);\n  useEffect(() => {', '''  }, []);
  useEffect(() => {
    const chart = instance.current;
    if (!chart || !linkGroup) return;
    chart.group = linkGroup;
    linkedCharts.set(linkGroup, (linkedCharts.get(linkGroup) ?? 0) + 1);
    echarts.connect(linkGroup);
    return () => {
      const remaining = (linkedCharts.get(linkGroup) ?? 1) - 1;
      if (remaining > 0) linkedCharts.set(linkGroup, remaining);
      else { linkedCharts.delete(linkGroup); echarts.disconnect(linkGroup); }
      if (!chart.isDisposed()) chart.group = '';
    };
  }, [linkGroup]);
  useEffect(() => {''')

new_file("frontend/src/features/market/SectorHistory.tsx", '''import { useEffect, useState } from 'react';
import type { EChartsOption } from 'echarts';
import { Chart } from '../../shared/Chart';
import type { SectorOpportunity } from './SectorOpportunities';

type History = {
  code: string; source_id: string; universe_type: string; history_source_id: string;
  history_source_code: string; history_relation: string; as_of: string | null;
  collection_error: string | null;
  rows: Array<{ date: string; open: string; high: string; low: string; close: string }>;
};

export function SectorHistory({ item }: { item: SectorOpportunity }) {
  const [open, setOpen] = useState(false);
  const [retry, setRetry] = useState(0);
  const [data, setData] = useState<History | null>(null);
  const [error, setError] = useState('');
  useEffect(() => {
    if (!open) return;
    const controller = new AbortController();
    setError(''); setData(null);
    const universe = item.universe_type ?? 'tracked_index';
    const params = new URLSearchParams({ source_id: item.source_id, universe_type: universe });
    fetch(`/api/sectors/${encodeURIComponent(item.code)}/series?${params}`, { signal: controller.signal })
      .then(async (response) => { if (!response.ok) throw new Error('行情暂不可用'); return await response.json() as History; })
      .then((result) => {
        if (controller.signal.aborted) return;
        if (result.code !== item.code || result.source_id !== item.source_id || result.universe_type !== universe || !Array.isArray(result.rows)) throw new Error('行情身份与请求不一致');
        const dates = new Set<string>();
        for (const row of result.rows) {
          const values = [row.open, row.high, row.low, row.close].map(Number);
          if (!/^\\d{4}-\\d{2}-\\d{2}$/.test(row.date) || dates.has(row.date) || values.some((value) => !Number.isFinite(value) || value <= 0)
              || values[2] > Math.min(values[0], values[3]) || values[1] < Math.max(values[0], values[3])) throw new Error('行情字段无效');
          dates.add(row.date);
        }
        setData(result);
      }).catch((cause: unknown) => { if (!controller.signal.aborted) setError(cause instanceof Error ? cause.message : '行情加载失败'); });
    return () => controller.abort();
  }, [open, retry, item.code, item.source_id, item.universe_type]);
  const option: EChartsOption = {
    animation: false, tooltip: { trigger: 'axis', axisPointer: { type: 'cross' } },
    grid: { left: 58, right: 20, top: 24, bottom: 60 },
    xAxis: { type: 'category', data: data?.rows.map((row) => row.date) ?? [] },
    yAxis: { type: 'value', scale: true, name: '点位' },
    dataZoom: [{ type: 'inside' }, { type: 'slider', bottom: 4, height: 20 }],
    series: [{ type: 'candlestick', name: '来源日线', data: data?.rows.map((row) => [Number(row.open), Number(row.close), Number(row.low), Number(row.high)]) ?? [] }],
  };
  return <section><button className="button secondary" onClick={() => setOpen((value) => !value)}>{open ? '收起真实日线' : '查看真实日线'}</button>
    {open && <>{error ? <p role="alert">{error} <button onClick={() => setRetry((value) => value + 1)}>重试行情</button></p> : !data ? <p role="status">正在读取该来源的行情…</p> : <>
      <p>行情截至 {data.as_of || '暂无'} · {data.history_source_id} / {data.history_source_code}</p>
      {data.history_relation === 'proxy_not_equivalent' && <p>跨源参考行情：未证明成分、权重或编制方法等价，不用于持仓归因或基金映射。</p>}
      {data.collection_error && <p role="alert">更新失败，以下保留历史行情供核对：{data.collection_error}</p>}
      {data.rows.length ? <Chart option={option} label={`${item.code}来源真实日线`} height={340} /> : <p>该区间没有可用日线。</p>}
    </>}</>}
  </section>;
}
''')
p = "frontend/src/features/market/SectorOpportunities.tsx"
replace(p, "import { SectorSummaryTable } from './SectorSummaryTable';", "import { SectorSummaryTable } from './SectorSummaryTable';\nimport { SectorHistory } from './SectorHistory';")
replace(p, '  funds: { code: string; name: string }[];', '  funds: { code: string; name: string; relation_status?: string; relation_reason?: string }[];')
replace(p, '    eligible_count: number;\n    candidate_count: number;', '    eligible_count: number;\n    comparison_as_of?: string | null;\n    candidate_count: number;')
replace(p, '优势表示截至榜单日的走势相对领先', '优势表示截至各周期价格比较日的走势相对领先')
replace(p, '                  <span>{summary.eligible_count} 个可比较</span>', '                  <span>{summary.eligible_count} 个可比较 · 价格比较截至 {formatDate(summary.comparison_as_of ?? null)}</span>')
replace(p, '      {item.funds.length > 0 && (', '      <SectorHistory item={item} />\n      {item.funds.length > 0 && (')
replace(p, '            {item.funds.map((fund) => (\n              <Link key={fund.code}', "            {item.funds.filter((fund) => fund.relation_status === 'linked').map((fund) => (\n              <Link key={fund.code}")
replace(p, '          <span>相关基金走势</span>', "          <span>已核验相关基金走势</span>\n          {item.funds.some((fund) => fund.relation_status !== 'linked') && <p>部分历史关联待核验，不作为当前候选；详情见研究页。</p>}")

# Required migration and contract fixture updates retain the original assertions.
p = "tests/test_backend_d11.py"
text = read(p)
if text.count(', 10)') != 4:
    raise RuntimeError("Migration-version assertions changed")
changes[p] = text.replace(', 10)', ', 11)')
p = "frontend/src/features/market/MarketPages.test.tsx"
replace(p, "related_market: { name: '上证50', code: '000016', kind: 'index', source_id: 'index-source',", "related_market: { name: '上证50', code: '000016', kind: 'index', source_id: 'index-source', relation_status: 'linked', relation_source_id: 'fixture.prospectus', verified_at: '2026-09-12T00:00:00Z', evidence_url: 'https://example.test/evidence',")

new_file("tests/test_market_consistency.py", '''import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.api.main import create_app
from backend.core.config import Settings
from backend.core.errors import AppError
from backend.storage.catalog import import_catalog
from backend.storage.database import connection_scope
from backend.storage.timeseries import _clean_rows, import_timeseries, get_timeseries
from backend.storage.related_market import import_related_index, get_related_market, get_related_markets, record_relation_check
from backend.storage.sector_heat import save_sector_heat, read_sector_heat, record_sector_heat_failure
from backend.storage.market_series import market_series
from backend.analysis.research_view import fund_associations
from backend.analysis.sector_strength import attach_strength

PRICE = {"date": "2026-09-11", "open": "10", "high": "12", "low": "9", "close": "11", "volume": "0", "amount": None}


class MarketConsistencyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.settings = Settings(database_path=Path(self.temp.name) / "app.sqlite3")
        import_catalog(self.settings, [{"code": "510050", "name": "测试ETF", "fund_type": "ETF"}], policy_version="fixture")

    def members(self, count):
        return [{"code": f"BK{index:04}", "name": f"测试行业{index}", "kind": "行业", "heat_rank": index + 1,
                 "heat_value": "100", "heat_updated_at": "2026-09-11T08:00:00Z", "updated_at": "2026-09-11T08:01:00Z",
                 "collection_error": None, "rows": [dict(PRICE)]} for index in range(count)]

    def save(self, count, scope="verified_industry_all"):
        save_sector_heat(self.settings, as_of="2026-09-11", updated_at="2026-09-11T08:01:00Z",
                         catalog_count=count, members=self.members(count), requested_count=count, universe_scope=scope)

    def relation(self, code):
        import_related_index(self.settings, fund_code="510050", index_code=code, index_name=f"测试指数{code}", rows=[dict(PRICE)],
                             source_id="fixture.index", relation_source_id="fixture.prospectus", evidence_url="https://example.test/prospectus")

    def test_snapshot_target_changes_and_all_industry_exceeds_100(self):
        self.save(100, "hot_board_top100")
        self.save(3)
        self.assertEqual(read_sector_heat(self.settings)["universe"]["requested_count"], 3)
        self.assertEqual(read_sector_heat(self.settings)["universe"]["status"], "ready")
        self.save(101)
        self.assertEqual(read_sector_heat(self.settings)["universe"]["member_count"], 101)
        with self.assertRaises(ValueError):
            self.save(101, "hot_board_top100")

    def test_initial_failure_does_not_pin_target_to_100(self):
        record_sector_heat_failure(self.settings, attempted_at="2026-09-11T08:00:00Z", error="fixture")
        self.save(3)
        self.assertEqual(read_sector_heat(self.settings)["universe"]["status"], "ready")

    def test_invalid_prices_and_nav_rejected_without_replacing_old_rows(self):
        import_timeseries(self.settings, "510050", "price", [dict(PRICE)], source_id="fixture", policy_version="v1")
        for change in ({"high": "9"}, {"low": "12"}, {"open": "0"}, {"volume": "-1"}, {"close": "NaN"}):
            with self.subTest(change=change), self.assertRaises(AppError):
                import_timeseries(self.settings, "510050", "price", [{**PRICE, **change}], source_id="fixture", policy_version="v1")
            self.assertEqual(get_timeseries(self.settings, "510050")["rows"], [PRICE])
        for value in ("-1", "0", "Infinity"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                _clean_rows("nav", [{"date": "2026-09-11", "unit_nav": value}])

    def test_explicit_series_kind_does_not_hide_nav(self):
        import_timeseries(self.settings, "510050", "price", [dict(PRICE)], source_id="fixture", policy_version="v1")
        import_timeseries(self.settings, "510050", "nav", [{"date": "2026-09-11", "unit_nav": "1.25"}], source_id="fixture", policy_version="v1")
        self.assertEqual(get_timeseries(self.settings, "510050")["kind"], "price")
        self.assertEqual(get_timeseries(self.settings, "510050", "nav")["rows"][0]["unit_nav"], "1.25")
        with self.assertRaises(AppError):
            get_timeseries(self.settings, "510050", "invalid")

    def test_new_relation_supersedes_old_and_failure_preserves_audit(self):
        self.relation("000001")
        self.relation("000002")
        self.assertEqual(get_related_market(self.settings, "510050")["code"], "000002")
        relations = get_related_markets(self.settings, "510050")
        self.assertEqual([row["relation_status"] for row in relations], ["superseded", "linked"])
        record_relation_check(self.settings, "510050", outcome="failed", error="network fixture")
        self.assertIsNone(get_related_market(self.settings, "510050"))
        with connection_scope(self.settings) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM fund_market_relation").fetchone()[0], 2)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM fund_relation_verification").fetchone()[0], 3)
        self.relation("000002")
        self.assertEqual(get_related_market(self.settings, "510050")["code"], "000002")

    def test_legacy_ambiguous_relations_are_withheld(self):
        self.relation("000001"); self.relation("000002")
        with connection_scope(self.settings) as connection:
            connection.execute("DELETE FROM fund_relation_verification")
        self.assertIsNone(get_related_market(self.settings, "510050"))
        self.assertTrue(all(row["relation_status"] == "withheld" for row in get_related_markets(self.settings, "510050")))

    def test_research_does_not_reenable_withheld_relation(self):
        item = {"universe_type": "tracked_index", "funds": [{"code": "510050", "name": "测试", "relation_type": "tracked_index",
                "relation_source_id": "fixture", "evidence_url": "https://example.test/evidence", "verified_at": "2026-09-11T08:00:00Z",
                "relation_status": "withheld", "relation_reason": "本轮核验失败"}]}
        self.assertEqual(fund_associations(item, generated_at="2026-09-15T08:00:00Z")[0]["status"], "withheld")

    def test_refresh_reports_partial_commit(self):
        app = create_app(self.settings)
        endpoint = next(route.endpoint for route in app.routes if route.path == "/api/funds/{code}/refresh")
        def success(settings, code):
            return import_timeseries(settings, code, "price", [dict(PRICE)], source_id="fixture", policy_version="v1")
        with patch("backend.api.main.refresh_fund_timeseries", side_effect=success), patch("backend.api.main.refresh_related_market", side_effect=RuntimeError("source fixture")):
            result = endpoint("510050")
        self.assertEqual(result["refresh"]["status"], "partial")
        self.assertEqual(result["refresh"]["stages"]["fund_series"]["status"], "updated")
        self.assertEqual(result["series"]["rows"], [PRICE])
        with patch("backend.api.main.refresh_fund_timeseries", side_effect=RuntimeError("fund failure")), patch("backend.api.main.refresh_related_market", side_effect=RuntimeError("index failure")):
            with self.assertRaises(AppError) as caught:
                endpoint("510050")
            self.assertEqual(caught.exception.status_code, 502)
        self.assertEqual(get_timeseries(self.settings, "510050")["rows"], [PRICE])

    def test_source_exact_market_query_and_range(self):
        self.save(1)
        result = market_series(self.settings, "BK0000", source_id="sector_daily.eastmoney", universe_type="hot_board")
        self.assertEqual(result["rows"], [PRICE])
        with self.assertRaises(AppError) as caught:
            market_series(self.settings, "BK0000", source_id="wrong-source", universe_type="hot_board")
        self.assertEqual(caught.exception.status_code, 404)
        with self.assertRaises(AppError):
            market_series(self.settings, "BK0000", source_id="sector_daily.eastmoney", universe_type="hot_board", start="2026-10-01", end="2026-09-01")

    def test_universes_choose_independent_comparison_dates(self):
        def item(code, universe, day):
            return {"code": code, "universe_type": universe, "as_of": day, "periods": [
                {"id": key, "status": "strong", "return_pct": 2, "reason": "", "observation_start": "2026-08-01"}
                for key in ("short", "medium", "long")]}
        items = [item("a", "hot_board", "2026-09-11"), item("b", "tracked_index", "2026-09-14")]
        attach_strength(items, {"hot_board": "2026-09-11", "tracked_index": "2026-09-14"})
        self.assertTrue(all(period["strength"]["eligible"] for item in items for period in item["periods"]))


if __name__ == "__main__":
    unittest.main()
''')
new_file("frontend/src/features/market/series-alignment.test.ts", '''import { describe, expect, it } from 'vitest';
import { alignRows, commonDates, rangeDates } from './series-alignment';

describe('真实日期联动', () => {
  it('按日期而不是行号对齐，缺少观测保持空白', () => {
    const fund = [{ date: '2026-09-11', unit_nav: '1' }, { date: '2026-09-15', unit_nav: '2' }];
    const index = [{ date: '2026-09-11' }, { date: '2026-09-14' }, { date: '2026-09-15' }];
    const dates = commonDates([fund, index]);
    expect(dates).toEqual(['2026-09-11', '2026-09-14', '2026-09-15']);
    expect(alignRows(fund, dates)).toEqual([fund[0], null, fund[1]]);
    expect(dates).not.toContain('2026-09-12');
  });
  it('重复日期不静默选一条', () => {
    expect(() => alignRows([{ date: '2026-09-11' }, { date: '2026-09-11' }], ['2026-09-11'])).toThrow();
  });
  it('使用最后来源日期裁剪区间，不冒用系统今天', () => {
    expect(rangeDates(['2025-01-01', '2025-05-01', '2025-05-15'], 'month')).toEqual(['2025-05-01', '2025-05-15']);
  });
  it('空、自定义逆序和越界区间不会生成伪数据', () => {
    expect(rangeDates([], 'all')).toEqual([]);
    expect(rangeDates(['2026-09-11', '2026-09-15'], 'custom', '2026-09-15', '2026-09-11')).toEqual([]);
    expect(rangeDates(['2026-09-11', '2026-09-15'], 'custom', '2026-09-10', '2026-09-15')).toEqual([]);
  });
});
''')

# Maintain existing designs and a single implementation status table.
replace("docs/fund-data-storage-design.md", '后端目前仅有目录占位。', '后端已有 FastAPI／SQLite、真实目录和部分净值／行情接入；完整事实版本及历史快照仍待实现。')
replace("docs/fund-data-storage-design.md", '## 2. 基金库范围与身份', '''### 1.3 当前市场一致性切片（MC1）

时序写入统一校验正净值、正 OHLC、最高／最低关系及非负成交量和成交额；无效批次不替换旧序列。`GET /api/funds/{code}/series?kind=price|nav` 显式选择数据集，省略时兼容价格优先。详情的 `series_options` 列出已存储数据集；这不表示场内基金的净值适配器已自动补齐。

迁移 `011_relation_verification.sql` 追加基金关系核验历史，不修改旧迁移校验和、不删除已有关系或行情。成功核验 B 后，原 A 只保留为历史证据；失败／未解析唯一标的不删除历史，但暂不开放当前关联；无新核验且有多条旧跟踪关系时全部待核验。核验时间不是业务生效时间，`effective_from`／`effective_to` 保持未知，不能用于严格历史回测。`relation_status` 与来源、证据、核验时间在详情、研究和板块列表共同消费。

刷新分基金序列和关联指数执行。部分成功返回 `refresh.status=partial` 与逐项结果及已提交数据；全部无可发布结果返回 502 和逐项原因。网络请求不放入长写事务。当前仍不是跨全部数据集的不可变快照，正式研究继续依赖 D1／D4。

`GET /api/sectors/{code}/series` 必须提供 `source_id`、`universe_type`，可选 `start`／`end`；身份不符返回 404，不按名称回退。响应单列实际 `history_source_id`／`history_source_code`；跨源行情标记 `proxy_not_equivalent`，只作参考，不证明成分、权重或编制方法等价。

行业快照的目标数随范围更新；全部行业不沿用百板块数量上限。零个可核对行情身份属于采集失败，保留旧快照。实现状态和实际验证仅见投资分析计划 MC1，不在本节重复记进度。

## 2. 基金库范围与身份''')
replace("docs/ui-interaction-design.md", '### 4.3 板块机会与板块详情', '''#### 4.2.1 真实图表当前契约（MC1）

详情可切换**已经存储**的价格／净值数据集，统一近 1／3／6／12 月、全部及自定义查看区间；预设使用来源最后日期回看近似自然日，不改变分析周期。基金与当前有效关联指数按日期并集对齐，缺少对应观测保持空值，缩放与光标分组联动。未接入完整交易日历，因此不能发现双方共同缺失的交易日，也不把周末自动插成缺口。

原始净值、累计净值、价格和指数仍按各自原值绘制，不输出总收益或超额收益。关联已核验但行情为空时有单独说明；历史／待核验关系保留可读原因，不默认绘图或生成候选。分项刷新成功后立即展示成功部分，保留失败原因和各自资料日期。

板块完整证据按需加载来源精确的真实日线；跨源参考明确标识，不将同名视为编制方法等价。优势列表分别显示价格比较日与成交额榜单日。

### 4.3 板块机会与板块详情''')
replace("docs/investment-analysis-design.md", '排名仅比较最新日期与榜单日相同、观察起点与同组多数样本相同的有效样本', '排名在行业池和参考指数池内分别确定价格比较日，仅比较最新日期与该池比较日相同、观察起点与同组多数样本相同的有效样本；成交额榜单日单列，不替代价格比较日')
replace("docs/investment-analysis-design.md", '## 2. 分析链路与三个周期', '''### 1.7 市场一致性修复的研究边界（MC1）

来源日线、跟踪关系和当前展示的一致性改动详见数据设计 §1.3 与界面设计 §4.2.1。行情采集失败时，旧数值只供核对，不继续作为本次机会判断的价格支持；已有经营反证仍保留。两个研究池独立计算价格比较日，不由行业池日期剔除本来有效的参考指数。

本切片不新增评分权重、投资阈值、概率、AI、个人操作或总收益。跨源日线为参考，不升级为基金暴露。行业模板、完整持仓穿透、可知时点快照、推荐筛选与样本外验证继续按 L3／A0—A6 实施；不得将 MC1 完成等同于上述能力完成。

## 2. 分析链路与三个周期''')
replace("docs/investment-analysis-plan.md", '## 2. 大阶段总览与前置关系', '''### 1.5 市场一致性与真实图表补齐（MC1，避免重复实施）

| 小阶段 | 状态 | 已实现范围与下一验收 |
| --- | --- | --- |
| MC1.1 当前数据与关系一致性 | 已实现，验证见下表；待用户真实数据复查 | 目标数／行业范围、数值校验、核验历史迁移、旧关系与歧义暂缓、统一关系门槛、分项刷新、双池比较日期；后续不能再次按未实现重做。 |
| MC1.2 已存储序列与图表 | 已实现，验证见下表；待浏览器与真实数据联调 | 显式序列选择、共同日期区间／空值对齐、图表分组联动、来源精确板块日线下钻、空行情与部分失败说明。 |
| MC1.3 尚未包含的依赖 | 未开始，沿用原计划 | 自动双数据集采集、完整交易日历、真实持仓穿透、业务生效区间、不可变研究快照、总收益／跟踪误差、正式投资候选与效果验证；分别复用 D2／D3／D4、L3，不重复建设。 |

MC1 是已授权的连续修复切片，不将此前 L2.4、L3 或 A0—A6 自动标为通过。迁移仅在测试数据库运行；用户本机真实数据库尚未执行，升级前按原备份要求处理。设计契约分别维护在数据设计 §1.3、界面设计 §4.2.1、投资分析设计 §1.7。

| 实际检查 | 结果 |
| --- | --- |
<!-- MARKET_VERIFICATION_RESULTS -->
| 本机真实数据与浏览器端到端验收 | 未运行；不得替代为通过 |

<!-- MARKET_VERIFICATION_ERRORS -->

人工复查：依次验证百板块／行业范围切换、基金净值成功但指数失败、关系 A→B 与网络失败后的状态、价格／净值切换、共同日期与缺值、盘中榜单／前日价格日期、板块来源精确下钻；在 1440／768／390px 核对布局。无投资收益或预测效果验收结论。

## 2. 大阶段总览与前置关系''')
replace("README.md", '## 完整启动流程', '''### 市场数据一致性与真实图表

基金详情新增已存储价格／净值切换与共同日期区间，关联指数同步查看；刷新返回成功／部分成功／失败，分别保留实际日期和原因。板块完整证据可按来源精确读取真实日线；跨源同名仅作参考，不表示成分或权重相同。

启动自动应用新增关系核验迁移；旧证据保留，旧关系歧义或最近核验失败时不会默认选一个指数。首次更新前备份本机数据库。当前仍未完成业务有效区间、完整交易日历、持仓穿透、总收益和正式推荐。已实现范围与实际验证统一见 [投资分析计划 MC1](docs/investment-analysis-plan.md#15-市场一致性与真实图表补齐mc1避免重复实施)，避免重复实现。

## 完整启动流程''')

# Validate every anchor and Python module before writing product files.
for path, content in changes.items():
    if path.endswith('.py'):
        compile(content, path, 'exec')
for path, content in changes.items():
    target = ROOT / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
print("Applied files:")
print("\n".join(sorted(changes)))
