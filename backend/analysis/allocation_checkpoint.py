"""JSON account state at a day boundary; allocator-private state is separate."""
from copy import deepcopy
from datetime import date
from decimal import Decimal, localcontext


def decimal_value(value, *, positive=False):
    if isinstance(value, bool):
        raise ValueError("Invalid checkpoint monetary value")
    try:
        result = Decimal(value)
    except (TypeError, ValueError, ArithmeticError) as exc:
        raise ValueError("Invalid checkpoint monetary value") from exc
    if not result.is_finite() or (result <= 0 if positive else result < 0):
        raise ValueError("Invalid checkpoint monetary value")
    return result


def serialize_orders(planned):
    result = []
    for order in planned:
        row = dict(order)
        for key in ("budget", "units"):
            if key in row and row[key] is not None:
                row[key] = str(row[key])
        result.append(row)
    return result


def make_checkpoint(data, calendar, resume_index, origin, phase, execution, costs, fee_bps,
                    cash, lots, receivables, orders, lot_counter, decisions):
    days = data["days"]
    previous = resume_index - 1
    asof = days[previous] if previous >= 0 else None
    resume_date = days[resume_index] if resume_index < len(days) else calendar.shift(date.fromisoformat(asof), 1).isoformat()
    prices = {c: str(p[previous]) for c, p in data["prices"].items()} if previous >= 0 else {}
    positions = {c: [{"id": lot["id"], "units": str(lot["units"]), "date": lot["date"]} for lot in entries]
                 for c, entries in lots.items()}
    pending = []
    for when, planned in sorted(orders.items()):
        execution_date = calendar.shift(date.fromisoformat(resume_date), when - resume_index).isoformat()
        signal = calendar.shift(date.fromisoformat(execution_date), -execution["execution_lag_sessions"]).isoformat()
        if signal not in decisions:
            raise ValueError("Checkpoint pending order lacks its earlier signal decision")
        pending.append({"execution_date": execution_date, "orders": serialize_orders(planned),
                        "origin_decision": deepcopy(decisions[signal])})
    with localcontext() as ctx:
        ctx.prec = 40
        market = sum((sum((lot["units"] for lot in entries), Decimal(0)) * Decimal(prices[c]) for c, entries in lots.items()), Decimal(0))
        equity = cash + market + sum((r["amount"] for r in receivables), Decimal(0))
    return {"version": "fund-allocation-state-v1", "asof_date": asof, "resume_date": resume_date,
            "review_origin_date": days[origin], "phase": phase, "execution": deepcopy(execution),
            "holding_cost_policy": deepcopy(costs), "fee_bps_each_side": str(fee_bps), "calendar_hash": calendar.hash,
            "prices_asof": prices, "cash": str(cash), "reference_equity": str(equity), "lot_counter": lot_counter,
            "lots": positions, "receivables": [{"settlement_date": r["settlement_date"], "amount": str(r["amount"])} for r in receivables],
            "queued_orders": pending}


def restore_checkpoint(snapshot, data, calendar, start, phase, execution, costs, fee_bps):
    state = deepcopy(snapshot)
    days, codes = data["days"], set(data["prices"])
    if (state.get("version") != "fund-allocation-state-v1" or state.get("resume_date") != days[start]
            or state.get("asof_date") != (days[start - 1] if start else None)
            or state.get("execution") != execution or state.get("holding_cost_policy") != costs
            or Decimal(state.get("fee_bps_each_side", "NaN")) != fee_bps or state.get("phase") != phase
            or state.get("calendar_hash") != calendar.hash):
        raise ValueError("Checkpoint date, calendar or account contract differs")
    origin = days.index(state["review_origin_date"])
    if origin > start:
        raise ValueError("Checkpoint review origin is in the future")
    expected_prices = {c: str(p[start - 1]) for c, p in data["prices"].items()} if start else {}
    if state["prices_asof"] != expected_prices:
        raise ValueError("Checkpoint prior prices differ from supplied NAV history")
    try:
        cash = Decimal(state["cash"])
    except (TypeError, ValueError, ArithmeticError) as exc:
        raise ValueError("Invalid checkpoint cash") from exc
    if not cash.is_finite() or cash < Decimal("-1e-25"):
        raise ValueError("Invalid checkpoint cash")
    counter = state["lot_counter"]
    if not isinstance(counter, int) or isinstance(counter, bool) or counter < 0 or set(state["lots"]) - codes:
        raise ValueError("Invalid checkpoint lot counter or fund universe")
    lots, ids = {}, set()
    for code, entries in state["lots"].items():
        if not entries:
            raise ValueError("Empty checkpoint position")
        previous = None
        lots[code] = []
        for lot in entries:
            key = lot["id"]
            if (not isinstance(key, int) or isinstance(key, bool) or not 0 < key <= counter or key in ids
                    or not state["asof_date"] or lot["date"] not in days or lot["date"] > state["asof_date"]):
                raise ValueError("Checkpoint lot has a duplicate id or future purchase")
            ordering = (lot["date"], key)
            if previous is not None and ordering <= previous:
                raise ValueError("Checkpoint lots are not in FIFO order")
            previous = ordering
            ids.add(key)
            lots[code].append({"id": key, "units": decimal_value(lot["units"], positive=True), "date": lot["date"]})
    receivables = []
    for row in state["receivables"]:
        settled = date.fromisoformat(row["settlement_date"])
        if (state["asof_date"] is None or row["settlement_date"] <= state["asof_date"] or not calendar.is_session(settled)):
            raise ValueError("Checkpoint receipt is already settled or not an exchange session")
        receivables.append({"settlement_date": row["settlement_date"], "amount": decimal_value(row["amount"], positive=True)})
    orders, origins = {}, {}
    for pending in state["queued_orders"]:
        when = date.fromisoformat(pending["execution_date"])
        offset = len(calendar.sessions(date.fromisoformat(days[start]), when)) - 1
        if pending["execution_date"] < days[start] or offset >= execution["execution_lag_sessions"] or not calendar.is_session(when):
            raise ValueError("Checkpoint pending execution is past or too far ahead")
        index = start + offset
        if index in orders:
            raise ValueError("Duplicate checkpoint execution queue")
        source = pending["origin_decision"]
        signal = source["signal_date"]
        if (state["asof_date"] is None or signal > state["asof_date"] or signal not in days
                or calendar.shift(date.fromisoformat(signal), execution["execution_lag_sessions"]) != when):
            raise ValueError("Checkpoint order uses a future or inconsistent signal")
        if source.get("planned_orders") != pending["orders"] or source["orders"] != len(pending["orders"]):
            raise ValueError("Checkpoint orders differ from their earlier actual decision")
        if set(source["targets"]) - codes or any(not 0 <= float(w) <= 1 for w in source["targets"].values()) or sum(source["targets"].values()) > 1 + 1e-10:
            raise ValueError("Invalid inherited target decision")
        restored = []
        for row in pending["orders"]:
            order = dict(row)
            if order["code"] not in codes or order["signal_date"] != signal or order["action"] not in ("BUY", "SELL"):
                raise ValueError("Invalid inherited order fund, side or signal date")
            if order["action"] == "BUY":
                order["budget"] = decimal_value(order["budget"], positive=True)
            else:
                if order["reason"] not in ("risk_exit", "allocation_reduction") or order["code"] not in lots:
                    raise ValueError("Invalid inherited redemption")
                if order["units"] is not None:
                    order["units"] = decimal_value(order["units"], positive=True)
            restored.append(order)
        orders[index] = restored
        origins[signal] = deepcopy(source)
    with localcontext() as ctx:
        ctx.prec = 40
        market = sum((sum((lot["units"] for lot in entries), Decimal(0)) * Decimal(expected_prices[c]) for c, entries in lots.items()), Decimal(0))
        equity = cash + market + sum((r["amount"] for r in receivables), Decimal(0))
    if not equity > 0 or decimal_value(state["reference_equity"], positive=True) != equity:
        raise ValueError("Checkpoint reference wealth differs from cash, holdings and receipts")
    return cash, lots, receivables, orders, counter, origin, equity, origins
