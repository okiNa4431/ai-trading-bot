"""Broker boundary shared by future paper/live implementations."""

from datetime import datetime
from typing import Protocol

from trading_bot.config import Instrument
from trading_bot.portfolio.models import Account, Execution, Order, Position, Side


class Broker(Protocol):
    def get_balance(self) -> Account: ...
    def get_positions(self) -> list[Position]: ...
    def place_order(
        self,
        instrument: Instrument,
        side: Side,
        quantity: int,
        requested_at: datetime,
        order_id: str | None = None,
    ) -> Order: ...
    def get_order(self, order_id: str) -> Order: ...
    def cancel_order(self, order_id: str) -> Order: ...
    def get_executions(self) -> list[Execution]: ...
