"""Independently rebuild theme observations, regressions and bounded sleeve weights."""
import math
import statistics

import numpy as np

from backend.analysis.theme_value_tilt import POLICIES


def rebuild_observation(day, lag, source, market_days):
    known = market_days.index(day) - lag
    chosen = None
    for i in range(max(0, known - 3), known + 1):
        if source.get(market_days[i]) is not None:
            chosen = i
    expected = {"status": "unavailable", "quote_date": None, "value": None, "percentile": None,
                "reference_count": 0, "reference_start": None, "reference_end": None, "coverage_fraction": None}
    if chosen is not None:
        prior = market_days[max(0, chosen - 1260):chosen]
        first = next((i for i, date in enumerate(prior) if source.get(date) is not None), len(prior))
        prior = prior[first:]
        reference = [source[date] for date in prior if source.get(date) is not None]
        fraction = len(reference) / len(prior) if prior else 0.
        value = source[market_days[chosen]]
        expected.update(status="insufficient_reference", quote_date=market_days[chosen], value=value,
                        reference_count=len(reference), reference_start=prior[0] if prior else None,
                        reference_end=prior[-1] if prior else None, coverage_fraction=fraction)
        if len(reference) >= 252 and fraction >= .95:
            rank = (sum(v < value for v in reference) + sum(v <= value for v in reference)) / (2 * len(reference))
            expected.update(status="ready", percentile=rank)
    return expected


def independent_quality(data, stocks, index):
    quality = {}
    for code in stocks:
        components = []
        for window in (60, 120):
            prices = np.asarray(data["marks"][code][index - window:index + 1])
            logs = np.log(prices / prices[0])
            x = np.arange(window + 1, dtype=float)
            x, logs = x - x.mean(), logs - logs.mean()
            xx, yy = float(x @ x), float(logs @ logs)
            xy = float(x @ logs)
            r2 = min(1., max(0., xy * xy / (xx * yy))) if yy > 1e-20 else 0.
            components.append((1 if xy > 0 else -1 if xy < 0 else 0) * r2)
        quality[code] = statistics.fmean(components)
    return quality


def audit_theme_decision(entry, data, index, mapping, raw_maps, market_days, policy):
    state = entry["theme_valuation"]
    if policy not in POLICIES or state["signal_date"] != data["days"][index] or entry["signal_date"] != state["signal_date"]:
        raise ValueError("Theme policy or signal date differs")
    lag = state["lag_sessions"]
    if isinstance(lag, bool) or not isinstance(lag, int) or lag < 1 or set(state["funds"]) != set(mapping):
        raise ValueError("Theme lag or universe differs")
    expected_states = {}
    for fund, code in mapping.items():
        expected = rebuild_observation(state["signal_date"], lag, raw_maps[code], market_days) | {"index_code": code}
        observed = state["funds"][fund]
        if set(observed) != set(expected):
            raise ValueError("Theme observation fields differ")
        for key, value in expected.items():
            actual = observed[key]
            if isinstance(value, float):
                if actual is None or not math.isfinite(actual) or abs(actual - value) > 1e-10:
                    raise ValueError("Theme numeric observation fails past-only reconstruction")
            elif actual != value:
                raise ValueError("Theme date/count/status fails reconstruction")
        expected_states[fund] = expected
    frames = {e["code"]: e for e in data["frames"][index]["entries"]}
    stocks = [c for c in frames if data["roles"][c] != "defensive" and not frames[c]["defensive_asset"]]
    if set(entry["stock_distribution"]) != set(stocks):
        raise ValueError("Theme distribution dropped or added an investible stock peer")
    quality = independent_quality(data, stocks, index)
    expected_raw, known = dict.fromkeys(stocks, 1.), []
    for code in stocks:
        ready = expected_states[code]["status"] == "ready"
        p = expected_states[code]["percentile"]
        if policy == "trend_tilt40" or (ready and policy != "equal40"):
            known.append(code)
        if policy in ("trend_tilt40", "trend_matched40") and (ready or policy == "trend_tilt40"):
            expected_raw[code] = 1 + .5 * quality[code]
        elif ready and policy == "theme_value40":
            expected_raw[code] = 1.5 - p
        elif ready and policy == "theme_blend40":
            expected_raw[code] = 1 + .25 * quality[code] + .5 * (.5 - p)
        elif ready and policy == "theme_confirmed40":
            if p <= .2 and quality[code] > 0:
                expected_raw[code] = 1.5
            elif p >= .8 and quality[code] < 0:
                expected_raw[code] = .5
    tilt = entry["theme_tilt"]
    if tilt["known_funds"] != known or set(tilt["multipliers"]) != set(stocks):
        raise ValueError("Theme tilt coverage differs")
    if any(not math.isfinite(tilt["multipliers"][c]) or abs(expected_raw[c] - tilt["multipliers"][c]) > 1e-9 for c in stocks):
        raise ValueError("Theme tilt multiplier cannot be rebuilt from price and valuation")
    n = len(stocks)
    expected_weights = dict.fromkeys(stocks, 1 / n)
    if known:
        cap = min(1., 2 / len(known))
        low, high = 0., 1 / min(expected_raw[c] for c in known)
        # Independent monotone solve instead of the allocator's iterative cap helper.
        for _ in range(80):
            middle = (low + high) / 2
            if sum(min(cap, middle * expected_raw[c]) for c in known) < 1:
                low = middle
            else:
                high = middle
        expected_weights.update({c: min(cap, high * expected_raw[c]) * len(known) / n for c in known})
    for weights in (entry["stock_distribution"], tilt["distribution"]):
        if set(weights) != set(stocks) or any(not math.isfinite(weights[c]) or abs(weights[c] - expected_weights[c]) > 1e-9 for c in stocks):
            raise ValueError("Theme distribution fails independent budget/cap reconstruction")
    if not math.isfinite(entry["proposed_stock_budget"]) or abs(entry["proposed_stock_budget"] - .4) > 1e-12 or not math.isfinite(entry["risky_scale"]) or not 0 <= entry["risky_scale"] <= 1:
        raise ValueError("Theme tilt altered aggregate equity budget or risk scale")
    scale = entry["risky_scale"]
    if entry["targets"]:
        bonds = [c for c in frames if data["roles"][c] == "defensive"]
        gold = [c for c in frames if frames[c]["defensive_asset"]]
        expected_targets = {c: .4 * scale * expected_weights[c] for c in stocks}
        expected_targets.update({c: .2 * scale / len(gold) for c in gold})
        expected_targets.update({c: min(.9, (1 - .6 * scale) / len(bonds)) for c in bonds})
        if set(entry["targets"]) != set(expected_targets) or any(not math.isfinite(entry["targets"][c]) or abs(entry["targets"][c] - w) > 1e-9 for c, w in expected_targets.items()):
            raise ValueError("Theme target weights do not preserve declared scaled asset budgets")
    else:
        bonds = [c for c in frames if data["roles"][c] == "defensive"]
        gold = [c for c in frames if frames[c]["defensive_asset"]]
        codes = list(frames)
        daily = np.asarray([frames[c]["daily60"] for c in codes])
        for scale in np.linspace(0, 1, 101):
            weights = {c: .4 * scale * expected_weights[c] for c in stocks}
            weights.update({c: .2 * scale / len(gold) for c in gold})
            weights.update({c: min(.9, (1 - .6 * scale) / len(bonds)) for c in bonds})
            series = np.asarray([weights[c] for c in codes]) @ daily
            if float(np.std(series) * np.sqrt(252)) <= .1:
                raise ValueError("Cash fallback was used despite a feasible risk-constrained allocation")
