from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas_market_calendars as mcal
import yaml

from trading_bot.broker.paper import PaperBroker
from trading_bot.config import Instrument
from trading_bot.models import PriceBar
from trading_bot.orchestration.engine import DecisionEngine
from trading_bot.persistence.decision_repository import DecisionRepository
from trading_bot.persistence.sqlite_repository import SQLiteRepository
from trading_bot.screening.models import ScreeningSettings
from trading_bot.storage import ParquetStore
from trading_bot.strategy.models import ActionDecision, AISettings, DecisionResult

NOW = datetime(2026, 10, 15, 2, 5, tzinfo=UTC)
JP = Instrument(symbol="7203.T", market="JP", currency="JPY", display_name="Synthetic JP")
US = Instrument(symbol="AAPL", market="US", currency="USD", display_name="Synthetic US")


def histories(instrument=JP, prices=None):
    schedule = (
        mcal.get_calendar("JPX" if instrument.market == "JP" else "NYSE")
        .schedule(
            "2026-08-01",
            "2026-10-14",
        )
        .iloc[-21:]
    )
    zone = ZoneInfo("Asia/Tokyo" if instrument.market == "JP" else "America/New_York")
    days = []
    for index, (day, session) in enumerate(schedule.iterrows()):
        close = prices[index] if prices is not None else 100 + index
        stamp = datetime.combine(day.date(), datetime.min.time(), tzinfo=zone).astimezone(UTC)
        days.append(
            PriceBar(
                symbol=instrument.symbol,
                market=instrument.market,
                currency=instrument.currency,
                timestamp=stamp,
                interval="1d",
                open=close,
                high=close + 2,
                low=close - 2,
                close=close,
                volume=1000 if index < 20 else 2000,
                fetched_at=session.market_close.to_pydatetime() + timedelta(seconds=1),
            )
        )
    start = (
        NOW.replace(hour=0, minute=0)
        if instrument.market == "JP"
        else datetime(2026, 10, 14, 13, 30, tzinfo=UTC)
    )
    hours = [
        PriceBar(
            symbol=instrument.symbol,
            market=instrument.market,
            currency=instrument.currency,
            timestamp=start + timedelta(hours=i),
            interval="1h",
            open=120 + i,
            high=122 + i,
            low=118 + i,
            close=120 + i,
            volume=1000,
            fetched_at=start + timedelta(hours=i + 1, seconds=1),
        )
        for i in range(2)
    ]
    return {"1d": days, "1h": hours}


def ai_settings(**overrides):
    config = yaml.safe_load(Path("config/ai.yaml").read_text())
    return AISettings.model_validate({**config, **overrides})


def environment(tmp_path, instruments=None, rules=None):
    instruments = instruments or [JP]
    store = ParquetStore(tmp_path / "market")
    for instrument in instruments:
        for interval, bars in histories(instrument).items():
            store.save(instrument, interval, bars)
    repository = SQLiteRepository(tmp_path / "account.sqlite3")
    broker = PaperBroker(repository, rules)
    broker.initialize(NOW - timedelta(days=30))
    decisions = DecisionRepository(repository)
    engine = DecisionEngine(
        store, broker, decisions, instruments, ScreeningSettings(), ai_settings(), clock=lambda: NOW
    )
    return engine


class BuyStrategy:
    def __init__(self, weight=0.05):
        self.weight = weight
        self.calls = 0

    def decide(self, context):
        self.calls += 1
        return DecisionResult(
            market=context.market,
            market_summary="Synthetic proposal",
            decisions=[
                ActionDecision(
                    symbol=c.instrument.symbol,
                    action="BUY",
                    target_weight=self.weight,
                    reason="Synthetic trend",
                )
                for c in context.screening.candidates
            ],
        )


def context_with_holding(context, instrument=JP, quantity=400, price=Decimal(121)):
    from trading_bot.portfolio.models import Position
    from trading_bot.portfolio.valuation import value_portfolio

    cost = price * quantity
    account = context.account.model_copy(
        update={
            "balances": {"JPY": Decimal("1000000") - cost, "USD": Decimal(0)},
            "positions": {
                instrument.symbol: Position(
                    instrument=instrument, quantity=quantity, average_cost=price, cost_basis=cost
                )
            },
        }
    )
    marks = {
        c.instrument.symbol: c.features.mark for c in context.screening.candidates if c.features
    }
    portfolio = value_portfolio(account, marks, context.fx, context.as_of, context.rules)
    return context.model_copy(update={"account": account, "portfolio": portfolio})
