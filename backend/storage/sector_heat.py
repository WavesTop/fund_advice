"""Current Eastmoney board snapshot, isolated from verified fund/index relations.

Only market context is supported. Turnover ranks measure traded amount, not
popularity, investment merit, fund exposure, or a recommendation score.
"""
from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

from backend.core.config import Settings
from backend.storage.database import connection_scope, migrate

DIRECTORY_URL = "https://emdatah5.eastmoney.com/dc/ZJLX/getZDYLBData"
HISTORY_URL = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
CONSTITUENT_URL = DIRECTORY_URL
SOURCE_ID = "sector_daily.eastmoney"
SOURCE_POLICY = {
    "usage": "market_context_only",
    "provider": "东方财富",
    "classification": "按来源平台的行业与概念分类，排除地区；概念可包含融资融券等资格集合，板块存在重叠。",
    "heat_definition": "完整读取行业和概念目录后，按来源成交额（元）降序选前100名；同额按代码排序，不是人气或涨幅榜。",
    "timing": "排名日期取来源更新时间；分页按同一交易日核对，不声称各页构成同一瞬时的市场快照。",
    "price_basis": "优先读取东方财富板块日线；不可用时，仅对分类一致、名称严格一致且标准代码已解析的板块采用同花顺日线。不复权；成交额为元，成交量保留来源单位。",
    "limitations": "仅供本地市场背景观察；成分股来自来源当前板块定义，来源未提供官方权重；不作为国证或中证指数，不推定基金映射，不晋升正式基金推荐。",
    "refresh": "人工运行采集命令刷新；榜单和成分来自东方财富移动资金页，只有当前快照，不是历史时点数据库。",
}


def read_sector_heat(settings: Settings) -> dict[str, Any]:
    """Read the universe and all members from one SQLite read transaction."""
    migrate(settings)
    with connection_scope(settings) as connection:
        try:
            connection.execute("BEGIN")
            snapshot = connection.execute("SELECT * FROM sector_heat_snapshot WHERE id=1").fetchone()
            members = connection.execute("SELECT * FROM sector_heat_member ORDER BY heat_rank").fetchall()
            membership_snapshots = connection.execute(
                """SELECT * FROM sector_membership_snapshot
                   WHERE board_source_id=? AND as_of=?""",
                (SOURCE_ID, snapshot["as_of"] if snapshot else None),
            ).fetchall()
            memberships = connection.execute(
                """SELECT * FROM sector_membership
                   WHERE board_source_id=? AND as_of=?
                   ORDER BY board_code, source_order""",
                (SOURCE_ID, snapshot["as_of"] if snapshot else None),
            ).fetchall()
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise
    state = dict(snapshot) if snapshot else {}
    membership_state = {row["board_code"]: dict(row) for row in membership_snapshots}
    membership_rows: dict[str, list[dict[str, Any]]] = {}
    for row in memberships:
        component = dict(row)
        for key in ("board_source_id", "board_code", "as_of"):
            component.pop(key)
        component["market_cap"] = float(Decimal(component["market_cap"])) if component["market_cap"] is not None else None
        component["weight"] = float(Decimal(component["weight"])) if component["weight"] is not None else None
        component["market_cap_status"] = "available" if component["market_cap"] is not None else "unavailable"
        membership_rows.setdefault(row["board_code"], []).append(component)
    items = []
    for member in members:
        item = dict(member)
        item.pop("snapshot_id")
        item["rows"] = json.loads(item.pop("rows_json"))
        # Decimal text remains authoritative in storage; floats are display only.
        item["heat_value"] = float(Decimal(item["heat_value"]))
        item["history_relation"] = ("proxy_not_equivalent" if item.get("history_source_id") == "sector_daily.ths"
                                    else "native_source")
        item.update(universe_type="hot_board", funds=[], use_scope="market_context_only",
                    amount_unit="CNY", volume_unit="source_native",
                    history_source_url=(HISTORY_URL if item.get("history_source_id") != "sector_daily.ths"
                                        else "https://d.10jqka.com.cn/v4/line/"))
        membership = membership_state.get(item["code"])
        item["membership"] = {
            "status": membership["status"] if membership else "missing",
            "as_of": membership["as_of"] if membership else None,
            "fetched_at": membership["fetched_at"] if membership else None,
            "member_count": membership["member_count"] if membership else 0,
            "error": membership["error"] if membership else "尚未采集成分股快照。",
            "weight_basis": "not_provided",
            "members": membership_rows.get(item["code"], []),
            "market_cap_missing_count": sum(row["market_cap"] is None for row in membership_rows.get(item["code"], [])),
            "quality_note": "市值为可选资料；缺失不删除证券，也不等于权重为零。",
        }
        items.append(item)
    status = "empty" if not items else (
        "ready" if len(items) == state.get("requested_count") and not state.get("failed_count")
        and not state.get("last_error") else "partial"
    )
    industry_only = state.get("universe_scope") == "verified_industry_all"
    universe = {
        "label": ("可核验日线的全部行业板块" if industry_only else "东方财富行业与概念成交额前100"),
        "heat_basis": "turnover_amount",
        "as_of": state.get("as_of"),
        "ranking_as_of": state.get("as_of"),
        "updated_at": state.get("updated_at"),
        "requested_count": state.get("requested_count", 100),
        "member_count": len(items),
        "catalog_count": state.get("catalog_count", 0),
        "collected_count": state.get("collected_count", 0),
        "failed_count": state.get("failed_count", 0),
        "source_url": DIRECTORY_URL,
        "status": status,
        "scope": state.get("universe_scope", "hot_board_top100"),
        "last_attempt_at": state.get("last_attempt_at"),
        "last_error": state.get("last_error"),
        "source_policy": dict(SOURCE_POLICY),
    }
    if industry_only:
        universe["source_policy"].update(
            classification="完整读取东方财富行业层级目录，只纳入能与同花顺标准行业严格同名并取得日线的行业；不含概念、风格、地区和资格集合。",
            heat_definition="行业按东方财富完整行业目录中的当日成交额排序；热度名次保留其在完整行业目录中的原始位置。",
        )
    return {"universe": universe, "items": items}


def record_sector_heat_failure(settings: Settings, *, attempted_at: str, error: str) -> None:
    """Keep the last ranking/data intact and expose the failed refresh separately."""
    migrate(settings)
    with connection_scope(settings) as connection:
        connection.execute(
            """INSERT INTO sector_heat_snapshot(id,last_attempt_at,last_error) VALUES(1,?,?)
               ON CONFLICT(id) DO UPDATE SET last_attempt_at=excluded.last_attempt_at,
               last_error=excluded.last_error""", (attempted_at, error),
        )


def save_sector_heat(settings: Settings, *, as_of: str, updated_at: str,
                     catalog_count: int, members: list[dict[str, Any]],
                     requested_count: int = 100, universe_scope: str = "hot_board_top100") -> None:
    """Publish a complete ranking plus each member's actual collection outcome."""
    limit = 20000 if universe_scope == "verified_industry_all" else 100
    if (universe_scope not in ("verified_industry_all", "hot_board_top100")
            or not isinstance(requested_count, int) or isinstance(requested_count, bool)
            or not 0 < requested_count <= limit
            or not members or len(members) > limit
            or len({item["code"] for item in members}) != len(members)):
        raise ValueError("热门板块快照成员无效")
    migrate(settings)
    failures = sum(item["collection_error"] is not None for item in members)
    with connection_scope(settings) as connection:
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """INSERT INTO sector_heat_snapshot
                   (id,as_of,updated_at,requested_count,catalog_count,collected_count,failed_count,last_attempt_at,last_error,universe_scope)
                   VALUES (1,?,?,?,?,?,?,?,NULL,?)
                   ON CONFLICT(id) DO UPDATE SET as_of=excluded.as_of,updated_at=excluded.updated_at,
                   requested_count=excluded.requested_count,
                   catalog_count=excluded.catalog_count,collected_count=excluded.collected_count,
                   failed_count=excluded.failed_count,last_attempt_at=excluded.last_attempt_at,last_error=NULL,
                   universe_scope=excluded.universe_scope""",
                (as_of, updated_at, requested_count, catalog_count, len(members) - failures, failures, updated_at,
                 universe_scope),
            )
            connection.execute("DELETE FROM sector_heat_member")
            connection.executemany(
                """INSERT INTO sector_heat_member
                   (code,name,kind,heat_rank,heat_value,heat_updated_at,source_id,updated_at,collection_error,rows_json,
                    history_source_id,history_source_code,history_identity_match)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                [(item["code"], item["name"], item["kind"], item["heat_rank"], item["heat_value"],
                  item["heat_updated_at"], SOURCE_ID, item["updated_at"], item["collection_error"],
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
                [(SOURCE_ID, item["code"], item["name"], item["kind"], "eastmoney_board",
                  as_of, updated_at) for item in members],
            )
            connection.execute(
                """INSERT OR IGNORE INTO sector_taxonomy_revision
                   (taxonomy_id,revision,provider,as_of,observed_at) VALUES (?,?,?,?,?)""",
                ("eastmoney_board", as_of, "东方财富", as_of, updated_at),
            )
            connection.executemany(
                """INSERT OR IGNORE INTO sector_identity_revision
                   (source_id,source_code,taxonomy_id,taxonomy_revision,name,kind)
                   VALUES (?,?,?,?,?,?)""",
                [(SOURCE_ID, item["code"], "eastmoney_board", as_of, item["name"], item["kind"])
                 for item in members],
            )
            for item in members:
                component_error = item.get("constituent_error")
                components = item.get("constituents", [])
                connection.execute(
                    """INSERT INTO sector_membership_snapshot
                       (board_source_id,board_code,as_of,fetched_at,member_count,status,error)
                       VALUES (?,?,?,?,?,?,?)
                       ON CONFLICT(board_source_id,board_code,as_of) DO UPDATE SET
                       fetched_at=excluded.fetched_at,member_count=excluded.member_count,
                       status=excluded.status,error=excluded.error""",
                    (SOURCE_ID, item["code"], as_of, item.get("constituent_updated_at") if component_error else updated_at,
                     len(components), "failed" if component_error else "ready", component_error),
                )
                connection.execute(
                    "DELETE FROM sector_membership WHERE board_source_id=? AND board_code=? AND as_of=?",
                    (SOURCE_ID, item["code"], as_of),
                )
                connection.executemany(
                    """INSERT INTO sector_membership
                       (board_source_id,board_code,as_of,stock_code,stock_name,market,source_order,market_cap,weight)
                       VALUES (?,?,?,?,?,?,?,?,NULL)""",
                    [(SOURCE_ID, item["code"], as_of, row["stock_code"], row["stock_name"],
                      row["market"], row["source_order"], row.get("market_cap")) for row in components],
                )
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise
