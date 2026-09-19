from copy import deepcopy
from datetime import date
from decimal import Decimal, localcontext
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from asgi_client import ASGITestClient
from backend.analysis.strategy_portfolio import _combine_curves, historical_strategy_report
from backend.analysis.strategy_service import build_recommendation, create_recommendation
from backend.api.main import create_app
from backend.core.config import Settings
from backend.core.trading_calendar import get_calendar
from backend.storage.catalog import import_catalog
from backend.storage.portfolio import append_condition, append_event, create_account
from backend.storage.related_market import import_related_index
from backend.storage.timeseries import import_timeseries


def nav_rows(end: str, *, count: int = 150, mode: str = "strong") -> list[dict[str, str]]:
    calendar = get_calendar()
    end_day = date.fromisoformat(end)
    sessions = calendar.sessions(date(2025, 1, 2), end_day)[-count:]
    price = Decimal("1")
    rows = []
    for index, session in enumerate(sessions):
        if index:
            if mode == "strong":
                daily = Decimal("0.003")
            else:
                if index <= 121:
                    daily = Decimal("0.002")
                elif index <= 150:
                    daily = Decimal("0.0005")
                elif index <= 205:
                    daily = Decimal("0.003")
                elif index <= 230:
                    daily = Decimal("-0.03")
                else:
                    daily = Decimal("0")
            price *= Decimal(1) + daily
        rows.append({"date": session.isoformat(), "unit_nav": format(price, "f"), "accumulated_nav": None})
    return rows


def add_relation_and_nav(settings: Settings, code: str, index_code: str, rows: list[dict[str, str]]) -> None:
    import_related_index(
        settings,
        fund_code=code,
        index_code=index_code,
        index_name=f"核验指数{index_code}",
        rows=[{
            "date": rows[-1]["date"], "open": "100", "high": "101", "low": "99",
            "close": "100", "volume": "1", "amount": None,
        }],
        source_id="index_daily.official_test_shape",
        relation_source_id="fund_relations.manager_documents",
        evidence_url="https://example.test/prospectus.pdf",
    )
    import_timeseries(
        settings, code, "nav", rows,
        source_id="fund_nav.manager_documents", policy_version="test-real-shape-v1",
    )


def account(settings: Settings, key: str, *, risk: str = "balanced", horizon: int = 365,
            no_holdings_confirmed: bool = True) -> str:
    account_id = create_account(settings, {"name": f"账户{key}", "base_currency": "CNY"},
                                idempotency_key=f"account-{key}")["account_id"]
    append_condition(
        settings,
        {
            "account_id": account_id,
            "effective_date": "2025-01-02",
            "risk_level": risk,
            "max_single_position_pct": "50",
            "investment_horizon_days": horizon,
            "channel": "测试隔离账户",
            "planned_additional_cash": None,
            "no_holdings_confirmed": no_holdings_confirmed,
        },
        idempotency_key=f"condition-{key}",
    )
    append_event(
        settings,
        {
            "account_id": account_id, "event_type": "cash_in", "fund_code": None,
            "amount": "100000", "units": None, "business_date": "2025-01-02",
            "confirmation_date": "2025-01-02", "settlement_date": "2025-01-02",
            "channel": "测试隔离账户", "confirmed_fee": None, "state": "confirmed",
            "source": "test_confirmed_fact", "note": None,
        },
        idempotency_key=f"cash-{key}",
    )
    return account_id


class StrategyRecommendationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.settings = Settings(database_path=Path(self.temp.name) / "strategy.sqlite3")
        readiness = patch(
            "backend.analysis.strategy_portfolio.investment_validation_readiness",
            return_value={
                "status": "ready",
                "investment_operations_enabled": True,
                "blockers": [],
                "meaning": "测试显式注入的投资级资格",
            },
        )
        readiness.start()
        self.addCleanup(readiness.stop)
        import_catalog(
            self.settings,
            [
                {"code": "510001", "name": "核验基金一", "fund_type": "ETF"},
                {"code": "510002", "name": "核验基金二", "fund_type": "ETF"},
                {"code": "510099", "name": "未纳入策略基金", "fund_type": "ETF"},
            ],
            policy_version="test-catalog",
            source_id="fund_catalog.manager_documents",
        )
        add_relation_and_nav(self.settings, "510001", "980001", nav_rows("2026-09-16"))
        add_relation_and_nav(self.settings, "510002", "980002", nav_rows("2026-09-11"))
        import_timeseries(
            self.settings, "510099", "nav", nav_rows("2026-09-16"),
            source_id="fund_nav.manager_documents", policy_version="test-real-shape-v1",
        )

    def test_ready_fund_can_buy_while_stale_and_unsupported_funds_wait(self):
        account_id = account(self.settings, "mixed")
        append_event(
            self.settings,
            {
                "account_id": account_id, "event_type": "initial_position", "fund_code": "510099",
                "amount": None, "units": "10", "business_date": "2025-01-02",
                "confirmation_date": "2025-01-02", "settlement_date": None,
                "channel": "测试隔离账户", "confirmed_fee": None, "state": "confirmed",
                "source": "test_confirmed_fact", "note": None,
            },
            idempotency_key="unknown-position",
        )
        _, output, input_cutoff, data_as_of = build_recommendation(
            self.settings, account_id=account_id, cutoff="2026-09-16",
        )
        by_code = {item["fund_code"]: item for item in output["items"]}
        self.assertEqual(by_code["510001"]["action"], "BUY")
        self.assertEqual(by_code["510001"]["data_quality"]["status"], "ready")
        self.assertEqual(by_code["510002"]["action"], "WAIT")
        self.assertIn("stale_last_session", by_code["510002"]["data_quality"]["errors"])
        self.assertEqual(by_code["510002"]["target_weight_pct"], by_code["510002"]["current_weight_pct"])
        self.assertEqual((by_code["510099"]["action"], by_code["510099"]["supported"]), ("WAIT", False))
        self.assertEqual(output["status"], "partial")
        self.assertEqual(input_cutoff, "2026-09-16")
        self.assertEqual(data_as_of, "2026-09-16")
        self.assertFalse(output["execution_policy"]["automatic_ordering"])

    def test_risk_level_caps_total_targets_and_short_horizon_blocks_increases(self):
        conservative = account(self.settings, "conservative", risk="conservative")
        _, output, _, _ = build_recommendation(
            self.settings, account_id=conservative, cutoff="2026-09-11",
        )
        target = sum((Decimal(item["target_weight_pct"]) for item in output["items"] if item["supported"]), Decimal(0))
        self.assertLessEqual(target, Decimal("35"))
        self.assertEqual(output["risk_controls"]["total_market_exposure_cap_pct"], "35")

        short = account(self.settings, "short", risk="aggressive", horizon=90)
        _, short_output, _, _ = build_recommendation(
            self.settings, account_id=short, cutoff="2026-09-11",
        )
        supported = [item for item in short_output["items"] if item["supported"]]
        self.assertTrue(supported)
        self.assertEqual({item["action"] for item in supported}, {"WAIT"})
        self.assertEqual({item["reason_code"] for item in supported}, {"HORIZON_TOO_SHORT_FOR_INCREASE"})

    def test_candidate_fact_source_cannot_generate_formal_trade(self):
        import_timeseries(
            self.settings, "510001", "nav", nav_rows("2026-09-16"),
            source_id="exchange_daily.sina", policy_version="candidate-only-v1",
        )
        account_id = account(self.settings, "candidate-source")
        _, output, _, _ = build_recommendation(
            self.settings, account_id=account_id, cutoff="2026-09-16",
        )
        self.assertEqual(output["status"], "blocked")
        self.assertIn("strategy_source_not_execution_grade", output["blockers"])
        self.assertEqual(output["data_eligibility"]["blocked_fund_codes"], ["510001"])
        self.assertEqual({item["action"] for item in output["items"]}, {"WAIT"})
        by_code = {item["fund_code"]: item for item in output["items"]}
        self.assertIn(
            "source_not_primary_evidence",
            by_code["510001"]["source"]["data_eligibility"]["signal_blockers"],
        )

    def test_investment_validation_gate_blocks_otherwise_ready_buy(self):
        account_id = account(self.settings, "investment-gate")
        blocked = {
            "status": "blocked",
            "investment_operations_enabled": False,
            "blockers": ["walk_forward_holdout_not_completed"],
            "meaning": "尚未完成样本外验证",
        }
        with patch(
            "backend.analysis.strategy_portfolio.investment_validation_readiness",
            return_value=blocked,
        ):
            _, output, _, _ = build_recommendation(
                self.settings, account_id=account_id, cutoff="2026-09-16",
            )
        self.assertEqual(output["status"], "blocked")
        self.assertIn("strategy_not_investment_validated", output["blockers"])
        self.assertEqual({item["action"] for item in output["items"]}, {"WAIT"})
        self.assertEqual(output["investment_eligibility"], blocked)

    def test_blocked_exit_signal_remains_wait_but_exposes_non_executable_risk_alert(self):
        blocked_settings = Settings(database_path=Path(self.temp.name) / "blocked-exit.sqlite3")
        import_catalog(
            blocked_settings,
            [{"code": "510010", "name": "风险退出基金", "fund_type": "ETF"}],
            policy_version="test-catalog",
            source_id="fund_catalog.manager_documents",
        )
        rows = nav_rows("2026-09-16", count=240, mode="cycle")
        add_relation_and_nav(blocked_settings, "510010", "980010", rows)
        account_id = account(
            blocked_settings, "blocked-exit", risk="balanced", no_holdings_confirmed=False,
        )
        append_event(
            blocked_settings,
            {
                "account_id": account_id, "event_type": "initial_position", "fund_code": "510010",
                "amount": None, "units": "1000", "business_date": "2025-01-02",
                "confirmation_date": "2025-01-02", "settlement_date": None,
                "channel": "测试隔离账户", "confirmed_fee": None, "state": "confirmed",
                "source": "confirmed_external_position", "note": None,
            },
            idempotency_key="blocked-exit-position",
        )
        append_event(
            blocked_settings,
            {
                "account_id": account_id, "event_type": "cash_in", "fund_code": None,
                "amount": "1", "units": None, "business_date": "2026-09-16",
                "confirmation_date": None, "settlement_date": None,
                "channel": "测试隔离账户", "confirmed_fee": None, "state": "pending",
                "source": "pending_confirmation", "note": None,
            },
            idempotency_key="blocked-exit-pending",
        )

        _, output, _, _ = build_recommendation(
            blocked_settings, account_id=account_id, cutoff="2026-09-16",
        )

        item = output["items"][0]
        self.assertEqual(output["status"], "blocked")
        self.assertEqual(item["action"], "WAIT")
        self.assertIsNone(item["proposed_amount"])
        self.assertIsNone(item["proposed_units"])
        self.assertEqual(item["strategy_signal_action"], "EXIT")
        self.assertEqual(item["risk_alert"]["original_action"], "EXIT")
        self.assertFalse(item["risk_alert"]["direct_execution_allowed"])
        self.assertIn("pending_ledger_events_present", item["risk_alert"]["blockers"])

    def test_unconfirmed_empty_holdings_and_pending_events_fail_closed(self):
        unconfirmed = account(self.settings, "unconfirmed-empty", no_holdings_confirmed=False)
        _, unconfirmed_output, _, _ = build_recommendation(
            self.settings, account_id=unconfirmed, cutoff="2026-09-11",
        )
        self.assertEqual(unconfirmed_output["status"], "blocked")
        self.assertIn("empty_holdings_not_confirmed", unconfirmed_output["blockers"])
        self.assertEqual({item["action"] for item in unconfirmed_output["items"]}, {"WAIT"})

        pending = account(self.settings, "pending-event")
        append_event(
            self.settings,
            {
                "account_id": pending, "event_type": "buy", "fund_code": "510001",
                "amount": "100", "units": "1", "business_date": "2026-09-10",
                "confirmation_date": None, "settlement_date": None,
                "channel": "测试隔离账户", "confirmed_fee": None, "state": "pending",
                "source": "pending_confirmation", "note": None,
            },
            idempotency_key="pending-buy",
        )
        _, pending_output, _, _ = build_recommendation(
            self.settings, account_id=pending, cutoff="2026-09-11",
        )
        self.assertEqual(pending_output["status"], "blocked")
        self.assertIn("pending_ledger_events_present", pending_output["blockers"])
        self.assertEqual({item["action"] for item in pending_output["items"]}, {"WAIT"})

    def test_exhausted_risk_budget_is_wait_not_zero_weight_hold(self):
        capped = account(self.settings, "risk-budget", risk="conservative")
        append_event(
            self.settings,
            {
                "account_id": capped, "event_type": "initial_position", "fund_code": "510099",
                "amount": None, "units": "100000", "business_date": "2025-01-02",
                "confirmation_date": "2025-01-02", "settlement_date": None,
                "channel": "测试隔离账户", "confirmed_fee": None, "state": "confirmed",
                "source": "confirmed_external_position", "note": None,
            },
            idempotency_key="risk-budget-position",
        )
        _, output, _, _ = build_recommendation(
            self.settings, account_id=capped, cutoff="2026-09-11",
        )
        selected = [item for item in output["items"] if item["supported"]]
        self.assertTrue(selected)
        self.assertEqual({item["action"] for item in selected}, {"WAIT"})
        self.assertEqual({item["reason_code"] for item in selected}, {"RISK_BUDGET_EXHAUSTED"})

    def test_latest_recommendation_reports_current_stale_and_unavailable_without_saving(self):
        account_id = account(self.settings, "freshness")
        app = create_app(self.settings)
        with ASGITestClient(app) as client:
            missing = client.get(
                "/api/v1/strategy/recommendations/latest",
                params={"account_id": account_id, "cutoff": "2026-09-11"},
            )
            self.assertEqual(missing.status_code, 200)
            self.assertIsNone(missing.json()["item"])
            self.assertEqual(missing.json()["freshness"]["state"], "unavailable")
            self.assertEqual(missing.json()["freshness"]["reason_code"], "recommendation_missing")

            created = client.post(
                "/api/v1/strategy/recommendations",
                json_body={"account_id": account_id, "cutoff": "2026-09-11"},
            )
            self.assertEqual(created.status_code, 200)
            current = client.get(
                "/api/v1/strategy/recommendations/latest",
                params={"account_id": account_id, "cutoff": "2026-09-11"},
            )
            self.assertEqual(current.status_code, 200)
            self.assertEqual(current.json()["freshness"]["state"], "current")
            self.assertEqual(
                current.json()["freshness"]["stored_input_hash"],
                current.json()["freshness"]["current_input_hash"],
            )

        append_condition(
            self.settings,
            {
                "account_id": account_id,
                "effective_date": "2026-09-10",
                "risk_level": "conservative",
                "max_single_position_pct": "25",
                "investment_horizon_days": 365,
                "channel": "测试隔离账户",
                "planned_additional_cash": None,
                "no_holdings_confirmed": True,
            },
            idempotency_key="condition-freshness-revised",
        )
        with ASGITestClient(app) as client:
            stale = client.get(
                "/api/v1/strategy/recommendations/latest",
                params={"account_id": account_id, "cutoff": "2026-09-11"},
            )
            self.assertEqual(stale.status_code, 200)
            freshness = stale.json()["freshness"]
            self.assertEqual(freshness["state"], "stale")
            self.assertEqual(freshness["reason_code"], "inputs_changed")
            self.assertIn("portfolio_condition", freshness["changed_inputs"])
            history = client.get(
                "/api/v1/strategy/recommendations",
                params={"account_id": account_id},
            ).json()["items"]
            self.assertEqual(len(history), 1, "时效检查不得写入一条新建议")

            unavailable = client.get(
                "/api/v1/strategy/recommendations/latest",
                params={"account_id": account_id, "cutoff": "2026-02-30"},
            )
            self.assertEqual(unavailable.status_code, 200)
            self.assertEqual(unavailable.json()["freshness"]["state"], "unavailable")
            self.assertEqual(unavailable.json()["freshness"]["reason_code"], "strategy_input_invalid")

    def test_stale_held_fund_blocks_amounts_for_the_whole_portfolio(self):
        held = account(self.settings, "stale-held")
        append_event(
            self.settings,
            {
                "account_id": held, "event_type": "initial_position", "fund_code": "510002",
                "amount": None, "units": "100", "business_date": "2025-01-02",
                "confirmation_date": "2025-01-02", "settlement_date": None,
                "channel": "测试隔离账户", "confirmed_fee": None, "state": "confirmed",
                "source": "confirmed_external_position", "note": None,
            },
            idempotency_key="stale-held-position",
        )
        _, output, _, _ = build_recommendation(
            self.settings, account_id=held, cutoff="2026-09-16",
        )
        self.assertEqual(output["status"], "blocked")
        self.assertIn("portfolio_valuation_stale", output["blockers"])
        self.assertEqual(output["valuation"]["stale_fund_codes"], ["510002"])
        self.assertEqual({item["action"] for item in output["items"]}, {"WAIT"})

    def test_rebalance_waits_for_sale_settlement_before_recommending_buys(self):
        rotate_settings = Settings(database_path=Path(self.temp.name) / "rotate.sqlite3")
        funds = [
            {"code": "510011", "name": "趋势基金", "fund_type": "ETF"},
            {"code": "510012", "name": "待退出基金", "fund_type": "ETF"},
        ]
        import_catalog(rotate_settings, funds, policy_version="test-catalog",
                       source_id="fund_catalog.manager_documents")
        strong_rows = nav_rows("2026-09-16", count=240, mode="strong")
        exit_rows = nav_rows("2026-09-16", count=240, mode="cycle")
        add_relation_and_nav(rotate_settings, "510011", "980011", strong_rows)
        add_relation_and_nav(rotate_settings, "510012", "980012", exit_rows)
        account_id = account(
            rotate_settings,
            "rotate",
            risk="balanced",
            no_holdings_confirmed=False,
        )
        exit_price = Decimal(exit_rows[-1]["unit_nav"])
        append_event(
            rotate_settings,
            {
                "account_id": account_id,
                "event_type": "buy",
                "fund_code": "510012",
                "amount": "100000",
                "units": format(Decimal("100000") / exit_price, "f"),
                "business_date": "2025-01-02",
                "confirmation_date": "2025-01-02",
                "settlement_date": "2025-01-02",
                "channel": "测试隔离账户",
                "confirmed_fee": "0",
                "state": "confirmed",
                "source": "test_execution",
                "note": None,
            },
            idempotency_key="rotate-invested-cash",
        )

        _, output, _, _ = build_recommendation(
            rotate_settings,
            account_id=account_id,
            cutoff="2026-09-16",
        )
        by_code = {item["fund_code"]: item for item in output["items"]}
        self.assertEqual(by_code["510012"]["action"], "EXIT")
        self.assertEqual(by_code["510011"]["action"], "WAIT")
        self.assertEqual(by_code["510011"]["reason_code"], "REBALANCE_SELLS_FIRST")
        self.assertIsNone(by_code["510011"]["proposed_amount"])
        self.assertIsNotNone(by_code["510011"]["deferred_target_weight_pct"])
        self.assertIn("sell_settlement_required_before_buys", output["warnings"])
        self.assertEqual(output["status"], "partial")

    def test_decision_and_trade_links_reject_cross_account_or_tampered_actions(self):
        first = account(self.settings, "first")
        second = account(self.settings, "second")
        with patch(
            "backend.storage.strategy_runs.utc_now",
            return_value="2026-09-11T09:00:00Z",
        ):
            recommendation = create_recommendation(
                self.settings, account_id=first, cutoff="2026-09-11",
            )
        item = next(value for value in recommendation["output"]["items"] if value["action"] == "BUY")
        app = create_app(self.settings)
        with ASGITestClient(app) as client:
            self.assertIn("strategy_execution_v1", client.get("/health").json()["capabilities"])
            body = {
                "recommendation_id": recommendation["recommendation_id"],
                "account_id": second,
                "cutoff": "2026-09-11",
                "fund_code": item["fund_code"],
                "proposed_action": "BUY",
                "choice": "accepted",
                "planned_amount": item["proposed_amount"],
                "reason": "按正式建议执行",
                "supersedes_decision_id": None,
            }
            self.assertEqual(client.post("/api/v1/strategy/decisions", json_body=body,
                                         headers={"Idempotency-Key": "cross"}).status_code, 409)
            self.assertEqual(client.post(
                "/api/v1/strategy/decisions",
                json_body={**body, "account_id": first, "proposed_action": "EXIT"},
                headers={"Idempotency-Key": "tampered"},
            ).status_code, 409)
            self.assertEqual(client.post(
                "/api/v1/strategy/decisions",
                json_body={**body, "account_id": first, "planned_amount": "NaN"},
                headers={"Idempotency-Key": "nan"},
            ).status_code, 422)
            self.assertEqual(client.post(
                "/api/v1/strategy/decisions",
                json_body={**body, "account_id": first, "planned_amount": "1"},
                headers={"Idempotency-Key": "wrong-full-amount"},
            ).status_code, 409)
            self.assertEqual(client.post(
                "/api/v1/strategy/decisions",
                json_body={**body, "account_id": first, "planned_amount": None},
                headers={"Idempotency-Key": "missing-full-amount"},
            ).status_code, 422)
            self.assertEqual(client.post(
                "/api/v1/strategy/decisions",
                json_body={**body, "account_id": first, "choice": "partially_accepted",
                           "planned_amount": item["proposed_amount"]},
                headers={"Idempotency-Key": "not-actually-partial"},
            ).status_code, 422)
            with patch(
                "backend.storage.strategy_runs.utc_now",
                return_value="2026-09-11T09:00:00Z",
            ):
                accepted = client.post(
                    "/api/v1/strategy/decisions", json_body={**body, "account_id": first},
                    headers={"Idempotency-Key": "accepted"},
                )
            self.assertEqual(accepted.status_code, 200)
            append_condition(
                self.settings,
                {
                    "account_id": first,
                    "effective_date": "2026-09-11",
                    "risk_level": "conservative",
                    "max_single_position_pct": "35",
                    "investment_horizon_days": 365,
                    "channel": "测试隔离账户",
                    "planned_additional_cash": None,
                    "no_holdings_confirmed": True,
                },
                idempotency_key="condition-first-revised-after-decision",
            )
            retry = client.post(
                "/api/v1/strategy/decisions", json_body={**body, "account_id": first},
                headers={"Idempotency-Key": "accepted"},
            )
            self.assertEqual(retry.status_code, 200)
            self.assertEqual(retry.json()["decision_id"], accepted.json()["decision_id"])
            stale = client.post(
                "/api/v1/strategy/decisions", json_body={**body, "account_id": first},
                headers={"Idempotency-Key": "stale-new-decision"},
            )
            self.assertEqual(stale.status_code, 409)
            self.assertEqual(stale.json()["error"]["code"], "strategy_decision_stale")

        old_event = append_event(
            self.settings,
            {
                "account_id": first, "event_type": "buy", "fund_code": item["fund_code"],
                "amount": "100", "units": "1", "business_date": "2026-09-11",
                "confirmation_date": "2026-09-11", "settlement_date": "2026-09-14",
                "channel": "测试隔离账户", "confirmed_fee": "0", "state": "confirmed",
                "source": "test_execution", "note": None,
            }, idempotency_key="old-buy",
        )
        first_event = append_event(
            self.settings,
            {
                "account_id": first, "event_type": "buy", "fund_code": item["fund_code"],
                "amount": "100", "units": "1", "business_date": "2026-09-14",
                "confirmation_date": "2026-09-14", "settlement_date": "2026-09-15",
                "channel": "测试隔离账户", "confirmed_fee": "0", "state": "confirmed",
                "source": "test_execution", "note": None,
            }, idempotency_key="first-buy",
        )
        second_event = append_event(
            self.settings,
            {
                "account_id": second, "event_type": "buy", "fund_code": item["fund_code"],
                "amount": "100", "units": "1", "business_date": "2026-09-14",
                "confirmation_date": "2026-09-14", "settlement_date": "2026-09-15",
                "channel": "测试隔离账户", "confirmed_fee": "0", "state": "confirmed",
                "source": "test_execution", "note": None,
            }, idempotency_key="second-buy",
        )
        wrong_fund_event = append_event(
            self.settings,
            {
                "account_id": first, "event_type": "buy", "fund_code": "510002",
                "amount": "100", "units": "1", "business_date": "2026-09-14",
                "confirmation_date": "2026-09-14", "settlement_date": "2026-09-15",
                "channel": "测试隔离账户", "confirmed_fee": "0", "state": "confirmed",
                "source": "test_execution", "note": None,
            }, idempotency_key="wrong-fund-buy",
        )
        wrong_direction_event = append_event(
            self.settings,
            {
                "account_id": first, "event_type": "sell", "fund_code": item["fund_code"],
                "amount": "50", "units": "0.5", "business_date": "2026-09-14",
                "confirmation_date": "2026-09-14", "settlement_date": "2026-09-15",
                "channel": "测试隔离账户", "confirmed_fee": "0", "state": "confirmed",
                "source": "test_execution", "note": None,
            }, idempotency_key="wrong-direction-sell",
        )
        completion_event = append_event(
            self.settings,
            {
                "account_id": first, "event_type": "buy", "fund_code": item["fund_code"],
                "amount": format(Decimal(str(item["proposed_amount"])) - Decimal("100"), "f"),
                "units": format(Decimal(str(item["proposed_units"])) - Decimal("1"), "f"),
                "business_date": "2026-09-14",
                "confirmation_date": "2026-09-15", "settlement_date": "2026-09-16",
                "channel": "测试隔离账户", "confirmed_fee": "0", "state": "confirmed",
                "source": "test_execution", "note": None,
            }, idempotency_key="completion-buy",
        )
        over_event = append_event(
            self.settings,
            {
                "account_id": first, "event_type": "buy", "fund_code": item["fund_code"],
                "amount": "1", "units": "0.000001",
                "business_date": "2026-09-14",
                "confirmation_date": "2026-09-15", "settlement_date": "2026-09-17",
                "channel": "测试隔离账户", "confirmed_fee": "0", "state": "confirmed",
                "source": "test_execution", "note": None,
            }, idempotency_key="over-buy",
        )
        expired_event = append_event(
            self.settings,
            {
                "account_id": first, "event_type": "buy", "fund_code": item["fund_code"],
                "amount": "1", "units": "0.000001", "business_date": "2026-09-15",
                "confirmation_date": "2026-09-15", "settlement_date": "2026-09-16",
                "channel": "测试隔离账户", "confirmed_fee": "0", "state": "confirmed",
                "source": "expired_execution", "note": None,
            }, idempotency_key="expired-buy",
        )
        app = create_app(self.settings)
        with ASGITestClient(app) as client:
            path = f"/api/v1/strategy/decisions/{accepted.json()['decision_id']}/trades"
            causal = client.post(f"{path}/{old_event['event_id']}",
                                 json_body={"role": "actual_execution"})
            self.assertEqual(causal.status_code, 409)
            self.assertEqual(causal.json()["error"]["code"], "strategy_trade_link_time_invalid")
            self.assertEqual(client.post(f"{path}/{second_event['event_id']}",
                                         json_body={"role": "actual_execution"}).status_code, 409)
            self.assertEqual(client.post(f"{path}/{wrong_fund_event['event_id']}",
                                         json_body={"role": "actual_execution"}).status_code, 409)
            self.assertEqual(client.post(f"{path}/{wrong_direction_event['event_id']}",
                                         json_body={"role": "actual_execution"}).status_code, 409)
            expired = client.post(f"{path}/{expired_event['event_id']}",
                                  json_body={"role": "actual_execution"})
            self.assertEqual(expired.status_code, 409)
            self.assertEqual(expired.json()["error"]["code"], "strategy_trade_link_expired")
            self.assertEqual(
                expired.json()["error"]["details"]["latest_execution_session"],
                "2026-09-14",
            )
            self.assertEqual(client.post(f"{path}/{first_event['event_id']}",
                                         json_body={"role": "actual_execution"}).status_code, 200)
            listed = client.get(
                "/api/v1/strategy/decisions", params={"account_id": first},
            ).json()["items"][0]
            self.assertEqual(listed["execution"]["status"], "partially_executed")
            self.assertEqual(listed["execution"]["actual_amount"], "100")
            self.assertEqual(listed["execution"]["actual_units"], "1")
            self.assertEqual(listed["execution"]["actual_fee"], "0")
            self.assertEqual(listed["execution"]["actual_event_count"], 1)
            self.assertEqual(Decimal(listed["planned_units"]), Decimal(str(item["proposed_units"])))
            self.assertEqual(len(listed["trade_links"]), 1)
            self.assertEqual(listed["trade_links"][0]["event_id"], first_event["event_id"])
            self.assertEqual(listed["trade_links"][0]["role"], "actual_execution")
            self.assertEqual(client.post(f"{path}/{completion_event['event_id']}",
                                         json_body={"role": "actual_execution"}).status_code, 200)
            completed = client.get(
                "/api/v1/strategy/decisions", params={"account_id": first},
            ).json()["items"][0]
            self.assertEqual(completed["execution"]["status"], "executed")
            self.assertLessEqual(
                abs(Decimal(completed["execution"]["actual_amount"])
                    - Decimal(str(item["proposed_amount"]))),
                Decimal("0.01"),
            )
            self.assertLessEqual(
                abs(Decimal(completed["execution"]["actual_units"])
                    - Decimal(str(item["proposed_units"]))),
                Decimal("0.000001"),
            )
            self.assertEqual(completed["execution"]["actual_event_count"], 2)
            self.assertEqual(client.post(f"{path}/{over_event['event_id']}",
                                         json_body={"role": "actual_execution"}).status_code, 200)
            over = client.get(
                "/api/v1/strategy/decisions", params={"account_id": first},
            ).json()["items"][0]
            self.assertEqual(over["execution"]["status"], "over_executed")
            self.assertEqual(over["execution"]["actual_event_count"], 3)

    def test_atomic_decision_execution_rolls_back_invalid_link_and_replays_once(self):
        account_id = account(self.settings, "atomic-execution")
        with patch(
            "backend.storage.strategy_runs.utc_now",
            return_value="2026-09-11T09:00:00Z",
        ):
            recommendation = create_recommendation(
                self.settings,
                account_id=account_id,
                cutoff="2026-09-11",
            )
        item = next(value for value in recommendation["output"]["items"] if value["action"] == "BUY")
        app = create_app(self.settings)
        with ASGITestClient(app) as client:
            with patch(
                "backend.storage.strategy_runs.utc_now",
                return_value="2026-09-11T09:00:00Z",
            ):
                decision = client.post(
                    "/api/v1/strategy/decisions",
                    json_body={
                        "recommendation_id": recommendation["recommendation_id"],
                        "account_id": account_id,
                        "cutoff": "2026-09-11",
                        "fund_code": item["fund_code"],
                        "proposed_action": "BUY",
                        "choice": "accepted",
                        "planned_amount": item["proposed_amount"],
                        "reason": "核验原子成交",
                        "supersedes_decision_id": None,
                    },
                    headers={"Idempotency-Key": "atomic-decision"},
                ).json()
            path = f"/api/v1/strategy/decisions/{decision['decision_id']}/executions"
            invalid = {
                "event_type": "buy",
                "fund_code": item["fund_code"],
                "amount": "100",
                "units": "1",
                "business_date": "2026-09-11",
                "confirmation_date": "2026-09-11",
                "settlement_date": "2026-09-14",
                "channel": "测试隔离账户",
                "confirmed_fee": "0",
                "state": "confirmed",
                "source": "atomic_invalid_should_rollback",
                "note": None,
                "role": "actual_execution",
            }
            failed = client.post(
                path,
                json_body=invalid,
                headers={"Idempotency-Key": "atomic-execution"},
            )
            self.assertEqual(failed.status_code, 409)
            self.assertEqual(failed.json()["error"]["code"], "strategy_trade_link_time_invalid")
            ledger_after_failure = client.get(
                "/api/v1/ledger/events",
                params={"account_id": account_id},
            ).json()["items"]
            self.assertFalse(any(row["source"] == invalid["source"] for row in ledger_after_failure))

            valid = {
                **invalid,
                "business_date": "2026-09-14",
                "confirmation_date": "2026-09-14",
                "settlement_date": "2026-09-15",
                "source": "atomic_valid_execution",
            }
            first = client.post(
                path,
                json_body=valid,
                headers={"Idempotency-Key": "atomic-execution"},
            )
            self.assertEqual(first.status_code, 200)
            replay = client.post(
                path,
                json_body=valid,
                headers={"Idempotency-Key": "atomic-execution"},
            )
            self.assertEqual(replay.status_code, 200)
            self.assertEqual(replay.json()["event"]["event_id"], first.json()["event"]["event_id"])
            self.assertEqual(replay.json()["link"]["link_id"], first.json()["link"]["link_id"])
            ledger = client.get(
                "/api/v1/ledger/events",
                params={"account_id": account_id},
            ).json()["items"]
            self.assertEqual(sum(row["source"] == "atomic_valid_execution" for row in ledger), 1)
            listed = client.get(
                "/api/v1/strategy/decisions",
                params={"account_id": account_id},
            ).json()["items"][0]
            self.assertEqual(listed["execution"]["actual_event_count"], 1)
            self.assertEqual(len(listed["trade_links"]), 1)

    def test_empty_universe_returns_account_wait_and_can_record_deferral(self):
        empty_settings = Settings(database_path=Path(self.temp.name) / "empty.sqlite3")
        import_catalog(
            empty_settings,
            [{"code": "510090", "name": "仅目录基金", "fund_type": "ETF"}],
            policy_version="test-catalog",
            source_id="fund_catalog.manager_documents",
        )
        account_id = account(empty_settings, "empty")
        recommendation = create_recommendation(
            empty_settings, account_id=account_id, cutoff="2026-09-16",
        )
        self.assertEqual(recommendation["status"], "blocked")
        self.assertEqual(len(recommendation["output"]["items"]), 1)
        item = recommendation["output"]["items"][0]
        self.assertEqual((item["fund_code"], item["action"], item["reason_code"]),
                         (None, "WAIT", "ELIGIBLE_UNIVERSE_EMPTY"))
        with ASGITestClient(create_app(empty_settings)) as client:
            response = client.post(
                "/api/v1/strategy/decisions",
                json_body={
                    "recommendation_id": recommendation["recommendation_id"],
                    "account_id": account_id,
                    "cutoff": "2026-09-16",
                    "fund_code": None,
                    "proposed_action": "WAIT",
                    "choice": "deferred",
                    "planned_amount": None,
                    "reason": "等待真实数据补齐",
                    "supersedes_decision_id": None,
                },
                headers={"Idempotency-Key": "empty-deferred"},
            )
            self.assertEqual(response.status_code, 200)
            self.assertIsNone(response.json()["fund_code"])


class StrategyHistoricalReportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.settings = Settings(database_path=Path(self.temp.name) / "backtest.sqlite3")
        funds = [
            {"code": f"51000{index}", "name": f"长周期基金{index}", "fund_type": "ETF"}
            for index in range(1, 4)
        ]
        import_catalog(self.settings, funds, policy_version="test-catalog",
                       source_id="fund_catalog.manager_documents")
        rows = nav_rows("2026-09-16", count=1000, mode="cycle")
        for index, fund in enumerate(funds, 1):
            add_relation_and_nav(self.settings, fund["code"], f"99000{index}", rows)

    def test_three_sleeve_curve_is_exact_and_every_execution_action_occurs(self):
        report = historical_strategy_report(self.settings, initial_cash="100000")
        self.assertEqual(report["status"], "review_required")
        self.assertFalse(report["investment_eligibility"]["investment_operations_enabled"])
        self.assertIn("verified_total_return_missing", report["investment_eligibility"]["blockers"])
        for action in ("BUY", "ADD", "REDUCE", "EXIT"):
            self.assertGreater(report["executed_action_summary"][action], 0)
        strategy_curve = report["curves"][0]["points"]
        self.assertGreater(len(strategy_curve), 250)
        with localcontext() as context:
            context.prec = report["protocol"]["decimal_precision"]
            for point in strategy_curve:
                self.assertEqual(
                    Decimal(point["equity"]),
                    Decimal(point["cash"]) + Decimal(point["market_value"]),
                )
                self.assertGreaterEqual(Decimal(point["cash"]), 0)
        self.assertEqual(len(strategy_curve), len(report["curves"][1]["points"]))
        curves = {item["id"]: item["points"] for item in report["curves"]}
        self.assertEqual(set(("strategy", "benchmark", "exposure_matched", "cash"))
                         - set(curves), set())
        self.assertTrue(all(Decimal(point["equity"]) == Decimal("100000")
                            for point in curves["cash"]))
        with localcontext() as context:
            context.prec = report["protocol"]["decimal_precision"]
            self.assertEqual(
                Decimal(report["metrics"]["exposure_timing_excess_pct"]),
                Decimal(report["metrics"]["total_return_pct"])
                - Decimal(report["metrics"]["exposure_matched_return_pct"]),
            )
        self.assertEqual(report["exposure_summary"]["observation_count"], len(strategy_curve))
        risk_quality = report["risk_quality"]
        self.assertEqual(risk_quality["return_observations"], len(strategy_curve) - 1)
        self.assertEqual(risk_quality["observations_per_year"], "244")
        self.assertEqual(
            Decimal(risk_quality["max_drawdown_pct"]),
            Decimal(report["metrics"]["max_drawdown_pct"]),
        )
        self.assertEqual(risk_quality["risk_free_return_assumption_pct"], "0")
        self.assertEqual(risk_quality["minimum_acceptable_return_assumption_pct"], "0")
        self.assertEqual(report["holding_cycles"]["trade_count"], report["metrics"]["trade_count"])
        scenarios = {
            (Decimal(item["buy_fee_bps"]), Decimal(item["sell_fee_bps"])): item
            for item in report["cost_stress"]
        }
        self.assertTrue({(Decimal(0), Decimal(0)), (Decimal(10), Decimal(10)),
                         (Decimal(50), Decimal(50)), (Decimal(100), Decimal(100))}
                        <= set(scenarios))
        requested = scenarios[(Decimal(10), Decimal(10))]
        self.assertTrue(requested["is_requested_scenario"])
        self.assertEqual(Decimal(requested["final_equity"]),
                         Decimal(report["metrics"]["final_equity"]))
        attribution = report["economic_attribution"]
        with localcontext() as context:
            context.prec = report["protocol"]["decimal_precision"]
            self.assertEqual(
                Decimal(attribution["market_exposure_return_pct"])
                + Decimal(attribution["signal_and_execution_before_cost_pct"])
                + Decimal(attribution["modeled_transaction_cost_drag_pct"]),
                Decimal(attribution["strategy_total_return_pct"]),
            )

    def test_combined_curve_uses_canonical_fund_order_for_exact_v5_reconciliation(self):
        # At precision 40 these three finite sleeve values differ by one final
        # digit when summed as C,A,B instead of A,B,C.  The public aggregate
        # must use the canonical fund-code order shared by v5 attribution and
        # its independent validator.
        values = {
            "A": "6790.047417836848885959141742454302745206",
            "B": "5540.302777388590563835179231498510111680",
            "C": "6208.915587606456411535047757498179530368",
        }
        results = [
            {
                "code": code,
                "initial_equity": "10000",
                "equity_curve": [{
                    "date": "2026-01-05",
                    "cash": "0",
                    "market_value": value,
                    "equity": value,
                }],
            }
            for code, value in (("C", values["C"]), ("A", values["A"]), ("B", values["B"]))
        ]
        with localcontext() as context:
            context.prec = 40
            combined = _combine_curves(results)
            expected_market = sum(
                (Decimal(values[code]) for code in ("A", "B", "C")), Decimal(0),
            )

        self.assertEqual(Decimal(combined[0]["market_value"]), expected_market)
        self.assertEqual(combined[0]["equity"], combined[0]["market_value"])

    def test_historical_report_is_independent_of_caller_decimal_precision(self):
        with localcontext() as context:
            context.prec = 28
            low_precision_caller = historical_strategy_report(self.settings, initial_cash="100000")
        with localcontext() as context:
            context.prec = 64
            high_precision_caller = historical_strategy_report(self.settings, initial_cash="100000")
        low_precision_caller.pop("generated_at", None)
        high_precision_caller.pop("generated_at", None)
        self.assertEqual(low_precision_caller, high_precision_caller)

    def test_backtest_api_persists_same_real_input_once(self):
        base_report = historical_strategy_report(self.settings, initial_cash="100000")
        reports = []
        for generated_at in (
            "2026-09-19T00:01:00Z",
            "2026-09-19T23:59:00Z",
            "2026-09-20T00:01:00Z",
        ):
            report = deepcopy(base_report)
            report["generated_at"] = generated_at
            reports.append(report)
        app = create_app(self.settings)
        with patch("backend.api.strategy.historical_strategy_report", side_effect=reports), \
             ASGITestClient(app) as client:
            body = {"initial_cash": "100000", "buy_fee_bps": "10", "sell_fee_bps": "10"}
            first = client.post("/api/v1/strategy/backtests", json_body=body)
            second = client.post("/api/v1/strategy/backtests", json_body={
                "initial_cash": "100000.0", "buy_fee_bps": "10.0", "sell_fee_bps": "10.00",
            })
            third = client.post("/api/v1/strategy/backtests", json_body=body)
            self.assertEqual((first.status_code, second.status_code, third.status_code), (200, 200, 200))
            self.assertEqual(first.json()["run_id"], second.json()["run_id"])
            self.assertNotEqual(first.json()["run_id"], third.json()["run_id"])
            self.assertEqual(first.json()["input"]["validation_as_of"], "2026-09-19")
            self.assertEqual(third.json()["input"]["validation_as_of"], "2026-09-20")
            latest = client.get("/api/v1/strategy/backtests/latest").json()["item"]
            self.assertEqual(latest["run_id"], third.json()["run_id"])
            self.assertNotIn("generated_at", latest["output"])


if __name__ == "__main__":
    unittest.main()
