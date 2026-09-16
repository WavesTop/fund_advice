"""Current relationship provenance checks; these are not investment approval."""
from __future__ import annotations
from collections.abc import Mapping
from datetime import datetime
from urllib.parse import urlsplit


def timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo is not None else None
    except ValueError:
        return None


def public_source(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        url = urlsplit(value)
        return (url.scheme in ("http", "https") and bool(url.hostname)
                and not url.username and not url.password)
    except ValueError:
        return False


def relation_problems(fund: Mapping[str, object], *, generated_at: str) -> list[str]:
    now = timestamp(generated_at)
    if now is None:
        raise ValueError("generated_at must include a timezone")
    problems: list[str] = []
    if fund.get("relation_type") != "tracked_index":
        problems.append("未提供明确的跟踪指数关系。")
    if not fund.get("relation_source_id") or not public_source(fund.get("evidence_url")):
        problems.append("缺少可核对的基金—指数关系来源。")
    verified = timestamp(fund.get("verified_at"))
    if verified is None:
        problems.append("关系核验时间缺失或无时区。")
    elif verified > now:
        problems.append("关系核验时间晚于本次读取时间。")
    if fund.get("relation_status") not in (None, "linked"):
        problems.append(str(fund.get("relation_reason") or "当前关系未通过核验。"))
    return problems
