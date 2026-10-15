"""Partial fixed-order document-fee sensitivity, with unknowns kept missing."""
import argparse
from collections import defaultdict
from datetime import date, datetime, timezone
from decimal import Decimal, localcontext
import gzip
from hashlib import sha256
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.core.trading_calendar import TradingCalendar
from scripts.research_fund_rotation import CALENDAR

EVIDENCE = ROOT/"config/observed-product-cost-documents-v1.json"
REFERENCE = ROOT/"runtime/verification/satellite-event-repair-research-v1.json.gz"
REFERENCE_HASH = "00d712f3a9bbcecb27dbe24bb50a3a32b889e911093fc29f30000a7a24053b3c"


def load_evidence():
    evidence = json.loads(EVIDENCE.read_text())
    path = ROOT/evidence["archive"]
    if sha256(path.read_bytes()).hexdigest() != evidence["archive_sha256"]:
        raise ValueError("Original disclosure provenance manifest changed")
    manifest = json.loads(path.read_text())
    documents = {row["id"]: row for row in manifest["documents"]}
    for key, review in evidence["reviewed_documents"].items():
        document = documents[key]
        raw = path.parent/document["raw_file"]
        if (review["code"] != document["code"] or document["sha256"] != review["sha256"]
                or sha256(raw.read_bytes()).hexdigest() != review["sha256"]):
            raise ValueError("Reviewed document identity or digest changed")
    for code, profile in evidence["profiles"].items():
        if profile["investment_eligible"] or profile["historical_effective_range_verified"]:
            raise ValueError("This partial diagnostic must not grant historical investment qualification")
        if any(evidence["reviewed_documents"][key]["code"] != code
               or evidence["reviewed_documents"][key]["share_class"] != profile["share_class"] for key in profile["sources"]):
            raise ValueError("Fee profile refers to a different share code")
    return evidence, manifest["collected_at"]


def document_rate(profiles, code, action, age=None, *, allow_document_scenario=False):
    if code not in profiles:
        raise LookupError("Unreviewed product fee is unknown, not zero")
    if allow_document_scenario is not True:
        raise ValueError("Historical applicability unverified; explicitly request a document scenario")
    profile = profiles[code]
    if action == "BUY":
        rate = Decimal(profile["purchase_bps"])
    elif action == "SELL":
        if isinstance(age, bool) or not isinstance(age, int) or age < 0:
            raise ValueError("Document holding age must be a nonnegative integer")
        bands = profile["redemption_bands"]
        if not bands or bands[-1]["until_days"] is not None:
            raise ValueError("Redemption schedule lacks its final age range")
        prior = 0
        for position, band in enumerate(bands):
            upper = band["until_days"]
            if upper is None and position != len(bands)-1:
                raise ValueError("Unbounded redemption range must be last")
            if upper is not None and (isinstance(upper, bool) or not isinstance(upper, int) or upper <= prior):
                raise ValueError("Unordered redemption age ranges")
            value = Decimal(band["bps"])
            if not value.is_finite() or not 0 <= value < 10000:
                raise ValueError("Invalid document redemption rate")
            if upper is not None:
                prior = upper
        rate = next(Decimal(band["bps"]) for band in bands if band["until_days"] is None or age < band["until_days"])
    else:
        raise ValueError("Unknown order action")
    if not rate.is_finite() or not 0 <= rate < 10000:
        raise ValueError("Invalid document fee rate")
    return rate


def fixed_order_costs(account, profiles, calendar):
    known, unknown, orders, early = {}, defaultdict(int), defaultdict(lambda: Decimal(0)), []
    with localcontext() as context:
        context.prec = 40
        for trade in account["trades"]:
            code = trade["code"]
            if code not in profiles:
                unknown[code] += 1
                continue
            age = trade.get("holding_calendar_days")
            rate = document_rate(profiles, code, trade["action"], age, allow_document_scenario=True)
            row = known.setdefault(code, {"records": 0, "model_fee": Decimal(0), "document_scenario_fee": Decimal(0)})
            row["records"] += 1
            row["model_fee"] += Decimal(trade["fee"])
            row["document_scenario_fee"] += Decimal(trade["gross_amount"])*rate/10000
            if trade["action"] != "SELL":
                continue
            orders[(code, trade["execution_date"], trade.get("signal_date"), trade["reason"])] += Decimal(trade["units"])
            lag = profiles[code].get("redemption_right_from_purchase_sessions")
            if lag is not None and trade["execution_date"] < calendar.shift(date.fromisoformat(trade["purchase_date"]), lag).isoformat():
                early.append({"code": code, "purchase_date": trade["purchase_date"], "sale_date": trade["execution_date"],
                              "units": trade["units"], "reason": trade["reason"]})
        small_orders = [{"code": code, "date": day, "signal_date": signal, "reason": reason,
                         "aggregate_units": str(units)} for (code, day, signal, reason), units in orders.items()
                        if profiles[code].get("standard_min_redemption_units") is not None
                        and units < Decimal(profiles[code]["standard_min_redemption_units"])]
        difference = sum((row["model_fee"]-row["document_scenario_fee"] for row in known.values()), Decimal(0))
    return {"known_products": {code: {key: str(value) if isinstance(value, Decimal) else value for key, value in row.items()}
                               for code, row in known.items()}, "unknown_products": dict(sorted(unknown.items())),
            "fixed_gross_known_fee_difference": str(difference), "complete_document_cost": None,
            "revised_portfolio_return_pct": None, "potential_early_redemption_records": early,
            "potential_below_standard_min_redemption_orders": small_orders,
            "scope": "Current observed document rates on fixed historical gross/units and model holding ages. Not historical actual fees, rounded channel receipts, changed order/cash/FIFO paths or a portfolio replay. Unknown products stay missing. Rights/minimum flags are current-document scenario checks, not verdicts about historical contracts or platform overrides"}


def run():
    evidence, observed = load_evidence()
    if sha256(REFERENCE.read_bytes()).hexdigest() != REFERENCE_HASH:
        raise ValueError("Frozen fee diagnostic reference changed")
    with gzip.open(REFERENCE, "rt") as stream:
        reference = json.load(stream)
    if sha256(CALENDAR.read_bytes()).hexdigest() != reference["calendar_sha256"]:
        raise ValueError("Frozen fee calendar changed")
    calendar = TradingCalendar(json.loads(CALENDAR.read_text()))
    results = {name: {key: fixed_order_costs(run["account"], evidence["profiles"], calendar)
                      for key, run in cohort["full"].items()}
               for name, cohort in reference["cohorts"].items()}
    source = Path(__file__).read_text()
    return {"generated_at": datetime.now(timezone.utc).isoformat(), "evidence_sha256": sha256(EVIDENCE.read_bytes()).hexdigest(),
            "conservative_document_known_at": observed, "reference_sha256": REFERENCE_HASH,
            "source_snapshot": source, "source_sha256": sha256(source.encode()).hexdigest(),
            "account_replays": 0, "evidence": evidence, "cohorts": results}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT/"runtime/verification/document-fee-diagnostic-v1.json")
    args = parser.parse_args()
    result = run()
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, indent=2, ensure_ascii=False, allow_nan=False)
    print(json.dumps({"output": str(args.output), "modern_fixed_gross_fee_differences": {
        key: value["fixed_gross_known_fee_difference"] for key, value in result["cohorts"]["all13"].items()}}), flush=True)
