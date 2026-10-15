"""Independent accounting audit for partial redemptions and multiple fund lots."""
from collections import defaultdict
from datetime import date
from decimal import Decimal, localcontext
from scripts.validate_allocation_checkpoint import validate_checkpoint_lineage


def validate_allocation(result, data, calendar):
    indexes = {d: i for i, d in enumerate(data["days"])}
    decisions = {d["signal_date"]: d for d in result["decisions"]}
    seed = result.get("initial_state")
    liquidated = result.get("liquidate_at_end", True)
    if not liquidated and result.get("valuation_basis") != "NAV_mark_including_unsettled_receipts":
        raise ValueError("Nonliquidating account must disclose its marked valuation basis")
    if seed:
        for queued in seed["queued_orders"]:
            source = queued["origin_decision"]
            decisions[source["signal_date"]] = source
    dates = data["days"][indexes[result["start_date"]]:indexes[result["end_date"]] + 1]
    if [p["date"] for p in result["equity_curve"]] != dates:
        raise ValueError("Incomplete allocation date grid")
    by_day, dividends = defaultdict(list), defaultdict(list)
    for trade in result["trades"]:
        if trade["execution_date"] not in dates:
            raise ValueError("Trade outside allocation interval")
        by_day[trade["execution_date"]].append(trade)
    for event in result["distribution_events"]:
        if event["date"] not in dates:
            raise ValueError("Distribution outside allocation interval")
        dividends[event["date"]].append(event)
    max_error = Decimal(0)
    costs = result["holding_cost_policy"]

    def check(a, b, message):
        nonlocal max_error
        error = abs(Decimal(str(a)) - Decimal(str(b)))
        max_error = max(max_error, error)
        if error > Decimal("1e-8"):
            raise ValueError(message)

    with localcontext() as ctx:
        ctx.prec = 40
        cash = Decimal(seed["cash"]) if seed else Decimal(result["initial_equity"])
        peak, fee_total, income, drawdown = Decimal(result["initial_equity"]), Decimal(0), Decimal(0), Decimal(0)
        lots, seen_ids, receivables, short_sales = {}, set(), [], 0
        if seed:
            for code, entries in seed["lots"].items():
                for entry in entries:
                    lots[entry["id"]] = {"code": code, "units": Decimal(entry["units"]), "date": entry["date"]}
                    seen_ids.add(entry["id"])
            receivables = [(r["settlement_date"], Decimal(r["amount"])) for r in seed["receivables"]]
            check(result["initial_equity"], seed["reference_equity"], "Seeded account initial wealth mismatch")

        def confirmation(day):
            return calendar.shift(date.fromisoformat(day), costs["confirmation_lag_sessions"]).isoformat()

        def credit(amount, settled, today):
            nonlocal cash
            if settled > today:
                receivables.append((settled, amount))
            else:
                cash += amount

        for point in result["equity_curve"]:
            today = point["date"]
            index = indexes[today]
            cash += sum((v for d, v in receivables if d <= today), Decimal(0))
            receivables = [(d, v) for d, v in receivables if d > today]
            entitled = {lot["code"] for lot in lots.values() if data["distributions"][lot["code"]][index] > 0}
            if sorted(e["code"] for e in dividends[today]) != sorted(entitled):
                raise ValueError("Missing or duplicate dividend entitlement")
            for event in dividends[today]:
                code = event["code"]
                per_unit = Decimal(str(data["distributions"][code][index]))
                amount = sum((l["units"] for l in lots.values() if l["code"] == code), Decimal(0)) * per_unit
                check(event["per_unit"], per_unit, "Dividend per unit mismatch")
                check(event["amount"], amount, "Dividend amount mismatch")
                settled = calendar.shift(date.fromisoformat(today), costs["settlement_lag_sessions"]).isoformat()
                if event["settlement_date"] != settled:
                    raise ValueError("Dividend settlement mismatch")
                credit(amount, settled, today)
                income += amount
            for trade in by_day[today]:
                code, lot_id = trade["code"], trade["lot_id"]
                units, price = Decimal(trade["units"]), Decimal(trade["price"])
                gross, fee = Decimal(trade["gross_amount"]), Decimal(trade["fee"])
                if units <= 0 or price <= 0 or gross <= 0 or fee < 0:
                    raise ValueError("Invalid lot trade amounts")
                check(price, data["prices"][code][index], "Price not from source NAV")
                check(gross, units * price, "Lot gross mismatch")
                terminal = trade["reason"] == "terminal_liquidation"
                lag = result["execution"]["execution_lag_sessions"]
                if terminal:
                    if not liquidated or today != dates[-1] or trade["action"] != "SELL" or trade["signal_date"] is not None:
                        raise ValueError("Invalid terminal lot sale")
                elif index < lag or trade["signal_date"] != data["days"][index - lag]:
                    raise ValueError("Allocation trade used wrong information date")
                rate = Decimal(result["fee_bps_each_side"]) / 10000
                if trade["action"] == "BUY":
                    if lot_id in seen_ids or gross + fee < costs["minimum_purchase_amount"]:
                        raise ValueError("Duplicate lot or below-minimum purchase")
                    seen_ids.add(lot_id)
                    if liquidated and confirmation(today) > dates[-1]:
                        raise ValueError("Purchase cannot confirm within account interval")
                    lots[lot_id] = {"code": code, "units": units, "date": today}
                    cash -= gross + fee
                elif trade["action"] == "SELL":
                    lot = lots.get(lot_id)
                    if lot is None or lot["code"] != code or units > lot["units"]:
                        raise ValueError("Sale exceeds matching lot")
                    if confirmation(lot["date"]) > today:
                        raise ValueError("Sale precedes purchase confirmation")
                    if not terminal:
                        decision = decisions.get(trade["signal_date"])
                        if decision is None:
                            raise ValueError("Sale is missing its actual allocation decision")
                        if decision["targets"].get(code, 0) == 0 and units != lot["units"]:
                            raise ValueError("Full target exit did not consume the entire eligible lot")
                    age = (date.fromisoformat(confirmation(today)) - date.fromisoformat(confirmation(lot["date"]))).days
                    minimum = result["execution"]["minimum_holding_days"]
                    if not terminal and trade["reason"] != "risk_exit" and age < minimum:
                        raise ValueError("Ordinary rebalance ignored minimum holding period")
                    eligible = [key for key, value in lots.items() if value["code"] == code
                                and confirmation(value["date"]) <= today]
                    if lot_id != min(eligible):
                        raise ValueError("Redemption did not consume the oldest eligible lot")
                    short = age < costs["short_holding_days"]
                    rate = max(rate, Decimal(costs["short_redemption_bps"]) / 10000) if short else rate
                    if (trade["holding_calendar_days"] != age or trade["purchase_date"] != lot["date"]
                            or trade["purchase_confirmation_date"] != confirmation(lot["date"])
                            or trade["redemption_confirmation_date"] != confirmation(today)
                            or trade["short_redemption_fee_applied"] != short):
                        raise ValueError("Incorrect lot holding age or confirmation")
                    check(Decimal(trade["sell_fee_bps"]) / 10000, rate, "Incorrect short redemption rate")
                    settled = calendar.shift(date.fromisoformat(today), costs["settlement_lag_sessions"]).isoformat()
                    if trade["settlement_date"] != settled:
                        raise ValueError("Incorrect redemption settlement")
                    credit(gross - fee, settled, today)
                    lot["units"] -= units
                    if lot["units"] == 0:
                        del lots[lot_id]
                    short_sales += short
                else:
                    raise ValueError("Unknown lot action")
                check(fee, gross * rate, "Incorrect lot fee")
                fee_total += fee
                if cash < Decimal("-1e-8"):
                    raise ValueError("Unsettled redemption financed a purchase")
            source_day = data["days"][index - result["execution"]["execution_lag_sessions"]] if index >= result["execution"]["execution_lag_sessions"] else None
            source = decisions.get(source_day)
            if source:
                urgent = {t["code"] for t in by_day[today] if t["action"] == "SELL" and t["reason"] == "risk_exit"}
                for lot in lots.values():
                    age = (date.fromisoformat(confirmation(today)) - date.fromisoformat(confirmation(lot["date"]))).days
                    if (source["targets"].get(lot["code"], 0) == 0 and lot["date"] <= source_day
                            and confirmation(lot["date"]) <= today
                            and (age >= result["execution"]["minimum_holding_days"] or lot["code"] in urgent)):
                        raise ValueError("An eligible lot remains after a full target exit")
            market = sum((l["units"] * Decimal(str(data["prices"][l["code"]][index])) for l in lots.values()), Decimal(0))
            unsettled = sum((v for _, v in receivables), Decimal(0))
            equity = cash + market + unsettled
            for field, value in (("cash", cash), ("market_value", market), ("receivables", unsettled), ("equity", equity)):
                check(point[field], value, f"Incorrect allocation {field}")
            if point["positions"] != len({l["code"] for l in lots.values()}):
                raise ValueError("Position count mismatch")
            peak = max(peak, equity)
            drawdown = min(drawdown, equity / peak - 1)
        if lots and liquidated:
            raise ValueError("Allocation lots not liquidated")
        check(result["final_equity"], equity, "Final assets mismatch")
        check(result["total_fees"], fee_total, "Total lot fees mismatch")
        check(result["distribution_income"], income, "Distribution income mismatch")
        check(result["final_receivables"], unsettled, "Final settlement mismatch")
        check(result["max_drawdown_pct"], drawdown * 100, "Drawdown mismatch")
        check(result["total_return_pct"], (equity / Decimal(result["initial_equity"]) - 1) * 100, "Return mismatch")
        if short_sales != result["short_redemption_sales"] or result["trade_count"] != len(result["trades"]):
            raise ValueError("Lot trade counts mismatch")
        for state in result.get("account_checkpoints", {}).values():
            validate_checkpoint_lineage(state, result, data, calendar)
        if not liquidated:
            validate_checkpoint_lineage(result["final_state"], result, data, calendar)
    return {"status": "passed", "dates": len(dates), "trades": result["trade_count"], "max_error": str(max_error)}
