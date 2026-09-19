#!/usr/bin/env python3
"""Retired automatic fund-to-index discovery entry points.

The previous implementation discovered the tracked index through an
Eastmoney fund profile before cross-checking official index catalogues. That
first hop is no longer allowed. Existing relationships remain in SQLite with
their original provenance, while new relationships must be imported from
versioned fund-manager documents.
"""
from __future__ import annotations

import re
import urllib.request
from typing import Callable, MutableMapping

from backend.core.config import Settings
from backend.storage.related_market import record_relation_check
from backend.storage.timeseries import get_fund
from scripts.probe_fund_source import ProbeError


RETIRED_MESSAGE = (
    "基金跟踪关系的东方财富发现入口已退役；"
    "请从基金管理人正式产品资料导入带有效期的跟踪关系"
)


def _validate_code(fund_code: str) -> None:
    if not isinstance(fund_code, str) or not re.fullmatch(r"\d{6}", fund_code):
        raise ProbeError("invalid_request", "基金代码必须是六位数字")


def resolve_cni_relation(
    fund_code: str,
    timeout: float,
    opener: Callable[..., object] = urllib.request.urlopen,
) -> None:
    """Reject the retired discovery path before invoking any network client."""
    _validate_code(fund_code)
    del timeout, opener
    raise ProbeError("source_retired", RETIRED_MESSAGE)


def resolve_related_relation(
    fund_code: str,
    timeout: float,
    opener: Callable[..., object] = urllib.request.urlopen,
    catalogue_cache: MutableMapping[str, object] | None = None,
) -> None:
    """Reject the retired discovery path before invoking any network client."""
    _validate_code(fund_code)
    del timeout, opener, catalogue_cache
    raise ProbeError("source_retired", RETIRED_MESSAGE)


def refresh_related_market(
    settings: Settings,
    fund_code: str,
    *,
    timeout: float = 30.0,
    opener: Callable[..., object] = urllib.request.urlopen,
    catalogue_cache: MutableMapping[str, object] | None = None,
) -> None:
    """Record the failed refresh while preserving historical provenance."""
    get_fund(settings, fund_code)
    try:
        resolve_related_relation(fund_code, timeout, opener, catalogue_cache)
    except ProbeError as exc:
        record_relation_check(
            settings,
            fund_code,
            outcome="failed",
            error=f"{type(exc).__name__}: {exc}",
        )
        raise
    raise AssertionError("retired relation discovery unexpectedly returned")
