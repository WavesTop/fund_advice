"""Synthetic responses for contract tests only; never used by production collectors."""
from datetime import date, datetime, timedelta
from decimal import Decimal
import json
import re
from urllib.parse import parse_qs, urlsplit

from backend.core.trading_calendar import SHANGHAI, get_calendar

NOW = datetime(2026, 9, 17, 10, 0, tzinfo=SHANGHAI)
SUBJECT = "hot_board:sector_daily.eastmoney:BK0732"
CODES = ("600547", "600489")


class FrozenDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW.astimezone(tz) if tz else NOW.replace(tzinfo=None)


def board():
    sessions = get_calendar().sessions(date(2026, 1, 1), date(2026, 9, 16))[-151:]
    rows = []
    for index, session in enumerate(sessions):
        price = Decimal(100) + Decimal(index) / 10
        rows.append({"date": session.isoformat(), "open": str(price), "close": str(price),
                     "high": str(price + 1), "low": str(price - 1), "volume": "10000", "amount": "1000000"})
    return {"code": "BK0732", "name": "贵金属", "kind": "行业", "source_id": "sector_daily.eastmoney",
            "heat_rank": 1, "heat_value": "1000000", "heat_updated_at": "2026-09-16T08:00:00Z",
            "updated_at": "2026-09-16T08:10:00Z", "collection_error": None, "universe_type": "hot_board",
            "history_source_id": "sector_daily.eastmoney", "history_source_code": "90.BK0732", "rows": rows,
            "membership": {"status": "ready", "as_of": "2026-09-16", "fetched_at": "2026-09-16T08:10:00Z",
                           "member_count": 2, "error": None, "members": [
                               {"stock_code": code, "stock_name": f"合成公司{index}", "market": 1, "source_order": index}
                               for index, code in enumerate(CODES, 1)]}}


class SourceFixture:
    def __init__(self, mutate=None):
        self.mutate = mutate
        self.calls = []

    def __call__(self, url, timeout):
        self.calls.append(url)
        params = parse_qs(urlsplit(url).query)
        report = params["reportName"][0]
        match = re.search(r"(?:REPORT_DATE|TRADE_DATE)='(\d{4}-\d{2}-\d{2})'", params["filter"][0])
        if match is None:
            raise AssertionError("fixture requires explicit source business date")
        day = match.group(1)
        rows = []
        for index, code in enumerate(CODES):
            row = {"SECURITY_CODE": code}
            if report == "RPT_VALUEANALYSIS_DET":
                row.update(TRADE_DATE=day, PE_TTM=str(20 + index * 10))
            else:
                row.update(REPORT_DATE=day, NOTICE_DATE=(date.fromisoformat(day) + timedelta(days=20)).isoformat())
                if report == "RPT_DMSK_FN_INCOME":
                    row.update(TOTAL_OPERATE_INCOME="120" if day.startswith("2026") else "100",
                               PARENT_NETPROFIT="15" if day.startswith("2026") else "10")
                elif report == "RPT_DMSK_FN_CASHFLOW":
                    row.update(NETCASH_OPERATE="20" if day.startswith("2026") else "10")
                else:
                    raise AssertionError(f"unexpected report {report}")
            rows.append(row)
        if self.mutate:
            rows = self.mutate(report, day, rows)
        return json.dumps({"success": True, "result": {"count": len(rows), "pages": 1 if rows else 0, "data": rows}}).encode()
