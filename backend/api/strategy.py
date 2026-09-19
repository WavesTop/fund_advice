"""Formal strategy, decision, execution-link and historical-validation API."""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Annotated, Literal

from fastapi import APIRouter, Header, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator

from backend.analysis.strategy_portfolio import eligible_fund_universe, historical_strategy_report
from backend.analysis.strategy_service import build_recommendation, create_recommendation
from backend.analysis.strategy_validation import validate_strategy_report
from backend.core.config import Settings
from backend.core.errors import AppError
from backend.api.portfolio import EventFields
from backend.storage.research import digest
from backend.storage.strategy_runs import (
    append_decision_execution,
    get_backtest,
    get_recommendation,
    latest_backtest,
    latest_recommendation,
    link_decision_trade,
    list_backtests,
    list_decisions,
    list_recommendations,
    save_backtest,
    save_decision,
)


class StrictInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _decimal_string(value: str | None, field: str, *, positive: bool = False) -> str | None:
    if value is None:
        return None
    try:
        parsed = Decimal(value)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{field}必须是精确小数字符串") from exc
    if not parsed.is_finite() or parsed < 0 or (positive and parsed <= 0):
        raise ValueError(f"{field}必须是{'正' if positive else '非负'}有限数值")
    rendered = format(parsed, "f")
    return rendered.rstrip("0").rstrip(".") if "." in rendered else rendered


class RecommendationInput(StrictInput):
    account_id: str = Field(min_length=1, max_length=64)
    cutoff: str


class DecisionInput(StrictInput):
    recommendation_id: str = Field(min_length=1, max_length=128)
    account_id: str = Field(min_length=1, max_length=64)
    cutoff: str
    fund_code: str | None = Field(default=None, pattern=r"^[0-9]{6}$")
    proposed_action: Literal["BUY", "ADD", "HOLD", "REDUCE", "EXIT", "WAIT"]
    choice: Literal["accepted", "partially_accepted", "rejected", "deferred"]
    planned_amount: str | None = None
    reason: str = Field(min_length=1, max_length=1000)
    supersedes_decision_id: str | None = Field(default=None, min_length=1, max_length=128)

    @field_validator("planned_amount")
    @classmethod
    def validate_amount(cls, value: str | None) -> str | None:
        return _decimal_string(value, "planned_amount")


class TradeLinkInput(StrictInput):
    role: Literal["planned_execution", "actual_execution", "independent_reference"]


class DecisionExecutionInput(EventFields):
    role: Literal["planned_execution", "actual_execution"]


class BacktestInput(StrictInput):
    initial_cash: str = "100000"
    buy_fee_bps: str = "10"
    sell_fee_bps: str = "10"

    @field_validator("initial_cash")
    @classmethod
    def validate_cash(cls, value: str) -> str:
        result = _decimal_string(value, "initial_cash", positive=True)
        assert result is not None
        return result

    @field_validator("buy_fee_bps", "sell_fee_bps")
    @classmethod
    def validate_fee(cls, value: str) -> str:
        result = _decimal_string(value, "fee_bps")
        assert result is not None
        if Decimal(result) >= Decimal("10000"):
            raise ValueError("fee_bps必须小于10000")
        return result


IdempotencyKey = Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=200)]


def strategy_router(settings: Settings) -> APIRouter:
    router = APIRouter(prefix="/api/v1/strategy", tags=["strategy"])

    @router.get("/universe")
    def universe():
        result = eligible_fund_universe(settings)
        return {
            **{key: value for key, value in result.items() if key != "items"},
            "items": [{key: value for key, value in item.items() if key != "rows"}
                      for item in result["items"]],
        }

    @router.post("/recommendations")
    def recommend(body: RecommendationInput):
        return create_recommendation(settings, **body.model_dump())

    @router.get("/recommendations/latest")
    def latest_recommendation_route(
        account_id: str = Query(min_length=1, max_length=64),
        cutoff: str = Query(min_length=10, max_length=10),
    ):
        item = latest_recommendation(settings, account_id)
        if item is None:
            return {
                "item": None,
                "freshness": {
                    "state": "unavailable",
                    "reason_code": "recommendation_missing",
                    "reason": "该账户尚未保存策略建议，请先生成建议。",
                    "checked_cutoff": cutoff,
                    "stored_input_hash": None,
                    "current_input_hash": None,
                    "changed_inputs": [],
                },
            }
        try:
            current_inputs, _, _, _ = build_recommendation(
                settings, account_id=account_id, cutoff=cutoff,
            )
        except AppError as exc:
            return {
                "item": item,
                "freshness": {
                    "state": "unavailable",
                    "reason_code": exc.body.code,
                    "reason": f"无法核验建议时效：{exc.body.message}",
                    "checked_cutoff": cutoff,
                    "stored_input_hash": item["input_hash"],
                    "current_input_hash": None,
                    "changed_inputs": [],
                },
            }

        current_input_hash = digest(current_inputs)
        stored_inputs = item.get("inputs", {})
        changed_inputs = sorted(
            key for key in set(stored_inputs) | set(current_inputs)
            if stored_inputs.get(key) != current_inputs.get(key)
        )
        is_current = current_input_hash == item["input_hash"]
        return {
            "item": item,
            "freshness": {
                "state": "current" if is_current else "stale",
                "reason_code": "inputs_match" if is_current else "inputs_changed",
                "reason": (
                    "建议输入与当前账本、约束及数据完全一致。"
                    if is_current else
                    "当前账本、约束、行情日期或策略输入已变化，必须重新生成建议后再记录决定或成交。"
                ),
                "checked_cutoff": cutoff,
                "stored_input_hash": item["input_hash"],
                "current_input_hash": current_input_hash,
                "changed_inputs": changed_inputs,
            },
        }

    @router.get("/recommendations")
    def recommendations(account_id: str = Query(min_length=1, max_length=64),
                        limit: int = Query(default=50, ge=1, le=100)):
        return {"account_id": account_id, "items": list_recommendations(settings, account_id, limit)}

    @router.get("/recommendations/{recommendation_id}")
    def recommendation(recommendation_id: str):
        return get_recommendation(settings, recommendation_id)

    @router.post("/decisions")
    def decide(body: DecisionInput, idempotency_key: IdempotencyKey):
        payload = body.model_dump()
        cutoff = payload.pop("cutoff")
        current_inputs, _, _, _ = build_recommendation(
            settings,
            account_id=body.account_id,
            cutoff=cutoff,
        )
        return save_decision(
            settings,
            **payload,
            current_input_hash=digest(current_inputs),
            idempotency_key=idempotency_key,
        )

    @router.get("/decisions")
    def decisions(account_id: str = Query(min_length=1, max_length=64),
                  limit: int = Query(default=100, ge=1, le=200)):
        return {"account_id": account_id, "items": list_decisions(settings, account_id, limit)}

    @router.post("/decisions/{decision_id}/trades/{event_id}")
    def link_trade(decision_id: str, event_id: str, body: TradeLinkInput):
        return link_decision_trade(settings, decision_id=decision_id, event_id=event_id, role=body.role)

    @router.post("/decisions/{decision_id}/executions")
    def append_execution(
        decision_id: str,
        body: DecisionExecutionInput,
        idempotency_key: IdempotencyKey,
    ):
        payload = body.model_dump()
        role = payload.pop("role")
        return append_decision_execution(
            settings,
            decision_id=decision_id,
            payload=payload,
            role=role,
            idempotency_key=idempotency_key,
        )

    @router.post("/backtests")
    def run_backtest(body: BacktestInput):
        try:
            report = historical_strategy_report(settings, **body.model_dump())
        except (ArithmeticError, ValueError) as exc:
            raise AppError("strategy_backtest_invalid", str(exc), 422) from exc
        if report["status"] == "insufficient":
            raise AppError("strategy_backtest_insufficient", str(report.get("reason", "真实数据不足")), 409, report)
        validation = validate_strategy_report(report)
        report["validation"] = validation
        if validation["status"] == "blocked":
            raise AppError(
                "strategy_backtest_validation_blocked",
                "历史验证未通过数据与账本一致性验收，结果未存档",
                422,
                {"report": report, "validation": validation},
            )
        validation_as_of = validation.get("coverage", {}).get("report_generated_date")
        if not isinstance(validation_as_of, str):
            raise AppError(
                "strategy_backtest_validation_date_missing",
                "历史验证缺少可复核的UTC验收日期，结果未存档",
                422,
            )
        return save_backtest(
            settings,
            strategy_version=str(report["strategy_version"]),
            start_date=str(report["start_date"]),
            end_date=str(report["end_date"]),
            calendar_basis=str(report["calendar_basis"]),
            protocol=report["protocol"],
            inputs={
                "input_identity": report["input_identity"],
                "universe": report["universe"],
                "calendar_document": report["calendar_document"],
                "validation_as_of": validation_as_of,
            },
            output=report,
            status="completed",
        )

    @router.get("/backtests/latest")
    def latest_backtest_route():
        return {"item": latest_backtest(settings)}

    @router.get("/backtests")
    def backtests(limit: int = Query(default=20, ge=1, le=100)):
        return {"items": list_backtests(settings, limit)}

    @router.get("/backtests/{run_id}")
    def backtest(run_id: str):
        return get_backtest(settings, run_id)

    return router
