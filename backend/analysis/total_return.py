"""Auditable per-pre-event-share cash/split convention, not accumulated NAV."""
from __future__ import annotations

from decimal import Decimal

from backend.core.trading_calendar import TradingCalendar, day
from backend.storage.research import number


def reinvested_nav(nav: list[dict], actions: list[dict], *, calendar: TradingCalendar,
                   coverage_start: str, coverage_end: str, actions_complete: bool) -> list[dict]:
    """Cash dividend is per OLD share; split_ratio is new shares per OLD share.

    Reinvestment occurs at the same ex-date NAV, without investor-level taxes or
    transaction fees. Missing corporate-action coverage is an error, not zero cash.
    """
    if actions_complete is not True or len(nav) < 2:
        raise ValueError("缺少完整分红拆分覆盖或至少两个净值")
    dates = [row["date"] for row in nav]
    quality = calendar.validate_grid(dates, end=day(dates[-1]), minimum=2)
    if quality["status"] != "ready" or day(coverage_start) > day(dates[0]) or day(coverage_end) < day(dates[-1]):
        raise ValueError("净值日历或分红拆分覆盖不完整")
    prices = [Decimal(number(row["unit_nav"], positive=True)) for row in nav]
    events = {}
    for event in actions:
        event_day = day(event["date"]).isoformat()
        if event_day in events or event_day not in dates[1:]:
            raise ValueError("公司行动日期重复、不在净值序列内或位于基准日")
        events[event_day] = (Decimal(number(event["cash_per_old_share"], nonnegative=True)),
                             Decimal(number(event["new_shares_per_old_share"], positive=True)))
    value = Decimal(1)
    result = [{"date": dates[0], "value": "1"}]
    for index in range(1, len(nav)):
        cash, split = events.get(dates[index], (Decimal(0), Decimal(1)))
        value *= (prices[index] * split + cash) / prices[index - 1]
        result.append({"date": dates[index], "value": str(value)})
    return result
