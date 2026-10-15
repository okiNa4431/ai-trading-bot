from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

import pandas as pd
import pytest
from pydantic import ValidationError

from trading_bot.cli import main
from trading_bot.config import Instrument, Settings, load_settings, load_universe
from trading_bot.models import PriceBar
from trading_bot.provider import DataError, YahooFinanceProvider, convert_frame
from trading_bot.service import collect
from trading_bot.storage import ParquetStore

NOW = datetime(2026, 10, 15, tzinfo=UTC)
JP = Instrument(symbol="7203.T", market="JP", currency="JPY", display_name="Toyota")
US = Instrument(symbol="AAPL", market="US", currency="USD", display_name="Apple")


def frame(zone="Asia/Tokyo"):
    return pd.DataFrame(
        {
            "Open": [100.0, 101.0],
            "High": [110.0, 111.0],
            "Low": [90.0, 91.0],
            "Close": [105.0, 106.0],
            "Volume": [0.0, 100.0],
        },
        index=pd.date_range("2026-10-13 09:00", periods=2, freq="h", tz=zone),
    )


def bars():
    return convert_frame(frame(), JP, "1h", NOW)


def test_configuration():
    assert load_settings(Path("config/settings.yaml")).hourly_days == 30
    universe = load_universe(Path("config/universe.yaml"))
    assert sum(i.market == "JP" for i in universe) == 10
    assert sum(i.market == "US" for i in universe) == 10
    with pytest.raises(ValidationError):
        Instrument(symbol="AAPL", market="JP", currency="JPY", display_name="bad")
    with pytest.raises(ValidationError):
        Settings(hourly_days=61)


def test_invalid_universe(tmp_path):
    path = tmp_path / "universe.yaml"
    path.write_text("[]")
    with pytest.raises(ValueError):
        load_universe(path)
    path.write_text(Path("config/universe.yaml").read_text(encoding="utf-8") * 2, encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate"):
        load_universe(path)


@pytest.mark.parametrize("zone", ["Asia/Tokyo", "America/New_York"])
def test_conversion_and_utc(zone):
    result = convert_frame(frame(zone), JP, "1h", NOW)
    assert result[0].timestamp.tzinfo == UTC
    assert result[0].timestamp == frame(zone).index[0].to_pydatetime().astimezone(UTC)
    assert result[0].volume == 0
    assert result[0].adjusted is False


def test_dst_and_lunch_timestamps_preserved():
    data = frame()
    data.index = pd.DatetimeIndex(
        [
            "2026-03-06 09:30",
            "2026-03-09 09:30",
        ]
    ).tz_localize("America/New_York")
    result = convert_frame(data, US, "1h", NOW)
    assert [b.timestamp.hour for b in result] == [14, 13]
    data.index = pd.DatetimeIndex(
        [
            "2026-10-13 11:00",
            "2026-10-13 12:30",
        ]
    ).tz_localize("Asia/Tokyo")
    result = convert_frame(data, JP, "1h", NOW)
    assert result[1].timestamp - result[0].timestamp == timedelta(minutes=90)


def test_possible_gap_warning(caplog):
    data = frame()
    data.index = pd.DatetimeIndex(
        [
            "2026-09-01 09:00",
            "2026-09-15 09:00",
        ]
    ).tz_localize("Asia/Tokyo")
    convert_frame(data, JP, "1h", NOW)
    assert "possible data gap" in caplog.text


@pytest.mark.parametrize(
    "column,value",
    [
        ("Open", 0),
        ("High", 80),
        ("Low", 120),
        ("Close", float("nan")),
        ("Open", float("inf")),
        ("Volume", -1),
        ("Volume", float("nan")),
    ],
)
def test_invalid_ohlcv_logged(column, value, caplog):
    data = frame()
    data.loc[data.index[0], column] = value
    with pytest.raises(DataError):
        convert_frame(data, JP, "1h", NOW)
    assert "validation error symbol=7203.T" in caplog.text


def test_empty_columns_naive_duplicate():
    for data in [
        pd.DataFrame(),
        frame().drop(columns="Open"),
        frame().set_axis(pd.date_range("2026-10-13", periods=2, freq="h")),
        pd.concat([frame(), frame()]),
    ]:
        with pytest.raises(DataError):
            convert_frame(data, JP, "1h", NOW)


def test_unconfirmed_and_price_jump(caplog):
    data = frame()
    assert len(convert_frame(data, JP, "1h", bars()[0].timestamp + timedelta(hours=1))) == 1
    with pytest.raises(DataError, match="confirmed"):
        convert_frame(data, JP, "1h", bars()[0].timestamp)
    data.iloc[1] = [200, 220, 190, 210, 100]
    convert_frame(data, JP, "1h", NOW)
    assert "large raw price change" in caplog.text
    first = bars()[0].timestamp
    with pytest.raises(DataError):
        convert_frame(frame(), JP, "1d", first + timedelta(hours=2))


def test_model_naive_and_market():
    raw = bars()[0].model_dump()
    raw["timestamp"] = datetime(2026, 1, 1)
    with pytest.raises(ValidationError):
        PriceBar.model_validate(raw)
    raw = bars()[0].model_dump()
    raw["market"] = "US"
    with pytest.raises(ValidationError):
        PriceBar.model_validate(raw)


def test_merge_and_idempotence(tmp_path):
    store = ParquetStore(tmp_path)
    data = bars()
    assert store.save(JP, "1h", data) == (2, 0)
    assert store.save(JP, "1h", data) == (0, 0)
    revised = PriceBar.model_validate({**data[1].model_dump(), "close": 107})
    extra = PriceBar.model_validate(
        {**revised.model_dump(), "timestamp": revised.timestamp + timedelta(hours=1)}
    )
    assert store.save(JP, "1h", [extra, revised]) == (1, 1)
    loaded = store.read(JP, "1h")
    assert len(loaded) == 3
    assert loaded[1].close == 107
    assert store.status()[0]["count"] == 3
    assert store.status()[0]["oldest"] == data[0].timestamp.isoformat()


def test_atomic_failure_and_lock(tmp_path):
    store = ParquetStore(tmp_path)
    store.save(JP, "1h", bars())
    before = store.path(JP, "1h").read_bytes()
    with patch("trading_bot.storage.os.replace", side_effect=OSError("disk failure")):
        with pytest.raises(OSError):
            store.save(JP, "1h", bars())
    assert store.path(JP, "1h").read_bytes() == before
    assert not list(tmp_path.rglob("*.tmp"))
    with store.writer(), pytest.raises(FileExistsError):
        store.save(JP, "1h", bars())
    assert not (tmp_path / ".write.lock").exists()
    with pytest.raises(ValueError):
        store.save(US, "1h", bars())
    with pytest.raises(ValueError):
        store.save(JP, "1h", bars() * 2)


def test_provider_retry_and_flags():
    settings = Settings(retries=1, request_interval=0)
    ticker = Mock()
    ticker.history.side_effect = [TimeoutError("secret"), frame()]
    with (
        patch("trading_bot.provider.yf.Ticker", return_value=ticker),
        patch("trading_bot.provider.time.sleep"),
    ):
        result = YahooFinanceProvider(settings).fetch(JP, NOW - timedelta(days=5), NOW, "1h")
    assert len(result) == 2
    assert ticker.history.call_count == 2
    assert ticker.history.call_args.kwargs["auto_adjust"] is False
    assert ticker.history.call_args.kwargs["timeout"] == 20


def test_provider_failure_does_not_leak(caplog):
    with patch("trading_bot.provider.yf.Ticker", side_effect=TimeoutError("secret-token")):
        with pytest.raises(DataError):
            YahooFinanceProvider(Settings(retries=0)).fetch(JP, NOW - timedelta(days=1), NOW, "1h")
    assert "secret-token" not in caplog.text


def test_partial_failure_and_overlap(tmp_path):
    store = ParquetStore(tmp_path)
    store.save(JP, "1h", bars())
    provider = Mock()
    provider.fetch.side_effect = [bars(), DataError("empty")]
    with patch("trading_bot.service.datetime") as clock:
        clock.now.return_value = NOW
        failures = collect(Settings(), [JP, US], ["1h"], provider, store, update=True)
    assert failures == ["US/AAPL/1h"]
    assert provider.fetch.call_args_list[0].args[1] == bars()[0].timestamp


@pytest.mark.parametrize("market,interval", [("JP", "1d"), ("US", "1h"), ("ALL", "1h")])
def test_cli_selection(market, interval):
    with patch("trading_bot.cli.collect", return_value=[]) as run:
        assert main(["fetch", "--market", market, "--interval", interval]) == 0
    assert all(market == "ALL" or i.market == market for i in run.call_args.args[1])
    assert run.call_args.args[2] == [interval]


def test_cli_status_and_errors(tmp_path, capsys):
    settings = tmp_path / "settings.yaml"
    root = tmp_path / "bars"
    settings.write_text(f"storage_dir: {root.as_posix()}\n")
    ParquetStore(root).save(JP, "1h", bars())
    assert main(["--settings", str(settings), "status"]) == 0
    assert "7203.T\tJP\t1h\t2" in capsys.readouterr().out
    with pytest.raises(SystemExit) as exit_info:
        main(["fetch", "--market", "BAD"])
    assert exit_info.value.code == 2
    with patch("trading_bot.cli.collect", return_value=["JP/7203.T/1h"]):
        assert main(["update", "--market", "JP"]) == 1
