"""Typed, append-only research facts and strictly system-known snapshots.

Ingestion never accepts a caller-supplied first_seen/committed timestamp. Current
files with old report dates therefore cannot enter an earlier historical replay.
"""
from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from backend.core.config import Settings
from backend.core.errors import AppError
from backend.core.relation_policy import public_source
from backend.core.trading_calendar import day, get_calendar, SHANGHAI
from backend.storage.database import connection_scope, migrate

PARSER_VERSION = "research-input-v1"
MAX_FACTS = 50000


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def timestamp(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("时间戳必须为含时区的字符串")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("时间戳必须含时区")
    return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def number(value: object, *, positive: bool = False, nonnegative: bool = False) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 80:
        raise ValueError("数值必须是长度受限的精确小数字符串")
    try:
        result = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError("数值无效") from exc
    if not result.is_finite() or result.copy_abs() > Decimal("1e30") or result.as_tuple().exponent < -30:
        raise ValueError("数值非有限或超出研究精度范围")
    if (positive and result <= 0) or (nonnegative and result < 0):
        raise ValueError("数值不满足正值/非负约束")
    rendered = format(result, "f")
    return (rendered.rstrip("0").rstrip(".") if "." in rendered else rendered) if result else "0"


def text(value: object, field: str, *, maximum: int = 500) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"{field} 必须是非空且长度受限的字符串")
    return value.strip()


def canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def digest(value: object) -> str:
    return sha256(canonical(value).encode()).hexdigest()


def subject_key(value: object) -> str:
    result = text(value, "subject_key", maximum=200)
    parts = result.split(":")
    if len(parts) != 3 or parts[0] not in ("fund", "tracked_index", "hot_board") or not all(parts):
        raise ValueError("对象必须为类型:来源体系:代码，禁止仅按名称或代码匹配")
    return result


def normalize_fact(raw: dict, now: str) -> dict:
    if not isinstance(raw, dict):
        raise ValueError("事实必须是对象")
    if any(key in raw for key in ("first_seen_at", "committed_at", "revision_id", "available_at")):
        raise ValueError("系统可知时间和版本标识不能由输入指定")
    kind = raw.get("kind")
    if kind not in ("numeric", "series", "fund_profile"):
        raise ValueError("不支持的事实类型")
    effective = day(raw.get("effective_date"))
    local_now = datetime.fromisoformat(now.replace("Z", "+00:00")).astimezone(SHANGHAI)
    if effective > local_now.date():
        raise ValueError("当前研究不接受未来业务日期")
    valid_to = raw.get("valid_to")
    if valid_to is not None and day(valid_to) <= effective:
        raise ValueError("业务有效区间必须左闭右开且非空")
    precision = raw.get("publication_precision", "unknown")
    published = raw.get("published_at")
    if precision == "time":
        published = timestamp(published)
        if published > now:
            raise ValueError("精确公开时间在未来")
    elif precision == "day":
        # A release date is not an invented time; use next-day midnight conservatively.
        published_day = day(published)
        if published_day > local_now.date():
            raise ValueError("公开日期在未来")
        try:
            zone = ZoneInfo(raw.get("source_timezone", "Asia/Shanghai"))
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("公开日期的来源时区无效") from exc
        published = timestamp(datetime.combine(published_day + timedelta(days=1), time(), zone).isoformat())
    elif precision == "unknown" and published is None:
        published = None
    else:
        raise ValueError("公开时间与精度不一致")
    if published is not None and effective > datetime.fromisoformat(published.replace("Z", "+00:00")).astimezone(SHANGHAI).date():
        raise ValueError("业务数据日期晚于公开时间")
    result = {"kind": kind, "subject_key": subject_key(raw.get("subject_key")),
              "source_id": text(raw.get("source_id"), "source_id", maximum=200),
              "effective_date": effective.isoformat(), "valid_to": valid_to,
              "published_at": published, "publication_precision": precision,
              "parser_version": text(raw.get("parser_version", PARSER_VERSION), "parser_version")}
    semantics = raw.get("semantic_status", "unverified")
    if semantics not in ("verified", "background", "unverified"):
        raise ValueError("语义核验状态无效")
    if kind == "numeric":
        details = {"metric": text(raw.get("metric"), "metric"), "value": number(raw.get("value")),
                   "unit": text(raw.get("unit"), "unit"), "scope": text(raw.get("scope"), "scope"),
                   "methodology": text(raw.get("methodology"), "methodology"), "semantic_status": semantics}
        if details["metric"] in ("pe_ttm", "pe_static", "pe_dynamic", "pb"):
            calendar = get_calendar()
            if not calendar.is_session(effective) or effective > calendar.latest_completed(local_now):
                raise ValueError("估值必须具有已完成交易日的业务日期")
        natural = [result["subject_key"], kind, details["metric"], details["scope"], details["methodology"], result["effective_date"]]
    elif kind == "series":
        series_kind = raw.get("series_kind")
        if series_kind not in ("price", "nav", "total_return") or semantics == "background":
            raise ValueError("序列种类或语义状态无效")
        calendar = get_calendar()
        if not calendar.is_session(effective) or effective > calendar.latest_completed(local_now):
            raise ValueError("序列包含非交易日或未结算数据")
        basis = text(raw.get("basis"), "basis")
        expected_basis = {"price": "unadjusted_price", "nav": "unit_nav", "total_return": "reinvested_total_return"}
        if basis != expected_basis[series_kind]:
            raise ValueError("序列与数值口径不一致；累计净值不能冒充总收益")
        if raw.get("currency") != "CNY":
            raise ValueError("首版仅支持人民币境内可比序列")
        details = {"series_kind": series_kind, "value": number(raw.get("value"), positive=True),
                   "amount": number(raw["amount"], nonnegative=True) if raw.get("amount") is not None else None,
                   "currency": "CNY", "basis": basis, "semantic_status": semantics}
        natural = [result["subject_key"], kind, series_kind, basis, result["effective_date"]]
    else:
        code = raw.get("fund_code")
        if (not isinstance(code, str) or len(code) != 6 or not code.isascii() or not code.isdigit()
                or result["subject_key"].split(":")[0] != "fund" or result["subject_key"].split(":")[-1] != code):
            raise ValueError("基金身份不一致")
        if semantics == "background":
            raise ValueError("基金身份不接受行业背景代替核验")
        category = raw.get("category")
        if category not in ("passive_etf", "passive_otc", "active", "feeder"):
            raise ValueError("基金类别无效")
        status = raw.get("subscription_status")
        if status not in ("open", "suspended", "limited", "unknown"):
            raise ValueError("交易状态无效")
        benchmark = subject_key(raw.get("benchmark_key"))
        if not benchmark.startswith("tracked_index:"):
            raise ValueError("基金基准必须是来源明确的指数")
        details = {"fund_code": code, "name": text(raw.get("name"), "name"), "category": category,
                   "benchmark_key": benchmark, "portfolio_id": text(raw.get("portfolio_id"), "portfolio_id"),
                   "annual_fee_bps": number(raw["annual_fee_bps"], nonnegative=True) if raw.get("annual_fee_bps") is not None else None,
                   "subscription_status": status, "semantic_status": semantics}
        natural = [result["subject_key"], kind, result["effective_date"]]
    result.update(details)
    result["record_key"] = canonical(natural)
    return result


def append_facts(settings: Settings, facts: list[dict], *, source_url: str,
                 raw_body: bytes, media_type: str = "application/json") -> dict:
    if not isinstance(facts, list) or not facts or len(facts) > MAX_FACTS:
        raise ValueError("事实批次不能为空或超过限制")
    if not public_source(source_url) or not isinstance(raw_body, bytes) or not raw_body or len(raw_body) > 20 * 1024 * 1024:
        raise ValueError("缺少合法来源地址/原始资料或资料超过20MiB")
    media_type = text(media_type, "media_type", maximum=200)
    migrate(settings)
    with connection_scope(settings) as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            now = utc_now()
            normalized = [normalize_fact(raw, now) for raw in facts]
            identities = [(item["record_key"], item["source_id"]) for item in normalized]
            if len(identities) != len(set(identities)):
                raise ValueError("同一批次自然键重复，无法确定修订顺序")
            last_commit = connection.execute("""SELECT MAX(t) FROM (
                SELECT MAX(committed_at) t FROM data_revision UNION ALL
                SELECT MAX(fetched_at) t FROM data_observation UNION ALL
                SELECT MAX(occurred_at) t FROM data_quality_event)""").fetchone()[0]
            if last_commit and now < last_commit:
                raise ValueError("系统时钟倒退，暂停事实入库")
            body_hash = sha256(raw_body).hexdigest()
            asset_id = digest([source_url, media_type, body_hash])
            connection.execute("INSERT OR IGNORE INTO raw_asset VALUES(?,?,?,?,?,?)",
                               (asset_id, source_url, media_type, raw_body, body_hash, now))
            revisions, created = [], 0
            for item in normalized:
                payload_hash = digest(item)
                previous = connection.execute(
                    "SELECT revision_id,payload_hash FROM data_revision WHERE record_key=? AND source_id=? ORDER BY rowid DESC LIMIT 1",
                    (item["record_key"], item["source_id"]),
                ).fetchone()
                if previous and previous["payload_hash"] == payload_hash:
                    revision_id = previous["revision_id"]
                else:
                    revision_id = uuid4().hex
                    connection.execute(
                        "INSERT INTO data_revision VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (revision_id, item["kind"], item["record_key"], item["subject_key"], item["source_id"],
                         item["effective_date"], item["valid_to"], item["published_at"], item["publication_precision"],
                         now, now, payload_hash, asset_id, item["parser_version"], previous["revision_id"] if previous else None),
                    )
                    if item["kind"] == "numeric":
                        fields, table = ("metric", "value", "unit", "scope", "methodology", "semantic_status"), "numeric_fact"
                    elif item["kind"] == "series":
                        fields, table = ("series_kind", "value", "amount", "currency", "basis", "semantic_status"), "series_fact"
                    else:
                        fields, table = ("fund_code", "name", "category", "benchmark_key", "portfolio_id", "annual_fee_bps", "subscription_status", "semantic_status"), "research_fund_profile"
                    connection.execute(f"INSERT INTO {table} VALUES({','.join('?' for _ in range(len(fields)+1))})",
                                       (revision_id, *(item[field] for field in fields)))
                    created += 1
                connection.execute("INSERT INTO data_observation(revision_id,raw_asset_id,fetched_at) VALUES(?,?,?)",
                                   (revision_id, asset_id, now))
                revisions.append(revision_id)
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise
    # A crash between the two commits leaves unpublished facts, not prematurely usable data.
    # An idempotent reimport can publish such drafts; it cannot clear a quarantine.
    with connection_scope(settings) as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            visible_at = utc_now()
            if visible_at < now:
                raise ValueError("系统时钟倒退，暂停事实发布")
            for revision_id in revisions:
                exists = connection.execute("SELECT 1 FROM data_quality_event WHERE revision_id=?", (revision_id,)).fetchone()
                if not exists:
                    connection.execute("INSERT INTO data_quality_event(revision_id,occurred_at,state,reason) VALUES(?,?,'available',?)",
                                       (revision_id, visible_at, "入库后发布；内容核验资格以semantic_status为准"))
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise
    return {"revision_ids": revisions, "new_revision_count": created, "observation_count": len(facts),
            "first_observed_now": now, "raw_asset_id": asset_id}


def quarantine(settings: Settings, revision_id: str, reason: str) -> None:
    migrate(settings)
    with connection_scope(settings) as connection:
        if not connection.execute("SELECT 1 FROM data_revision WHERE revision_id=?", (revision_id,)).fetchone():
            raise AppError("revision_not_found", "事实版本不存在", 404)
        connection.execute("INSERT INTO data_quality_event(revision_id,occurred_at,state,reason) VALUES(?,?,'quarantined',?)",
                           (revision_id, utc_now(), text(reason, "reason")))


def _read_fact(connection, revision, cutoff: str) -> dict:
    row = dict(revision)
    row.pop("ordinal", None)
    kind = row["dataset_kind"]
    table = {"numeric": "numeric_fact", "series": "series_fact", "fund_profile": "research_fund_profile"}[kind]
    details = connection.execute(f"SELECT * FROM {table} WHERE revision_id=?", (row["revision_id"],)).fetchone()
    asset = connection.execute("SELECT source_url,body_hash FROM raw_asset WHERE asset_id=?", (row["raw_asset_id"],)).fetchone()
    observed = connection.execute("SELECT MAX(fetched_at) FROM data_observation WHERE revision_id=? AND fetched_at<=?",
                                  (row["revision_id"], cutoff)).fetchone()[0]
    return {**row, **dict(details), **dict(asset), "last_observed_at": observed}


def read_manifest(connection, subjects: list[str], cutoff: str, *, purpose: str = "research", mode: str = "system_as_of") -> dict:
    """Select only knowledge available by cutoff; caller holds the read transaction."""
    business_day = datetime.fromisoformat(cutoff.replace("Z", "+00:00")).astimezone(SHANGHAI).date().isoformat()
    placeholders = ",".join("?" for _ in subjects)
    rows = connection.execute(
        f"""SELECT rowid AS ordinal,* FROM data_revision WHERE subject_key IN ({placeholders})
            AND effective_date<=? AND first_seen_at<=? AND committed_at<=?
            AND (published_at IS NULL OR published_at<=?) ORDER BY rowid""",
        (*subjects, business_day, cutoff, cutoff, cutoff),
    ).fetchall()
    # Latest revision of each source-local natural key first, then quality gating.
    # A quarantined correction must not silently resurrect its superseded value.
    chosen = {(row["record_key"], row["source_id"]): row for row in rows}
    facts, excluded = [], []
    for row in chosen.values():
        quality = connection.execute(
            "SELECT state,reason FROM data_quality_event WHERE revision_id=? AND occurred_at<=? ORDER BY id DESC LIMIT 1",
            (row["revision_id"], cutoff),
        ).fetchone()
        if quality is None or quality["state"] != "available":
            excluded.append({"revision_id": row["revision_id"], "subject_key": row["subject_key"], "record_key": row["record_key"], "kind": "quality", "reason": quality["reason"] if quality else "缺少质量事件"})
            continue
        if row["valid_to"] and row["valid_to"] <= business_day:
            excluded.append({"revision_id": row["revision_id"], "subject_key": row["subject_key"], "record_key": row["record_key"], "kind": "validity", "reason": "业务有效期已结束"})
            continue
        facts.append(_read_fact(connection, row, cutoff))
    facts.sort(key=lambda item: (item["subject_key"], item["record_key"], item["source_id"]))
    keys: dict[str, set[str]] = {}
    for item in facts:
        keys.setdefault(item["record_key"], set()).add(item["source_id"])
    conflicts = [key for key, sources in keys.items() if len(sources) > 1]
    calendar = get_calendar()
    manifest = {"version": "system-snapshot-v2", "mode": mode, "cutoff": cutoff,
                "purpose": purpose, "subjects": subjects, "facts": facts, "excluded": excluded,
                "source_conflicts": conflicts, "calendar": calendar.document,
                "calendar_hash": calendar.hash, "missing_subjects": [s for s in subjects if not any(f["subject_key"] == s for f in facts)]}
    from backend.storage.sector_fundamentals import known_observations
    manifest["sector_fundamentals"] = known_observations(connection, subjects, cutoff)
    return manifest


def freeze_snapshot(settings: Settings, subjects: list[str], *, cutoff: str | None = None,
                    purpose: str = "research", mode: str = "system_as_of") -> dict:
    if mode != "system_as_of":
        raise ValueError("当前仅开放system_as_of；未核验历史原版本不能用public_as_of重建")
    if not isinstance(subjects, list) or not 0 < len(subjects) <= 200:
        raise ValueError("快照需1至200个来源明确的对象")
    subjects = sorted({subject_key(value) for value in subjects})
    purpose = text(purpose, "purpose", maximum=100)
    migrate(settings)
    with connection_scope(settings) as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            now = utc_now()
            cutoff = timestamp(cutoff) if cutoff is not None else now
            if cutoff > now:
                raise ValueError("快照截止时间不能在未来")
            manifest = read_manifest(connection, subjects, cutoff, purpose=purpose, mode=mode)
            hashed = digest(manifest)
            snapshot_id = hashed
            connection.execute("INSERT OR IGNORE INTO data_snapshot VALUES(?,?,?,?,?,?,?)",
                               (snapshot_id, purpose, mode, cutoff, now, hashed, canonical(manifest)))
            connection.executemany("INSERT OR IGNORE INTO snapshot_item VALUES(?,?)", [(snapshot_id, fact["revision_id"]) for fact in manifest["facts"]])
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise
    return get_snapshot(settings, snapshot_id)


def get_snapshot(settings: Settings, snapshot_id: str) -> dict:
    migrate(settings)
    with connection_scope(settings) as connection:
        row = connection.execute("SELECT * FROM data_snapshot WHERE snapshot_id=?", (snapshot_id,)).fetchone()
    if row is None:
        raise AppError("snapshot_not_found", "研究快照不存在", 404)
    result = dict(row)
    manifest = json.loads(result.pop("manifest_json"))
    if digest(manifest) != result["manifest_hash"]:
        raise AppError("snapshot_integrity_failed", "研究快照校验失败", 500)
    result["manifest"] = manifest
    return result


def list_snapshots(settings: Settings, limit: int = 20) -> list[dict]:
    migrate(settings)
    if not 1 <= limit <= 100:
        raise ValueError("列表数量无效")
    with connection_scope(settings) as connection:
        return [dict(row) for row in connection.execute(
            "SELECT snapshot_id,purpose,selection_mode,cutoff,created_at,manifest_hash FROM data_snapshot ORDER BY rowid DESC LIMIT ?", (limit,))]
