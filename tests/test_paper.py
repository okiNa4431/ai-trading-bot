"""Deterministic synthetic trading scenarios. No live prices or currencies."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from trading_bot.broker.execution import known_marks, window
from trading_bot.broker.paper import PaperBroker
from trading_bot.cli import main
from trading_bot.config import Instrument
from trading_bot.models import PriceBar
from trading_bot.persistence.sqlite_repository import SQLiteRepository, SQLiteSession
from trading_bot.portfolio.models import FXRate, Mark, TradingRules

D = Decimal
T = datetime(2026, 10, 5, 0, tzinfo=UTC)  # Monday JP open 09:00 JST.
JP = Instrument(symbol="7203.T", market="JP", currency="JPY", display_name="Synthetic Toyota")
US = Instrument(symbol="AAPL", market="US", currency="USD", display_name="Synthetic Apple")
NT = datetime(2026, 10, 5, 13, 30, tzinfo=UTC)


def bar(price=500, at=T, instrument=JP, volume=10000, fetched=None, interval="1h"):
    return PriceBar(
        symbol=instrument.symbol,
        market=instrument.market,
        currency=instrument.currency,
        timestamp=at,
        interval=interval,
        open=price,
        high=price,
        low=price,
        close=price,
        volume=volume,
        fetched_at=fetched or at + timedelta(hours=1),
    )


def rate(at=T, price="150"):
    return FXRate(usd_jpy=D(price), timestamp=at, source="fixed-simulation")


@pytest.fixture
def broker(tmp_path):
    instance = PaperBroker(SQLiteRepository(tmp_path / "paper.sqlite3"))
    instance.initialize(T - timedelta(days=1))
    return instance


def fill(broker, price=500, quantity=100, side="BUY", at=T, instrument=JP, fx=None):
    order = broker.place_order(instrument, side, quantity, at - timedelta(seconds=1))
    result = broker.execute(
        order.order_id,
        {instrument.symbol: [bar(price, at, instrument)]},
        fx,
        at + timedelta(hours=1),
    )
    return result


def test_initial_cash_and_repeated_init(broker):
    account = broker.get_balance()
    assert account.balances == {"JPY": D("1000000"), "USD": D(0)}
    assert account.base_currency == "JPY"
    assert broker.initialize(T).balances == account.balances
    assert len(broker.history()["cash_ledger"]) == 1


def test_buy_is_pending_until_explicit_execution(broker):
    order = broker.place_order(JP, "BUY", 100, T - timedelta(seconds=1), "order-1")
    assert order.status == "PENDING"
    assert not broker.get_executions()
    assert broker.get_balance().balances["JPY"] == D("1000000")


def test_buy_slippage_and_acquisition_cost(broker):
    assert fill(broker).status == "FILLED"
    execution = broker.get_executions()[0]
    assert execution.price == D("500.500")
    assert execution.slippage_cost == D("50")
    assert execution.notional == D("50050")
    assert broker.get_positions()[0].average_cost == D("500.5")
    assert broker.get_balance().balances["JPY"] == D("949950")
    assert execution.price_at == T
    assert execution.observed_at == T + timedelta(hours=1)


@pytest.mark.parametrize("quantity", [1, 99, 101])
def test_lot_violation(broker, quantity):
    assert broker.place_order(JP, "BUY", quantity, T).status == "REJECTED"


def test_configured_lot_size(tmp_path):
    broker = PaperBroker(
        SQLiteRepository(tmp_path / "paper.sqlite3"), TradingRules(lot_sizes={JP.symbol: 1})
    )
    broker.initialize(T - timedelta(days=1))
    assert fill(broker, quantity=1).status == "FILLED"


def test_integer_us_shares_only(broker):
    with pytest.raises(ValidationError):
        broker.place_order(US, "BUY", 1.5, T)
    with pytest.raises(ValidationError):
        broker.place_order(US, "BUY", 0, T)


def test_no_short_and_no_implicit_fx(broker):
    assert broker.place_order(JP, "SELL", 100, T).status == "REJECTED"
    assert (
        fill(broker, price=100, quantity=1, at=NT, instrument=US, fx=rate(NT)).status == "REJECTED"
    )
    assert broker.get_balance().balances["USD"] == 0


def test_cash_shortfall_including_fees(tmp_path):
    broker = PaperBroker(
        SQLiteRepository(tmp_path / "p.sqlite3"),
        TradingRules(
            commission_jpy=D("999999"),
        ),
    )
    broker.initialize(T - timedelta(days=1))
    assert fill(broker).status == "REJECTED"
    assert broker.get_balance().balances["JPY"] == D("1000000")


def test_ten_percent_includes_cost_effect(broker):
    # Raw exposure 100,000 JPY, but fees/slippage reduce total below 1,000,000.
    order = fill(broker, price=1000)
    assert order.status == "REJECTED"
    assert "symbol ratio" in order.reason
    assert not broker.get_positions()


def test_eighty_percent_limit(tmp_path):
    broker = PaperBroker(SQLiteRepository(tmp_path / "p.sqlite3"), TradingRules(slippage=D(0)))
    broker.initialize(T - timedelta(days=1))
    data = {}
    orders = []
    # All decisions precede Tuesday fills. Monday afternoon marks are <24h old.
    at = T + timedelta(days=1)
    for index in range(9):
        instrument = Instrument(
            symbol=f"{1000 + index}.T", market="JP", currency="JPY", display_name="Synthetic"
        )
        data[instrument.symbol] = [
            bar(900, T + timedelta(hours=5), instrument),
            bar(900, at, instrument),
        ]
        orders.append(broker.place_order(instrument, "BUY", 100, at - timedelta(seconds=1)))
    for index, order in enumerate(orders):
        result = broker.execute(order.order_id, data, None, at + timedelta(hours=1))
        if index < 8:
            assert result.status == "FILLED", result.reason
        else:
            assert result.status == "REJECTED"
            assert "stock ratio" in result.reason


def test_fx_cost_independent_balances_and_replay(broker):
    event = broker.exchange("JPY", D("100000"), rate(T), T, "fx-1")
    assert event.received_amount == D("665.00")
    assert event.cost_in_source == D("250")
    account = broker.get_balance()
    assert account.balances == {"JPY": D("900000"), "USD": D("665")}
    assert broker.exchange("JPY", D("100000"), rate(T), T, "fx-1") == event
    assert broker.get_balance().balances == account.balances
    reverse = broker.exchange("USD", D("100"), rate(T), T)
    assert reverse.received_amount == D("14962")
    assert broker.get_balance().balances["USD"] == D("565")


def test_us_buy_uses_explicit_fx_and_usd_costs(broker):
    broker.exchange("JPY", D("100000"), rate(NT - timedelta(minutes=2)), NT - timedelta(minutes=2))
    assert fill(broker, price=100, quantity=2, at=NT, instrument=US, fx=rate(NT)).status == "FILLED"
    account = broker.get_balance()
    assert account.balances["JPY"] == D("900000")
    assert account.balances["USD"] == D("464.80")
    assert account.positions[US.symbol].average_cost == D("100.10")


@pytest.mark.parametrize("at", [T - timedelta(hours=2), T + timedelta(seconds=1)])
def test_stale_and_future_fx_rejected(broker, at):
    with pytest.raises(ValueError):
        broker.exchange("JPY", D("10000"), rate(at), T)
    assert broker.get_balance().balances["JPY"] == D("1000000")


def test_cancel_and_order_idempotency(broker):
    requested = T - timedelta(seconds=1)
    order = broker.place_order(JP, "BUY", 100, requested, "same")
    assert broker.place_order(JP, "BUY", 100, requested, "same") == order
    with pytest.raises(ValueError):
        broker.place_order(JP, "BUY", 200, requested, "same")
    assert broker.cancel_order("same").status == "CANCELLED"
    assert (
        broker.execute("same", {JP.symbol: [bar()]}, None, T + timedelta(hours=1)).status
        == "CANCELLED"
    )
    assert not broker.get_executions()


def test_order_double_execution_no_duplicate_cash(broker):
    order = fill(broker)
    cash = broker.get_balance().balances
    assert broker.execute(order.order_id, {}, None, T + timedelta(hours=2)).status == "FILLED"
    assert broker.get_balance().balances == cash
    assert len(broker.get_executions()) == 1


def test_average_cost_partial_sale_realized_and_unrealized(broker):
    assert fill(broker, price=300).status == "FILLED"
    assert fill(broker, price=400, at=T + timedelta(hours=1)).status == "FILLED"
    assert broker.get_positions()[0].average_cost == D("350.35")
    assert fill(broker, price=500, side="SELL", at=T + timedelta(hours=2)).status == "FILLED"
    account = broker.get_balance()
    assert account.positions[JP.symbol].quantity == 100
    assert account.positions[JP.symbol].average_cost == D("350.35")
    assert account.realized_pnl == {"JPY": D("14915"), "USD": D(0)}
    stamp = T + timedelta(hours=3)
    valuation = broker.value(
        {JP.symbol: Mark(symbol=JP.symbol, currency="JPY", price=D(500), available_at=stamp)},
        None,
        stamp,
    )
    assert valuation.positions[0].unrealized_pnl == D("14965")
    assert valuation.total_jpy == D("1029880")


def test_commission_in_average_cost_and_sale(tmp_path):
    broker = PaperBroker(
        SQLiteRepository(tmp_path / "p.sqlite3"), TradingRules(commission_jpy=D(100))
    )
    broker.initialize(T - timedelta(days=1))
    fill(broker, price=300)
    assert broker.get_positions()[0].average_cost == D("301.3")
    fill(broker, price=400, side="SELL", at=T + timedelta(hours=1))
    assert broker.get_balance().realized_pnl["JPY"] == D("9730")
    assert not broker.get_positions()


def test_missing_and_stale_valuation_data(broker):
    fill(broker)
    for marks in [
        {},
        {
            JP.symbol: Mark(
                symbol=JP.symbol, currency="JPY", price=D(500), available_at=T - timedelta(days=2)
            )
        },
    ]:
        with pytest.raises(ValueError):
            broker.value(marks, None, T + timedelta(hours=1))
    broker.exchange("JPY", D(1000), rate(T), T)
    with pytest.raises(ValueError, match="rate is required"):
        broker.value(
            {JP.symbol: Mark(symbol=JP.symbol, currency="JPY", price=D(500), available_at=T)},
            None,
            T,
        )


def test_price_drift_records_breach_without_sell(broker, caplog):
    fill(broker)
    valuation = broker.value(
        {JP.symbol: Mark(symbol=JP.symbol, currency="JPY", price=D(2000), available_at=T)}, None, T
    )
    assert valuation.breaches
    assert "valuation breach" in caplog.text
    assert broker.get_positions()[0].quantity == 100
    assert len(broker.get_executions()) == 1


def test_transaction_failure_rolls_back_all_tables(broker):
    order = broker.place_order(JP, "BUY", 100, T - timedelta(seconds=1))
    before = broker.history()
    with patch.object(SQLiteSession, "save_account", side_effect=RuntimeError("injected failure")):
        with pytest.raises(RuntimeError):
            broker.execute(order.order_id, {JP.symbol: [bar()]}, None, T + timedelta(hours=1))
    assert broker.get_order(order.order_id).status == "PENDING"
    assert broker.get_balance().balances["JPY"] == D("1000000")
    assert broker.history() == before


def test_restart_restores_account_orders_and_ledger(broker):
    fill(broker)
    restored = PaperBroker(SQLiteRepository(broker.repository.path))
    assert restored.get_balance() == broker.get_balance()
    assert restored.get_orders() == broker.get_orders()
    assert restored.get_executions() == broker.get_executions()
    assert restored.history() == broker.history()


@pytest.mark.parametrize(
    "data,asof",
    [
        ([], T + timedelta(hours=1)),
        ([bar(at=T - timedelta(hours=1))], T + timedelta(hours=1)),
        ([bar(volume=0)], T + timedelta(hours=1)),
        ([bar()], T + timedelta(minutes=30)),
    ],
)
def test_no_old_missing_zero_volume_or_unconfirmed_fill(broker, data, asof):
    order = broker.place_order(JP, "BUY", 100, T - timedelta(seconds=1))
    assert broker.execute(order.order_id, {JP.symbol: data}, None, asof).status == "PENDING"
    assert not broker.get_executions()


@pytest.mark.parametrize(
    "instrument,at",
    [
        (JP, datetime(2026, 1, 1, 0, tzinfo=UTC)),
        (US, datetime(2026, 7, 3, 13, 30, tzinfo=UTC)),
        (JP, datetime(2026, 10, 4, 0, tzinfo=UTC)),
        (JP, T + timedelta(hours=3)),  # JP lunch 12:00.
        (US, datetime(2026, 11, 27, 19, tzinfo=UTC)),  # After early close.
    ],
)
def test_closed_market_bar_is_not_eligible(instrument, at):
    assert window(bar(at=at, instrument=instrument)) is None


def test_jp_lunch_and_us_dst():
    assert window(bar(at=T + timedelta(hours=3, minutes=30))) is not None
    winter = datetime(2026, 3, 6, 14, 30, tzinfo=UTC)
    summer = datetime(2026, 3, 9, 13, 30, tzinfo=UTC)
    assert window(bar(at=winter, instrument=US))[0] == winter
    assert window(bar(at=summer, instrument=US))[0] == summer


def test_no_future_close_for_decision():
    assert not known_marks([JP], {JP.symbol: [bar(price=90000)]}, T, "historical")


def test_paper_mode_records_fetch_time_and_rejects_stale_fetch(broker):
    order = broker.place_order(JP, "BUY", 100, T - timedelta(seconds=1))
    data = {JP.symbol: [bar(fetched=T - timedelta(minutes=1))]}
    assert (
        broker.execute(order.order_id, data, None, T + timedelta(hours=1), "paper").status
        == "PENDING"
    )
    data = {JP.symbol: [bar(fetched=T + timedelta(hours=1))]}
    assert (
        broker.execute(order.order_id, data, None, T + timedelta(hours=1), "paper").status
        == "FILLED"
    )
    assert broker.get_executions()[0].price_fetched_at == T + timedelta(hours=1)


def test_future_fx_keeps_order_pending(broker):
    broker.exchange("JPY", D("100000"), rate(NT), NT)
    order = broker.place_order(US, "BUY", 1, NT)
    at = NT + timedelta(hours=1)
    data = {US.symbol: [bar(100, at, US)]}
    assert (
        broker.execute(
            order.order_id, data, rate(at + timedelta(seconds=1)), at + timedelta(hours=1)
        ).status
        == "PENDING"
    )
    assert not broker.get_executions()


def test_execute_rejects_invalid_or_duplicate_bar(broker):
    order = broker.place_order(JP, "BUY", 100, T - timedelta(seconds=1))
    for data in [[bar().model_copy(update={"open": -1})], [bar(), bar()]]:
        with pytest.raises(ValueError):
            broker.execute(order.order_id, {JP.symbol: data}, None, T + timedelta(hours=1))


def test_paper_cli_pending_and_restore(tmp_path, capsys):
    prefix = ["paper", "--db", str(tmp_path / "p.sqlite3")]
    assert main(prefix + ["init"]) == 0
    assert (
        main(prefix + ["buy", "--symbol", "7203.T", "--quantity", "100", "--order-id", "cli-demo"])
        == 0
    )
    for command in ["balance", "positions", "orders", "history"]:
        assert main(prefix + [command]) == 0
    assert '"status": "PENDING"' in capsys.readouterr().out
    assert main(prefix + ["cancel", "--order-id", "cli-demo"]) == 0


def test_partial_snapshot_never_becomes_confirmed_by_waiting(broker):
    order = broker.place_order(JP, "BUY", 100, T - timedelta(seconds=1))
    partial = bar(fetched=T + timedelta(minutes=20))
    assert (
        broker.execute(order.order_id, {JP.symbol: [partial]}, None, T + timedelta(hours=2)).status
        == "PENDING"
    )
    assert not known_marks([JP], {JP.symbol: [partial]}, T + timedelta(hours=2), "historical")


def test_concurrent_execution_is_exactly_once(broker):
    from concurrent.futures import ThreadPoolExecutor

    order = broker.place_order(JP, "BUY", 100, T - timedelta(seconds=1))

    def execute(_):
        return broker.execute(
            order.order_id, {JP.symbol: [bar()]}, None, T + timedelta(hours=1)
        ).status

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert list(pool.map(execute, range(2))) == ["FILLED", "FILLED"]
    assert len(broker.get_executions()) == 1
    assert broker.get_balance().balances["JPY"] == D("949950")


def test_fx_rollback_and_negative_amount(broker):
    for amount in [D("-1"), D("NaN"), D("Infinity"), D("1.5")]:
        with pytest.raises(ValueError):
            broker.exchange("JPY", amount, rate(T), T)
    before = broker.history()
    with patch.object(SQLiteSession, "save_account", side_effect=RuntimeError("injected failure")):
        with pytest.raises(RuntimeError):
            broker.exchange("JPY", D("100000"), rate(T), T)
    assert broker.history() == before
    assert broker.get_balance().balances["JPY"] == D("1000000")


def test_us_partial_sale_native_pnl_and_remaining_basis(tmp_path):
    instance = PaperBroker(
        SQLiteRepository(tmp_path / "us.sqlite3"), TradingRules(commission_usd=D("0.01"))
    )
    instance.initialize(T - timedelta(days=1))
    instance.exchange(
        "JPY", D("100000"), rate(NT - timedelta(minutes=1)), NT - timedelta(minutes=1)
    )
    assert (
        fill(instance, price=100, quantity=3, at=NT, instrument=US, fx=rate(NT)).status == "FILLED"
    )
    assert (
        fill(
            instance,
            price=110,
            quantity=1,
            side="SELL",
            at=NT + timedelta(hours=1),
            instrument=US,
            fx=rate(NT + timedelta(hours=1)),
        ).status
        == "FILLED"
    )
    assert instance.get_positions()[0].quantity == 2
    assert (
        fill(
            instance,
            price=120,
            quantity=2,
            side="SELL",
            at=NT + timedelta(hours=2),
            instrument=US,
            fx=rate(NT + timedelta(hours=2)),
        ).status
        == "FILLED"
    )
    account = instance.get_balance()
    assert not instance.get_positions()
    assert account.realized_pnl["JPY"] == 0
    assert account.realized_pnl["USD"] == D("49.32")
    assert account.balances["USD"] == D("714.32")
    assert account.positions[US.symbol].cost_basis == 0


def test_cash_ledger_reconciles_each_currency(broker):
    from trading_bot.portfolio.models import CashEntry

    broker.exchange("JPY", D("100000"), rate(NT), NT)
    fill(
        broker,
        price=100,
        quantity=1,
        at=NT + timedelta(hours=1),
        instrument=US,
        fx=rate(NT + timedelta(hours=1)),
    )
    entries = [CashEntry.model_validate_json(s) for s in broker.history()["cash_ledger"]]
    for currency in ["JPY", "USD"]:
        assert (
            sum((e.amount for e in entries if e.currency == currency), D(0))
            == broker.get_balance().balances[currency]
        )


def test_no_time_travel_after_account_changes(broker):
    fill(broker)
    with pytest.raises(ValueError, match="latest account event"):
        broker.value({}, None, T - timedelta(seconds=1))
    with pytest.raises(ValueError, match="latest account event"):
        broker.place_order(JP, "BUY", 100, T - timedelta(seconds=1))
    with pytest.raises(ValueError):
        broker.exchange("JPY", D(100), rate(T - timedelta(seconds=1)), T - timedelta(seconds=1))


def test_first_appropriate_future_bar_is_used(broker):
    order = broker.place_order(JP, "BUY", 100, T + timedelta(seconds=1))
    result = broker.execute(
        order.order_id,
        {
            JP.symbol: [
                bar(500, T + timedelta(hours=2)),
                bar(300, T),
                bar(400, T + timedelta(hours=1)),
            ]
        },
        None,
        T + timedelta(hours=3),
    )
    assert result.status == "FILLED"
    assert broker.get_executions()[0].reference_price == D(400)


def test_daily_start_label_maps_to_session_open(broker):
    # Yahoo daily timestamps are local midnight; execution reference is session open.
    midnight = T - timedelta(hours=9)
    daily = bar(price=500, at=midnight, interval="1d", fetched=T + timedelta(days=1))
    order = broker.place_order(JP, "BUY", 100, T - timedelta(minutes=1))
    result = broker.execute(order.order_id, {JP.symbol: [daily]}, None, T + timedelta(days=1))
    assert result.status == "FILLED"
    assert broker.get_executions()[0].price_at == T


def test_missing_rate_is_pending_after_us_funding(broker):
    broker.exchange("JPY", D("100000"), rate(NT - timedelta(minutes=1)), NT - timedelta(minutes=1))
    order = broker.place_order(US, "BUY", 1, NT - timedelta(seconds=1))
    assert (
        broker.execute(
            order.order_id, {US.symbol: [bar(100, NT, US)]}, None, NT + timedelta(hours=1)
        ).status
        == "PENDING"
    )
    assert broker.get_balance().balances["USD"] == D("665")


def test_paper_mode_old_observation_keeps_pending(broker):
    order = broker.place_order(JP, "BUY", 100, T - timedelta(seconds=1))
    assert (
        broker.execute(
            order.order_id, {JP.symbol: [bar()]}, None, T + timedelta(days=2), "paper"
        ).status
        == "PENDING"
    )


@pytest.mark.parametrize(
    "config",
    [
        {"max_symbol_ratio": D("0.11")},
        {"max_stock_ratio": D("0.81")},
        {"min_cash_ratio": D("0.19")},
        {"lot_sizes": {"7203.T": 0}},
    ],
)
def test_phase2_risk_settings_cannot_weaken_limits(config):
    with pytest.raises(ValidationError):
        TradingRules.model_validate(config)


def test_sqlite_constructor_closes_its_connection(tmp_path):
    path = tmp_path / "closed.sqlite3"
    SQLiteRepository(path)
    # On Windows an open SQLite handle prevents this unlink.
    path.unlink()
    assert not path.exists()


def test_complete_paper_cli_jp_us_fx_execute_and_value(tmp_path, capsys):
    from trading_bot.storage import ParquetStore

    config = tmp_path / "config"
    config.mkdir()
    settings = config / "settings.yaml"
    settings.write_text("storage_dir: market\n")
    store = ParquetStore(tmp_path / "market")
    store.save(
        JP, "1h", [bar(300), bar(400, T + timedelta(hours=1)), bar(400, T + timedelta(hours=5))]
    )
    store.save(US, "1h", [bar(100, NT, US)])
    db = tmp_path / "cli.sqlite3"
    prefix = ["--settings", str(settings), "paper", "--db", str(db)]
    assert main(prefix + ["init", "--at", (T - timedelta(days=1)).isoformat()]) == 0
    assert (
        main(
            prefix
            + [
                "buy",
                "--symbol",
                JP.symbol,
                "--quantity",
                "200",
                "--order-id",
                "jp",
                "--at",
                (T - timedelta(seconds=1)).isoformat(),
            ]
        )
        == 0
    )
    assert (
        main(
            prefix
            + [
                "execute",
                "--order-id",
                "jp",
                "--mode",
                "historical",
                "--at",
                (T + timedelta(hours=1)).isoformat(),
            ]
        )
        == 0
    )
    assert (
        main(
            prefix
            + [
                "sell",
                "--symbol",
                JP.symbol,
                "--quantity",
                "100",
                "--order-id",
                "sale",
                "--at",
                (T + timedelta(minutes=1)).isoformat(),
            ]
        )
        == 0
    )
    assert (
        main(
            prefix
            + [
                "execute",
                "--order-id",
                "sale",
                "--mode",
                "historical",
                "--at",
                (T + timedelta(hours=2)).isoformat(),
            ]
        )
        == 0
    )
    fx_time = (NT - timedelta(minutes=1)).isoformat()
    assert (
        main(
            prefix
            + [
                "fx",
                "--from-currency",
                "JPY",
                "--amount",
                "100000",
                "--usd-jpy",
                "150",
                "--rate-at",
                fx_time,
                "--at",
                fx_time,
            ]
        )
        == 0
    )
    assert (
        main(
            prefix
            + [
                "buy",
                "--symbol",
                US.symbol,
                "--quantity",
                "2",
                "--order-id",
                "us",
                "--at",
                (NT - timedelta(seconds=1)).isoformat(),
            ]
        )
        == 0
    )
    assert (
        main(
            prefix
            + [
                "execute",
                "--order-id",
                "us",
                "--mode",
                "historical",
                "--usd-jpy",
                "150",
                "--rate-at",
                NT.isoformat(),
                "--at",
                (NT + timedelta(hours=1)).isoformat(),
            ]
        )
        == 0
    )
    final_time = (NT + timedelta(hours=1)).isoformat()
    assert (
        main(
            prefix
            + [
                "value",
                "--mode",
                "historical",
                "--usd-jpy",
                "150",
                "--rate-at",
                final_time,
                "--at",
                final_time,
            ]
        )
        == 0
    )
    restored = PaperBroker(SQLiteRepository(db))
    assert restored.get_balance().realized_pnl["JPY"] == D("9930")
    assert len(restored.get_executions()) == 3
    assert "SIMULATION" in capsys.readouterr().out
