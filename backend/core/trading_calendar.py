"""Reviewed exchange sessions; unknown years fail closed, never infer workday trading.

The 16:00 cutoff is a project policy for daily index observations. Fund NAVs
still require their own actual publication/first-seen times.
"""
from __future__ import annotations

from bisect import bisect_left, bisect_right
from datetime import date, datetime, time, timedelta
from functools import lru_cache
from hashlib import sha256
import json
from pathlib import Path
from zoneinfo import ZoneInfo

CALENDAR_PATH = Path(__file__).resolve().parents[2] / "config" / "exchange-calendar.json"
SHANGHAI = ZoneInfo("Asia/Shanghai")


class CalendarUnavailable(ValueError):
    pass


def day(value: str) -> date:
    if not isinstance(value, str):
        raise ValueError("日期必须是 YYYY-MM-DD")
    result = date.fromisoformat(value)
    if result.isoformat() != value:
        raise ValueError("日期必须是 YYYY-MM-DD")
    return result


class TradingCalendar:
    def __init__(self, document: dict):
        self.document = document
        self.version = document["version"]
        self.hash = sha256(json.dumps(document, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        self.cutoff = time.fromisoformat(document["settlement_cutoff"])
        self.years = frozenset(int(year) for year in document["years"])
        self._sessions: list[date] = []
        for year in sorted(self.years):
            entry = document["years"][str(year)]
            if not entry.get("sources"):
                raise ValueError("日历年份缺少来源")
            closed = set()
            for start, end in entry["closed_ranges"]:
                first, last = day(start), day(end)
                if first.year != year or last.year != year or first > last:
                    raise ValueError("休市区间无效")
                closed.update(first + timedelta(days=i) for i in range((last - first).days + 1))
            current = date(year, 1, 1)
            while current.year == year:
                if current.weekday() < 5 and current not in closed:
                    self._sessions.append(current)
                current += timedelta(days=1)
        self._session_set = frozenset(self._sessions)

    def require(self, value: date) -> None:
        if value.year not in self.years:
            raise CalendarUnavailable(f"交易日历未覆盖 {value.year} 年，不能用工作日推定交易日")

    def is_session(self, value: date) -> bool:
        self.require(value)
        return value in self._session_set

    def sessions(self, start: date, end: date) -> list[date]:
        if start > end:
            raise ValueError("起始日晚于截止日")
        for year in range(start.year, end.year + 1):
            self.require(date(year, 1, 1))
        return self._sessions[bisect_left(self._sessions, start):bisect_right(self._sessions, end)]

    def latest_completed(self, now: datetime) -> date:
        if now.tzinfo is None:
            raise ValueError("计算时间必须含时区")
        local = now.astimezone(SHANGHAI)
        self.require(local.date())
        boundary = local.date() if local.time() >= self.cutoff else local.date() - timedelta(days=1)
        self.require(boundary)
        pos = bisect_right(self._sessions, boundary) - 1
        if pos < 0:
            raise CalendarUnavailable("缺少截止日前的已核验交易日历")
        return self._sessions[pos]

    def shift(self, session: date, count: int) -> date:
        if not isinstance(count, int) or isinstance(count, bool):
            raise ValueError("交易日偏移必须是整数")
        if not self.is_session(session):
            raise ValueError("偏移起点不是交易日")
        pos = bisect_left(self._sessions, session) + count
        if not 0 <= pos < len(self._sessions):
            raise CalendarUnavailable("目标日期超出已核验交易日历")
        result = self._sessions[pos]
        self.sessions(min(session, result), max(session, result))
        return result

    def next_session(self, value: date) -> date:
        self.require(value)
        pos = bisect_right(self._sessions, value)
        if pos == len(self._sessions):
            raise CalendarUnavailable("下一交易日超出已核验交易日历")
        result = self._sessions[pos]
        self.sessions(value, result)
        return result

    def validate_grid(self, dates: list[str], *, end: date, minimum: int = 1) -> dict:
        """Missing/duplicate/non-session observations are not padded or annualized."""
        if not isinstance(minimum, int) or isinstance(minimum, bool) or minimum < 1:
            raise ValueError("最小样本量无效")
        self.require(end)
        parsed = [day(value) for value in dates]
        errors: list[str] = []
        if len(parsed) != len(set(parsed)):
            errors.append("duplicate_dates")
        if parsed != sorted(parsed):
            errors.append("unordered_dates")
        if any(not self.is_session(value) for value in parsed):
            errors.append("non_session_dates")
        if any(value > end for value in parsed):
            errors.append("unsettled_or_future_dates")
        eligible = sorted({value for value in parsed if value <= end})
        missing = []
        if eligible:
            missing = sorted(set(self.sessions(eligible[0], end)) - set(eligible))
            if missing:
                errors.append("missing_sessions")
            if eligible[-1] != end:
                errors.append("stale_last_session")
        if len(eligible) < minimum:
            errors.append("insufficient_observations")
        return {"status": "ready" if not errors else "blocked", "errors": errors,
                "missing_sessions": [value.isoformat() for value in missing],
                "expected_as_of": end.isoformat(), "observed_count": len(eligible),
                "calendar_version": self.version, "calendar_hash": self.hash}


@lru_cache(maxsize=1)
def get_calendar() -> TradingCalendar:
    return TradingCalendar(json.loads(CALENDAR_PATH.read_text(encoding="utf-8")))
