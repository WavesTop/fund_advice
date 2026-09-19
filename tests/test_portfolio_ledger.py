from decimal import Decimal
from pathlib import Path
import tempfile
import unittest

from asgi_client import ASGITestClient
from backend.api.main import create_app
from backend.core.config import Settings
from backend.storage.catalog import import_catalog
from backend.storage.database import connection_scope


class PortfolioLedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.settings = Settings(database_path=Path(self.temp.name) / "portfolio.sqlite3")
        import_catalog(
            self.settings,
            [
                {"code": "510300", "name": "沪深300ETF", "fund_type": "ETF"},
                {"code": "510500", "name": "中证500ETF", "fund_type": "ETF"},
            ],
            policy_version="fixture",
        )
        self.client = ASGITestClient(create_app(self.settings))
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        response = self.post("/api/v1/portfolio/accounts", {"name": "实际账户"}, "account-1")
        self.assertEqual(response.status_code, 200)
        self.account_id = response.json()["account_id"]

    def post(self, path, body, key):
        return self.client.post(path, json_body=body, headers={"Idempotency-Key": key})

    def event(self, event_type, key, **changes):
        body = {
            "account_id": self.account_id,
            "event_type": event_type,
            "fund_code": None,
            "amount": None,
            "units": None,
            "business_date": "2026-01-02",
            "confirmation_date": "2026-01-02",
            "settlement_date": None,
            "channel": "证券账户",
            "confirmed_fee": None,
            "state": "confirmed",
            "source": "user_confirmation",
            "note": None,
        }
        body.update(changes)
        return self.post("/api/v1/ledger/events", body, key)

    def snapshot(self, cutoff="2026-12-31"):
        return self.client.get(
            "/api/v1/portfolio/snapshots", params={"account_id": self.account_id, "cutoff": cutoff}
        )

    def test_account_and_condition_revisions_are_idempotent_and_append_only(self):
        repeated = self.post("/api/v1/portfolio/accounts", {"name": "实际账户"}, "account-1")
        self.assertEqual(repeated.json()["account_id"], self.account_id)
        self.assertTrue(repeated.json()["idempotent_replay"])
        self.assertEqual(
            self.post("/api/v1/portfolio/accounts", {"name": "另一个账户"}, "account-1").status_code, 409
        )
        first = {
            "account_id": self.account_id,
            "effective_date": "2026-01-01",
            "risk_level": "balanced",
            "max_single_position_pct": "20.00",
            "investment_horizon_days": 365,
            "channel": "证券账户",
            "planned_additional_cash": "1000.00",
            "no_holdings_confirmed": True,
        }
        one = self.post("/api/v1/portfolio/conditions", first, "condition-1")
        replay = self.post("/api/v1/portfolio/conditions", first, "condition-1")
        self.assertEqual((one.status_code, replay.status_code), (200, 200))
        self.assertEqual(one.json()["revision_id"], replay.json()["revision_id"])
        self.assertEqual(one.json()["max_single_position_pct"], "20")
        second = self.post(
            "/api/v1/portfolio/conditions",
            {**first, "effective_date": "2026-06-01", "risk_level": "conservative"},
            "condition-2",
        )
        self.assertEqual(second.json()["version"], 2)
        future = self.post(
            "/api/v1/portfolio/conditions",
            {**first, "effective_date": "2027-01-01", "risk_level": "aggressive"},
            "condition-future",
        )
        self.assertEqual(future.status_code, 200)
        history = self.client.get(
            "/api/v1/portfolio/conditions",
            params={"account_id": self.account_id, "cutoff": "2026-12-31"},
        ).json()
        self.assertEqual([item["version"] for item in history["items"]], [3, 2, 1])
        self.assertEqual(history["current"]["risk_level"], "conservative")
        self.assertEqual(
            self.event(
                "initial_position",
                "position-after-condition",
                fund_code="510300",
                units="1",
                business_date="2026-01-01",
                confirmation_date="2026-01-01",
            ).status_code,
            200,
        )
        replay_after_position = self.post("/api/v1/portfolio/conditions", first, "condition-1")
        self.assertEqual(replay_after_position.status_code, 200)
        self.assertEqual(replay_after_position.json()["revision_id"], one.json()["revision_id"])
        self.assertTrue(replay_after_position.json()["idempotent_replay"])
        with connection_scope(self.settings) as connection:
            with self.assertRaises(Exception):
                connection.execute(
                    "UPDATE portfolio_condition_revision SET risk_level='aggressive' WHERE revision_id=?",
                    (one.json()["revision_id"],),
                )

    def test_decimal_ledger_snapshot_and_pending_data_are_not_treated_as_confirmed(self):
        self.assertEqual(self.event("cash_in", "cash", amount="1000.10",
                                    settlement_date="2026-01-02").status_code, 200)
        self.assertEqual(
            self.event(
                "initial_position", "initial", fund_code="510500", units="3.2500", amount=None,
                confirmed_fee=None,
            ).status_code,
            200,
        )
        buy = self.event(
            "buy", "buy", fund_code="510300", amount="300.03", units="10.1250", confirmed_fee="0.10"
        )
        self.assertEqual(buy.status_code, 200)
        pending = self.event(
            "sell", "pending-sell", fund_code="510300", amount=None, units="1", confirmed_fee=None,
            state="pending", confirmation_date=None,
        )
        self.assertEqual(pending.status_code, 200)
        dividend = self.event(
            "dividend", "dividend", fund_code="510300", amount="1.005", confirmed_fee=None,
            business_date="2026-02-01", confirmation_date="2026-02-02",
        )
        self.assertEqual(dividend.status_code, 200)

        result = self.snapshot().json()
        self.assertEqual(Decimal(result["cash"]["confirmed"]), Decimal("700.975"))
        self.assertEqual(
            {item["fund_code"]: Decimal(item["confirmed_units"]) for item in result["positions"]},
            {"510300": Decimal("10.125"), "510500": Decimal("3.25")},
        )
        self.assertEqual(result["coverage"]["cost_history"], "incomplete")
        self.assertEqual([item["event_id"] for item in result["pending_events"]], [pending.json()["event_id"]])
        self.assertEqual(self.snapshot("2026-01-15").json()["cash"]["confirmed"], "699.97")
        before_dividend_confirmation = self.snapshot("2026-02-01").json()
        self.assertEqual(before_dividend_confirmation["cash"]["confirmed"], "699.97")
        self.assertIn(dividend.json()["event_id"],
                      [item["event_id"] for item in before_dividend_confirmation["pending_events"]])

    def test_event_idempotency_unknown_fund_dates_and_negative_balances_are_rejected(self):
        cash = self.event("cash_in", "cash", amount="100")
        replay = self.event("cash_in", "cash", amount="100.0")
        self.assertEqual(cash.json()["event_id"], replay.json()["event_id"])
        self.assertTrue(replay.json()["idempotent_replay"])
        self.assertEqual(self.event("cash_in", "cash", amount="101").status_code, 409)
        self.assertEqual(
            self.event("buy", "bad-fund", fund_code="999999", amount="10", units="1", confirmed_fee="0").status_code,
            422,
        )
        self.assertEqual(
            self.event("cash_out", "too-much", amount="100.01").status_code,
            409,
        )
        self.assertEqual(self.event("cash_in", "future-cash", amount="100", business_date="2026-01-03",
                                    confirmation_date="2026-01-10").status_code, 200)
        self.assertEqual(self.event("buy", "before-cash", fund_code="510300", amount="150", units="1",
                                    confirmed_fee="0", business_date="2026-01-04",
                                    confirmation_date="2026-01-05").status_code, 409)
        invalid_date = self.event("cash_in", "bad-date", amount="1", business_date="2026-02-30",
                                  confirmation_date="2026-02-30")
        self.assertEqual(invalid_date.status_code, 422)
        before_confirmation = self.event("cash_in", "bad-confirm", amount="1", business_date="2026-02-02",
                                         confirmation_date="2026-02-01")
        self.assertEqual(before_confirmation.status_code, 422)

    def test_reverse_and_replace_are_append_only_and_distinct_from_a_sell(self):
        self.event("cash_in", "cash", amount="1000", settlement_date="2026-01-02")
        buy = self.event("buy", "buy", fund_code="510300", amount="100", units="10", confirmed_fee="0")
        target = buy.json()["event_id"]
        replacement = {
            "action": "replace",
            "reason": "确认单份额更正",
            "replacement": {
                "event_type": "buy",
                "fund_code": "510300",
                "amount": "100",
                "units": "8",
                "business_date": "2026-01-02",
                "confirmation_date": "2026-01-02",
                "settlement_date": None,
                "channel": "证券账户",
                "confirmed_fee": "0",
                "state": "confirmed",
                "source": "user_confirmation",
                "note": "更正后的确认单",
            },
        }
        corrected = self.post(f"/api/v1/ledger/events/{target}/corrections", replacement, "correct-buy")
        self.assertEqual(corrected.status_code, 200)
        replacement_id = corrected.json()["replacement_event_id"]
        self.assertNotEqual(replacement_id, target)
        self.assertEqual(self.snapshot().json()["positions"][0]["confirmed_units"], "8")
        events = self.client.get("/api/v1/ledger/events", params={"account_id": self.account_id}).json()["items"]
        original = next(item for item in events if item["event_id"] == target)
        self.assertFalse(original["is_current"])
        self.assertEqual(original["correction"]["action"], "replace")

        reversed_response = self.post(
            f"/api/v1/ledger/events/{replacement_id}/corrections",
            {"action": "reverse", "reason": "误录，撤销", "replacement": None},
            "reverse-replacement",
        )
        self.assertEqual(reversed_response.status_code, 200)
        self.assertEqual(self.snapshot().json()["positions"], [])
        self.assertEqual(
            self.post(
                f"/api/v1/ledger/events/{replacement_id}/corrections",
                {"action": "reverse", "reason": "再次撤销", "replacement": None},
                "another-correction",
            ).status_code,
            409,
        )
        # A reverse correction removes a mistaken fact; it does not create sale proceeds.
        self.assertEqual(self.snapshot().json()["cash"]["confirmed"], "1000")

    def test_unsettled_sale_proceeds_are_not_available_for_new_buys(self):
        self.event("cash_in", "settled-cash", amount="1000", settlement_date="2026-01-02")
        self.event("initial_position", "opening-position", fund_code="510300", units="10")
        sale = self.event(
            "sell", "sale-awaiting-settlement", fund_code="510300", amount="500", units="5",
            confirmed_fee="0", business_date="2026-01-03", confirmation_date="2026-01-03",
            settlement_date="2026-01-05",
        )
        self.assertEqual(sale.status_code, 200)
        before = self.snapshot("2026-01-03").json()
        self.assertEqual(before["cash"]["confirmed"], "1500")
        self.assertEqual(before["cash"]["available"], "1000")
        self.assertEqual(before["cash"]["pending_settlement"], "500")
        self.assertIn(sale.json()["event_id"], [item["event_id"] for item in before["pending_events"]])
        premature_buy = self.event(
            "buy", "premature-reuse", fund_code="510500", amount="1200", units="10",
            confirmed_fee="0", business_date="2026-01-04", confirmation_date="2026-01-04",
            settlement_date="2026-01-04",
        )
        self.assertEqual(premature_buy.status_code, 409)
        after = self.snapshot("2026-01-05").json()
        self.assertEqual(after["cash"]["available"], "1500")

    def test_cannot_confirm_no_holdings_when_position_exists(self):
        self.event("initial_position", "known-position", fund_code="510300", units="1")
        response = self.post(
            "/api/v1/portfolio/conditions",
            {
                "account_id": self.account_id,
                "effective_date": "2026-01-02",
                "risk_level": "balanced",
                "max_single_position_pct": "20",
                "investment_horizon_days": 365,
                "channel": "本人确认",
                "planned_additional_cash": None,
                "no_holdings_confirmed": True,
            },
            "invalid-empty-confirmation",
        )
        self.assertEqual(response.status_code, 409)

    def test_health_and_api_reject_missing_idempotency_header_or_extra_fields(self):
        health = self.client.get("/health").json()
        self.assertGreaterEqual(health["schema_version"], 14)
        self.assertIn("portfolio_ledger_v1", health["capabilities"])
        self.assertEqual(
            self.client.post("/api/v1/portfolio/accounts", json_body={"name": "无幂等键"}).status_code, 422
        )
        response = self.post(
            "/api/v1/portfolio/accounts", {"name": "多字段", "unexpected": True}, "bad-extra"
        )
        self.assertEqual(response.status_code, 422)

    def test_pre_lifecycle_rows_remain_visible_as_unknown_instead_of_becoming_zero(self):
        with connection_scope(self.settings) as connection:
            connection.execute(
                """INSERT INTO ledger_event
                   (event_id,account_id,event_type,fund_code,amount,units,business_date,state,source,note,
                    recorded_at,idempotency_key,request_hash)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                ("evt_legacy", self.account_id, "initial_position", "510300", None, "5", "2025-12-31",
                 "confirmed", "legacy_formal_draft", None, "2026-01-01T00:00:00.000000Z", "legacy-row", "fixture"),
            )
        result = self.snapshot().json()
        self.assertEqual(result["positions"], [])
        self.assertEqual(result["coverage"]["cash_and_units"], "incomplete")
        self.assertEqual([item["event_id"] for item in result["unknown_events"]], ["evt_legacy"])


if __name__ == "__main__":
    unittest.main()
