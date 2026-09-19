"""Shared admission rules for provenance attached to new database writes.

The application keeps retired source ids in existing databases for audit and
replay.  These helpers are therefore deliberately called by write entry
points, rather than encoded as a database constraint that would make an old
database unreadable or unmigratable.
"""
from __future__ import annotations

import unicodedata


RETIRED_SOURCE_MARKERS = (
    "eastmoney",
    "东方财富",
    "东财",
    "emdata",
    "push2",
    "datacenterweb",
)


class SourceAdmissionError(ValueError):
    """Raised before storage when provenance belongs to a retired source."""


def contains_retired_source_reference(value: object) -> bool:
    """Match retired provider names in ids, labels, adapters and evidence URLs."""
    if not isinstance(value, str):
        return False
    folded = unicodedata.normalize("NFKC", value).casefold()
    compact = "".join(character for character in folded if character.isalnum())
    return any(marker in folded or marker in compact for marker in RETIRED_SOURCE_MARKERS)


def writable_source_id(value: object, *, field: str = "source_id") -> str:
    """Return a normalized source id or reject it for a new write."""
    if not isinstance(value, str) or not value.strip():
        raise SourceAdmissionError(f"{field}必须显式提供")
    normalized = value.strip()
    if contains_retired_source_reference(normalized):
        raise SourceAdmissionError(f"{field}引用已退役的东方财富来源")
    return normalized


def reject_retired_source_reference(value: object, *, field: str) -> None:
    """Reject a retired provider hidden in other provenance such as a URL."""
    if contains_retired_source_reference(value):
        raise SourceAdmissionError(f"{field}引用已退役的东方财富来源")
