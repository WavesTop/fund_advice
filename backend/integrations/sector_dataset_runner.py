"""Bounded independent table jobs: one slow table cannot consume every job's budget."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import time
from typing import Callable


def run_datasets(keys: list[tuple[str, str]], load: Callable, *, budget_seconds: float = 240,
                 workers: int = 2, clock: Callable = time.monotonic,
                 progress: Callable | None = None) -> tuple[dict, list[dict]]:
    if not keys or len(set(keys)) != len(keys) or not 1 <= workers <= 2 or budget_seconds <= 0:
        raise ValueError("数据集、并发或时间预算无效")
    waves = (len(keys) + workers - 1) // workers
    per_job = budget_seconds / waves

    def collect(key: tuple[str, str]) -> tuple[dict, dict]:
        kind, business_date = key
        started = clock()
        if progress:
            progress({"stage": "financials", "dataset": kind,
                      "business_date": business_date, "state": "running"})
        try:
            dataset = load(kind, business_date, started + per_job)
        except Exception as exc:
            dataset = {"status": "failed", "rows": {}, "excluded": {},
                       "sources": getattr(exc, "sources", []),
                       "source_count": getattr(exc, "received_row_count", 0),
                       "error": f"{kind}/{business_date}: {type(exc).__name__}: {exc}"[:2000]}
        diagnostic = {"dataset": kind, "business_date": business_date,
                      "state": dataset["status"], "row_count": len(dataset["rows"]),
                      "source_count": dataset.get("source_count", 0),
                      "excluded_count": len(dataset["excluded"]),
                      "budget_seconds": per_job, "elapsed_seconds": round(clock() - started, 3),
                      "sources": dataset.get("sources", []), "error": dataset.get("error")}
        if progress:
            progress({"stage": "financials", **diagnostic})
        return dataset, diagnostic

    completed = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(collect, key): key for key in keys}
        for future in as_completed(futures):
            completed[futures[future]] = future.result()
    # Stable report ordering, irrespective of completion order.
    return ({key: completed[key][0] for key in keys}, [completed[key][1] for key in keys])
