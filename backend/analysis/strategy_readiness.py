"""Data-use gates for strategy signals and investment-performance claims.

The source registry records what a collector is allowed to support.  Strategy
code must enforce those decisions instead of treating every stored projection
as execution-grade merely because it has rows.
"""
from __future__ import annotations

from functools import lru_cache
import json
from pathlib import Path
from typing import Any


REGISTRY_PATH = Path(__file__).resolve().parents[2] / "config" / "data-source-registry.json"


@lru_cache(maxsize=1)
def _registry() -> dict[str, dict[str, Any]]:
    value = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    sources = value.get("sources")
    if not isinstance(sources, list):
        raise ValueError("数据来源注册表缺少sources")
    result: dict[str, dict[str, Any]] = {}
    for source in sources:
        if not isinstance(source, dict) or not isinstance(source.get("id"), str):
            raise ValueError("数据来源注册表包含无效来源")
        result[str(source["id"])] = source
    return result


def is_primary_evidence(dataset: str, source_id: str | None) -> bool:
    """Return whether a registered source is primary evidence for this dataset."""
    source = _registry().get(str(source_id)) if source_id else None
    return bool(
        source
        and source.get("dataset") == dataset
        and source.get("decision") == "authoritative_evidence"
        and "primary_evidence" in source.get("allowed_uses", [])
    )


def assess_strategy_series(*, kind: str | None, source_id: str | None) -> dict[str, Any]:
    """Classify one current projection for signal, performance and execution use.

    Unit NAV and unadjusted market price may describe current price risk when
    their source is primary evidence.  They are never silently promoted to a
    reinvested total-return series for performance validation.
    """

    source = _registry().get(str(source_id)) if source_id else None
    allowed_uses = list(source.get("allowed_uses", [])) if source else []
    decision = str(source.get("decision")) if source else "unregistered"
    expected_dataset = {
        "price": "exchange_daily",
        "nav": "fund_nav",
        "total_return": "fund_total_return",
    }.get(str(kind))
    return_basis = {
        "price": "unadjusted_market_price",
        "nav": "unit_nav_without_verified_distributions",
        "total_return": "reinvested_total_return",
    }.get(str(kind), "unknown")
    signal_reasons: list[str] = []
    if source is None:
        signal_reasons.append("source_not_registered")
    elif decision != "authoritative_evidence" or "primary_evidence" not in allowed_uses:
        signal_reasons.append("source_not_primary_evidence")
    if source is not None and expected_dataset is not None and source.get("dataset") != expected_dataset:
        signal_reasons.append("source_dataset_mismatch")
    if kind not in {"price", "nav", "total_return"}:
        signal_reasons.append("series_kind_unsupported")

    performance_reasons = list(signal_reasons)
    if return_basis != "reinvested_total_return":
        performance_reasons.append("verified_total_return_missing")

    return {
        "source_id": source_id,
        "registered": source is not None,
        "registry_decision": decision,
        "allowed_uses": allowed_uses,
        "return_basis": return_basis,
        "eligible_for_signal": not signal_reasons,
        "eligible_for_execution": not signal_reasons,
        "eligible_for_performance": not performance_reasons,
        "signal_blockers": signal_reasons,
        "performance_blockers": performance_reasons,
    }


def assess_catalog_source(*, source_id: str | None) -> dict[str, Any]:
    """Require a primary catalogue identity before a fund can drive trades."""
    source = _registry().get(str(source_id)) if source_id else None
    allowed_uses = list(source.get("allowed_uses", [])) if source else []
    decision = str(source.get("decision")) if source else "unregistered"
    blockers: list[str] = []
    if source is None:
        blockers.append("catalog_source_not_registered")
    elif (source.get("dataset") != "fund_catalog"
          or decision != "authoritative_evidence"
          or "primary_evidence" not in allowed_uses):
        blockers.append("catalog_source_not_primary_evidence")
    return {
        "source_id": source_id,
        "registered": source is not None,
        "registry_decision": decision,
        "allowed_uses": allowed_uses,
        "eligible_for_signal": not blockers,
        "eligible_for_execution": not blockers,
        "eligible_for_performance": not blockers,
        "signal_blockers": blockers,
        "performance_blockers": blockers,
    }


def assess_relation_source(*, source_id: str | None) -> dict[str, Any]:
    """Require primary, registered evidence for the fund-to-index identity link."""
    source = _registry().get(str(source_id)) if source_id else None
    allowed_uses = list(source.get("allowed_uses", [])) if source else []
    decision = str(source.get("decision")) if source else "unregistered"
    blockers: list[str] = []
    if source is None:
        blockers.append("relation_source_not_registered")
    elif (source.get("dataset") != "fund_relations"
          or decision != "authoritative_evidence"
          or "primary_evidence" not in allowed_uses):
        blockers.append("relation_source_not_primary_evidence")
    return {
        "source_id": source_id,
        "registered": source is not None,
        "registry_decision": decision,
        "allowed_uses": allowed_uses,
        "eligible_for_signal": not blockers,
        "eligible_for_execution": not blockers,
        "eligible_for_performance": not blockers,
        "signal_blockers": blockers,
        "performance_blockers": blockers,
    }


def aggregate_execution_readiness(items: list[dict[str, Any]]) -> dict[str, Any]:
    blockers = sorted({
        str(reason)
        for item in items
        for eligibility in (
            item.get("catalog_eligibility", {}), item.get("data_eligibility", {}),
            item.get("relation_eligibility", {}),
        )
        for reason in eligibility.get("signal_blockers", [])
    })
    blocked_codes = sorted({
        str(item.get("code"))
        for item in items
        if (not item.get("catalog_eligibility", {}).get("eligible_for_execution", False)
            or not item.get("data_eligibility", {}).get("eligible_for_execution", False)
            or not item.get("relation_eligibility", {}).get("eligible_for_execution", False))
    })
    return {
        "status": "ready" if not blockers else "blocked",
        "blockers": blockers,
        "blocked_fund_codes": blocked_codes,
        "policy": "primary_evidence_required_for_price_risk_execution",
    }


def investment_validation_readiness(items: list[dict[str, Any]]) -> dict[str, Any]:
    """Return the fail-closed gate for claims that can affect real money.

    A registry label describes intended source policy; it is not itself a
    point-in-time evidence chain.  The dedicated blocker below remains until
    catalogue identity, observations and fund relationships are read from, or
    reconciled against, immutable raw assets and revisions.
    """
    reasons = {
        str(reason)
        for item in items
        for eligibility in (
            item.get("catalog_eligibility", {}), item.get("data_eligibility", {}),
            item.get("relation_eligibility", {}),
        )
        for reason in eligibility.get("performance_blockers", [])
    }
    reasons.update({
        "immutable_source_evidence_chain_missing",
        "point_in_time_universe_missing",
        "point_in_time_observation_revisions_missing",
        "verified_historical_trading_calendar_missing",
        "stable_investable_universe_policy_missing",
        "sector_research_signal_not_integrated",
        "risk_free_cash_return_series_missing",
        "shared_live_portfolio_engine_not_used",
        "atomic_pretrade_revalidation_missing",
        "product_specific_execution_terms_missing",
        "walk_forward_holdout_not_completed",
    })
    return {
        "status": "blocked" if reasons else "ready",
        "investment_operations_enabled": not reasons,
        "blockers": sorted(reasons),
        "meaning": (
            "当前结果只可用于规则行为与账本核验，不能证明策略可用于真实资金。"
            if reasons else "数据、执行与样本外验证门槛均已满足。"
        ),
    }
