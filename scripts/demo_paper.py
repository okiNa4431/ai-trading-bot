"""Entirely synthetic, temporary paper-account demo. Never touches production data."""

import tempfile
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from trading_bot.broker.execution import known_marks
from trading_bot.broker.paper import PaperBroker
from trading_bot.config import Instrument
from trading_bot.models import PriceBar
from trading_bot.persistence.sqlite_repository import SQLiteRepository
from trading_bot.portfolio.models import FXRate


def main() -> None:
    print("SIMULATION ONLY: synthetic prices, fixed USD/JPY=150; not investment results.")
    t = datetime(2026, 10, 5, tzinfo=UTC)
    jp = Instrument(symbol="7203.T", market="JP", currency="JPY", display_name="Synthetic JP")
    us = Instrument(symbol="AAPL", market="US", currency="USD", display_name="Synthetic US")

    def bar(instrument: Instrument, price: int, at: datetime) -> PriceBar:
        print(f"SYNTHETIC {instrument.symbol}: open={price} {instrument.currency} at={at}")
        return PriceBar(
            symbol=instrument.symbol,
            market=instrument.market,
            currency=instrument.currency,
            timestamp=at,
            interval="1h",
            open=price,
            high=price,
            low=price,
            close=price,
            volume=10000,
            fetched_at=at + timedelta(hours=1),
        )

    def fx(at: datetime) -> FXRate:
        print(f"FIXED SIMULATION USD/JPY=150 rate_timestamp={at}")
        return FXRate(usd_jpy=Decimal(150), timestamp=at, source="fixed-simulation")

    with tempfile.TemporaryDirectory(prefix="paper-demo-") as directory:
        repository = SQLiteRepository(Path(directory) / "account.sqlite3")
        broker = PaperBroker(repository)
        broker.initialize(t - timedelta(days=1))
        order = broker.place_order(jp, "BUY", 200, t - timedelta(seconds=1), "jp-buy")
        assert order.status == "PENDING"
        assert (
            broker.execute(
                order.order_id, {jp.symbol: [bar(jp, 300, t)]}, None, t + timedelta(hours=1)
            ).status
            == "FILLED"
        )
        order = broker.place_order(jp, "SELL", 100, t + timedelta(minutes=1), "jp-partial-sell")
        assert (
            broker.execute(
                order.order_id,
                {jp.symbol: [bar(jp, 400, t + timedelta(hours=1))]},
                None,
                t + timedelta(hours=2),
            ).status
            == "FILLED"
        )
        ny = t + timedelta(hours=13, minutes=30)
        print(
            broker.exchange(
                "JPY", Decimal("100000"), fx(ny - timedelta(minutes=1)), ny - timedelta(minutes=1)
            ).model_dump_json(indent=2)
        )
        order = broker.place_order(us, "BUY", 2, ny - timedelta(seconds=1), "us-buy")
        data = {jp.symbol: [bar(jp, 400, t + timedelta(hours=5))], us.symbol: [bar(us, 100, ny)]}
        assert (
            broker.execute(order.order_id, data, fx(ny), ny + timedelta(hours=1)).status == "FILLED"
        )
        as_of = ny + timedelta(hours=1)
        valuation = broker.value(known_marks([jp, us], data, as_of, "historical"), fx(as_of), as_of)
        print(valuation.model_dump_json(indent=2))
        restored = PaperBroker(SQLiteRepository(repository.path))
        assert restored.get_balance() == broker.get_balance()
        assert restored.get_balance().realized_pnl["JPY"] == Decimal("9930")
        print("PASS: JP partial sale, explicit FX, US buy, valuation, SQLite restore.")


if __name__ == "__main__":
    main()
