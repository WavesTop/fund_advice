from contextlib import asynccontextmanager
from threading import Lock
from typing import Literal

from fastapi import FastAPI, Query

from backend.core.config import Settings
from backend.integrations.market_refresh import refresh_market_data
from backend.core.errors import AppError, app_error_handler, unexpected_error_handler
from backend.storage.database import connection_scope, migrate, sqlite_runtime_info
from backend.storage.catalog import list_catalog
from backend.storage.timeseries import get_fund, get_timeseries
from scripts.import_fund_timeseries import refresh_fund_timeseries
from backend.storage.related_market import get_related_markets
from backend.storage.market_series import market_series
from scripts.import_related_market import refresh_related_market
from backend.analysis.sector_status import sector_opportunities


_refresh_locks: dict[str, Lock] = {}
_refresh_locks_guard = Lock()


def _refresh_lock(code: str) -> Lock:
    with _refresh_locks_guard:
        return _refresh_locks.setdefault(code, Lock())


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        migrate(resolved)
        yield

    app = FastAPI(title=resolved.app_name, lifespan=lifespan)
    app.add_exception_handler(AppError, app_error_handler)
    app.add_exception_handler(Exception, unexpected_error_handler)

    @app.get("/health")
    def health() -> dict[str, object]:
        with connection_scope(resolved) as connection:
            return {"status": "ok", "schema_version": migrate(resolved), "sqlite": sqlite_runtime_info(connection)}

    @app.get("/api/funds")
    def funds(q: str = Query(default=""), page: int = Query(default=1), page_size: int = Query(default=20)) -> dict[str, object]:
        return list_catalog(resolved, q, page, page_size)

    @app.get("/api/sectors/opportunities")
    def opportunities() -> dict[str, object]:
        return sector_opportunities(resolved)

    def detail(code: str) -> dict[str, object]:
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

    @app.post("/api/funds/catalog/refresh")
    def refresh_catalog() -> dict[str, object]:
        return refresh_market_data(resolved, "catalog")

    @app.post("/api/sectors/refresh")
    def refresh_sectors() -> dict[str, object]:
        return refresh_market_data(resolved, "sectors")

    @app.get("/api/sectors/{code}")
    def sector_detail(code: str, source_id: str, universe_type: Literal["hot_board", "tracked_index"]) -> dict[str, object]:
        result = sector_opportunities(resolved)
        matched = [item for item in result["items"] if item["code"] == code
                   and item["source_id"] == source_id and item["universe_type"] == universe_type]
        if len(matched) != 1:
            raise AppError("sector_not_found", "未找到该来源与研究范围下的真实板块；未回退到演示数据", 404)
        return {"item": matched[0], "generated_at": result["generated_at"], "method_version": result["method_version"]}

    @app.post("/api/funds/{code}/refresh")
    def refresh_fund(code: str) -> dict[str, object]:
        get_fund(resolved, code)
        lock = _refresh_lock(code)
        if not lock.acquire(blocking=False):
            raise AppError("fund_refresh_in_progress", "该基金正在采集中，请稍后再试", 409)
        try:
            stages = {}
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
                raise AppError("fund_refresh_failed", "本次更新未取得可发布数据，旧资料已保留；关系状态以最近核验为准", 502, {"refresh": refresh_state, "current": detail(code)})
            return {**detail(code), "refresh": refresh_state}
        finally:
            lock.release()

    return app


app = create_app()
