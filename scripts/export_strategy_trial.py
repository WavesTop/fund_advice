"""Materialize a small read-only trial from the reviewed, immutable report."""
import argparse
from bisect import bisect_left, bisect_right
from datetime import date
import gzip
from hashlib import sha256
import json
import math
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT / "runtime/verification/satellite-selection-diagnostic-v1.json.gz"
REPORT_SHA256 = "b793bde6b4d1f6add731993bc14e330e914018bd267fa1dc1b63a82d6c2d236e"
OUTPUT = ROOT / "frontend/src/features/trial/snapshot.json"
POLICIES = ("inverse20", "fixed20")
COHORTS = {
    "long4": ("长期宽基池", "2只宽基股票基金、黄金、纯债"),
    "older4": ("早期主题池", "3只股票主题基金、黄金、纯债"),
    "older9": ("8主题池", "8只股票主题基金、黄金、纯债"),
    "all13": ("12主题池", "12只股票主题基金、黄金、3只债券基金"),
}
INPUTS = {
    "input_sha256": "runtime/verification/fund-rotation-expanded-20261013/input.json",
    "bond_input_sha256": "runtime/verification/fund-allocation-defensive-20261013/input.json",
    "calendar_sha256": "config/research-exchange-calendar-2009-2026-v2.json",
}


def verified_report(path=REPORT):
    if sha256(path.read_bytes()).hexdigest() != REPORT_SHA256:
        raise ValueError("Reviewed report digest changed")
    with gzip.open(path, "rt", encoding="utf-8") as stream:
        report = json.load(stream)
    for key, relative in INPUTS.items():
        if sha256((ROOT / relative).read_bytes()).hexdigest() != report[key]:
            raise ValueError(f"Frozen input changed: {key}")
    for path, source in report["source_snapshots"].items():
        if sha256(source.encode()).hexdigest() != report["source_hashes"][path]:
            raise ValueError(f"Archived source digest changed: {path}")
    return report


def checked_metrics(run):
    audits = [value for key, value in run.items() if key.endswith("validation")]
    if not audits or any(value.get("status") != "passed" for value in audits):
        raise ValueError("Account has an incomplete audit")
    measured = run["metrics"]
    keys = ("total_return_pct", "annualized_return_pct", "max_drawdown_pct",
            "total_fees", "final_equity", "trade_count", "short_redemption_sales")
    result = {key: float(measured[key]) for key in keys}
    if any(not math.isfinite(value) for value in result.values()):
        raise ValueError("Nonfinite account metric")
    if "account" in run:
        account = run["account"]
        curve = account["equity_curve"]
        days = [row["date"] for row in curve]
        if not curve or days != sorted(set(days)):
            raise ValueError("Account curve dates are invalid")
        if (days[0], days[-1]) != (measured["start_date"], measured["end_date"]):
            raise ValueError("Account coverage differs from metrics")
        initial = float(account["initial_equity"])
        if initial != 100000 or float(curve[0]["equity"]) != initial:
            raise ValueError("Trial requires the original 100000 account")
        values = [float(row["equity"]) for row in curve]
        if any(not math.isfinite(value) or value <= 0 for value in values):
            raise ValueError("Invalid account wealth")
        peak, drawdown = initial, 0.
        for value in values:
            peak = max(peak, value)
            drawdown = min(drawdown, value / peak - 1)
        elapsed = (date.fromisoformat(days[-1]) - date.fromisoformat(days[0])).days / 365.2425
        rebuilt = {"final_equity": values[-1], "total_return_pct": (values[-1] / initial - 1) * 100,
                   "max_drawdown_pct": drawdown * 100,
                   "annualized_return_pct": ((values[-1] / initial) ** (1 / elapsed) - 1) * 100}
        if any(not math.isclose(result[key], value, abs_tol=1e-7, rel_tol=1e-10)
               for key, value in rebuilt.items()):
            raise ValueError("Account curve does not reproduce reported metrics")
    return result


def interval(curve, start, end):
    days = [row["date"] for row in curve]
    first, last = bisect_left(days, start), bisect_right(days, end) - 1
    if first > last or last - first < 20:
        return None
    initial = float(curve[first - 1]["equity"]) if first else float(curve[0]["equity"])
    peak, drawdown = initial, 0.
    for row in curve[first:last + 1]:
        wealth = float(row["equity"])
        peak = max(peak, wealth)
        drawdown = min(drawdown, wealth / peak - 1)
    return {"start": days[first], "end": days[last],
            "reference_date": days[first - 1] if first else None,
            "partial": start < days[0] or end > days[-1],
            "total_return_pct": (float(curve[last]["equity"]) / initial - 1) * 100,
            "max_drawdown_pct": drawdown * 100}


def project(report):
    cohorts = []
    for key, (label, scope) in COHORTS.items():
        source = report["cohorts"][key]
        runs = {policy: source["full"][policy] for policy in POLICIES}
        measured = {policy: checked_metrics(run) for policy, run in runs.items()}
        curves = {policy: run["account"]["equity_curve"] for policy, run in runs.items()}
        days = [row["date"] for row in curves["inverse20"]]
        if days != [row["date"] for row in curves["fixed20"]]:
            raise ValueError("Paired accounts have different date coverage")
        years = sorted({day[:4] for day in days})
        annual = [{"year": year, "policies": {policy: interval(curve, f"{year}-01-01", f"{year}-12-31")
                   for policy, curve in curves.items()}} for year in years]
        episodes = [{"id": episode["id"], "group": episode["evaluation_group"],
                     "requested_start": episode["start"], "requested_end": episode["end"],
                     "policies": {policy: interval(curve, episode["start"], episode["end"])
                                  for policy, curve in curves.items()}}
                    for episode in report["regime_protocol"]["episodes"]]
        rolling = {}
        for policy, curve in curves.items():
            values = [float(row["equity"]) for row in curve]
            returns = [(values[i] / values[i - 252] - 1) * 100 for i in range(252, len(values))]
            rolling[policy] = {"count": len(returns), "positive": sum(value > 0 for value in returns),
                               "worst_return_pct": min(returns) if returns else None}
        scenarios = list(source["stress"]["inverse20"])
        if scenarios != list(source["stress"]["fixed20"]):
            raise ValueError("Stress scenarios are not paired")
        stress = [{"id": scenario, "policies": {
            policy: checked_metrics(source["stress"][policy][scenario]) for policy in POLICIES}}
            for scenario in scenarios]
        cohorts.append({"id": key, "label": label, "scope": scope, "codes": source["codes"],
                        "start": days[0], "end": days[-1], "metrics": measured,
                        "curve": [[day, *[round(float(curves[p][i]["equity"]), 4) for p in POLICIES]]
                                  for i, day in enumerate(days)],
                        "annual": annual, "episodes": episodes, "rolling": rolling, "stress": stress})
    return {"version": "strategy-research-trial-v1", "as_of": max(c["end"] for c in cohorts),
            "initial_equity": 100000, "report_generated_at": report["generated_at"],
            "source": {"path": str(REPORT.relative_to(ROOT)), "sha256": REPORT_SHA256},
            "input_hashes": {key: report[key] for key in INPUTS}, "cohorts": cohorts}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=OUTPUT)
    args = parser.parse_args()
    snapshot = project(verified_report())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n",
                           encoding="utf-8")
    print(json.dumps({"output": str(args.output), "cohorts": len(snapshot["cohorts"]), "as_of": snapshot["as_of"]}))


if __name__ == "__main__":
    main()
