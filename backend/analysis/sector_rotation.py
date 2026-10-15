"""Experimental lagged sector rotation with retained holdings and cash budgets."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import date
from decimal import Decimal, ROUND_DOWN, localcontext
import statistics

from backend.analysis.simulation import SimulationCosts, _trade_to_target
from backend.analysis.strategy_portfolio import _benchmark
from backend.analysis.strategy_risk import summarize_curve_risk
from backend.analysis.fund_holding_costs import FundHoldingCosts
from backend.core.trading_calendar import TradingCalendar

VERSION = "sector-rotation-experiment-v1"


@dataclass(frozen=True)
class RotationPolicy:
    score_mode: str = "combined"
    entry_score: float = 60
    exit_score: float = 45
    review_sessions: int = 5
    max_positions: int = 10
    max_exposure: float = 1.0
    regime: str = "none"
    trailing_stop: float = .10
    daily_exits: bool = True
    ma_exit: bool = True
    volume_gate: bool = False
    volatility_sizing: bool = False
    cooldown_sessions: int = 10
    exit_confirm_sessions: int = 1
    entry_state_gate: bool = False
    regime_exit: bool = False
    soft_exit_min_calendar_days: int = 0
    allow_defensive_assets: bool = False
    entry_ma_sessions: int = 20
    exit_ma_sessions: int = 20
    recent_return_sessions: int = 5
    benchmark_ma_sessions: int = 60
    entry_min_days_to_liquidation: int = 0

    def validate(self) -> None:
        if self.score_mode not in ("combined", "short", "medium", "long"):
            raise ValueError("Unknown score mode")
        if self.regime not in ("none", "breadth", "benchmark", "both"):
            raise ValueError("Unknown market regime")
        if not 0 <= self.exit_score < self.entry_score <= 100 or not 0 < self.max_exposure <= 1:
            raise ValueError("Invalid score or exposure thresholds")
        if self.review_sessions < 1 or self.max_positions < 1 or self.cooldown_sessions < 0 or self.exit_confirm_sessions < 1 or self.soft_exit_min_calendar_days < 0 or self.entry_min_days_to_liquidation < 0:
            raise ValueError("Invalid session count or position limit")
        if not 0 <= self.trailing_stop < 1:
            raise ValueError("Invalid trailing stop")
        if self.entry_ma_sessions not in (20, 60, 120) or self.exit_ma_sessions not in (20, 60, 120):
            raise ValueError("Unsupported price moving average")
        if self.recent_return_sessions not in (5, 15, 30) or self.benchmark_ma_sessions not in (60, 120, 200):
            raise ValueError("Unsupported return or benchmark window")


def rotation_score(entry: Mapping, mode: str) -> float:
    scores = entry["scores"]
    if mode != "combined":
        return scores[mode]
    return .5 * scores["short"] + .3 * scores["medium"] + .2 * scores["long"]


def market_ready(context: Mapping, regime: str, benchmark_ma_sessions: int = 60) -> bool:
    breadth = context["breadth_above_ma60"] >= .5
    benchmark = context.get(f"benchmark_above_ma{benchmark_ma_sessions}") is True
    return {"none": True, "breadth": breadth, "benchmark": benchmark, "both": breadth and benchmark}[regime]


def entry_ready(entry: Mapping, context: Mapping, policy: RotationPolicy) -> bool:
    return (asset_market_ready(entry, context, policy)
            and rotation_score(entry, policy.score_mode) >= policy.entry_score
            and entry["price"] > entry[f"ma{policy.entry_ma_sessions}"] and entry[f"return{policy.recent_return_sessions}_pct"] > 0
            and (not policy.entry_state_gate or entry["short_state"] in ("steady_up", "strengthening"))
            and (not policy.volume_gate or entry["up_volume_ratio20"] >= .5 and entry["volume_ratio5_20"] >= .8))


def asset_market_ready(entry: Mapping, context: Mapping, policy: RotationPolicy) -> bool:
    return market_ready(context, policy.regime, policy.benchmark_ma_sessions) or policy.allow_defensive_assets and entry.get("defensive_asset") is True


def exit_reason(entry: Mapping, holding: Mapping, policy: RotationPolicy) -> str | None:
    if policy.trailing_stop and entry["price"] <= holding["peak"] * (1 - policy.trailing_stop):
        return "trailing_stop"
    if rotation_score(entry, policy.score_mode) < policy.exit_score:
        return "score_weakening"
    if policy.ma_exit and entry["price"] < entry[f"ma{policy.exit_ma_sessions}"] and entry[f"return{policy.recent_return_sessions}_pct"] < 0:
        return "ma_weakening"
    return None


def simulate_rotation(prices: Mapping[str, Sequence[float]], days: Sequence[str], features: Mapping[int, dict], *,
                      policy: RotationPolicy, start: int = 120, end: int | None = None,
                      fee_bps: Decimal = Decimal("10"), offset: int = 0,
                      initial: Decimal = Decimal("100000"), holding_costs: FundHoldingCosts | None = None,
                      calendar: TradingCalendar | None = None,
                      distributions: Mapping[str, Sequence[float]] | None = None,
                      signal_prices: Mapping[str, Sequence[float]] | None = None) -> dict:
    policy.validate()
    costs = SimulationCosts(buy_fee_bps=fee_bps, sell_fee_bps=fee_bps)
    costs.validate()
    if holding_costs:
        holding_costs.validate()
    end = len(days) - 1 if end is None else end
    if initial <= 0 or not 0 <= start < end < len(days) or offset < 0:
        raise ValueError("Invalid account interval")
    if any(len(values) != len(days) or any(not 0 < p < float("inf") for p in values) for values in prices.values()):
        raise ValueError("Missing or invalid prices")
    marks = signal_prices or prices
    if set(marks) != set(prices) or any(len(values) != len(days) or any(not 0 < p < float("inf") for p in values) for values in marks.values()):
        raise ValueError("Missing or invalid signal price grid")
    if distributions and (set(distributions) != set(prices) or any(len(values) != len(days)
                         or any(not 0 <= value < float("inf") for value in values) for values in distributions.values())):
        raise ValueError("Invalid distribution grid")
    cash, fees = initial, Decimal(0)
    holdings, cooldown = {}, {}
    pending_sells, pending_buys = {}, []
    trades, curve, decisions = [], [], []
    receivables, distribution_events = [], []
    distribution_income = Decimal(0)
    peak_equity = initial
    with localcontext() as context:
        context.prec = 40

        def sell(code, holding, day_index, reason):
            nonlocal cash, fees
            price = Decimal(str(prices[code][day_index]))
            metadata = (holding_costs.redemption(days[holding["entry_index"]], days[day_index], fee_bps, calendar)
                        if holding_costs else {})
            sale_costs = SimulationCosts(buy_fee_bps=fee_bps, sell_fee_bps=Decimal(metadata.get("sell_fee_bps", str(fee_bps))))
            cash, _, trade = _trade_to_target(cash=cash, units=holding["units"], price=price, target=Decimal(0), costs=sale_costs)
            fees += trade["fee"]
            settled = metadata.get("settlement_date", days[day_index])
            if settled > days[day_index]:
                proceeds = trade["gross_amount"] - trade["fee"]
                cash -= proceeds
                receivables.append({"settlement_date": settled, "amount": proceeds})
            trades.append({"code": code, "action": "SELL", "reason": reason,
                           "signal_date": None if reason == "terminal_liquidation" else days[day_index - 1],
                           "execution_date": days[day_index], "units": str(holding["units"]), "price": str(price),
                           "gross_amount": str(trade["gross_amount"]), "fee": str(trade["fee"]), **metadata})

        for day_index in range(start, end + 1):
            matured = [value for value in receivables if value["settlement_date"] <= days[day_index]]
            cash += sum((value["amount"] for value in matured), Decimal(0))
            receivables = [value for value in receivables if value["settlement_date"] > days[day_index]]
            if distributions:
                for code, holding in sorted(holdings.items()):
                    amount = holding["units"] * Decimal(str(distributions[code][day_index]))
                    if amount:
                        settle_lag = holding_costs.settlement_lag_sessions if holding_costs else 0
                        settled = calendar.shift(date.fromisoformat(days[day_index]), settle_lag).isoformat() if settle_lag else days[day_index]
                        if settled > days[day_index]:
                            receivables.append({"settlement_date": settled, "amount": amount})
                        else:
                            cash += amount
                        distribution_income += amount
                        distribution_events.append({"date": days[day_index], "code": code,
                                                    "per_unit": str(distributions[code][day_index]), "amount": str(amount),
                                                    "settlement_date": settled})
            # All orders were decided using the preceding close, except terminal liquidation.
            for code, reason in sorted(pending_sells.items()):
                holding = holdings.pop(code)
                sell(code, holding, day_index, reason)
                cooldown[code] = day_index + policy.cooldown_sessions
            for order in pending_buys:
                if holding_costs and (day_index == end or holding_costs.confirmation(days[day_index], calendar) > days[end]):
                    continue
                if holding_costs and policy.entry_min_days_to_liquidation:
                    final_fee = holding_costs.redemption(days[day_index], days[end], fee_bps, calendar)
                    if final_fee["holding_calendar_days"] < policy.entry_min_days_to_liquidation:
                        continue
                code = order["code"]
                budget = min(cash, order["budget"]).quantize(Decimal(".01"), rounding=ROUND_DOWN)
                if budget <= 0:
                    continue
                if holding_costs and budget < holding_costs.minimum_purchase_amount:
                    continue
                price = Decimal(str(prices[code][day_index]))
                bought, buy_fee = _benchmark({code: [{"date": days[day_index], "close": str(price)}]}, {code: budget}, fee_bps)
                gross = Decimal(bought[0]["market_value"])
                units = gross / price
                cash -= budget
                fees += buy_fee
                holdings[code] = {"units": units, "peak": marks[code][day_index], "entry_index": day_index,
                                  "weak_sessions": 0}
                trades.append({"code": code, "action": "BUY", "reason": "eligible_entry",
                               "signal_date": days[day_index - 1], "execution_date": days[day_index],
                               "units": str(units), "price": str(price), "gross_amount": str(gross), "fee": str(buy_fee)})
            pending_sells, pending_buys = {}, []
            market = sum((h["units"] * Decimal(str(prices[code][day_index])) for code, h in sorted(holdings.items())), Decimal(0))
            unsettled = sum((value["amount"] for value in receivables), Decimal(0))
            equity = cash + market + unsettled
            if day_index == end:
                for code, holding in sorted(holdings.items()):
                    sell(code, holding, day_index, "terminal_liquidation")
                holdings.clear()
                unsettled = sum((value["amount"] for value in receivables), Decimal(0))
                market, equity = Decimal(0), cash + unsettled
            if cash < 0 or equity <= 0:
                raise ArithmeticError("Invalid account balance")
            peak_equity = max(peak_equity, equity)
            curve.append({"date": days[day_index], "cash": str(cash), "market_value": str(market),
                          "receivables": str(unsettled),
                          "equity": str(equity), "positions": len(holdings),
                          "drawdown_pct": str((equity / peak_equity - 1) * 100)})
            if day_index == end:
                break
            frame = features[day_index]
            entries = {entry["code"]: entry for entry in frame["entries"]}
            review = day_index >= start + offset and (day_index - start - offset) % policy.review_sessions == 0
            for code, holding in holdings.items():
                holding["peak"] = max(holding["peak"], marks[code][day_index])
                if policy.daily_exits or review:
                    reason = exit_reason(entries[code], holding, policy)
                    if policy.regime_exit and not asset_market_ready(entries[code], frame["context"], policy):
                        reason = reason or "market_weakening"
                    holding["weak_sessions"] = holding["weak_sessions"] + 1 if reason else 0
                    if reason and (reason == "trailing_stop" or holding["weak_sessions"] >= policy.exit_confirm_sessions):
                        metadata = (holding_costs.redemption(days[holding["entry_index"]], days[day_index + 1], fee_bps, calendar)
                                    if holding_costs else {})
                        age = metadata.get("holding_calendar_days", (date.fromisoformat(days[day_index + 1]) - date.fromisoformat(days[holding["entry_index"]])).days)
                        confirmed = metadata.get("purchase_confirmation_date", days[holding["entry_index"]]) <= days[day_index + 1]
                        hard_risk = reason == "trailing_stop" or policy.regime_exit and not asset_market_ready(entries[code], frame["context"], policy)
                        if confirmed and (hard_risk or age >= policy.soft_exit_min_calendar_days):
                            pending_sells[code] = reason
            if review:
                retained = set(holdings) - set(pending_sells)
                candidates = sorted((entry for entry in entries.values()
                                     if entry["code"] not in holdings and day_index + 1 >= cooldown.get(entry["code"], 0)
                                     and entry_ready(entry, frame["context"], policy)),
                                    key=lambda entry: (-rotation_score(entry, policy.score_mode), entry["code"]))
                slots = max(0, policy.max_positions - len(retained))
                retained_value = sum((holdings[code]["units"] * Decimal(str(prices[code][day_index]))
                                      for code in sorted(retained)), Decimal(0))
                remaining_risk_budget = max(Decimal(0), equity * Decimal(str(policy.max_exposure)) - retained_value)
                for entry in candidates[:slots]:
                    weight = policy.max_exposure / policy.max_positions
                    if policy.volatility_sizing:
                        weight = min(weight, .002 / max(.005, entry["volatility20"]))
                    budget = min(equity * Decimal(str(weight)), remaining_risk_budget)
                    if budget > 0:
                        pending_buys.append({"code": entry["code"], "budget": budget})
                        remaining_risk_budget -= budget
                decisions.append({"signal_date": days[day_index], "context": frame["context"],
                                  "retained": sorted(retained), "sell": dict(pending_sells),
                                  "buy": [order["code"] for order in pending_buys]})
        years = []
        for year in sorted({point["date"][:4] for point in curve}):
            indexes = [i for i, point in enumerate(curve) if point["date"].startswith(year)]
            baseline = curve[max(0, indexes[0] - 1)]
            final = curve[indexes[-1]]
            years.append({"year": year, "return_pct": float((Decimal(final["equity"]) / Decimal(baseline["equity"]) - 1) * 100)})
        return {"policy": asdict(policy), "fee_bps_each_side": float(fee_bps),
                "start_date": days[start], "end_date": days[end], "initial_equity": str(initial),
                "final_equity": str(equity), "total_return_pct": float((equity / initial - 1) * 100),
                "holding_cost_policy": holding_costs.document() if holding_costs else None,
                "short_redemption_sales": sum(t.get("short_redemption_fee_applied", False) for t in trades),
                "distribution_income": str(distribution_income), "distribution_events": distribution_events,
                "final_receivables": str(unsettled),
                "max_drawdown_pct": float(summarize_curve_risk(curve)["max_drawdown_pct"]),
                "total_fees": str(fees), "trade_count": len(trades), "year_returns": years,
                "mean_market_exposure_pct": statistics.fmean(float(Decimal(p["market_value"]) / Decimal(p["equity"]) * 100) for p in curve),
                "equity_curve": curve, "trades": trades, "decisions": decisions}
