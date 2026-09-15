"""Explain the existing screen without upgrading it to an investment recommendation.

This is a current-data presentation contract, not an immutable analysis snapshot.
Only explicit fund/index relations are exposed. Names and momentum never infer a
fund mapping, a buy amount, a probability, or investment suitability.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from urllib.parse import urlsplit

VIEW_VERSION = "research-view-v1"


def _timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo is not None else None
    except ValueError:
        return None


def _public_source(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        url = urlsplit(value)
        return (url.scheme in ("http", "https") and bool(url.hostname)
                and not url.username and not url.password)
    except ValueError:
        return False


def fund_associations(item: Mapping[str, object], *, generated_at: str) -> list[dict[str, object]]:
    """Keep provenance or withhold a relation; never fill it from a similar name."""
    if item.get("universe_type") != "tracked_index":
        return []
    now = _timestamp(generated_at)
    if now is None:
        raise ValueError("generated_at must include a timezone")
    result: list[dict[str, object]] = []
    raw_funds = item.get("funds")
    funds = raw_funds if isinstance(raw_funds, list) else []
    for fund in funds:
        if not isinstance(fund, Mapping):
            continue
        code, name = fund.get("code"), fund.get("name")
        if (not isinstance(code, str) or len(code) != 6 or not code.isascii()
                or not code.isdigit() or not isinstance(name, str) or not name.strip()):
            continue
        verified_at = _timestamp(fund.get("verified_at"))
        problems: list[str] = []
        if fund.get("relation_type") != "tracked_index":
            problems.append("未提供明确的跟踪指数关系。")
        if not fund.get("relation_source_id") or not _public_source(fund.get("evidence_url")):
            problems.append("缺少可核对的基金—指数关系来源。")
        if verified_at is None:
            problems.append("关系核验时间缺失或无时区。")
        elif verified_at > now:
            problems.append("关系核验时间晚于本次读取时间。")
        result.append({
            "code": code, "name": name,
            "status": "withheld" if problems else "linked",
            "relation_type": fund.get("relation_type"),
            "source_id": fund.get("relation_source_id"),
            "evidence_url": fund.get("evidence_url") if _public_source(fund.get("evidence_url")) else None,
            "verified_at": fund.get("verified_at"),
            "limitations": problems + [
                "这是已存储的指数关联，不代表该基金已通过推荐筛选。",
                "关系为核验时的当前投影，尚无生效区间及后续变更核查。",
                "跟踪表现、持有成本、申赎状态和个人适用性尚未完整评估。",
            ],
        })
    return sorted(result, key=lambda fund: str(fund["code"]))


def build_research_view(item: Mapping[str, object], *, generated_at: str) -> dict[str, object]:
    """Preserve contrary evidence even when the price comparison is unavailable."""
    if _timestamp(generated_at) is None:
        raise ValueError("generated_at must include a timezone")
    raw_periods = item.get("periods")
    periods = raw_periods if isinstance(raw_periods, list) else []
    states: list[dict[str, object]] = []
    for period in periods:
        if not isinstance(period, Mapping) or period.get("id") not in ("short", "medium", "long"):
            continue
        raw = period.get("opportunity")
        opportunity = raw if isinstance(raw, Mapping) else {}
        raw_strength = period.get("strength")
        strength = raw_strength if isinstance(raw_strength, Mapping) else {}
        evidence_state = opportunity.get("status")
        if evidence_state not in ("watch", "conflict", "risk", "insufficient"):
            evidence_state = "insufficient"
        missing = opportunity.get("missing")
        gaps = [value for value in missing if isinstance(value, str)] if isinstance(missing, list) else []
        if item.get("collection_error"):
            gaps.insert(0, "行情更新失败；旧行情仅供核对，本次不参与走势比较。")
        market_available = (period.get("status") in ("strong", "neutral", "weak")
                            and not item.get("collection_error"))
        states.append({
            "id": period["id"],
            "evidence_state": evidence_state,
            "market_available": market_available,
            "comparison_available": market_available and strength.get("eligible") is True,
            "label": {"watch": "有待核查的研究线索", "conflict": "证据存在分歧",
                      "risk": "已有风险或反证", "insufficient": "研究依据不足"}[str(evidence_state)],
            "gaps": gaps,
            # Removing a gap or changing a price must not silently enable advice.
            "recommendation_status": "not_evaluated",
            "recommendation_reason": "当前仅提供研究观察；尚未完成投资假设、基金择优与投资效果验证。",
        })
    return {
        "version": VIEW_VERSION,
        "subject_key": f"{item.get('universe_type', 'tracked_index')}:{item.get('source_id', '')}:{item.get('code', '')}",
        "periods": states,
        "fund_associations": fund_associations(item, generated_at=generated_at),
        "operation_status": "unavailable",
        "operation_reason": "研究与个人操作分开；本次不生成金额、仓位或买卖指令。",
    }


def attach_research_views(items: Sequence[dict[str, object]], *, generated_at: str) -> None:
    for item in items:
        item["research"] = build_research_view(item, generated_at=generated_at)
