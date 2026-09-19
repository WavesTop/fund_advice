#!/usr/bin/env python3
"""Run the formal portfolio/strategy workflow against an isolated real-data copy.

The source database is opened with SQLite ``mode=ro`` and is never migrated or
written.  The copy is migrated independently, then all verification accounts,
ledger events, recommendations, decisions and backtests are created only in
that disposable copy.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import AbstractContextManager
from datetime import date
from decimal import Decimal, InvalidOperation, localcontext
from hashlib import sha256
import json
import math
from pathlib import Path
import sqlite3
import tempfile
from typing import Any
from unittest.mock import patch
from urllib.parse import parse_qsl, quote, urlencode, urlsplit

from backend.analysis.strategy import StrategyPolicy, evaluate_strategy
from backend.analysis.strategy_portfolio import eligible_fund_universe
from backend.api.main import create_app
from backend.core.config import Settings
from backend.core.trading_calendar import CalendarUnavailable, get_calendar


EMPTY_FILE_SHA256 = sha256(b"").hexdigest()
BUSINESS_TABLES = (
    "portfolio_account",
    "portfolio_condition_revision",
    "ledger_event",
    "ledger_event_correction",
    "strategy_recommendation_run",
    "strategy_decision",
    "decision_trade_link",
    "strategy_backtest_run",
)


class VerificationFailure(RuntimeError):
    pass


class JsonASGIClient(AbstractContextManager):
    """Small synchronous JSON client that exercises the real ASGI routes."""

    def __init__(self, app: Any):
        self.app = app
        self.loop: asyncio.AbstractEventLoop | None = None
        self.lifespan: Any = None

    def __enter__(self) -> "JsonASGIClient":
        self.loop = asyncio.new_event_loop()
        self.lifespan = self.app.router.lifespan_context(self.app)
        self.loop.run_until_complete(self.lifespan.__aenter__())
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        assert self.loop is not None
        try:
            self.loop.run_until_complete(self.lifespan.__aexit__(exc_type, exc, traceback))
            self.loop.run_until_complete(self.loop.shutdown_asyncgens())
        finally:
            self.loop.close()

    def request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, Any]:
        if self.loop is None:
            raise RuntimeError("JsonASGIClient must be used as a context manager")
        parts = urlsplit(url)
        query = parse_qsl(parts.query, keep_blank_values=True)
        if params:
            query.extend((key, str(value)) for key, value in params.items())
        messages: list[dict[str, Any]] = []
        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": method,
            "scheme": "http",
            "path": parts.path,
            "raw_path": quote(parts.path).encode("ascii"),
            "query_string": urlencode(query).encode("ascii"),
            "root_path": "",
            "headers": [(b"host", b"verification.local")],
            "client": ("127.0.0.1", 1234),
            "server": ("verification.local", 80),
        }
        encoded = b"" if body is None else json.dumps(body, allow_nan=False).encode("utf-8")
        if body is not None:
            scope["headers"].append((b"content-type", b"application/json"))
        if headers:
            scope["headers"].extend(
                (str(key).lower().encode("latin-1"), str(value).encode("latin-1"))
                for key, value in headers.items()
            )

        async def receive() -> dict[str, Any]:
            return {"type": "http.request", "body": encoded, "more_body": False}

        async def send(message: dict[str, Any]) -> None:
            messages.append(message)

        self.loop.run_until_complete(self.app(scope, receive, send))
        status = next(message["status"] for message in messages if message["type"] == "http.response.start")
        content = b"".join(
            message.get("body", b"") for message in messages if message["type"] == "http.response.body"
        )
        return status, json.loads(content) if content else None

    def get(self, url: str, *, params: dict[str, Any] | None = None) -> tuple[int, Any]:
        return self.request("GET", url, params=params)

    def post(
        self,
        url: str,
        *,
        body: dict[str, Any],
        idempotency_key: str | None = None,
    ) -> tuple[int, Any]:
        headers = {"Idempotency-Key": idempotency_key} if idempotency_key else None
        return self.request("POST", url, body=body, headers=headers)


def require(condition: bool, message: str) -> None:
    if not condition:
        raise VerificationFailure(message)


def api_ok(response: tuple[int, Any], operation: str) -> dict[str, Any]:
    status, payload = response
    if status != 200 or not isinstance(payload, dict):
        raise VerificationFailure(f"{operation}失败: HTTP {status}: {payload}")
    return payload


def number(value: Decimal) -> str:
    require(value.is_finite(), "验收计算出现非有限数值")
    rendered = format(value, "f")
    rendered = rendered.rstrip("0").rstrip(".") if "." in rendered else rendered
    return rendered if rendered and rendered != "-0" else "0"


def decimal(value: Any, label: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise VerificationFailure(f"{label}不是有效小数: {value}") from exc
    require(parsed.is_finite(), f"{label}不是有限数值")
    return parsed


def file_fingerprints(database: Path) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for suffix in ("", "-wal", "-shm"):
        path = Path(f"{database}{suffix}")
        if not path.exists():
            result[suffix or "database"] = None
            continue
        hasher = sha256()
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                hasher.update(block)
        stat = path.stat()
        result[suffix or "database"] = {
            "size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
            "sha256": hasher.hexdigest(),
        }
    return result


def durable_content_fingerprints(files: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Normalize the database and WAL by durable content, ignoring reader-only file lifecycle.

    Opening a WAL-mode database read-only can create an empty ``-wal`` file and
    its transient ``-shm`` index.  An absent WAL and a verified empty WAL carry
    the same durable database content; a non-empty WAL remains significant.
    Modification times are intentionally excluded because readers can update
    SQLite coordination metadata without changing database content.
    """

    database = files.get("database")
    wal = files.get("-wal")

    def signature(item: Any, *, empty_is_absent: bool) -> dict[str, Any]:
        if item is None:
            return {"state": "empty_or_absent" if empty_is_absent else "missing"}
        size = int(item.get("size", -1))
        digest = str(item.get("sha256") or "")
        if empty_is_absent and size == 0 and digest == EMPTY_FILE_SHA256:
            return {"state": "empty_or_absent"}
        return {"state": "content", "size": size, "sha256": digest}

    return {
        "database": signature(database, empty_is_absent=False),
        "-wal": signature(wal, empty_is_absent=True),
    }


def read_only_connection(database: Path) -> sqlite3.Connection:
    uri = f"file:{quote(str(database.resolve()))}?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=5)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    require(connection.execute("PRAGMA query_only").fetchone()[0] == 1, "源库只读模式未生效")
    return connection


def logical_counts(database: Path) -> dict[str, int | None]:
    with read_only_connection(database) as connection:
        tables = {
            str(row[0])
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        return {
            table: int(connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
            if table in tables else None
            for table in BUSINESS_TABLES
        }


def verification_marker_counts(database: Path) -> dict[str, int]:
    """Prove that disposable verification records never reached the source."""
    with read_only_connection(database) as connection:
        tables = {
            str(row[0])
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        account_names = (
            int(connection.execute(
                "SELECT COUNT(*) FROM portfolio_account WHERE name LIKE '隔离验收-%'"
            ).fetchone()[0])
            if "portfolio_account" in tables else 0
        )
        ledger_sources = (
            int(connection.execute(
                "SELECT COUNT(*) FROM ledger_event WHERE source='isolated_workflow_verification'"
            ).fetchone()[0])
            if "ledger_event" in tables else 0
        )
        idempotency_keys = 0
        for table in (
            "portfolio_account", "portfolio_condition_revision", "ledger_event",
            "ledger_event_correction", "strategy_decision",
        ):
            if table in tables:
                idempotency_keys += int(connection.execute(
                    f'SELECT COUNT(*) FROM "{table}" WHERE idempotency_key LIKE ?',
                    ("workflow-%",),
                ).fetchone()[0])
        return {
            "verification_account_names": account_names,
            "verification_ledger_sources": ledger_sources,
            "verification_idempotency_keys": idempotency_keys,
        }


def isolated_backup(source: Path, target: Path) -> dict[str, Any]:
    require(source.is_file(), f"真实数据库不存在: {source}")
    require(not target.exists(), "隔离副本目标必须不存在")
    target.parent.mkdir(parents=True, exist_ok=True)
    with read_only_connection(source) as source_connection:
        target_connection = sqlite3.connect(target)
        try:
            source_connection.backup(target_connection)
            target_connection.execute("PRAGMA journal_mode=DELETE")
            integrity = str(target_connection.execute("PRAGMA integrity_check").fetchone()[0])
            foreign_key_errors = target_connection.execute("PRAGMA foreign_key_check").fetchall()
        finally:
            target_connection.close()
    require(integrity == "ok", f"隔离副本完整性异常: {integrity}")
    require(not foreign_key_errors, f"隔离副本存在外键异常: {len(foreign_key_errors)}")
    return {
        "method": "sqlite_online_backup_from_mode_ro_source",
        "integrity": integrity,
        "foreign_key_error_count": len(foreign_key_errors),
        "bytes": target.stat().st_size,
    }


def rows_through(rows: list[dict[str, str]], cutoff: str, size: int) -> list[dict[str, str]]:
    visible = [row for row in rows if str(row["date"]) <= cutoff]
    return visible[-size:]


def derive_signal_dates(universe: dict[str, Any]) -> dict[str, Any]:
    """Find one real series with both an entry tier and a no-entry tier."""
    policy = StrategyPolicy()
    calendar = get_calendar()
    priority = ("2025-07-08", "2025-07-04")
    exit_priority = ("2026-09-11", "2026-09-16")

    def decision(item: dict[str, Any], cutoff: str, weight: Decimal) -> dict[str, Any] | None:
        try:
            result = evaluate_strategy(
                rows_through(item["rows"], cutoff, policy.long_window + 1),
                as_of=cutoff,
                current_weight=weight,
                calendar=calendar,
                policy=policy,
            )
        except (CalendarUnavailable, ValueError):
            return None
        return result if result["data_quality"]["status"] == "ready" else None

    for item in universe["items"]:
        dates = [str(row["date"]) for row in item["rows"]]
        entry_candidates = [value for value in priority if value in dates]
        entry_candidates.extend(value for value in reversed(dates) if value not in entry_candidates)
        entry_date = next(
            (
                value for value in entry_candidates
                if (result := decision(item, value, Decimal(0))) is not None
                and result["action"] == "BUY"
            ),
            None,
        )
        if entry_date is None:
            continue
        # Keep at least one later observed date so the accepted EXIT decision
        # can be linked to a subsequent confirmed sale without inventing an
        # execution date.
        last_observation = dates[-1]
        no_entry_candidates = [
            value for value in exit_priority
            if value in dates and entry_date < value < last_observation
        ]
        no_entry_candidates.extend(
            value for value in reversed(dates)
            if entry_date < value < last_observation and value not in no_entry_candidates
        )
        exit_date = next(
            (
                value for value in no_entry_candidates
                if (empty := decision(item, value, Decimal(0))) is not None
                and empty["action"] == "WAIT"
                and empty["reason_code"] == "NO_LONG_ENTRY"
                and (held := decision(item, value, Decimal("0.3"))) is not None
                and held["action"] == "EXIT"
            ),
            None,
        )
        if exit_date is None:
            continue
        prices = {str(row["date"]): str(row["close"]) for row in item["rows"]}
        future_dates = [value for value in dates if value > exit_date]
        require(future_dates, f"{item['code']}在退出信号日后没有真实观察日可记录成交")
        return {
            "fund_code": str(item["code"]),
            "fund_name": str(item["name"]),
            "index_code": str(item["index_code"]),
            "entry_date": entry_date,
            "entry_price": prices[entry_date],
            "exit_date": exit_date,
            "exit_price": prices[exit_date],
            "execution_date": future_dates[0],
            "execution_price": prices[future_dates[0]],
        }
    raise VerificationFailure("真实历史中未找到同一基金兼具BUY和EXIT/WAIT的可核验日期")


def create_account(
    client: JsonASGIClient,
    *,
    label: str,
    effective_date: str,
    position: tuple[str, Decimal, Decimal] | None = None,
) -> dict[str, Any]:
    account = api_ok(
        client.post(
            "/api/v1/portfolio/accounts",
            body={"name": f"隔离验收-{label}", "base_currency": "CNY"},
            idempotency_key=f"workflow-account-{label}",
        ),
        f"创建{label}账户",
    )
    account_id = str(account["account_id"])
    api_ok(
        client.post(
            "/api/v1/portfolio/conditions",
            body={
                "account_id": account_id,
                "effective_date": effective_date,
                "risk_level": "aggressive",
                "max_single_position_pct": "35",
                "investment_horizon_days": 730,
                "channel": "人工确认账本",
                "planned_additional_cash": "0",
                "no_holdings_confirmed": position is None,
            },
            idempotency_key=f"workflow-condition-{label}",
        ),
        f"设置{label}账户条件",
    )
    api_ok(
        client.post(
            "/api/v1/ledger/events",
            body={
                "account_id": account_id,
                "event_type": "cash_in",
                "fund_code": None,
                "amount": "100000",
                "units": None,
                "business_date": effective_date,
                "confirmation_date": effective_date,
                "settlement_date": effective_date,
                "channel": "人工确认账本",
                "confirmed_fee": None,
                "state": "confirmed",
                "source": "isolated_workflow_verification",
                "note": "仅写入真实数据库隔离副本",
            },
            idempotency_key=f"workflow-cash-{label}",
        ),
        f"登记{label}账户现金",
    )
    initial_event = None
    if position is not None:
        code, price, desired_weight = position
        require(Decimal(0) < desired_weight < Decimal(1), f"{label}场景仓位权重无效")
        market_value = Decimal("100000") * desired_weight / (Decimal(1) - desired_weight)
        # The formal ledger accepts at most 30 decimal places. Twelve decimal
        # places are materially tighter than fund dealing precision while
        # keeping the constructed portfolio weight stable around the policy
        # bands.
        units = (market_value / price).quantize(Decimal("0.000000000001"))
        initial_event = api_ok(
            client.post(
                "/api/v1/ledger/events",
                body={
                    "account_id": account_id,
                    "event_type": "initial_position",
                    "fund_code": code,
                    "amount": None,
                    "units": number(units),
                    "business_date": effective_date,
                    "confirmation_date": effective_date,
                    "settlement_date": None,
                    "channel": "人工确认账本",
                    "confirmed_fee": None,
                    "state": "confirmed",
                    "source": "isolated_workflow_verification",
                    "note": f"构造{label}动作所需的隔离仓位",
                },
                idempotency_key=f"workflow-position-{label}",
            ),
            f"登记{label}账户持仓",
        )
    return {"account_id": account_id, "initial_event": initial_event}


def recommend(
    client: JsonASGIClient,
    account_id: str,
    cutoff: str,
    *,
    fund_code: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    recommendation = api_ok(
        client.post(
            "/api/v1/strategy/recommendations",
            body={"account_id": account_id, "cutoff": cutoff},
        ),
        f"生成{account_id}策略建议",
    )
    item = next(
        (value for value in recommendation["output"]["items"] if value["fund_code"] == fund_code),
        None,
    )
    require(item is not None, f"策略建议中未找到真实基金{fund_code}")
    return recommendation, item


def assert_action(item: dict[str, Any], expected: str, label: str) -> None:
    require(item["action"] == expected, f"{label}场景预期{expected}，实际为{item['action']}: {item['reason_code']}")
    require(item["data_quality"]["status"] == "ready", f"{label}场景数据质量未就绪")
    for field in ("current_weight_pct", "target_weight_pct", "valuation_price"):
        if item[field] is not None:
            decimal(item[field], f"{label}.{field}")


def curve_integrity(backtest: dict[str, Any]) -> dict[str, Any]:
    output = backtest["output"]
    curves = output["curves"]
    require(len(curves) >= 2, "回测缺少策略或基准曲线")
    strategy = next((curve for curve in curves if curve["id"] == "strategy"), None)
    benchmark = next((curve for curve in curves if curve["id"] == "benchmark"), None)
    require(strategy is not None and benchmark is not None, "回测缺少策略或基准曲线")
    points = strategy["points"]
    require(len(points) >= StrategyPolicy().long_window + 2, "策略收益曲线长度不足")
    require(len(points) == len(benchmark["points"]), "策略和基准曲线日期长度不一致")
    previous_date = ""
    for index, point in enumerate(points):
        require(point["date"] > previous_date, "收益曲线日期未严格递增")
        require(point["date"] == benchmark["points"][index]["date"], "策略和基准曲线日期错位")
        equity = decimal(point["equity"], "equity")
        cash = decimal(point["cash"], "cash")
        market = decimal(point["market_value"], "market_value")
        require(equity == cash + market, f"{point['date']}资产恒等式不成立")
        require(equity > 0 and cash >= 0 and market >= 0, f"{point['date']}出现负资产或负现金")
        previous_date = str(point["date"])
    for value in output["metrics"].values():
        if isinstance(value, (str, int, float)) and not isinstance(value, bool):
            try:
                numeric = float(value)
            except (TypeError, ValueError):
                continue
            require(math.isfinite(numeric), "回测指标出现NaN或Infinity")
    executed = output["executed_action_summary"]
    missing = [action for action in ("BUY", "ADD", "REDUCE", "EXIT") if int(executed[action]) <= 0]
    require(not missing, f"长周期历史回测没有执行这些动作: {missing}")
    validation = output.get("validation")
    require(isinstance(validation, dict), "回测缺少正式validation结果")
    require(validation.get("status") != "blocked", "正式validation阻止了回测结果")
    blocking = validation.get("blocking_errors", [])
    require(not blocking, f"正式validation存在阻断错误: {blocking}")
    return {
        "run_id": backtest["run_id"],
        "status": output["status"],
        "validation_status": validation["status"],
        "observations": len(points),
        "start_date": output["start_date"],
        "end_date": output["end_date"],
        "metrics": output["metrics"],
        "risk_quality": output.get("risk_quality"),
        "exposure_summary": output.get("exposure_summary"),
        "economic_attribution": output.get("economic_attribution"),
        "investment_eligibility": output.get("investment_eligibility"),
        "action_summary": output["action_summary"],
        "executed_action_summary": executed,
        "diagnostics": output["diagnostics"],
        "validation_checks": validation.get("checks", {}),
        "validation_coverage": validation.get("coverage", {}),
        "validation_review_findings": validation.get("review_findings", []),
        "validation_action_metrics": validation.get("actions", {}),
        "validation_performance": validation.get("performance", {}),
        "asset_identity_checked_for_every_point": True,
    }


def run_workflow(copy_path: Path) -> dict[str, Any]:
    settings = Settings(database_path=copy_path)
    # App startup migrates only the copy. The source has already been closed.
    with JsonASGIClient(create_app(settings)) as client:
        health = api_ok(client.get("/health"), "读取隔离服务健康状态")
        require("portfolio_ledger_v1" in health["capabilities"], "服务未启用正式持仓账本")
        require("strategy_execution_v1" in health["capabilities"], "服务未启用正式策略执行")

        production_universe = eligible_fund_universe(settings)
        require(
            not production_universe["items"],
            "当前验收预期退役关系全部退出正式基金策略池，但仍发现可用项目",
        )
        retired_exclusions = [
            item for item in production_universe.get("excluded", [])
            if "已退役历史来源" in str(item.get("reason") or "")
        ]
        require(retired_exclusions, "正式策略池没有报告已退役关系的排除原因")

        # Recover the same real stored rows only inside this disposable process
        # so transaction mechanics and historical arithmetic remain testable.
        # The live universe above remains empty, and the unmodified readiness
        # rules still mark every archived source as ineligible for real money.
        with patch("backend.storage.related_market.relation_problems", return_value=[]):
            universe = eligible_fund_universe(settings)
        require(universe["items"], "隔离副本没有可供机械验收的真实历史存量序列")
        archived_universe_patchers = [
            patch("backend.analysis.strategy_portfolio.eligible_fund_universe", return_value=universe),
            patch("backend.analysis.strategy_service.eligible_fund_universe", return_value=universe),
            patch("backend.api.strategy.eligible_fund_universe", return_value=universe),
        ]
        archived_universe_mocks = [patcher.start() for patcher in archived_universe_patchers]
        signal = derive_signal_dates(universe)
        code = signal["fund_code"]
        entry_price = decimal(signal["entry_price"], "entry_price")
        exit_price = decimal(signal["exit_price"], "exit_price")

        # First verify the production gate without any override.  Legacy rows
        # remain useful for exercising deterministic rules, but their source
        # and investment-validation gaps must force the formal action to WAIT.
        formal_account = create_account(
            client, label="formal-gate", effective_date=signal["entry_date"], position=None,
        )
        formal_run, formal_item = recommend(
            client, formal_account["account_id"], signal["entry_date"], fund_code=code,
        )
        require(formal_run["output"]["status"] == "blocked", "真实来源缺口未阻断正式建议")
        require(formal_item["action"] == "WAIT", "正式建议在投资门槛未满足时仍生成了交易动作")
        require(formal_item["strategy_signal_action"] == "BUY", "底层真实历史BUY信号未被保留供审计")
        require(
            {"strategy_source_not_execution_grade", "strategy_not_investment_validated"}
            <= set(formal_run["output"]["blockers"]),
            "正式建议没有同时报告来源与投资验证阻断",
        )
        formal_gate = {
            "account_id": formal_account["account_id"],
            "recommendation_id": formal_run["recommendation_id"],
            "formal_action": formal_item["action"],
            "underlying_signal_action": formal_item["strategy_signal_action"],
            "blockers": formal_run["output"]["blockers"],
            "investment_eligibility": formal_run["output"].get("investment_eligibility"),
            "production_universe_item_count": len(production_universe["items"]),
            "retired_relation_exclusion_count": len(retired_exclusions),
            "passed": True,
        }

        # The remaining scenarios validate sizing, ledger and action mechanics
        # only.  They run on this disposable copy with an explicit in-process
        # readiness override; their results are never described as investment
        # evidence and can never reach the source database.
        execution_readiness_patch = patch(
            "backend.analysis.strategy_portfolio.aggregate_execution_readiness",
            return_value={
                "status": "ready", "blockers": [], "blocked_fund_codes": [],
                "policy": "isolated_mechanics_verification_override",
            },
        )
        investment_readiness_patch = patch(
            "backend.analysis.strategy_portfolio.investment_validation_readiness",
            return_value={
                "status": "ready", "investment_operations_enabled": True, "blockers": [],
                "meaning": "仅用于隔离副本的交易机械验收；不代表投资资格。",
            },
        )
        execution_readiness_patch.start()
        investment_readiness_patch.start()

        mechanics_universe = {
            **universe,
            "execution_readiness": {
                "status": "ready", "blockers": [], "blocked_fund_codes": [],
                "policy": "isolated_mechanics_verification_override",
            },
            "investment_readiness": {
                "status": "ready", "investment_operations_enabled": True, "blockers": [],
                "meaning": "仅用于隔离副本的交易机械验收；不代表投资资格。",
            },
        }
        for universe_mock in archived_universe_mocks:
            universe_mock.return_value = mechanics_universe

        buy_account = create_account(
            client, label="buy", effective_date=signal["entry_date"], position=None,
        )
        buy_run, buy_item = recommend(
            client, buy_account["account_id"], signal["entry_date"], fund_code=code,
        )
        assert_action(buy_item, "BUY", "买入")
        target_weight = decimal(buy_item["target_weight_pct"], "buy.target_weight_pct") / 100
        require(target_weight > 0, "BUY建议的目标仓位不是正数")
        band = StrategyPolicy().rebalance_band / Decimal(len(universe["items"]))

        weights = {
            "ADD": max(Decimal("0.001"), target_weight - band * 2),
            "HOLD": target_weight,
            "REDUCE": min(Decimal("0.75"), target_weight + band * 2),
        }
        scenario_items: dict[str, dict[str, Any]] = {"BUY": buy_item}
        scenario_runs: dict[str, dict[str, Any]] = {"BUY": buy_run}
        scenario_accounts: dict[str, dict[str, Any]] = {"BUY": buy_account}
        for action, weight in weights.items():
            account = create_account(
                client,
                label=action.lower(),
                effective_date=signal["entry_date"],
                position=(code, entry_price, weight),
            )
            run, item = recommend(client, account["account_id"], signal["entry_date"], fund_code=code)
            assert_action(item, action, action)
            scenario_items[action] = item
            scenario_runs[action] = run
            scenario_accounts[action] = account

        wait_account = create_account(
            client, label="wait", effective_date=signal["exit_date"], position=None,
        )
        wait_run, wait_item = recommend(
            client, wait_account["account_id"], signal["exit_date"], fund_code=code,
        )
        assert_action(wait_item, "WAIT", "等待")
        require(wait_item["reason_code"] == "NO_LONG_ENTRY", "WAIT不是由真实空仓无入场信号产生")
        scenario_items["WAIT"] = wait_item
        scenario_runs["WAIT"] = wait_run
        scenario_accounts["WAIT"] = wait_account

        exit_account = create_account(
            client,
            label="exit",
            effective_date=signal["exit_date"],
            position=(code, exit_price, Decimal("0.10")),
        )
        exit_run, exit_item = recommend(
            client, exit_account["account_id"], signal["exit_date"], fund_code=code,
        )
        assert_action(exit_item, "EXIT", "清仓")
        require(decimal(exit_item["proposed_units"], "exit.proposed_units") > 0, "EXIT没有给出清仓份额")
        scenario_items["EXIT"] = exit_item
        scenario_runs["EXIT"] = exit_run
        scenario_accounts["EXIT"] = exit_account

        earliest_text = str(exit_run["output"]["execution_policy"]["earliest_execution_session"])
        # Historical signals cannot truthfully be accepted "today" and then
        # linked to a past business date.  Freeze only the disposable harness
        # clock to the recommendation's one valid execution session.
        harness_timestamp = f"{earliest_text}T01:00:00.000000Z"
        strategy_clock_patch = patch(
            "backend.storage.strategy_runs.utc_now", return_value=harness_timestamp,
        )
        portfolio_clock_patch = patch(
            "backend.storage.portfolio.utc_now", return_value=harness_timestamp,
        )
        strategy_clock_patch.start()
        portfolio_clock_patch.start()
        decision = api_ok(
            client.post(
                "/api/v1/strategy/decisions",
                body={
                    "recommendation_id": exit_run["recommendation_id"],
                    "account_id": exit_account["account_id"],
                    "cutoff": signal["exit_date"],
                    "fund_code": code,
                    "proposed_action": "EXIT",
                    "choice": "accepted",
                    "planned_amount": exit_item["proposed_amount"],
                    "reason": "隔离验收：按真实历史信号接受清仓建议",
                    "supersedes_decision_id": None,
                },
                idempotency_key="workflow-exit-decision",
            ),
            "记录清仓决定",
        )
        units = decimal(exit_item["current_units"], "exit.current_units")
        calendar = get_calendar()
        earliest = date.fromisoformat(earliest_text)
        require(calendar.is_session(earliest), "建议给出的最早执行日不是已核验交易日")
        execution_date = earliest.isoformat()
        execution = api_ok(
            client.post(
                f"/api/v1/strategy/decisions/{decision['decision_id']}/executions",
                body={
                    "event_type": "sell",
                    "fund_code": code,
                    "amount": exit_item["proposed_amount"],
                    "units": number(units),
                    "business_date": execution_date,
                    "confirmation_date": execution_date,
                    "settlement_date": execution_date,
                    "channel": "人工确认账本",
                    "confirmed_fee": "0",
                    "state": "confirmed",
                    "source": "isolated_workflow_verification",
                    "note": "隔离验收：模拟清仓确认；不写入生产数据库",
                    "role": "actual_execution",
                },
                idempotency_key="workflow-exit-sale",
            ),
            "原子登记清仓成交并关联决定",
        )
        sale = execution["event"]
        trade_link = execution["link"]
        after_exit = api_ok(
            client.get(
                "/api/v1/portfolio/snapshots",
                params={"account_id": exit_account["account_id"], "cutoff": execution_date},
            ),
            "读取清仓后快照",
        )
        require(not after_exit["positions"], "卖出全部份额后仍残留持仓")
        require(not after_exit["pending_events"] and not after_exit["unknown_events"], "清仓后存在未确认账本事件")
        portfolio_clock_patch.stop()
        strategy_clock_patch.stop()

        reverse_account = create_account(
            client,
            label="reverse",
            effective_date=signal["entry_date"],
            position=(code, entry_price, Decimal("0.10")),
        )
        before_reverse = api_ok(
            client.get(
                "/api/v1/portfolio/snapshots",
                params={"account_id": reverse_account["account_id"], "cutoff": signal["entry_date"]},
            ),
            "读取更正前快照",
        )
        original_cash = before_reverse["cash"]["confirmed"]
        correction = api_ok(
            client.post(
                f"/api/v1/ledger/events/{reverse_account['initial_event']['event_id']}/corrections",
                body={"action": "reverse", "reason": "隔离验收：撤销误录初始持仓", "replacement": None},
                idempotency_key="workflow-reverse-position",
            ),
            "撤销误录持仓",
        )
        after_reverse = api_ok(
            client.get(
                "/api/v1/portfolio/snapshots",
                params={"account_id": reverse_account["account_id"], "cutoff": signal["entry_date"]},
            ),
            "读取更正后快照",
        )
        require(not after_reverse["positions"], "reverse更正后误录持仓仍存在")
        require(after_reverse["cash"]["confirmed"] == original_cash, "reverse更正错误地产生了买卖现金流")

        investment_readiness_patch.stop()
        execution_readiness_patch.stop()
        for universe_mock in archived_universe_mocks:
            universe_mock.return_value = universe

        backtest = api_ok(
            client.post(
                "/api/v1/strategy/backtests",
                body={"initial_cash": "100000", "buy_fee_bps": "10", "sell_fee_bps": "10"},
            ),
            "执行真实历史回测",
        )
        historical = curve_integrity(backtest)
        for patcher in reversed(archived_universe_patchers):
            patcher.stop()

        actions = {
            action: {
                "account_id": scenario_accounts[action]["account_id"],
                "recommendation_id": scenario_runs[action]["recommendation_id"],
                "cutoff": scenario_runs[action]["input_cutoff"],
                "data_as_of": scenario_runs[action]["data_as_of"],
                "action": item["action"],
                "reason_code": item["reason_code"],
                "current_weight_pct": item["current_weight_pct"],
                "target_weight_pct": item["target_weight_pct"],
                "proposed_amount": item["proposed_amount"],
                "proposed_units": item["proposed_units"],
                "valuation_price": item["valuation_price"],
                "valuation_as_of": item["valuation_as_of"],
                "data_quality": item["data_quality"]["status"],
            }
            for action, item in scenario_items.items()
        }
        require(set(actions) == {"BUY", "ADD", "HOLD", "REDUCE", "EXIT", "WAIT"}, "动作覆盖不完整")
        require({value["action"] for value in actions.values()} == set(actions), "动作结果与场景名称不一致")

        return {
            "health": {
                "schema_version": health["schema_version"],
                "capabilities_verified": ["portfolio_ledger_v1", "strategy_execution_v1"],
            },
            "real_data": {
                "universe_version": universe["version"],
                "production_eligible_fund_count": len(production_universe["items"]),
                "retired_relation_exclusions": retired_exclusions,
                "archived_mechanics_fund_count": len(universe["items"]),
                "archived_mechanics_funds": [
                    {
                        "code": item["code"],
                        "name": item["name"],
                        "index_code": item["index_code"],
                        "observation_count": item["observation_count"],
                        "first_date": item["first_date"],
                        "last_date": item["last_date"],
                        "series_source_id": item["series_source_id"],
                    }
                    for item in universe["items"]
                ],
                "derived_signal": signal,
            },
            "formal_fail_closed_gate": formal_gate,
            "isolated_mechanics_harness": {
                "readiness_override": True,
                "source_database_written": False,
                "investment_effectiveness_claimed": False,
                "purpose": "仅验证六类动作、金额、账本、更正与成交关联的程序机械完整性",
            },
            "action_scenarios": actions,
            "ledger_lifecycle": {
                "sell_to_zero": {
                    "decision_id": decision["decision_id"],
                    "event_id": sale["event_id"],
                    "link_id": trade_link["link_id"],
                    "execution_date": execution_date,
                    "remaining_positions": len(after_exit["positions"]),
                    "pending_events": len(after_exit["pending_events"]),
                    "unknown_events": len(after_exit["unknown_events"]),
                },
                "reverse_correction": {
                    "correction_id": correction["correction_id"],
                    "remaining_positions": len(after_reverse["positions"]),
                    "cash_before": original_cash,
                    "cash_after": after_reverse["cash"]["confirmed"],
                    "cash_flow_created": False,
                },
            },
            "historical_validation": historical,
        }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="在真实数据库只读副本上验收持仓、策略、成交关联与历史回测闭环",
    )
    parser.add_argument(
        "--database",
        type=Path,
        default=Settings.from_env().database_path,
        help="真实源数据库；脚本仅用SQLite mode=ro打开",
    )
    parser.add_argument("--output", type=Path, help="可选JSON验收报告路径")
    return parser.parse_args(argv)


def _main(args: argparse.Namespace) -> int:
    source = args.database.expanduser().resolve()
    report: dict[str, Any]
    try:
        before_files = file_fingerprints(source)
        before_counts = logical_counts(source)
        before_markers = verification_marker_counts(source)
        with tempfile.TemporaryDirectory(prefix="fundadvicer-strategy-workflow-") as directory:
            copy_path = Path(directory) / "isolated.sqlite3"
            backup = isolated_backup(source, copy_path)
            workflow = run_workflow(copy_path)
            isolated_counts = logical_counts(copy_path)
        after_counts = logical_counts(source)
        after_markers = verification_marker_counts(source)
        after_files = file_fingerprints(source)
        # SQLite readers may update the transient -shm lock/index file even
        # when the database was opened mode=ro. Compare the durable database
        # and WAL contents plus all formal business-table counts; do not treat
        # reader coordination bytes as a business write.
        durable_before = durable_content_fingerprints(before_files)
        durable_after = durable_content_fingerprints(after_files)
        durable_unchanged = durable_before == durable_after
        auxiliary_lifecycle_observed = any(
            (before_files.get(key) is None) != (after_files.get(key) is None)
            for key in ("-wal", "-shm")
        )
        source_business_unchanged = before_counts == after_counts and before_markers == after_markers
        require(source_business_unchanged, "验收期间源数据库业务表发生变化；请排除外部并发写入后重试")
        require(not any(after_markers.values()), "源数据库中发现隔离验收标记，拒绝通过")
        report = {
            "status": "passed",
            "source_database": {
                "path": str(source),
                "access_mode": "sqlite_uri_mode_ro_and_query_only",
                "logical_counts_before": before_counts,
                "logical_counts_after": after_counts,
                "business_rows_unchanged_during_run": source_business_unchanged,
                "verification_markers_before": before_markers,
                "verification_markers_after": after_markers,
                "durable_content_fingerprints_before": durable_before,
                "durable_content_fingerprints_after": durable_after,
                "durable_source_files_unchanged_during_run": durable_unchanged,
                "external_or_background_database_activity_observed": not durable_unchanged,
                "sqlite_auxiliary_file_lifecycle_observed": auxiliary_lifecycle_observed,
                "shared_memory_coordination_changed": before_files.get("-shm") != after_files.get("-shm"),
            },
            "isolation": {
                **backup,
                "temporary_copy_removed": True,
                "isolated_business_counts": isolated_counts,
            },
            **workflow,
            "acceptance": {
                "real_stored_series_only": True,
                "production_business_rows_written": 0,
                "formal_investment_gate_fail_closed_verified": True,
                "formal_actions_enabled": False,
                "isolated_mechanics_actions_verified": ["BUY", "ADD", "HOLD", "REDUCE", "EXIT", "WAIT"],
                "sell_to_zero_verified": True,
                "reverse_is_not_sale_verified": True,
                "decision_actual_execution_link_verified": True,
                "long_history_curve_and_validation_verified": True,
                "no_non_finite_or_negative_balance_detected": True,
            },
        }
        exit_code = 0
    except Exception as exc:
        report = {
            "status": "failed",
            "source_database": str(source),
            "error_type": type(exc).__name__,
            "error": str(exc),
        }
        exit_code = 1

    serialized = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output:
        output = args.output.expanduser().resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary = output.with_suffix(output.suffix + ".tmp")
        temporary.write_text(serialized, encoding="utf-8")
        temporary.replace(output)
    print(serialized, end="")
    return exit_code


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    with localcontext() as context:
        context.prec = 40
        return _main(args)


if __name__ == "__main__":
    raise SystemExit(main())
