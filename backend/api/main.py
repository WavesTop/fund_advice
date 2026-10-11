from contextlib import asynccontextmanager
from threading import Lock
from typing import Literal

from fastapi import Body, FastAPI, Query

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
            return {"status": "ok", "api_contract": "market-workbench-v2",
                    "capabilities": ["fund_browse_codes", "sector_refresh_then_evaluate", "point_in_time_research_v1",
                                     "portfolio_ledger_v1", "strategy_execution_v1",
                                     "strategy_atomic_execution_v2"],
                    "schema_version": migrate(resolved), "sqlite": sqlite_runtime_info(connection)}

    @app.get("/api/funds")
    def funds(q: str = Query(default=""), page: int = Query(default=1), page_size: int = Query(default=20),
              codes: str | None = Query(default=None, max_length=419)) -> dict[str, object]:
        return list_catalog(resolved, q, page, page_size, codes=codes)

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

    @app.get("/api/sectors/collection-status")
    def sector_collection_status() -> dict[str, object]:
        from backend.analysis.sector_status import _current_collection_status
        return _current_collection_status(resolved)

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

    def run_sector_search(q: str) -> dict[str, object]:
        from scripts.import_sector_heat import search_ths_boards
        try:
            ths = search_ths_boards(q)
        except ValueError as exc:
            raise AppError("sector_search_invalid", str(exc), 422) from exc
        except Exception as exc:
            return {"items": [], "source_attempts": [], "errors": [str(exc)]}
        items = ths.get("items", [])
        attempts = ths.get("source_attempts", [])
        errors = ths.get("errors", [])
        seen: set[tuple[str, str, str]] = set()
        deduped = []
        for item in items:
            if not isinstance(item, dict):
                continue
            key = (str(item.get("universe_type") or "hot_board"),
                   str(item.get("source_id") or ""), str(item.get("code") or ""))
            if key in seen:
                continue
            seen.add(key)
            deduped.append(item)
        if not deduped and errors and not attempts:
            raise AppError("sector_search_failed", "同花顺板块搜索失败，请稍后重试", 502,
                           {"reason": "；".join(errors)[:1000]})
        return {"items": deduped[:30], "source_attempts": attempts, "errors": errors}

    @app.get("/api/sector-board-search")
    def search_sector_boards(q: str = Query(min_length=1, max_length=40)) -> dict[str, object]:
        return run_sector_search(q)

    @app.get("/api/sectors/search")
    def search_sectors(q: str = Query(min_length=1, max_length=40)) -> dict[str, object]:
        return run_sector_search(q)

    @app.post("/api/sectors/custom")
    def add_custom_sectors(payload: dict[str, object] = Body(...)) -> dict[str, object]:
        from backend.storage.sector_heat import add_custom_boards, remove_sector_subjects
        from backend.storage.collection_runs import finish_run, record_progress, start_run
        from scripts.import_sector_heat import refresh_selected_ths_boards
        from scripts.import_related_market import refresh_selected_csi_indexes
        items = payload.get("items")
        if not isinstance(items, list):
            raise AppError("sector_custom_invalid", "请选择要添加的板块", 422)
        raw_items = [item for item in items if isinstance(item, dict)]
        if not raw_items:
            raise AppError("sector_custom_invalid", "请选择要添加的板块或指数", 422)
        board_items = [item for item in raw_items if item.get("universe_type", "hot_board") == "hot_board"]
        index_items = [item for item in raw_items if item.get("universe_type") == "tracked_index"]
        if len(board_items) + len(index_items) != len(raw_items):
            raise AppError("sector_custom_invalid", "请选择可添加的板块或指数", 422)
        try:
            added = add_custom_boards(resolved, board_items) if board_items else []
            if added:
                legacy_index_items = [
                    {"universe_type": "tracked_index", "source_id": source_id,
                     "code": board["code"], "name": board["name"]}
                    for board in added
                    for source_id in ("index_daily.csi_official", "index_daily.sina")
                ]
                remove_sector_subjects(resolved, legacy_index_items)
        except ValueError as exc:
            raise AppError("sector_custom_invalid", str(exc), 422) from exc
        run_id = start_run(resolved, "sectors")
        terminal_state, terminal_result = "failed", {"message": "添加板块未完成"}
        try:
            record_progress(resolved, run_id, {"stage": "market", "state": "running", "percent": 5,
                                               "message": "正在准备采集新增板块。"})
            board_refresh = {"items": []}
            index_refresh = {"items": []}
            if added:
                board_refresh = refresh_selected_ths_boards(
                    resolved, added,
                    progress=lambda event: record_progress(resolved, run_id, event),
                )
            if index_items:
                index_refresh = refresh_selected_csi_indexes(
                    resolved, index_items,
                    progress=lambda event: record_progress(resolved, run_id, event),
                )
            updated = [item for item in board_refresh["items"]
                       if any(item["code"] == board["code"] for board in added)]
            index_updated = list(index_refresh["items"])
            failed = sum(bool(item.get("collection_error")) for item in updated)
            failed += sum(item.get("status") == "failed" for item in index_updated)
            updated_count = len(updated) + len(index_updated)
            status = "success" if updated_count and failed == 0 else "partial" if updated_count else "failed"
            terminal_state = status
            terminal_result = {"target": "sectors", "status": status,
                               "message": f"新增板块/指数 {updated_count} 个；日线失败 {failed} 个。",
                               "updated_codes": [item["code"] for item in updated + index_updated]}
            evaluation = sector_opportunities(resolved)
            return {"added": added + index_items, "refresh": terminal_result, "evaluation": evaluation,
                    "api_contract": "market-workbench-v2", "collection_run_id": run_id}
        except Exception as exc:
            terminal_result = {"target": "sectors", "status": "failed",
                               "message": f"新增板块采集未完成：{type(exc).__name__}: {str(exc)[:1000]}"}
            raise AppError("sector_custom_refresh_failed", terminal_result["message"], 502,
                           {"refresh": terminal_result}) from exc
        finally:
            finish_run(resolved, run_id, terminal_state, terminal_result)

    @app.post("/api/sectors/remove")
    def remove_custom_sectors(payload: dict[str, object] = Body(...)) -> dict[str, object]:
        from backend.storage.sector_heat import remove_sector_subjects
        items = payload.get("items")
        if not isinstance(items, list):
            raise AppError("sector_remove_invalid", "请选择需要删除的板块", 422)
        raw_items = [item for item in items if isinstance(item, dict)]
        if not raw_items:
            raise AppError("sector_remove_invalid", "请选择需要删除的板块", 422)
        try:
            removed = remove_sector_subjects(resolved, raw_items)
        except ValueError as exc:
            raise AppError("sector_remove_invalid", str(exc), 422) from exc
        evaluation = sector_opportunities(resolved)
        return {"removed": removed, "evaluation": evaluation,
                "message": f"已从板块库移除 {len(removed)} 个板块/指数。",
                "api_contract": "market-workbench-v2"}

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
                    preserved = bool(isinstance(result, dict) and result.get("preserved"))
                    stages[name] = {
                        "status": "preserved" if preserved else "updated" if result is not None else "unresolved",
                        "message": (str(result.get("message") or "已保留更高等级的正式数据") if preserved else
                                    "更新成功" if result is not None else "未取得唯一的跟踪关系，旧关系待核验。"),
                    }
                except Exception as exc:
                    stages[name] = {"status": "failed", "message": str(exc)[:1000] or type(exc).__name__}
            completed = sum(stage["status"] in {"updated", "preserved"} for stage in stages.values())
            refresh_state = {"status": "success" if completed == 2 else "partial" if completed else "failed", "stages": stages}
            if not completed:
                raise AppError("fund_refresh_failed", "本次更新未取得可发布数据，旧资料已保留；关系状态以最近核验为准", 502, {"refresh": refresh_state, "current": detail(code)})
            return {**detail(code), "refresh": refresh_state}
        finally:
            lock.release()

    from backend.api.research import research_router
    from backend.api.portfolio import portfolio_router
    from backend.api.strategy import strategy_router
    app.include_router(research_router(resolved))
    app.include_router(portfolio_router(resolved))
    app.include_router(strategy_router(resolved))
    return app


app = create_app()
