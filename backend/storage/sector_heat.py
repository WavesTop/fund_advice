"""Current Tonghuashun sector snapshot, isolated from verified fund/index relations.

Only market context is supported. Turnover ranks measure traded amount, not
popularity, investment merit, fund exposure, or a recommendation score.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timezone
from decimal import Decimal
import re
from typing import Any

from backend.core.config import Settings
from backend.core.errors import AppError
from backend.core.source_admission import SourceAdmissionError, writable_source_id
from backend.storage.database import connection_scope, migrate

DIRECTORY_URL = "https://q.10jqka.com.cn/thshy/"
HISTORY_URL = "https://d.10jqka.com.cn/v4/line/"
CONSTITUENT_URL = DIRECTORY_URL
SOURCE_ID = "sector_daily.ths"
PRICE_HISTORY_FIELDS = ("open", "high", "low", "close", "volume", "amount")
SOURCE_POLICY = {
    "usage": "market_context_only",
    "provider": "同花顺",
    "classification": "同花顺原生行业分类；行业与后续主题观察池分开展示，不与概念板块混排。",
    "heat_definition": "完整读取同花顺行业目录；优先使用行业摘要成交额，摘要不可用时使用最新日线成交额排序；同额按代码排序，不是涨幅榜。",
    "timing": "交易日开盘后合并当日最新行情，盘中观测随刷新更新并标注行情时间；行业摘要和行情请求不是同一瞬时快照。",
    "price_basis": "按同花顺原生行业代码读取日线。不复权；成交额为元，成交量保留来源单位。",
    "limitations": "仅供本地市场背景观察；成分股来自来源当前板块定义，来源未提供官方权重；不作为国证或中证指数，不推定基金映射，不晋升正式基金推荐。",
    "refresh": "通过页面重新评估或采集命令刷新；当前版本以同花顺90个行业为主分析池，主题观察池另行扩展。",
}

REMOVABLE_UNIVERSE_TYPES = {"hot_board", "tracked_index"}
THS_CODE_PATTERN = r"[0-9A-Z]{6}"
THS_HISTORY_SOURCE_PATTERN = r"(?:[a-z]{2}|[0-9]{2})_[0-9A-Z]{6}"


class SectorHistoryIntegrityError(ValueError):
    """A same-identity history refresh is incomplete or contradicts stored facts."""


def _history_rows_by_date(rows: list[dict[str, Any]], *, label: str) -> dict[str, dict[str, Any]]:
    if not isinstance(rows, list):
        raise SectorHistoryIntegrityError(f"{label}不是日线列表")
    indexed: dict[str, dict[str, Any]] = {}
    observed_dates: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            raise SectorHistoryIntegrityError(f"{label}包含非对象日线")
        value = row.get("date")
        if not isinstance(value, str):
            raise SectorHistoryIntegrityError(f"{label}包含无效日期")
        try:
            if date.fromisoformat(value).isoformat() != value:
                raise ValueError
        except ValueError as exc:
            raise SectorHistoryIntegrityError(f"{label}包含无效日期：{value}") from exc
        if value in indexed:
            raise SectorHistoryIntegrityError(f"{label}包含重复日期：{value}")
        observed_dates.append(value)
        indexed[value] = row
    if observed_dates != sorted(observed_dates):
        raise SectorHistoryIntegrityError(f"{label}日期不是升序")
    return indexed


def _same_history_value(left: Any, right: Any) -> bool:
    if left is None or right is None:
        return left is right
    try:
        return Decimal(str(left)) == Decimal(str(right))
    except Exception:
        return left == right


def merge_consistent_sector_history(
    previous_rows: list[dict[str, Any]], fetched_rows: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Merge a source window only when every overlapping price fact agrees."""
    previous = _history_rows_by_date(previous_rows, label="已保存历史")
    fetched = _history_rows_by_date(fetched_rows, label="本次来源历史")
    if not fetched:
        raise SectorHistoryIntegrityError("本次来源历史为空，不能缩短已保存历史")
    if not previous:
        return list(fetched_rows), {
            "previous_row_count": 0,
            "fetched_row_count": len(fetched),
            "overlap_row_count": 0,
            "preserved_row_count": 0,
            "merged_row_count": len(fetched),
        }
    overlap = sorted(previous.keys() & fetched.keys())
    if not overlap:
        raise SectorHistoryIntegrityError("同来源同身份的新旧日线没有重叠日期，无法核对连续性")
    for observed_on in overlap:
        old_row, new_row = previous[observed_on], fetched[observed_on]
        # An explicitly provisional observation can change until finalized.
        if old_row.get("provisional") is True:
            old_time, new_time = old_row.get("quote_as_of"), new_row.get("quote_as_of")
            if old_time and new_time and new_time < old_time:
                raise SectorHistoryIntegrityError("本次盘中行情时间早于已保存行情")
            continue
        for field in PRICE_HISTORY_FIELDS:
            if not _same_history_value(old_row.get(field), new_row.get(field)):
                raise SectorHistoryIntegrityError(
                    f"同来源同身份日线重叠值冲突：{observed_on} {field}"
                )
    merged = {**previous, **fetched}
    merged_rows = [merged[observed_on] for observed_on in sorted(merged)]
    if len(merged_rows) < len(previous_rows) or merged_rows[0]["date"] > previous_rows[0]["date"]:
        raise SectorHistoryIntegrityError("同来源同身份刷新导致历史覆盖范围缩短")
    return merged_rows, {
        "previous_row_count": len(previous),
        "fetched_row_count": len(fetched),
        "overlap_row_count": len(overlap),
        "preserved_row_count": len(previous.keys() - fetched.keys()),
        "merged_row_count": len(merged),
    }


def _assert_saved_history_non_regression(
    existing: dict[str, Any], incoming: dict[str, Any],
) -> None:
    """Reject a destructive direct save for an explicitly identified source series."""
    history_source_id = incoming.get("history_source_id")
    history_source_code = incoming.get("history_source_code")
    if not history_source_id or not history_source_code:
        return
    same_identity = (
        existing.get("code") == incoming.get("code")
        and existing.get("source_id") == incoming.get("source_id", SOURCE_ID)
        and existing.get("history_source_id") == history_source_id
        and existing.get("history_source_code") == history_source_code
    )
    if not same_identity:
        return
    old_rows = json.loads(existing["rows_json"])
    new_rows = incoming.get("rows", [])
    if not old_rows and not new_rows:
        # A repeated failed first collection has no saved history to lose.
        # Its empty member must not roll back other successfully updated boards.
        return
    merged_rows, _ = merge_consistent_sector_history(old_rows, new_rows)
    if [row["date"] for row in merged_rows] != [row.get("date") for row in new_rows]:
        raise SectorHistoryIntegrityError(
            f"{incoming['code']} 同来源同身份刷新缺少已保存日期，拒绝缩短历史"
        )


def read_sector_heat(settings: Settings) -> dict[str, Any]:
    """Read the universe and all members from one SQLite read transaction."""
    migrate(settings)
    with connection_scope(settings) as connection:
        try:
            connection.execute("BEGIN")
            snapshot = connection.execute("SELECT * FROM sector_heat_snapshot WHERE id=1").fetchone()
            members = connection.execute("SELECT * FROM sector_heat_member ORDER BY heat_rank").fetchall()
            removed = {
                (row["universe_type"], row["source_id"], row["code"])
                for row in connection.execute("SELECT universe_type,source_id,code FROM sector_removed_board")
            }
            source_ids = sorted({row["source_id"] for row in members}) or [SOURCE_ID]
            placeholders = ",".join("?" for _ in source_ids)
            membership_snapshots = connection.execute(
                f"""SELECT * FROM sector_membership_snapshot
                    WHERE board_source_id IN ({placeholders}) AND as_of=?""",
                (*source_ids, snapshot["as_of"] if snapshot else None),
            ).fetchall()
            memberships = connection.execute(
                f"""SELECT * FROM sector_membership
                    WHERE board_source_id IN ({placeholders}) AND as_of=?
                    ORDER BY board_source_id, board_code, source_order""",
                (*source_ids, snapshot["as_of"] if snapshot else None),
            ).fetchall()
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise
    state = dict(snapshot) if snapshot else {}
    directory_provenance = (json.loads(state["directory_provenance_json"])
                            if state.get("directory_provenance_json") else None)
    directory_fallback = (directory_provenance is not None
                          and directory_provenance["directory"]["mode"] != "live")
    membership_state = {(row["board_source_id"], row["board_code"]): dict(row) for row in membership_snapshots}
    membership_rows: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in memberships:
        component = dict(row)
        for key in ("board_source_id", "board_code", "as_of"):
            component.pop(key)
        component["market_cap"] = float(Decimal(component["market_cap"])) if component["market_cap"] is not None else None
        component["weight"] = float(Decimal(component["weight"])) if component["weight"] is not None else None
        component["market_cap_status"] = "available" if component["market_cap"] is not None else "unavailable"
        membership_rows.setdefault((row["board_source_id"], row["board_code"]), []).append(component)
    items = []
    for member in members:
        if ("hot_board", member["source_id"], member["code"]) in removed:
            continue
        item = dict(member)
        item.pop("snapshot_id")
        item["rows"] = json.loads(item.pop("rows_json"))
        # Decimal text remains authoritative in storage; floats are display only.
        item["heat_value"] = float(Decimal(item["heat_value"]))
        item["history_relation"] = ("proxy_not_equivalent" if item.get("history_source_id")
                                    and item.get("history_source_id") != item.get("source_id")
                                    else "native_source")
        if item["history_relation"] == "proxy_not_equivalent":
            item["collection_error"] = item.get("collection_error") or (
                "已有日线为跨源参考序列，不等同当前原生板块；重新评估后核验原生行情。")
        item.update(universe_type="hot_board", funds=[], use_scope="market_context_only",
                    amount_unit="CNY", volume_unit="source_native",
                    history_source_url=HISTORY_URL)
        membership_key = (item["source_id"], item["code"])
        membership = membership_state.get(membership_key)
        member_rows = membership_rows.get(membership_key, [])
        item["membership"] = {
            "status": membership["status"] if membership else "missing",
            "as_of": membership["as_of"] if membership else None,
            "fetched_at": membership["fetched_at"] if membership else None,
            "member_count": membership["member_count"] if membership else 0,
            "error": membership["error"] if membership else "尚未采集成分股快照。",
            "weight_basis": "not_provided",
            "members": member_rows,
            "market_cap_missing_count": sum(row["market_cap"] is None for row in member_rows),
            "quality_note": "市值为可选资料；缺失不删除证券，也不等于权重为零。",
        }
        items.append(item)
    identity_blocked = sum(item["history_relation"] == "proxy_not_equivalent" for item in items)
    status = "empty" if not items else (
        "ready" if len(items) == state.get("requested_count") and not state.get("failed_count")
        and not state.get("last_error") and not identity_blocked and not directory_fallback else "partial"
    )
    industry_only = state.get("universe_scope") == "verified_industry_all"
    universe = {
        "label": ("同花顺种子行业目录（完整性待核验）" if industry_only and directory_fallback
                  else "同花顺完整行业目录" if industry_only else "同花顺行业成交额前100"),
        "heat_basis": "turnover_amount",
        "as_of": state.get("as_of"),
        "ranking_as_of": state.get("as_of"),
        "updated_at": state.get("updated_at"),
        "requested_count": len(items) if items else state.get("requested_count", 100),
        "member_count": len(items),
        "catalog_count": state.get("catalog_count", 0),
        "collected_count": sum(not item.get("collection_error") for item in items),
        "failed_count": sum(bool(item.get("collection_error")) for item in items),
        "identity_blocked_count": identity_blocked,
        "source_url": DIRECTORY_URL,
        "status": status,
        "scope": state.get("universe_scope", "hot_board_top100"),
        "last_attempt_at": state.get("last_attempt_at"),
        "last_error": state.get("last_error"),
        "source_policy": dict(SOURCE_POLICY),
        "directory_provenance": directory_provenance,
    }
    if industry_only:
        universe["source_policy"].update(
            classification=("使用本地同花顺行业种子目录；当前行业分类完整性尚未通过实时目录核验。"
                            if directory_fallback else
                            "完整读取同花顺90个原生行业；不含概念、风格、地区和资格集合。"),
            heat_definition="行业按同花顺行业摘要中的总成交额降序排序；热度名次保留本次采集计算位置。",
        )
    return {"universe": universe, "items": items, "directory_provenance": directory_provenance}


def removed_subject_keys(settings: Settings) -> set[tuple[str, str, str]]:
    """Return user-hidden board/index identities."""
    migrate(settings)
    with connection_scope(settings) as connection:
        rows = connection.execute(
            "SELECT universe_type,source_id,code FROM sector_removed_board"
        ).fetchall()
    return {(row["universe_type"], row["source_id"], row["code"]) for row in rows}


def restore_removed_subjects(settings: Settings, subjects: list[dict[str, Any]]) -> None:
    """Allow re-adding a board or index previously removed from the local library."""
    if not subjects:
        return
    keys = []
    for subject in subjects:
        universe_type = str(subject.get("universe_type") or "hot_board")
        source_id = str(subject.get("source_id") or SOURCE_ID).strip()
        code = str(subject.get("code") or "").strip()
        if universe_type not in REMOVABLE_UNIVERSE_TYPES or not re.fullmatch(r"[0-9A-Z]{6}", code):
            continue
        writable_source_id(source_id, field="source_id")
        keys.append((universe_type, source_id, code))
    if not keys:
        return
    migrate(settings)
    with connection_scope(settings) as connection:
        connection.executemany(
            "DELETE FROM sector_removed_board WHERE universe_type=? AND source_id=? AND code=?",
            keys,
        )


def remove_sector_subjects(settings: Settings, subjects: list[dict[str, Any]],
                           *, removed_at: str | None = None) -> list[dict[str, Any]]:
    """Persistently hide selected boards/indexes from the user's local library."""
    if not subjects:
        raise ValueError("请选择需要删除的板块")
    observed = removed_at or datetime.now(timezone.utc).isoformat(timespec="seconds")
    cleaned = []
    seen: set[tuple[str, str, str]] = set()
    for subject in subjects:
        universe_type = str(subject.get("universe_type") or "hot_board")
        source_id = str(subject.get("source_id") or SOURCE_ID).strip()
        code = str(subject.get("code") or "").strip()
        name = str(subject.get("name") or "").strip()
        if universe_type not in REMOVABLE_UNIVERSE_TYPES:
            raise ValueError("只能删除板块库中的板块或指数")
        writable_source_id(source_id, field="source_id")
        if not re.fullmatch(r"[0-9A-Z]{6}", code):
            raise ValueError("板块或指数身份无效")
        key = (universe_type, source_id, code)
        if key in seen:
            continue
        cleaned.append({"universe_type": universe_type, "source_id": source_id,
                        "code": code, "name": name, "removed_at": observed})
        seen.add(key)
    migrate(settings)
    with connection_scope(settings) as connection:
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.executemany(
                """INSERT INTO sector_removed_board(universe_type,source_id,code,removed_at)
                   VALUES (?,?,?,?)
                   ON CONFLICT(universe_type,source_id,code) DO UPDATE SET
                   removed_at=excluded.removed_at""",
                [(item["universe_type"], item["source_id"], item["code"], item["removed_at"])
                 for item in cleaned],
            )
            hot = [(item["source_id"], item["code"]) for item in cleaned
                   if item["universe_type"] == "hot_board"]
            if hot:
                connection.executemany(
                    "DELETE FROM sector_custom_board WHERE source_id=? AND code=?",
                    hot,
                )
                connection.executemany(
                    "DELETE FROM sector_heat_member WHERE source_id=? AND code=?",
                    hot,
                )
                connection.executemany(
                    "DELETE FROM sector_membership_snapshot WHERE board_source_id=? AND board_code=?",
                    hot,
                )
                connection.executemany(
                    "DELETE FROM sector_membership WHERE board_source_id=? AND board_code=?",
                    hot,
                )
                stats = connection.execute(
                    "SELECT COUNT(*) AS total, SUM(collection_error IS NULL) AS ok, "
                    "SUM(collection_error IS NOT NULL) AS failed FROM sector_heat_member"
                ).fetchone()
                total = int(stats["total"] or 0)
                ok = int(stats["ok"] or 0)
                failed = int(stats["failed"] or 0)
                connection.execute(
                    """UPDATE sector_heat_snapshot SET requested_count=?, catalog_count=?,
                       collected_count=?, failed_count=? WHERE id=1""",
                    (total, total, ok, failed),
                )
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise
    return cleaned


def record_sector_heat_failure(settings: Settings, *, attempted_at: str, error: str) -> None:
    """Keep the last ranking/data intact and expose the failed refresh separately."""
    migrate(settings)
    with connection_scope(settings) as connection:
        connection.execute(
            """INSERT INTO sector_heat_snapshot(id,last_attempt_at,last_error) VALUES(1,?,?)
               ON CONFLICT(id) DO UPDATE SET last_attempt_at=excluded.last_attempt_at,
               last_error=excluded.last_error""", (attempted_at, error),
        )


def list_custom_boards(settings: Settings) -> list[dict[str, Any]]:
    """Return manually added Tonghuashun boards that join normal refreshes."""
    migrate(settings)
    with connection_scope(settings) as connection:
        rows = connection.execute(
            """SELECT source_id,code,name,kind,added_at,ths_category,ths_page_code,ths_market_code,
                      ths_prefix,history_source_id,history_source_code
               FROM sector_custom_board
               ORDER BY added_at, code"""
        ).fetchall()
    return [dict(row) for row in rows]


def list_manual_index_projections_as_ths_boards(settings: Settings) -> list[dict[str, Any]]:
    """Return manually watched index identities that should be retried as Tonghuashun boards."""
    migrate(settings)
    with connection_scope(settings) as connection:
        rows = connection.execute(
            """SELECT i.code,i.name,i.source_id AS original_source_id
               FROM market_index_projection i
               LEFT JOIN fund_market_relation r
                 ON r.index_code=i.code AND r.relation_type='tracked_index'
               WHERE r.index_code IS NULL
               ORDER BY i.updated_at, i.code"""
        ).fetchall()
    removed = removed_subject_keys(settings)
    boards = []
    for row in rows:
        code = str(row["code"])
        source_id = str(row["original_source_id"])
        if ("tracked_index", source_id, code) in removed:
            continue
        boards.append({
            "code": code,
            "name": str(row["name"]),
            "kind": "行业" if code.startswith("881") else "指数" if code.startswith(("93", "39")) else "概念",
            "source_id": SOURCE_ID,
            "legacy_index_source_id": source_id,
        })
    return boards


def add_custom_boards(settings: Settings, boards: list[dict[str, Any]], *, added_at: str | None = None) -> list[dict[str, Any]]:
    """Persist user-selected Tonghuashun boards without fabricating market data."""
    if not boards:
        raise ValueError("请选择至少一个板块")
    observed = added_at or datetime.now(timezone.utc).isoformat(timespec="seconds")
    cleaned = []
    seen: set[tuple[str, str]] = set()
    for board in boards:
        source_id = str(board.get("source_id") or SOURCE_ID).strip()
        code = str(board.get("code") or "").strip().upper()
        name = str(board.get("name") or "").strip()
        kind = str(board.get("kind") or "").strip()
        writable_source_id(source_id, field="source_id")
        if not re.fullmatch(THS_CODE_PATTERN, code) or not name or kind not in ("行业", "概念", "指数", "ETF"):
            raise ValueError("板块身份无效")
        ths_category = str(board.get("ths_category") or "").strip() or None
        ths_page_code = str(board.get("ths_page_code") or "").strip() or None
        ths_market_code = str(board.get("ths_market_code") or "").strip() or None
        ths_prefix = str(board.get("ths_prefix") or "").strip() or None
        history_source_id = str(board.get("history_source_id") or "").strip() or None
        history_source_code = str(board.get("history_source_code") or "").strip() or None
        if ths_category is not None and ths_category not in ("industry", "concept", "index", "etf"):
            raise ValueError("同花顺板块类别无效")
        for value in (ths_page_code, ths_market_code):
            if value is not None and not re.fullmatch(THS_CODE_PATTERN, value):
                raise ValueError("同花顺板块代码无效")
        if ths_prefix is not None and ths_prefix not in ("bk", "zs", "hs", "16", "17", "33"):
            raise ValueError("同花顺行情前缀无效")
        if history_source_id is not None:
            writable_source_id(history_source_id, field="history_source_id")
        if history_source_code is not None and not re.fullmatch(THS_HISTORY_SOURCE_PATTERN, history_source_code):
            raise ValueError("同花顺行情代码无效")
        key = (source_id, code)
        if key not in seen:
            cleaned.append({
                "source_id": source_id, "code": code, "name": name, "kind": kind, "added_at": observed,
                "ths_category": ths_category, "ths_page_code": ths_page_code,
                "ths_market_code": ths_market_code, "ths_prefix": ths_prefix,
                "history_source_id": history_source_id, "history_source_code": history_source_code,
            })
            seen.add(key)
    migrate(settings)
    with connection_scope(settings) as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            connection.executemany(
                """INSERT INTO sector_custom_board
                   (source_id,code,name,kind,added_at,ths_category,ths_page_code,ths_market_code,
                    ths_prefix,history_source_id,history_source_code)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(source_id,code) DO UPDATE SET
                   name=excluded.name,kind=excluded.kind,
                   ths_category=excluded.ths_category,
                   ths_page_code=excluded.ths_page_code,
                   ths_market_code=excluded.ths_market_code,
                   ths_prefix=excluded.ths_prefix,
                   history_source_id=excluded.history_source_id,
                   history_source_code=excluded.history_source_code""",
                [(
                    item["source_id"], item["code"], item["name"], item["kind"], item["added_at"],
                    item["ths_category"], item["ths_page_code"], item["ths_market_code"], item["ths_prefix"],
                    item["history_source_id"], item["history_source_code"],
                ) for item in cleaned],
            )
            connection.executemany(
                "DELETE FROM sector_removed_board WHERE universe_type='hot_board' AND source_id=? AND code=?",
                [(item["source_id"], item["code"]) for item in cleaned],
            )
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise
    return cleaned


def save_sector_heat(settings: Settings, *, as_of: str, updated_at: str,
                     catalog_count: int, members: list[dict[str, Any]],
                     requested_count: int = 100, universe_scope: str = "hot_board_top100",
                     directory_provenance: dict[str, Any] | None = None) -> None:
    """Publish a complete ranking plus each member's actual collection outcome."""
    try:
        for item in members:
            writable_source_id(item.get("source_id", SOURCE_ID), field="source_id")
            if item.get("history_source_id") is not None:
                writable_source_id(item["history_source_id"], field="history_source_id")
    except (AttributeError, SourceAdmissionError) as exc:
        raise AppError("sector_source_not_allowed", "板块行情来源不允许写入", 422,
                       {"reason": str(exc)}) from exc
    limit = 20000 if universe_scope == "verified_industry_all" else 100
    if (universe_scope not in ("verified_industry_all", "hot_board_top100")
            or not isinstance(requested_count, int) or isinstance(requested_count, bool)
            or not 0 < requested_count <= limit
            or not members or len(members) > limit
            or len({item["code"] for item in members}) != len(members)):
        raise ValueError("热门板块快照成员无效")
    if directory_provenance is not None and (
            not isinstance(directory_provenance, dict)
            or not isinstance(directory_provenance.get("directory"), dict)
            or directory_provenance["directory"].get("mode") not in ("live", "seed")):
        raise ValueError("行业目录来源证明无效")
    provenance_json = (json.dumps(directory_provenance, ensure_ascii=False, separators=(",", ":"),
                                  allow_nan=False) if directory_provenance is not None else None)
    migrate(settings)
    failures = sum(item["collection_error"] is not None for item in members)
    with connection_scope(settings) as connection:
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing_by_code = {
                row["code"]: dict(row)
                for row in connection.execute(
                    """SELECT code,source_id,history_source_id,history_source_code,rows_json
                       FROM sector_heat_member"""
                ).fetchall()
            }
            for item in members:
                existing = existing_by_code.get(item["code"])
                if existing is not None:
                    _assert_saved_history_non_regression(existing, item)
            connection.execute(
                """INSERT INTO sector_heat_snapshot
                   (id,as_of,updated_at,requested_count,catalog_count,collected_count,failed_count,last_attempt_at,last_error,universe_scope,directory_provenance_json)
                   VALUES (1,?,?,?,?,?,?,?,NULL,?,?)
                   ON CONFLICT(id) DO UPDATE SET as_of=excluded.as_of,updated_at=excluded.updated_at,
                   requested_count=excluded.requested_count,
                   catalog_count=excluded.catalog_count,collected_count=excluded.collected_count,
                   failed_count=excluded.failed_count,last_attempt_at=excluded.last_attempt_at,last_error=NULL,
                   universe_scope=excluded.universe_scope,
                   directory_provenance_json=excluded.directory_provenance_json""",
                (as_of, updated_at, requested_count, catalog_count, len(members) - failures, failures, updated_at,
                 universe_scope, provenance_json),
            )
            connection.execute("DELETE FROM sector_heat_member")
            connection.executemany(
                """INSERT INTO sector_heat_member
                   (code,name,kind,heat_rank,heat_value,heat_updated_at,source_id,updated_at,collection_error,rows_json,
                    history_source_id,history_source_code,history_identity_match)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                [(item["code"], item["name"], item["kind"], item["heat_rank"], item["heat_value"],
                  item["heat_updated_at"], item.get("source_id", SOURCE_ID), item["updated_at"], item["collection_error"],
                  json.dumps(item["rows"], ensure_ascii=False, separators=(",", ":")),
                  item.get("history_source_id"), item.get("history_source_code"),
                  item.get("history_identity_match")) for item in members],
            )
            connection.executemany(
                """INSERT INTO sector_identity
                   (source_id,source_code,name,kind,taxonomy_id,taxonomy_revision,observed_at)
                   VALUES (?,?,?,?,?,?,?)
                   ON CONFLICT(source_id,source_code) DO UPDATE SET
                   name=excluded.name,kind=excluded.kind,taxonomy_revision=excluded.taxonomy_revision,
                   observed_at=excluded.observed_at""",
                [(item.get("source_id", SOURCE_ID), item["code"], item["name"], item["kind"], "industry_board",
                  as_of, updated_at) for item in members],
            )
            connection.execute(
                """INSERT OR IGNORE INTO sector_taxonomy_revision
                   (taxonomy_id,revision,provider,as_of,observed_at) VALUES (?,?,?,?,?)""",
                ("industry_board", as_of, SOURCE_POLICY["provider"], as_of, updated_at),
            )
            connection.executemany(
                """INSERT OR IGNORE INTO sector_identity_revision
                   (source_id,source_code,taxonomy_id,taxonomy_revision,name,kind)
                   VALUES (?,?,?,?,?,?)""",
                [(item.get("source_id", SOURCE_ID), item["code"], "industry_board", as_of, item["name"], item["kind"])
                 for item in members],
            )
            for item in members:
                if item["kind"] == "ETF":
                    continue
                source_id = item.get("source_id", SOURCE_ID)
                component_error = item.get("constituent_error")
                components = item.get("constituents", [])
                connection.execute(
                    """INSERT INTO sector_membership_snapshot
                       (board_source_id,board_code,as_of,fetched_at,member_count,status,error)
                       VALUES (?,?,?,?,?,?,?)
                       ON CONFLICT(board_source_id,board_code,as_of) DO UPDATE SET
                       fetched_at=excluded.fetched_at,member_count=excluded.member_count,
                       status=excluded.status,error=excluded.error""",
                    (source_id, item["code"], as_of, item.get("constituent_updated_at") if component_error else updated_at,
                     len(components), "failed" if component_error else "ready", component_error),
                )
                connection.execute(
                    "DELETE FROM sector_membership WHERE board_source_id=? AND board_code=? AND as_of=?",
                    (source_id, item["code"], as_of),
                )
                connection.executemany(
                    """INSERT INTO sector_membership
                       (board_source_id,board_code,as_of,stock_code,stock_name,market,source_order,market_cap,weight)
                       VALUES (?,?,?,?,?,?,?,?,NULL)""",
                    [(source_id, item["code"], as_of, row["stock_code"], row["stock_name"],
                      row["market"], row["source_order"], row.get("market_cap")) for row in components],
                )
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise
