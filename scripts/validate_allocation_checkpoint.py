"""Rebuild checkpoint ancestry from a parent's executed ledger and signal plans."""
from copy import deepcopy
from datetime import date
from decimal import Decimal, localcontext


def validate_checkpoint_lineage(state, parent, data, calendar):
    """A valid seed schema alone does not establish that this account ever existed."""
    if state.get("version") != "fund-allocation-state-v1":
        raise ValueError("Unknown checkpoint schema version")
    asof = state["asof_date"]
    seed = parent.get("initial_state")
    previous = seed["asof_date"] if seed else None
    if asof is not None and asof < parent["start_date"] and asof != previous:
        # A fresh account capture before its first session still contains only cash.
        first = data["days"].index(parent["start_date"])
        previous = data["days"][first - 1] if first else None
        if asof != previous:
            raise ValueError("Checkpoint is outside its parent history")
    if asof is not None and asof > parent["end_date"]:
        raise ValueError("Checkpoint is after its parent history")
    with localcontext() as ctx:
        ctx.prec = 40
        lots = deepcopy(seed["lots"]) if seed else {}
        for entries in lots.values():
            for row in entries:
                row["units"] = Decimal(row["units"])
        counter = seed["lot_counter"] if seed else 0
        receipts = deepcopy(seed["receivables"]) if seed else []
        for trade in parent["trades"]:
            if asof is None or trade["execution_date"] > asof:
                continue
            code, key = trade["code"], trade["lot_id"]
            if trade["action"] == "BUY":
                counter = max(counter, key)
                lots.setdefault(code, []).append({"id": key, "units": Decimal(trade["units"]), "date": trade["execution_date"]})
            else:
                row = next(r for r in lots[code] if r["id"] == key)
                row["units"] -= Decimal(trade["units"])
                lots[code] = [r for r in lots[code] if r["units"] > 0]
                if not lots[code]:
                    del lots[code]
                receipts.append({"settlement_date": trade["settlement_date"],
                                 "amount": str(Decimal(trade["gross_amount"]) - Decimal(trade["fee"]))})
        receipts.extend({"settlement_date": r["settlement_date"], "amount": r["amount"]}
                        for r in parent["distribution_events"] if asof is not None and r["date"] <= asof)
        receipts = [r for r in receipts if asof is not None and r["settlement_date"] > asof]
        # Settlement order does not carry information; exact decimal amounts do.
        receipt_key = lambda r: (r["settlement_date"], Decimal(r["amount"]))
        if sorted(receipts, key=receipt_key) != sorted(state["receivables"], key=receipt_key):
            raise ValueError("Checkpoint receivables differ from parent ledger")
        positions = {c: [{**r, "units": str(r["units"])} for r in entries] for c, entries in lots.items()}
        if positions != state["lots"] or counter != state["lot_counter"]:
            raise ValueError("Checkpoint lots or lifetime id counter differ from parent ledger")
        points = [r for r in parent["equity_curve"] if r["date"] == asof]
        cash = points[0]["cash"] if points else seed["cash"] if seed else parent["initial_equity"]
        if state["cash"] != cash:
            raise ValueError("Checkpoint cash differs from parent ledger")
        index = data["days"].index(asof) if asof is not None else None
        prices = {c: str(p[index]) for c, p in data["prices"].items()} if index is not None else {}
        if prices != state["prices_asof"]:
            raise ValueError("Checkpoint NAV differs from its past-date inputs")
        market = sum((sum((r["units"] for r in entries), Decimal(0)) * Decimal(prices[c])
                      for c, entries in lots.items()), Decimal(0))
        wealth = Decimal(cash) + market + sum((Decimal(r["amount"]) for r in receipts), Decimal(0))
        if abs(wealth - Decimal(state["reference_equity"])) > Decimal("1e-25"):
            raise ValueError("Checkpoint wealth differs from parent assets")
    sources = ([q["origin_decision"] for q in seed["queued_orders"]] if seed else []) + parent["decisions"]
    pending = []
    for source in sources:
        signal = source["signal_date"]
        if asof is None or signal > asof:
            continue
        execute = calendar.shift(date.fromisoformat(signal), parent["execution"]["execution_lag_sessions"]).isoformat()
        if execute > asof:
            pending.append({"execution_date": execute, "orders": source["planned_orders"], "origin_decision": source})
    if sorted(pending, key=lambda q: q["execution_date"]) != state["queued_orders"]:
        raise ValueError("Checkpoint queued orders differ from earlier parent decisions")
    expected_resume = calendar.shift(date.fromisoformat(asof), 1).isoformat() if asof else data["days"][0]
    origin = seed["review_origin_date"] if seed else parent["start_date"]
    if (state["resume_date"] != expected_resume or state["review_origin_date"] != origin
            or state["execution"] != parent["execution"] or state["holding_cost_policy"] != parent["holding_cost_policy"]
            or state["fee_bps_each_side"] != parent["fee_bps_each_side"] or state["calendar_hash"] != calendar.hash
            or state["phase"] != parent["phase"]):
        raise ValueError("Checkpoint date or execution contract differs from its parent")
    return {"status": "passed", "asof_date": asof, "lots": sum(map(len, lots.values()))}
