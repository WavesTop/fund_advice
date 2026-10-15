"""Explicit calendar-day redemption floors and modelled fund confirmation lags."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date
from decimal import Decimal

from backend.core.trading_calendar import TradingCalendar


@dataclass(frozen=True)
class FundHoldingCosts:
    short_holding_days: int = 7
    short_redemption_bps: int = 150
    confirmation_lag_sessions: int = 1
    settlement_lag_sessions: int = 2
    minimum_purchase_amount: int = 1

    def validate(self) -> None:
        for value in (self.short_holding_days, self.short_redemption_bps,
                      self.confirmation_lag_sessions, self.settlement_lag_sessions, self.minimum_purchase_amount):
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError("Fund fee and lag settings must be nonnegative integers")
        if self.short_redemption_bps >= 10000:
            raise ValueError("Redemption fee must be below 100%")

    def confirmation(self, execution_date: str, calendar: TradingCalendar | None) -> str:
        self.validate()
        if self.confirmation_lag_sessions == 0:
            return execution_date
        if calendar is None:
            raise ValueError("Fund confirmation dates need a verified calendar")
        return calendar.shift(date.fromisoformat(execution_date), self.confirmation_lag_sessions).isoformat()

    def redemption(self, bought: str, sold: str, normal_bps: Decimal,
                   calendar: TradingCalendar | None) -> dict:
        if not normal_bps.is_finite() or not 0 <= normal_bps < 10000:
            raise ValueError("Invalid normal redemption fee")
        if sold < bought:
            raise ValueError("Redemption precedes purchase")
        purchase_confirmation = self.confirmation(bought, calendar)
        redemption_confirmation = self.confirmation(sold, calendar)
        days = (date.fromisoformat(redemption_confirmation) - date.fromisoformat(purchase_confirmation)).days
        short = days < self.short_holding_days
        rate = max(normal_bps, Decimal(self.short_redemption_bps)) if short else normal_bps
        if self.settlement_lag_sessions:
            if calendar is None:
                raise ValueError("Fund settlement dates need a verified calendar")
            settled = calendar.shift(date.fromisoformat(sold), self.settlement_lag_sessions).isoformat()
        else:
            settled = sold
        return {"holding_calendar_days": days, "purchase_date": bought,
                "purchase_confirmation_date": purchase_confirmation,
                "redemption_confirmation_date": redemption_confirmation,
                "sell_fee_bps": str(rate), "short_redemption_fee_applied": short,
                "settlement_date": settled}

    def document(self) -> dict:
        return {**asdict(self), "short_fee_mode": "floor_replaces_normal_sell_rate_not_added",
                "scope": "Requested fund fee scenario; not a verified product-specific full fee schedule"}
