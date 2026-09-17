"""Synthetic engineering fixtures only; not real market/valuation/fund evidence."""
from contextlib import ExitStack, contextmanager
from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import patch
from backend.core.trading_calendar import get_calendar
from backend.storage.research import canonical

NOW = "2026-09-16T13:00:00.000000Z"
SUBJECT = "tracked_index:fixture:A"
BENCHMARK = "tracked_index:fixture:B"
FUND_A, FUND_B = "fund:catalog:510001", "fund:catalog:510002"


@contextmanager
def clock(value):
    with ExitStack() as stack:
        for module in ("backend.storage.research", "backend.analysis.research_pipeline", "backend.analysis.validation", "backend.integrations.research_inputs", "backend.storage.collection_runs"):
            stack.enter_context(patch(module + ".utc_now", return_value=value))
        yield


def series(subject=SUBJECT, *, kind="price", end="2026-09-16", count=121, rate="0.001"):
    calendar = get_calendar()
    last = date.fromisoformat(end)
    dates = calendar.sessions(calendar.shift(last, -count + 1), last)
    return [{"kind": "series", "subject_key": subject, "source_id": "fixture.normalized",
             "effective_date": day.isoformat(), "series_kind": kind,
             "value": str(Decimal(100) * (Decimal(1) + Decimal(rate)) ** index),
             "basis": {"price": "unadjusted_price", "nav": "unit_nav", "total_return": "reinvested_total_return"}[kind],
             "amount": "1000000" if kind == "price" else None, "currency": "CNY", "semantic_status": "verified"}
            for index, day in enumerate(dates)]


def numeric(*, subject=SUBJECT, metric="profit_yoy", value="10", effective="2026-07-31", **kwargs):
    return {"kind": "numeric", "subject_key": subject, "source_id": "fixture.reviewed",
            "effective_date": effective, "metric": metric, "value": value, "unit": "%",
            "scope": "index_constituents", "methodology": "fixture_consistent_definition", "semantic_status": "verified", **kwargs}


def profile(subject=FUND_A, *, fee="15", **kwargs):
    return {"kind": "fund_profile", "subject_key": subject, "source_id": "fixture.prospectus_and_status",
            "effective_date": "2026-09-16", "fund_code": subject.split(":")[-1], "name": "测试份额",
            "category": "passive_otc", "benchmark_key": SUBJECT, "portfolio_id": "fixture_shared_portfolio",
            "annual_fee_bps": fee, "subscription_status": "open", "semantic_status": "verified", **kwargs}


def full_facts():
    facts = series() + series(SUBJECT, kind="total_return") + series(BENCHMARK, kind="total_return", rate="0.0005")
    for subject, fee in ((FUND_A, "15"), (FUND_B, "25")):
        facts += [profile(subject, fee=fee)] + series(subject, kind="total_return")
    for metric in ("revenue_yoy", "profit_yoy", "operating_cashflow_yoy"):
        for effective, value in (("2026-06-30", "5"), ("2026-07-31", "10")):
            facts.append(numeric(metric=metric, value=value, effective=effective, published_at="2026-08-27T08:00:00+08:00", publication_precision="time"))
    for index, point in enumerate(get_calendar().sessions(date(2025, 5, 5), date(2026, 9, 16))[::5]):
        facts.append(numeric(metric="pe_ttm", value=str(Decimal("20") + Decimal(index) / 10), effective=point.isoformat(), unit="times"))
    facts.append(numeric(metric="pe_ttm", value="20", effective="2026-09-16", unit="times"))
    # End date may already be the last 5-session sample.
    unique = {}
    for fact in facts:
        key = (fact["subject_key"], fact["kind"], fact.get("metric", fact.get("series_kind")), fact["effective_date"])
        unique[key] = fact
    return list(unique.values())


def specification(**kwargs):
    return {"name": "合成价格基线验证", "hypothesis": "测试隔离与反例，不代表投资假设已证实",
            "subjects": [SUBJECT], "benchmark_key": BENCHMARK, "signal_rule": "price_strong", "period": "short",
            "horizon_sessions": 20, "embargo_sessions": 2, "minimum_observations": 20,
            "development": {"start": "2025-01-02", "end": "2025-06-20"},
            "validation": {"start": "2025-07-01", "end": "2025-12-20"},
            "test": {"start": "2026-01-05", "end": "2026-12-31"}, **kwargs}
