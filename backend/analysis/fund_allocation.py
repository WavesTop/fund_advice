"""Experimental cash-funded target allocations with FIFO fund redemption lots."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from copy import deepcopy
from datetime import date
from decimal import Decimal, ROUND_DOWN, localcontext
import math
from types import MappingProxyType
from typing import Callable, Mapping

from backend.analysis.fund_holding_costs import FundHoldingCosts
from backend.analysis.allocation_checkpoint import make_checkpoint, restore_checkpoint, serialize_orders
from backend.analysis.strategy_risk import summarize_curve_risk
from backend.core.trading_calendar import TradingCalendar


@dataclass(frozen=True)
class AllocationExecution:
    review_sessions: int = 20
    execution_lag_sessions: int = 1
    rebalance_band: float = .05
    minimum_holding_days: int = 7
    reinvest_on_settlement: bool = True
    deadline_entry_guard_days: int = 0
    relative_rebalance_band: float | None = None
    initial_review: bool = False
    review_on_holding_release: bool = False

    def validate(self):
        if not isinstance(self.initial_review, bool):
            raise ValueError("Initial allocation review must be boolean")
        if not isinstance(self.review_on_holding_release, bool):
            raise ValueError("Holding release review must be boolean")
        if any(not isinstance(n, int) or isinstance(n, bool) or n < minimum for n, minimum in
               ((self.review_sessions, 1), (self.execution_lag_sessions, 1), (self.minimum_holding_days, 0),
                (self.deadline_entry_guard_days, 0))):
            raise ValueError("Invalid allocation timing")
        if not math.isfinite(self.rebalance_band) or not 0 <= self.rebalance_band < 1:
            raise ValueError("Invalid rebalance band")
        if self.relative_rebalance_band is not None and (not math.isfinite(self.relative_rebalance_band)
                                                        or not 0 <= self.relative_rebalance_band < 1):
            raise ValueError("Invalid relative rebalance band")


@dataclass(frozen=True)
class AllocationTargets:
    weights: dict[str, float]
    risk_exits: frozenset[str] = frozenset()
    enforce_risk_exits: bool = False


@dataclass(frozen=True)
class AllocationState:
    signal_index: int
    signal_date: str
    weights: Mapping[str, float]


def simulate_allocations(data: dict, calendar: TradingCalendar, allocator: Callable, *, start=252, end=None,
                         execution=AllocationExecution(), costs=FundHoldingCosts(), fee_bps=Decimal(10),
                         initial=Decimal(100000), phase=0, extra_review_indices=frozenset(),
                         include_portfolio_state=False, initial_state=None, liquidate_at_end=True,
                         capture_state_indices=frozenset(), resume_review=False) -> dict:
    """Plan money/units from signal-date prices, never target from execution NAV."""
    execution.validate()
    costs.validate()
    if not isinstance(include_portfolio_state, bool):
        raise ValueError("Portfolio state option must be boolean")
    days, prices = data["days"], data["prices"]
    if not isinstance(extra_review_indices, frozenset) or any(not isinstance(i, int) or isinstance(i, bool)
                                                             or not 0 <= i < len(days) for i in extra_review_indices):
        raise ValueError("Extra allocation reviews require valid absolute session indices")
    end = len(days) - 1 if end is None else end
    if not isinstance(liquidate_at_end, bool) or not isinstance(resume_review, bool):
        raise ValueError("Account continuation options must be boolean")
    if not isinstance(capture_state_indices, frozenset) or any(
            not isinstance(i, int) or isinstance(i, bool) or not start <= i <= end for i in capture_state_indices):
        raise ValueError("Checkpoint captures require valid interval indices")
    state_mode = initial_state is not None or not liquidate_at_end or bool(capture_state_indices)
    if resume_review and initial_state is None:
        raise ValueError("A fork review requires an initial account state")
    if state_mode and (execution.review_on_holding_release or execution.deadline_entry_guard_days):
        raise ValueError("Continuation does not yet support holding-release clocks or known-end entry guards")
    if state_mode and days != [d.isoformat() for d in calendar.sessions(date.fromisoformat(days[0]), date.fromisoformat(days[-1]))]:
        raise ValueError("Account continuation requires a complete exchange-session date grid")
    if initial_state is not None and initial != Decimal(100000):
        raise ValueError("Seeded accounts derive their initial wealth from the checkpoint")
    if not 0 <= start < end < len(days) or initial <= 0 or phase < 0:
        raise ValueError("Invalid allocation interval")
    if not fee_bps.is_finite() or not 0 <= fee_bps < 10000:
        raise ValueError("Invalid allocation fee")
    if any(len(p) != len(days) or any(not math.isfinite(v) or v <= 0 for v in p) for p in prices.values()):
        raise ValueError("Invalid allocation prices")
    distributions = data["distributions"]
    if set(distributions) != set(prices) or any(len(p) != len(days) or any(not math.isfinite(v) or v < 0 for v in p)
                                              for p in distributions.values()):
        raise ValueError("Invalid allocation distributions")
    trades, curve, decisions, distribution_events = [], [], [], []
    lots, receivables, orders = {}, [], {}
    holding_reviews, holding_events, holding_review_dates = {}, [], []
    cash, fees, distribution_income = initial, Decimal(0), Decimal(0)
    fee_rate, lot_counter = fee_bps / 10000, 0
    origin, decision_origins, captured = start, {}, {}
    with localcontext() as ctx:
        ctx.prec = 40
        if initial_state is not None:
            cash, lots, receivables, orders, lot_counter, origin, initial, decision_origins = restore_checkpoint(
                initial_state, data, calendar, start, phase, asdict(execution), costs.document(), fee_bps)

        def checkpoint(resume_index):
            return make_checkpoint(data, calendar, resume_index, origin, phase, asdict(execution),
                                   costs.document(), fee_bps, cash, lots, receivables, orders,
                                   lot_counter, decision_origins)

        def credit(amount, settlement, today):
            nonlocal cash
            if settlement > today:
                receivables.append({"settlement_date": settlement, "amount": amount})
            else:
                cash += amount

        def defer_holding_review(lot, code, index, signal):
            if not execution.review_on_holding_release or lot["id"] in holding_reviews:
                return
            purchase_confirmed = costs.confirmation(lot["date"], calendar)
            due = None
            # Schedule a fresh decision, never a deferred order using an old target.
            for candidate in range(index + 1, end - execution.execution_lag_sessions):
                execution_day = days[candidate + execution.execution_lag_sessions]
                age = (date.fromisoformat(costs.confirmation(execution_day, calendar))
                       - date.fromisoformat(purchase_confirmed)).days
                if execution_day >= purchase_confirmed and age >= execution.minimum_holding_days:
                    due = candidate
                    break
            holding_events.append({"signal_date": signal, "blocked_date": days[index], "code": code,
                                   "lot_id": lot["id"], "purchase_date": lot["date"],
                                   "purchase_confirmation_date": purchase_confirmed,
                                   "review_date": days[due] if due is not None else None,
                                   "eligible_execution_date": days[due + execution.execution_lag_sessions] if due is not None else None})
            if due is not None:
                holding_reviews[lot["id"]] = due

        def redeem(code, wanted, index, reason, signal):
            nonlocal fees
            remaining = wanted
            for lot in list(lots.get(code, [])):
                if remaining is not None and remaining <= 0:
                    break
                if costs.confirmation(lot["date"], calendar) > days[index]:
                    if reason not in ("terminal_liquidation", "risk_exit"):
                        defer_holding_review(lot, code, index, signal)
                    continue
                metadata = costs.redemption(lot["date"], days[index], fee_bps, calendar)
                if reason not in ("terminal_liquidation", "risk_exit") and metadata["holding_calendar_days"] < execution.minimum_holding_days:
                    defer_holding_review(lot, code, index, signal)
                    continue
                units = lot["units"] if remaining is None else min(lot["units"], remaining)
                price = Decimal(str(prices[code][index]))
                gross = units * price
                fee = gross * Decimal(metadata["sell_fee_bps"]) / 10000
                fees += fee
                credit(gross - fee, metadata["settlement_date"], days[index])
                trades.append({"action": "SELL", "code": code, "lot_id": lot["id"], "reason": reason,
                               "signal_date": signal, "execution_date": days[index], "units": str(units),
                               "price": str(price), "gross_amount": str(gross), "fee": str(fee), **metadata})
                lot["units"] -= units
                if remaining is not None:
                    remaining -= units
                if lot["units"] == 0:
                    lots[code].remove(lot)
            if not lots.get(code):
                lots.pop(code, None)

        for index in range(start, end + 1):
            if index in capture_state_indices:
                captured[days[index]] = checkpoint(index)
            cash_released = any(r["settlement_date"] <= days[index] for r in receivables)
            cash += sum((r["amount"] for r in receivables if r["settlement_date"] <= days[index]), Decimal(0))
            receivables = [r for r in receivables if r["settlement_date"] > days[index]]
            for code, entries in sorted(lots.items()):
                per_unit = Decimal(str(distributions[code][index]))
                if per_unit:
                    amount = sum((lot["units"] for lot in entries), Decimal(0)) * per_unit
                    settlement = calendar.shift(date.fromisoformat(days[index]), costs.settlement_lag_sessions).isoformat()
                    credit(amount, settlement, days[index])
                    distribution_income += amount
                    distribution_events.append({"date": days[index], "code": code, "amount": str(amount),
                                                "per_unit": str(per_unit), "settlement_date": settlement})
            scheduled = orders.pop(index, [])
            for order in scheduled:
                if order["action"] == "SELL":
                    redeem(order["code"], order["units"], index, order["reason"], order["signal_date"])
            # Sales remain receivables; today's unknown proceeds never finance buys.
            for order in scheduled:
                if order["action"] != "BUY":
                    continue
                if liquidate_at_end and (costs.confirmation(days[index], calendar) > days[end]
                        or costs.redemption(days[index], days[end], fee_bps, calendar)["holding_calendar_days"] < execution.deadline_entry_guard_days):
                    continue
                budget = min(cash, order["budget"]).quantize(Decimal(".01"), rounding=ROUND_DOWN)
                if budget < costs.minimum_purchase_amount or budget <= 0:
                    continue
                code = order["code"]
                price = Decimal(str(prices[code][index]))
                units = budget / (1 + fee_rate) / price
                gross = units * price
                fee = gross * fee_rate
                cash -= gross + fee
                fees += fee
                lot_counter += 1
                lots.setdefault(code, []).append({"id": lot_counter, "units": units, "date": days[index]})
                trades.append({"action": "BUY", "code": code, "lot_id": lot_counter, "reason": "allocation_increase",
                               "signal_date": order["signal_date"], "execution_date": days[index], "units": str(units),
                               "price": str(price), "gross_amount": str(gross), "fee": str(fee)})
            if index == end and liquidate_at_end:
                for code in sorted(list(lots)):
                    redeem(code, None, index, "terminal_liquidation", None)
                if lots:
                    raise ArithmeticError("Unconfirmed lot prevents terminal liquidation")
            market = sum((sum((l["units"] for l in entries), Decimal(0)) * Decimal(str(prices[code][index]))
                          for code, entries in lots.items()), Decimal(0))
            unsettled = sum((r["amount"] for r in receivables), Decimal(0))
            equity = cash + market + unsettled
            if cash < Decimal("-1e-25") or equity <= 0:
                raise ArithmeticError("Invalid allocation balance")
            curve.append({"date": days[index], "cash": str(cash), "market_value": str(market),
                          "receivables": str(unsettled), "equity": str(equity), "positions": len(lots)})
            if liquidate_at_end and index + execution.execution_lag_sessions >= end:
                continue
            if execution.review_on_holding_release:
                alive = {lot["id"] for entries in lots.values() for lot in entries}
                holding_reviews = {lot_id: due for lot_id, due in holding_reviews.items() if lot_id in alive}
            released = {lot_id for lot_id, due in holding_reviews.items() if due <= index}
            review = ((execution.initial_review and index == origin)
                      or (resume_review and index == start)
                      or index in extra_review_indices
                      or bool(released)
                      or (index >= origin + phase and (index - origin - phase) % execution.review_sessions == 0))
            if not review and not (execution.reinvest_on_settlement and cash_released):
                continue
            # Unit orders queued for later execution may not overlap a new review.
            if orders:
                continue
            if released:
                holding_review_dates.append(days[index])
                for lot_id in released:
                    holding_reviews.pop(lot_id)
            held = set(lots)
            if include_portfolio_state:
                weights = {code: float(sum((l["units"] for l in lots.get(code, [])), Decimal(0))
                                       * Decimal(str(prices[code][index])) / equity) for code in prices}
                state = AllocationState(index, days[index], MappingProxyType(weights))
                targets = allocator(index, held, state)
            else:
                targets = allocator(index, held)
            if targets is None:
                continue
            risk_exits = targets.risk_exits if isinstance(targets, AllocationTargets) else frozenset()
            enforce_exits = targets.enforce_risk_exits if isinstance(targets, AllocationTargets) else False
            if not isinstance(enforce_exits, bool):
                raise ValueError("Risk exit enforcement must be boolean")
            targets = targets.weights if isinstance(targets, AllocationTargets) else targets
            if set(targets) - set(prices) or any(not math.isfinite(v) or v < 0 for v in targets.values()) or sum(targets.values()) > 1 + 1e-10:
                raise ValueError("Invalid target allocation")
            future, planned, cash_budget = index + execution.execution_lag_sessions, [], cash
            requested_buys, funded_buys, ignored_sells = Decimal(0), Decimal(0), Decimal(0)
            for code in sorted(prices):
                units = sum((l["units"] for l in lots.get(code, [])), Decimal(0))
                value = units * Decimal(str(prices[code][index]))
                target = Decimal(str(targets.get(code, 0))) * equity
                delta = target - value
                tolerance = equity * Decimal(str(execution.rebalance_band))
                if execution.relative_rebalance_band is not None:
                    tolerance = min(tolerance, target * Decimal(str(execution.relative_rebalance_band)))
                if (units and target > 0 and abs(delta) <= tolerance
                        and not (enforce_exits and code in risk_exits and delta < 0)):
                    if delta < 0:
                        ignored_sells -= delta
                    continue
                if delta < 0 and units:
                    # Full exits consume each eligible lot exactly; a rounded sum
                    # can leave dust that spuriously wakes settlement reviews.
                    planned.append({"action": "SELL", "code": code, "units": None if target == 0 else min(units, -delta / Decimal(str(prices[code][index]))),
                                    "signal_date": days[index], "reason": "risk_exit" if code in risk_exits else "allocation_reduction"})
                elif delta > 0:
                    requested_buys += delta
                    budget = min(delta, cash_budget)
                    if budget >= costs.minimum_purchase_amount and budget > 0:
                        planned.append({"action": "BUY", "code": code, "budget": budget, "signal_date": days[index]})
                        cash_budget -= budget
                        funded_buys += budget
            orders[future] = planned
            decisions.append({"signal_date": days[index], "targets": targets, "orders": len(planned),
                              "requested_buy_budget": str(requested_buys), "scheduled_buy_budget": str(funded_buys),
                              "cash_funding_gap": str(requested_buys - funded_buys), "overweight_ignored_by_band": str(ignored_sells)})
            if state_mode:
                decisions[-1].update(planned_orders=serialize_orders(planned), risk_exit_codes=sorted(risk_exits),
                                     enforce_risk_exits=enforce_exits)
                decision_origins[days[index]] = deepcopy(decisions[-1])
        result = {"start_date": days[start], "end_date": days[end], "initial_equity": str(initial),
                  "final_equity": str(equity), "total_return_pct": float((equity / initial - 1) * 100),
                  "total_fees": str(fees), "trade_count": len(trades), "fee_bps_each_side": str(fee_bps),
                  "execution": asdict(execution), "holding_cost_policy": costs.document(),
                  "extra_review_dates": [days[i] for i in sorted(extra_review_indices) if start <= i <= end],
                  "short_redemption_sales": sum(t.get("short_redemption_fee_applied", False) for t in trades),
                  "distribution_income": str(distribution_income), "distribution_events": distribution_events,
                  "final_receivables": str(unsettled), "trades": trades, "equity_curve": curve, "decisions": decisions}
        risk_curve = curve if initial_state is None or initial_state["asof_date"] is None else [
            {"date": initial_state["asof_date"], "equity": str(initial)}, *curve]
        result["max_drawdown_pct"] = float(summarize_curve_risk(risk_curve)["max_drawdown_pct"])
        if initial_state is not None:
            result.update(initial_state=deepcopy(initial_state), resume_review=resume_review)
        if state_mode:
            result["phase"] = phase
        if capture_state_indices:
            result["account_checkpoints"] = captured
        if not liquidate_at_end:
            result.update(liquidate_at_end=False, valuation_basis="NAV_mark_including_unsettled_receipts",
                          final_state=checkpoint(end + 1))
        if execution.review_on_holding_release:
            result.update(holding_release_events=holding_events, holding_release_review_dates=holding_review_dates)
        return result
