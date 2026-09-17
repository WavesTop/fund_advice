"""Local-only research tools. Write times are always supplied by the server."""
from __future__ import annotations
from typing import Annotated
from fastapi import APIRouter, Body, Query, Response
from pydantic import BaseModel, ConfigDict, Field

from backend.analysis.research_pipeline import get_run, replay_run, run_snapshot
from backend.analysis.validation import get_trial, register_forward, register_trial, score_forward, validate_history
from backend.core.config import Settings
from backend.core.errors import AppError
from backend.core.trading_calendar import get_calendar
from backend.integrations.research_inputs import capture_current_inputs
from backend.storage.collection_runs import runs
from backend.storage.database import connection_scope, migrate
from backend.storage.research import append_facts, freeze_snapshot, get_snapshot, list_snapshots, quarantine


class StrictInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SnapshotInput(StrictInput):
    subjects: list[str] = Field(min_length=1, max_length=200)
    cutoff: str | None = None
    purpose: str = "research"
    mode: str = "system_as_of"


class CaptureInput(StrictInput):
    subjects: list[str] | None = Field(default=None, min_length=1, max_length=200)


class IngestInput(StrictInput):
    facts: list[dict] = Field(min_length=1, max_length=50000)
    source_url: str = Field(max_length=4000)
    raw_text: str = Field(min_length=1, max_length=10 * 1024 * 1024)
    media_type: str = Field(default="text/plain; charset=utf-8", max_length=200)


class ReasonInput(StrictInput):
    reason: str = Field(min_length=1, max_length=500)


class ForwardInput(StrictInput):
    run_id: str = Field(min_length=1, max_length=64)


class OutcomeInput(StrictInput):
    outcome_snapshot_id: str = Field(min_length=1, max_length=64)


def _validated(action, *args, **kwargs):
    try:
        return action(*args, **kwargs)
    except ValueError as exc:
        raise AppError("research_input_invalid", str(exc), 422) from exc


def research_router(settings: Settings) -> APIRouter:
    router = APIRouter(prefix="/api/research", tags=["research"])

    @router.get("/status")
    def status():
        migrate(settings)
        with connection_scope(settings) as connection:
            counts = {table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                      for table in ("data_revision", "data_snapshot", "analysis_run", "validation_trial", "forward_observation", "validation_report")}
        return {"version": "research-pipeline-v1", "counts": counts, "snapshot_modes": ["system_as_of"],
                "calendar_years": sorted(get_calendar().years), "fund_scope": "same_benchmark_passive_only",
                "investment_effectiveness": "not_established", "operation_status": "unavailable"}

    @router.get("/collections")
    def collections(limit: int = Query(default=20, ge=1, le=100)):
        return {"items": runs(settings, limit)}

    @router.post("/facts")
    def ingest(body: IngestInput):
        return _validated(append_facts, settings, body.facts, source_url=body.source_url,
                          raw_body=body.raw_text.encode("utf-8"), media_type=body.media_type)

    @router.post("/facts/{revision_id}/quarantine")
    def quarantine_fact(revision_id: str, body: ReasonInput):
        _validated(quarantine, settings, revision_id, body.reason)
        return {"revision_id": revision_id, "status": "quarantined_new_snapshots_only"}

    @router.get("/assets/{asset_id}")
    def asset(asset_id: str):
        migrate(settings)
        with connection_scope(settings) as connection:
            row = connection.execute("SELECT body,body_hash FROM raw_asset WHERE asset_id=?", (asset_id,)).fetchone()
        if row is None:
            raise AppError("asset_not_found", "原始资料不存在", 404)
        return Response(content=row["body"], media_type="application/octet-stream", headers={"X-Content-SHA256": row["body_hash"]})

    @router.post("/capture")
    def capture(body: CaptureInput):
        result = _validated(capture_current_inputs, settings, body.subjects)
        run = _validated(run_snapshot, settings, result["snapshot_id"])
        return {**result, "run_id": run["run_id"], "status": "recorded_unvalidated"}

    @router.get("/snapshots")
    def snapshots(limit: int = Query(default=20, ge=1, le=100)):
        return {"items": list_snapshots(settings, limit)}

    @router.post("/snapshots")
    def snapshot(body: SnapshotInput):
        return _validated(freeze_snapshot, settings, **body.model_dump())

    @router.get("/snapshots/{snapshot_id}")
    def read_snapshot(snapshot_id: str):
        return get_snapshot(settings, snapshot_id)

    @router.post("/snapshots/{snapshot_id}/run")
    def run(snapshot_id: str):
        return _validated(run_snapshot, settings, snapshot_id)

    @router.get("/runs/{run_id}")
    def read_run(run_id: str):
        return get_run(settings, run_id)

    @router.post("/runs/{run_id}/replay")
    def replay(run_id: str):
        return _validated(replay_run, settings, run_id)

    @router.post("/trials")
    def trial(body: Annotated[dict, Body()]):
        return _validated(register_trial, settings, body)

    @router.get("/trials/{trial_id}")
    def read_trial(trial_id: str):
        return get_trial(settings, trial_id)

    @router.post("/trials/{trial_id}/forward")
    def forward(trial_id: str, body: ForwardInput):
        return _validated(register_forward, settings, trial_id, body.run_id)

    @router.post("/trials/{trial_id}/score")
    def score(trial_id: str, body: OutcomeInput):
        return _validated(score_forward, settings, trial_id, body.outcome_snapshot_id)

    @router.post("/trials/{trial_id}/history")
    def history(trial_id: str, body: OutcomeInput):
        return _validated(validate_history, settings, trial_id, body.outcome_snapshot_id)

    return router
