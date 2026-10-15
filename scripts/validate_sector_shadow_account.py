"""Rebuild callbacks, cached sources and repair state from actual executed lots."""
from bisect import bisect_right

from scripts.validate_trend_satellite import close, rebuild_executed_weights


def audit_shadow_account(account, checks, data, schedule, method, *, repair):
    indexes = {day: i for i, day in enumerate(data["days"])}
    start, end = indexes[account["start_date"]], indexes[account["end_date"]]
    lag = account["execution"]["execution_lag_sessions"]
    decisions = {indexes[row["signal_date"]]: row["targets"] for row in account["decisions"]}
    blocked = {i for day in decisions for i in range(day + 1, day + lag)}
    expected_dates = [i for i in range(start, end - lag) if i not in blocked]
    if [row["signal_index"] for row in checks] != expected_dates:
        raise ValueError("Shadow account omitted a callback or read a queued-order day")
    actual = rebuild_executed_weights(account, data)
    dates, codes = sorted(schedule), list(data["prices"])
    applied, working, target = None, False, dict.fromkeys(codes, 0.)
    expected_decisions = {}
    for row in checks:
        index = row["signal_index"]
        day = data["days"][index]
        if row["signal_date"] != day:
            raise ValueError("Shadow callback date differs")
        close(row["current_weights"], actual[day])
        at = bisect_right(dates, index) - 1
        due = dates[at] if at >= 0 else None
        expected_source = schedule[due]["source_date"] if due is not None else None
        if row["source_date"] != expected_source or expected_source is not None and expected_source > day:
            raise ValueError("Shadow account used the wrong or a future source")
        if due is not None and due != applied:
            applied, working = due, repair
            if method == "all_industries":
                target = dict.fromkeys(codes, 1 / len(codes))
            elif method == "exposure_matched_all":
                total = len(schedule[due]["selections"]["upward_quality"]) * .1
                target = dict.fromkeys(codes, total / len(codes))
            else:
                selected = set(schedule[due]["selections"][method])
                target = {code: .1 if code in selected else 0. for code in codes}
            expected, action = dict(target), "review"
        elif working and any(abs(actual[day][code] - weight) > min(.03, weight * .25) + 1e-12 for code, weight in target.items()):
            expected, action = dict(target), "repair"
        else:
            working = False
            expected, action = None, "awaiting_source" if due is None else "hold"
        close(row["targets"], expected)
        if row["action"] != action or row["work_after"] != working:
            raise ValueError("Shadow review/repair state differs")
        if expected is not None:
            expected_decisions[index] = expected
    close(decisions, expected_decisions)
    return {"status": "passed", "callbacks": len(checks), "decisions": len(decisions),
            "repaired_callbacks": sum(row["action"] == "repair" for row in checks),
            "deferred_review_callbacks": sum(row["action"] == "review" and row["source_date"] < row["signal_date"] for row in checks)}
