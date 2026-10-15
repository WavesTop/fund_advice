"""Independently reconstruct cash, holdings and fees from a rotation ledger."""
from __future__ import annotations

import argparse
from collections import defaultdict
from decimal import Decimal, localcontext
from datetime import date
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.core.config import Settings
from backend.core.trading_calendar import TradingCalendar
from scripts.optimize_sector_rotation import prepare, source_hashes

TOLERANCE = Decimal("0.00000001")


def validate_ledger(result: dict, days: list[str], prices: dict[str, list[float]], *,
                    calendar: TradingCalendar | None = None, distributions: dict[str, list[float]] | None = None) -> dict:
    day_indexes = {day: i for i, day in enumerate(days)}
    curves = result["equity_curve"]
    expected_dates = days[day_indexes[result["start_date"]]:day_indexes[result["end_date"]] + 1]
    if [point["date"] for point in curves] != expected_dates:
        raise ValueError("Equity curve does not match the complete observation grid")
    by_day = defaultdict(list)
    distribution_by_day = defaultdict(list)
    for event in result.get("distribution_events", []):
        distribution_by_day[event["date"]].append(event)
    for trade in result["trades"]:
        if trade["execution_date"] not in expected_dates:
            raise ValueError("Trade is outside the evaluation interval")
        by_day[trade["execution_date"]].append(trade)
    max_error = Decimal(0)
    with localcontext() as context:
        context.prec = 40
        cash = Decimal(result["initial_equity"])
        initial, peak, fees, drawdown = cash, cash, Decimal(0), Decimal(0)
        holdings = {}
        entry_dates, receivables = {}, []
        fund_costs = result.get("holding_cost_policy")
        short_sales = 0
        fee_rate = Decimal(str(result["fee_bps_each_side"])) / 10000

        def check(actual: Decimal, expected: Decimal, message: str) -> None:
            nonlocal max_error
            error = abs(actual - expected)
            max_error = max(max_error, error)
            if error > TOLERANCE:
                raise ValueError(message)

        for point in curves:
            index = day_indexes[point["date"]]
            cash += sum((r["amount"] for r in receivables if r["settlement_date"] <= point["date"]), Decimal(0))
            receivables = [r for r in receivables if r["settlement_date"] > point["date"]]
            events = distribution_by_day[point["date"]]
            expected_codes = [code for code in holdings if distributions and distributions[code][index] > 0]
            if sorted(e["code"] for e in events) != sorted(expected_codes):
                raise ValueError("Distribution ledger does not match source entitlements")
            for event in events:
                per_unit = Decimal(str(distributions[event["code"]][index]))
                check(Decimal(event["per_unit"]), per_unit, "Distribution differs from source")
                amount = per_unit * holdings[event["code"]]
                check(Decimal(event["amount"]), amount, "Distribution amount does not match held units")
                settle_lag = fund_costs["settlement_lag_sessions"] if fund_costs else 0
                settled = calendar.shift(date.fromisoformat(point["date"]), settle_lag).isoformat() if settle_lag else point["date"]
                if event.get("settlement_date", point["date"]) != settled:
                    raise ValueError("Distribution settlement date is incorrect")
                if settled > point["date"]:
                    receivables.append({"settlement_date": settled, "amount": amount})
                else:
                    cash += amount
            for trade in by_day[point["date"]]:
                code = trade["code"]
                price, units = Decimal(trade["price"]), Decimal(trade["units"])
                gross, fee = Decimal(trade["gross_amount"]), Decimal(trade["fee"])
                if price <= 0 or units <= 0 or gross <= 0 or fee < 0:
                    raise ValueError("Trade has invalid amounts")
                if trade["reason"] == "terminal_liquidation":
                    if trade["action"] != "SELL" or point["date"] != result["end_date"] or trade["signal_date"] is not None:
                        raise ValueError("Invalid terminal liquidation")
                elif index == 0 or trade["signal_date"] != days[index - 1]:
                    raise ValueError("Trade was not executed one session after its signal")
                check(price, Decimal(str(prices[code][index])), "Execution price differs from stored close")
                check(gross, units * price, "Gross amount differs from units times price")
                expected_rate = fee_rate
                expected_settled = point["date"]
                if trade["action"] == "SELL" and fund_costs:
                    bought = entry_dates.get(code)
                    if bought is None:
                        raise ValueError("Redemption has no purchase date")
                    lag = fund_costs["confirmation_lag_sessions"]
                    if (lag or fund_costs["settlement_lag_sessions"]) and calendar is None:
                        raise ValueError("Confirmation/settlement validation needs a calendar")
                    buy_confirm = calendar.shift(date.fromisoformat(bought), lag).isoformat() if lag else bought
                    sell_confirm = calendar.shift(date.fromisoformat(point["date"]), lag).isoformat() if lag else point["date"]
                    holding_days = (date.fromisoformat(sell_confirm) - date.fromisoformat(buy_confirm)).days
                    short = holding_days < fund_costs["short_holding_days"]
                    if short:
                        expected_rate = max(expected_rate, Decimal(fund_costs["short_redemption_bps"]) / 10000)
                        short_sales += 1
                    if (trade.get("holding_calendar_days") != holding_days or trade.get("purchase_confirmation_date") != buy_confirm
                            or trade.get("redemption_confirmation_date") != sell_confirm
                            or trade.get("short_redemption_fee_applied") != short
                            or Decimal(trade.get("sell_fee_bps", "-1")) / 10000 != expected_rate):
                        raise ValueError("Holding-day fee metadata is incorrect")
                    settle_lag = fund_costs["settlement_lag_sessions"]
                    expected_settled = calendar.shift(date.fromisoformat(point["date"]), settle_lag).isoformat() if settle_lag else point["date"]
                    if trade.get("settlement_date") != expected_settled:
                        raise ValueError("Redemption settlement date is incorrect")
                check(fee, gross * expected_rate, "Fee is not the declared rate")
                if trade["action"] == "BUY":
                    if fund_costs and gross + fee < fund_costs.get("minimum_purchase_amount", 0):
                        raise ValueError("Purchase is below the modelled fund minimum")
                    if code in holdings:
                        raise ValueError("Unexpected repeated purchase of an existing holding")
                    cash -= gross + fee
                    holdings[code] = units
                    entry_dates[code] = point["date"]
                elif trade["action"] == "SELL":
                    if code not in holdings:
                        raise ValueError("Sale has no holding")
                    check(units, holdings.pop(code), "Sale units do not match the full holding")
                    entry_dates.pop(code)
                    if expected_settled > point["date"]:
                        receivables.append({"settlement_date": expected_settled, "amount": gross - fee})
                    else:
                        cash += gross - fee
                else:
                    raise ValueError("Unknown action")
                fees += fee
                if cash < -TOLERANCE:
                    raise ValueError("Cash became negative")
            market = sum((units * Decimal(str(prices[code][index])) for code, units in sorted(holdings.items())), Decimal(0))
            unsettled = sum((r["amount"] for r in receivables), Decimal(0))
            equity = cash + market + unsettled
            check(cash, Decimal(point["cash"]), "Reported cash does not match reconstructed cash")
            check(market, Decimal(point["market_value"]), "Reported holdings do not match reconstructed holdings")
            check(equity, Decimal(point["equity"]), "Reported equity does not match reconstructed equity")
            check(unsettled, Decimal(point.get("receivables", "0")), "Unsettled cash is incorrect")
            if len(holdings) != point["positions"]:
                raise ValueError("Position count is incorrect")
            peak = max(peak, equity)
            drawdown = min(drawdown, equity / peak - 1)
        if holdings:
            raise ValueError("Account was not liquidated at the end")
        check(cash + unsettled, Decimal(result["final_equity"]), "Final equity is incorrect")
        check(fees, Decimal(result["total_fees"]), "Total fees are incorrect")
        check(((cash + unsettled) / initial - 1) * 100, Decimal(str(result["total_return_pct"])), "Total return is incorrect")
        check(drawdown * 100, Decimal(str(result["max_drawdown_pct"])), "Maximum drawdown is incorrect")
        if fund_costs and short_sales != result["short_redemption_sales"]:
            raise ValueError("Short-holding redemption count is incorrect")
    return {"status": "passed", "dates": len(curves), "trades": len(result["trades"]),
            "max_error": str(max_error), "tolerance": str(TOLERANCE)}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", type=Path, default=Settings.from_env().database_path)
    parser.add_argument("--report", type=Path, default=ROOT / "runtime/verification/sector-rotation-optimization.json")
    parser.add_argument("--output", type=Path, default=ROOT / "runtime/verification/sector-rotation-ledger-validation.json")
    args = parser.parse_args()
    report = json.loads(args.report.read_text(encoding="utf-8"))
    _, days, _, data = prepare(args.database)
    if data["audit"] != report["data_audit"]:
        raise ValueError("Source data changed since the optimization report was generated")
    if report["source_hashes"] != source_hashes():
        raise ValueError("Simulation source changed since the report was generated")
    if "result" in report:
        results = {"frozen": report["result"]}
    else:
        results = {f"fee_{key}": result for key, result in report["selected_runs"].items()}
        results.update({f"phase_{key}": result for key, result in report["phase_sensitivity"].items()})
        results.update(report["validation_segments"])
        results["fresh_2026"] = report["validation_fresh_account"]
    output = {name: validate_ledger(result, days, data["prices"]) for name, result in results.items()}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output, indent=2), encoding="utf-8")
    print(json.dumps({"output": str(args.output.resolve()), "validated_accounts": len(output), "results": output}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
