"""Target schedules for hypothetical sector-price accounts, never live orders."""
from bisect import bisect_right

METHODS = ("return_top10", "low_vol_top10", "rank_top10", "maximin_top10", "upward_quality",
           "upward_count_return", "exposure_matched_all", "all_industries")


def source_targets(source, codes, method):
    if method not in METHODS:
        raise ValueError("Unknown sector shadow-account method")
    target = dict.fromkeys(codes, 0.)
    if method in ("all_industries", "exposure_matched_all"):
        budget = 1. if method == "all_industries" else len(source["selections"]["upward_quality"]) / 10
        return dict.fromkeys(codes, budget / len(codes))
    selected = source["selections"][method]
    if len(selected) > 10 or len(selected) != len(set(selected)) or not set(selected) <= set(codes):
        raise ValueError("Invalid frozen sector candidate list")
    target.update(dict.fromkeys(selected, .1))
    return target


class SectorShadowAllocator:
    def __init__(self, codes, schedule, method, *, repair=True):
        if not codes or len(codes) != len(set(codes)) or method not in METHODS or not isinstance(repair, bool):
            raise ValueError("Invalid sector shadow-account configuration")
        self.codes, self.schedule, self.method, self.repair = codes, schedule, method, repair
        self.dates = sorted(schedule)
        self.applied, self.work, self.target = None, False, dict.fromkeys(codes, 0.)
        self.checks = []

    def __call__(self, index, held, state):
        at = bisect_right(self.dates, index) - 1
        due = self.dates[at] if at >= 0 else None
        if due is not None and self.schedule[due]["source_date"] > state.signal_date:
            raise ValueError("Shadow allocation cannot read a future forecast source")
        new = due is not None and due != self.applied
        if new:
            self.applied = due
            self.target = source_targets(self.schedule[due], self.codes, self.method)
            self.work = self.repair
            target, action = dict(self.target), "review"
        elif self.work:
            in_band = all(abs(state.weights[code] - weight) <= min(.03, weight * .25) + 1e-12
                          for code, weight in self.target.items())
            if in_band:
                self.work = False
                target, action = None, "hold"
            else:
                target, action = dict(self.target), "repair"
        else:
            target, action = None, "awaiting_source" if due is None else "hold"
        self.checks.append({"signal_index": index, "signal_date": state.signal_date,
                            "source_date": self.schedule[due]["source_date"] if due is not None else None,
                            "current_weights": dict(state.weights), "action": action, "targets": target,
                            "work_after": self.work})
        return target
