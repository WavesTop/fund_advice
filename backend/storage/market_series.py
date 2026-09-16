"""Source-exact current series; a cross-provider proxy is not an equivalent index."""
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
