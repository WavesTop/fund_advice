"""Past-only discrete class-selection changes, not profitable-date selection."""
from backend.analysis.cross_asset_trend import class_proposal


def selection_event_indices(data, policy, *, start=252):
    if start < 252 or start >= len(data["days"]):
        raise ValueError("Selection events require past warmup")
    events, previous = set(), None
    for index in range(start, len(data["days"])):
        stats = data["cross_stats"][index]
        if stats["signal_index"] != index or stats["fund_risk"]["signal_index"] != index:
            raise ValueError("Selection event cache uses a different date")
        proposal = class_proposal(policy, stats["signals"])
        selection = tuple(sorted(name for name, weight in proposal.items() if weight > 0))
        if previous is not None and selection != previous:
            events.add(index)
        previous = selection
    return frozenset(events)
