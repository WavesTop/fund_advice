"""Formal local portfolio ledger API; all write operations are idempotent."""
from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Header, Query
from pydantic import BaseModel, ConfigDict, Field

from backend.core.config import Settings
from backend.storage.portfolio import (
    ACCOUNT_VERSION,
    append_condition,
    append_event,
    correct_event,
    create_account,
    list_accounts,
    list_conditions,
    list_events,
    portfolio_snapshot,
)


class StrictInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AccountInput(StrictInput):
    name: str = Field(min_length=1, max_length=200)
    base_currency: str = Field(default="CNY", min_length=3, max_length=3)


class ConditionInput(StrictInput):
    account_id: str = Field(min_length=1, max_length=64)
    effective_date: str
    risk_level: Literal["conservative", "balanced", "aggressive"]
    max_single_position_pct: str
    investment_horizon_days: int = Field(gt=0, le=36500)
    channel: str = Field(min_length=1, max_length=200)
    planned_additional_cash: str | None = None
    no_holdings_confirmed: bool = False


class EventFields(StrictInput):
    event_type: Literal["initial_position", "buy", "sell", "cash_in", "cash_out", "dividend"]
    fund_code: str | None = Field(default=None, max_length=6)
    amount: str | None = None
    units: str | None = None
    business_date: str
    confirmation_date: str | None = None
    settlement_date: str | None = None
    channel: str = Field(min_length=1, max_length=200)
    confirmed_fee: str | None = None
    state: Literal["confirmed", "pending", "unknown"] = "confirmed"
    source: str = Field(min_length=1, max_length=200)
    note: str | None = Field(default=None, max_length=1000)


class EventInput(EventFields):
    account_id: str = Field(min_length=1, max_length=64)


class CorrectionInput(StrictInput):
    action: Literal["reverse", "replace"]
    reason: str = Field(min_length=1, max_length=1000)
    replacement: EventFields | None = None


IdempotencyKey = Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=200)]


def portfolio_router(settings: Settings) -> APIRouter:
    router = APIRouter(prefix="/api/v1", tags=["portfolio"])

    @router.get("/portfolio/accounts")
    def accounts():
        return {"version": ACCOUNT_VERSION, "items": list_accounts(settings)}

    @router.post("/portfolio/accounts")
    def add_account(body: AccountInput, idempotency_key: IdempotencyKey):
        return create_account(settings, body.model_dump(), idempotency_key=idempotency_key)

    @router.get("/portfolio/conditions")
    def conditions(account_id: str = Query(min_length=1, max_length=64), cutoff: str | None = Query(default=None)):
        return list_conditions(settings, account_id, cutoff)

    @router.post("/portfolio/conditions")
    def add_condition(body: ConditionInput, idempotency_key: IdempotencyKey):
        return append_condition(settings, body.model_dump(), idempotency_key=idempotency_key)

    @router.get("/ledger/events")
    def events(account_id: str = Query(min_length=1, max_length=64)):
        return list_events(settings, account_id)

    @router.post("/ledger/events")
    def add_event(body: EventInput, idempotency_key: IdempotencyKey):
        return append_event(settings, body.model_dump(), idempotency_key=idempotency_key)

    @router.post("/ledger/events/{event_id}/corrections")
    def add_correction(event_id: str, body: CorrectionInput, idempotency_key: IdempotencyKey):
        return correct_event(settings, event_id, body.model_dump(), idempotency_key=idempotency_key)

    @router.get("/portfolio/snapshots")
    def snapshot(account_id: str = Query(min_length=1, max_length=64), cutoff: str = Query()):
        return portfolio_snapshot(settings, account_id, cutoff)

    return router
