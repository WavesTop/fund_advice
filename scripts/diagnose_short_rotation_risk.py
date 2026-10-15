"""Rebuild actual signal-day holdings risk for young, still-eligible rotations."""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal, localcontext
import gzip
from hashlib import sha256
import json
import math
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import numpy as np
from backend.core.trading_calendar import TradingCalendar
from scripts.research_defensive_allocations import BONDS
from scripts.research_fund_rotation import CALENDAR, INPUT, load_funds

REFERENCE = ROOT / "runtime/verification/class-selection-event-review-v1.json.gz"
OUTPUT = ROOT / "runtime/verification/short-rotation-current-risk-v1.json"
CLASSES = {"000217": "gold", "000051": "stock", "000008": "stock", "000016": "bond"}


def run():
    reference = json.load(gzip.open(REFERENCE, "rt", encoding="utf-8"))
    for key, path in (("input_sha256", INPUT), ("bond_input_sha256", BONDS), ("calendar_sha256", CALENDAR)):
        if sha256(path.read_bytes()).hexdigest() != reference[key]:
            raise ValueError(f"Reference input changed: {key}")
    calendar = TradingCalendar(json.loads(CALENDAR.read_text()))
    funds, _ = load_funds(INPUT, calendar)
    bonds, _ = load_funds(BONDS, calendar)
    funds.update(bonds)
    trial = reference["results"]["dual_return60"]["modes"]["event20"]["runs"]["0"]
    account = trial["account"]
    trace = {r["signal_date"]: r for r in trial["signal_trace"]}
    candidates = []
    for trade in account["trades"]:
        if trade["action"] != "SELL" or not trade.get("short_redemption_fee_applied") or trade.get("signal_date") not in trace:
            continue
        entry = trace[trade["signal_date"]]
        if entry["class_signals"][CLASSES[trade["code"]]]["vote"] >= 2/3 and entry["targets"][trade["code"]] == 0:
            candidates.append(trade)
    needed = {t["signal_date"] for t in candidates}
    by_date = {}
    for trade in account["trades"]:
        by_date.setdefault(trade["execution_date"], []).append(trade)
    risks = {}
    with localcontext() as ctx:
        ctx.prec = 40
        units = dict.fromkeys(CLASSES, Decimal(0))
        for row in account["equity_curve"]:
            day = row["date"]
            for trade in by_date.get(day, []):
                units[trade["code"]] += Decimal(trade["units"]) * (1 if trade["action"] == "BUY" else -1)
            values = {c: n * Decimal(str(float(funds[c]["rows"][day]["unit_nav"]))) for c, n in units.items()}
            if abs(sum(values.values(), Decimal(0)) - Decimal(row["market_value"])) > Decimal("1e-20"):
                raise ValueError("Reconstructed holdings do not match audited account market value")
            if day not in needed:
                continue
            weights = {c: float(v / Decimal(row["equity"])) for c, v in values.items()}
            sessions = [d.isoformat() for d in calendar.sessions(calendar.shift(date.fromisoformat(day), -60), date.fromisoformat(day))]
            if len(sessions) != 61:
                raise ValueError("Holdings risk lacks complete past sessions")
            daily = []
            for left, right in zip(sessions, sessions[1:]):
                returns = {c: math.log((float(funds[c]["rows"][right]["unit_nav"]) + funds[c]["distributions"][right])
                                      / float(funds[c]["rows"][left]["unit_nav"])) for c in CLASSES}
                daily.append(sum(weights[c] * returns[c] for c in CLASSES))
            risk = float(np.std(daily) * np.sqrt(252))
            if not math.isfinite(risk):
                raise ValueError("Invalid reconstructed current holdings risk")
            risks[day] = {"weights": weights, "projected_annual_volatility": risk,
                          "within_original10pct_limit": risk <= .1 + 1e-10}
    rows = [{"signal_date": t["signal_date"], "execution_date": t["execution_date"], "code": t["code"],
             "lot_id": t["lot_id"], "fee": t["fee"], **risks[t["signal_date"]]} for t in candidates]
    groups = {}
    for flag in (True, False):
        selected = [r for r in rows if r["within_original10pct_limit"] is flag]
        groups["within_limit" if flag else "above_limit"] = {"lots": len(selected),
               "distinct_signal_dates": len({r["signal_date"] for r in selected}),
               "incurred_short_fees": str(sum((Decimal(r["fee"]) for r in selected), Decimal(0)))}
    paths = ("scripts/diagnose_short_rotation_risk.py", "scripts/research_fund_rotation.py", "backend/core/trading_calendar.py")
    return {"generated_at": datetime.now(timezone.utc).isoformat(), "groups": groups, "records": rows,
            "reference_sha256": sha256(REFERENCE.read_bytes()).hexdigest(), "input_sha256": reference["input_sha256"],
            "bond_input_sha256": reference["bond_input_sha256"], "calendar_sha256": reference["calendar_sha256"],
            "source_snapshots": {p: (ROOT / p).read_text() for p in paths},
            "source_hashes": {p: sha256((ROOT / p).read_bytes()).hexdigest() for p in paths},
            "limitations": ["Observed cost classification, not proof these fees or losses could be avoided",
                            "Actual holdings and cash/receivable-inclusive wealth at signal day; past60totalreturn covariance proxy, not future risk",
                            "Removing risk-exit treatment for relative rotations still requires preserving genuine risk reductions and absolute failures",
                            "Same already-viewed survivor data, hypothetical fees and retrospective NAV/dividend timing"]}


if __name__ == "__main__":
    report = run()
    OUTPUT.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    print(json.dumps({"output": str(OUTPUT), "groups": report["groups"]}, ensure_ascii=False))
