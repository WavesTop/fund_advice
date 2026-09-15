from contextlib import asynccontextmanager
from threading import Lock

from fastapi import FastAPI, Query

from backend.core.config import Settings
from backend.core.errors import AppError, app_error_handler, unexpected_error_handler
from backend.storage.database import connection_scope, migrate, sqlite_runtime_info
from backend.storage.catalog import list_catalog
from backend.storage.timeseries import get_fund, get_timeseries
from scripts.import_fund_timeseries import refresh_fund_timeseries
from backend.storage.related_market import get_related_market
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

    @app.get("/api/funds/{code}")
    def fund(code: str) -> dict[str, object]:
        return {"fund": get_fund(resolved, code), "series": get_timeseries(resolved, code),
                "related_market": get_related_market(resolved, code)}

    @app.get("/api/funds/{code}/series")
    def fund_series(code: str) -> dict[str, object]:
        return get_timeseries(resolved, code)

    @app.post("/api/funds/{code}/refresh")
    def refresh_fund(code: str) -> dict[str, object]:
        lock = _refresh_lock(code)
        if not lock.acquire(blocking=False):
            raise AppError("fund_refresh_in_progress", "该基金正在采集中，请稍后再试", 409)
        try:
            try:
                refresh_fund_timeseries(resolved, code)
                refresh_related_market(resolved, code)
            except AppError:
                raise
            except ValueError as exc:
                message = str(exc)
                if "不存在该代码" in message:
                    raise AppError("fund_not_found", "未找到该基金", 404) from exc
                raise AppError("fund_refresh_invalid", message, 422) from exc
            except Exception as exc:
                raise AppError("fund_refresh_failed", "基金真实数据采集失败，请稍后重试", 502) from exc
            return {"fund": get_fund(resolved, code), "series": get_timeseries(resolved, code),
                    "related_market": get_related_market(resolved, code)}
        finally:
            lock.release()

    return app


app = create_app()
