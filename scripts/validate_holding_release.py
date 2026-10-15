"""Audit calendar-only holding-release requests and their fresh decision dates."""
from datetime import date
from decimal import Decimal, localcontext
import math


def audit_holding_release(account, data, calendar, trace):
    if account["execution"].get("review_on_holding_release") is not True:
        raise ValueError("Holding-release audit requires the declared review option")
    days = data["days"]
    indexes = {day: i for i, day in enumerate(days)}
    start, end = indexes[account["start_date"]], indexes[account["end_date"]]
    lag = account["execution"]["execution_lag_sessions"]
    confirmation_lag = account["holding_cost_policy"]["confirmation_lag_sessions"]
    minimum = account["execution"]["minimum_holding_days"]
    traces = {entry["signal_date"]: entry for entry in trace}
    decisions = {entry["signal_date"]: entry for entry in account["decisions"]}
    reviews = account["holding_release_review_dates"]
    if reviews != sorted(set(reviews)) or any(day not in days[start:end + 1] for day in reviews):
        raise ValueError("Invalid holding-release decision dates")
    purchases = {t["lot_id"]: t for t in account["trades"] if t["action"] == "BUY"}
    equities = {r["date"]: Decimal(r["equity"]) for r in account["equity_curve"]}
    trades_by_date, events_by_date = {}, {}
    for t in account["trades"]:
        trades_by_date.setdefault(t["execution_date"], []).append(t)
    for event in account["holding_release_events"]:
        if event["blocked_date"] not in days[start:end + 1]:
            raise ValueError("Holding-release request is outside the account")
        events_by_date.setdefault(event["blocked_date"], []).append(event)

    def confirmation(day):
        return calendar.shift(date.fromisoformat(day), confirmation_lag).isoformat()

    pending, unit_balances, signal_values = {}, {}, {}
    previous_order_until = -1
    with localcontext() as ctx:
        ctx.prec = 40
        for row in account["equity_curve"]:
            day, index = row["date"], indexes[row["date"]]
            for t in trades_by_date.get(day, []):
                unit_balances[t["lot_id"]] = unit_balances.get(t["lot_id"], Decimal(0)) + Decimal(t["units"]) * (1 if t["action"] == "BUY" else -1)
            pending = {lot_id: due for lot_id, due in pending.items() if unit_balances.get(lot_id, 0) > 0}
            for event in events_by_date.get(day, []):
                lot_id = event["lot_id"]
                purchase = purchases.get(lot_id)
                if (purchase is None or purchase["code"] != event["code"] or purchase["execution_date"] != event["purchase_date"]
                        or confirmation(event["purchase_date"]) != event["purchase_confirmation_date"]
                        or unit_balances.get(lot_id, 0) <= 0 or lot_id in pending):
                    raise ValueError("Holding-release request does not identify a live blocked lot")
                signal = days[index - lag] if index >= lag else None
                if event["signal_date"] != signal or signal not in signal_values or signal not in decisions:
                    raise ValueError("Holding-release request is not based on a prior actual decision")
                code = event["code"]
                value, wealth = signal_values[signal][code], equities[signal]
                target = Decimal(str(decisions[signal]["targets"].get(code, 0))) * wealth
                tolerance = wealth * Decimal(str(account["execution"]["rebalance_band"]))
                relative = account["execution"]["relative_rebalance_band"]
                if relative is not None:
                    tolerance = min(tolerance, target * Decimal(str(relative)))
                if (purchase["execution_date"] > signal or target >= value or (target > 0 and value - target <= tolerance)
                        or code in traces[signal]["risk_exit_codes"]):
                    raise ValueError("Deferred review was not caused by an ordinary planned reduction")
                age = (date.fromisoformat(confirmation(day)) - date.fromisoformat(confirmation(purchase["execution_date"]))).days
                if day >= confirmation(purchase["execution_date"]) and age >= minimum:
                    raise ValueError("Deferred review was requested for a mature confirmed lot")
                due = next((candidate for candidate in range(index + 1, end - lag)
                            if days[candidate + lag] >= confirmation(purchase["execution_date"])
                            and (date.fromisoformat(confirmation(days[candidate + lag]))
                                 - date.fromisoformat(confirmation(purchase["execution_date"]))).days >= minimum), None)
                if (event["review_date"] != (days[due] if due is not None else None)
                        or event["eligible_execution_date"] != (days[due + lag] if due is not None else None)):
                    raise ValueError("Release deadline differs from confirmation-calendar arithmetic")
                if due is not None:
                    pending[lot_id] = due
            due_lots = {lot_id for lot_id, due in pending.items() if due <= index}
            possible = index + lag < end and previous_order_until <= index
            expected_review = bool(due_lots) and possible
            if (day in reviews) != expected_review:
                raise ValueError("Holding-release review missing, premature, stale or overlapping a pending order")
            if expected_review:
                for lot_id in due_lots:
                    pending.pop(lot_id)
                if day not in decisions or day not in traces:
                    raise ValueError("Release did not re-evaluate the latest signal")
            if day in decisions:
                if day not in traces or decisions[day]["targets"] != traces[day]["targets"]:
                    raise ValueError("Fresh release account targets do not match the audited signal trace")
                values = dict.fromkeys(data["prices"], Decimal(0))
                for lot_id, units in unit_balances.items():
                    code = purchases[lot_id]["code"]
                    values[code] += units * Decimal(str(data["prices"][code][index]))
                if any(not math.isfinite(float(v)) or v < 0 for v in values.values()):
                    raise ValueError("Invalid holding-release signal-date values")
                signal_values[day] = values
                previous_order_until = index + lag
    return {"status": "passed", "blocked_lot_requests": len(account["holding_release_events"]), "fresh_review_dates": len(reviews)}
