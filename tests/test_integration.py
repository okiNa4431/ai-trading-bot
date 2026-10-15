"""Offline integration and regression tests; every write uses pytest tmp_path."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

import pandas as pd
import pytest
import yaml
from pydantic import ValidationError

from trading_bot.cli import main
from trading_bot.config import Instrument, Settings, load_settings
from trading_bot.provider import DataError, YahooFinanceProvider, convert_frame
from trading_bot.service import collect
from trading_bot.storage import ParquetStore

NOW = datetime(2026, 10, 15, tzinfo=UTC)
JP = Instrument(symbol="7203.T", market="JP", currency="JPY", display_name="Toyota")
US = Instrument(symbol="AAPL", market="US", currency="USD", display_name="Apple")


def sample(instrument=JP, interval="1h"):
    data = pd.DataFrame(
        {
            "Open": [100.0, 101.0],
            "High": [110.0, 111.0],
            "Low": [90.0, 91.0],
            "Close": [105.0, 106.0],
            "Volume": [0.0, 100.0],
        },
        index=pd.date_range("2026-10-10", periods=2, freq="h", tz="UTC"),
    )
    return convert_frame(data, instrument, interval, NOW)


def sample_frame():
    return pd.DataFrame(
        {"Open": [100.0], "High": [110.0], "Low": [90.0], "Close": [105.0], "Volume": [0.0]},
        index=pd.DatetimeIndex([NOW - timedelta(days=1)]),
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("Open", None),
        ("High", float("nan")),
        ("Low", float("-inf")),
        ("Close", -1),
        ("Open", 120),
        ("Close", 80),
        ("High", 80),
        ("Volume", -1),
        ("Volume", float("inf")),
    ],
)
def test_bad_observations(field, value):
    data = sample_frame()
    data.loc[data.index[0], field] = value
    with pytest.raises(DataError):
        convert_frame(data, JP, "1h", NOW)


@pytest.mark.parametrize(
    "index", [pd.DatetimeIndex([pd.NaT]), pd.DatetimeIndex([datetime(2026, 10, 10)])]
)
def test_missing_or_naive_timestamp(index):
    with pytest.raises(DataError):
        convert_frame(sample_frame().set_axis(index), JP, "1h", NOW)


@pytest.mark.parametrize(
    "symbol,market,currency",
    [
        ("../AAPL", "US", "USD"),
        ("BAD SPACE", "US", "USD"),
        ("AAPL", "JP", "JPY"),
        ("7203.T", "US", "USD"),
    ],
)
def test_invalid_instrument(symbol, market, currency):
    with pytest.raises(ValidationError):
        Instrument(symbol=symbol, market=market, currency=currency, display_name="bad")


def test_validation_error_identifies_field(caplog):
    data = sample_frame()
    data.loc[data.index[0], "Open"] = -1
    with pytest.raises(DataError):
        convert_frame(data, JP, "1h", NOW)
    assert "open" in caplog.text.lower()


@pytest.mark.parametrize("mode", ["write", "replace", "permission"])
def test_save_failure_preserves_bytes_and_logs(tmp_path, caplog, mode):
    store = ParquetStore(tmp_path / "new" / "market")
    store.save(JP, "1h", sample())
    before = store.path(JP, "1h").read_bytes()
    target = "pandas.DataFrame.to_parquet" if mode == "write" else "trading_bot.storage.os.replace"
    error = PermissionError("denied") if mode == "permission" else OSError("disk error")
    with patch(target, side_effect=error), pytest.raises(OSError):
        store.save(JP, "1h", sample())
    assert store.path(JP, "1h").read_bytes() == before
    assert not list(tmp_path.rglob("*.tmp"))
    assert not list(tmp_path.rglob(".write.lock"))
    assert "save failed" in caplog.text


@pytest.mark.parametrize("corruption", ["bytes", "empty-schema", "missing-metadata"])
def test_corrupt_storage_is_not_overwritten(tmp_path, corruption):
    store = ParquetStore(tmp_path)
    path = store.path(JP, "1h")
    path.parent.mkdir(parents=True)
    if corruption == "bytes":
        path.write_bytes(b"not parquet")
    elif corruption == "empty-schema":
        pd.DataFrame().to_parquet(path)
    else:
        data = pd.DataFrame([b.model_dump() for b in sample()]).drop(columns="adjusted")
        data.to_parquet(path)
    before = path.read_bytes()
    with pytest.raises((ValueError, OSError)):
        store.save(JP, "1h", sample())
    assert path.read_bytes() == before


def test_invalid_model_copy_rejected_at_storage_boundary(tmp_path):
    store = ParquetStore(tmp_path)
    bad = sample()[0].model_copy(update={"close": -100.0})
    with pytest.raises(ValueError):
        store.save(JP, "1h", [bad])
    assert not store.path(JP, "1h").exists()


@pytest.mark.parametrize("failure", [TimeoutError, ConnectionError, RuntimeError])
def test_bounded_retries_and_recovery(failure):
    settings = Settings(retries=2, request_interval=0)
    ticker = Mock()
    ticker.history.side_effect = failure("HTTP 429 or network failure secret-token")
    end = datetime.now(UTC)
    with (
        patch("trading_bot.provider.yf.Ticker", return_value=ticker),
        patch("trading_bot.provider.time.sleep"),
    ):
        with pytest.raises(DataError):
            YahooFinanceProvider(settings).fetch(JP, end - timedelta(days=2), end, "1h")
    assert ticker.history.call_count == 3
    ticker.history.side_effect = [failure("transient"), sample_frame()]
    with (
        patch("trading_bot.provider.yf.Ticker", return_value=ticker),
        patch("trading_bot.provider.time.sleep"),
    ):
        assert YahooFinanceProvider(settings).fetch(JP, end - timedelta(days=2), end, "1h")


def test_empty_api_response_and_missing_columns():
    end = datetime.now(UTC)
    for data in [pd.DataFrame(), sample_frame().drop(columns="Close")]:
        with patch("trading_bot.provider.yf.Ticker") as ticker:
            ticker.return_value.history.return_value = data
            with pytest.raises(DataError):
                YahooFinanceProvider(Settings(retries=0)).fetch(
                    JP,
                    end - timedelta(days=2),
                    end,
                    "1h",
                )


def test_first_symbol_failure_does_not_lose_later_success(tmp_path, caplog):
    store = ParquetStore(tmp_path)
    provider = Mock()
    provider.fetch.side_effect = [DataError("provider returned empty data"), sample(US)]
    assert collect(Settings(), [JP, US], ["1h"], provider, store) == ["JP/7203.T/1h"]
    assert len(store.read(US, "1h")) == 2
    assert not store.path(JP, "1h").exists()
    assert "empty data" in caplog.text


def test_interrupt_releases_lock_and_preserves_original(tmp_path):
    store = ParquetStore(tmp_path)
    store.save(JP, "1h", sample())
    before = store.path(JP, "1h").read_bytes()
    with patch("pandas.DataFrame.to_parquet", side_effect=KeyboardInterrupt):
        with pytest.raises(KeyboardInterrupt):
            store.save(JP, "1h", sample())
    assert store.path(JP, "1h").read_bytes() == before
    assert not list(tmp_path.rglob(".write.lock"))
    assert not list(tmp_path.rglob("*.tmp"))


@pytest.mark.parametrize(
    "command,market,interval",
    [
        ("fetch", "JP", "1d"),
        ("fetch", "US", "1h"),
        ("fetch", "ALL", "1h"),
        ("update", "ALL", None),
    ],
)
def test_cli_end_to_end_mocked_provider(tmp_path, capsys, command, market, interval):
    config = tmp_path / "config"
    config.mkdir()
    settings = config / "settings.yaml"
    settings.write_text("storage_dir: market\nrequest_interval: 0\n")
    universe = config / "universe.yaml"
    universe.write_text(yaml.safe_dump([i.model_dump() for i in [JP, US]]))
    arguments = [
        "--settings",
        str(settings),
        "--universe",
        str(universe),
        command,
        "--market",
        market,
    ]
    if interval:
        arguments += ["--interval", interval]

    def fetch(instrument, start, end, foot):
        return sample(instrument, foot)

    with patch("trading_bot.cli.YahooFinanceProvider") as provider:
        provider.return_value.fetch.side_effect = fetch
        assert main(arguments) == 0
        assert main(arguments) == 0
    assert main(["--settings", str(settings), "status"]) == 0
    rows = ParquetStore(tmp_path / "market").status()
    assert len(rows) == (4 if interval is None else 2 if market == "ALL" else 1)
    assert all(row["count"] == 2 for row in rows)
    assert all(market == "ALL" or row["market"] == market for row in rows)
    assert "completed=" in capsys.readouterr().out


def test_cli_error_does_not_disclose_config_secret(tmp_path, capsys):
    config = tmp_path / "settings.yaml"
    config.write_text("api_key: secret-do-not-disclose\n")
    assert main(["--settings", str(config), "status"]) == 2
    assert "secret-do-not-disclose" not in capsys.readouterr().out


def test_duplicate_intervals_and_nonfinite_settings_rejected():
    for config in [{"intervals": ["1h", "1h"]}, {"request_interval": float("inf")}]:
        with pytest.raises(ValidationError):
            Settings.model_validate(config)


def test_posix_settings_resolution(tmp_path):
    config = tmp_path / "a" / "config" / "settings.yaml"
    config.parent.mkdir(parents=True)
    config.write_text("storage_dir: data/market\n")
    assert load_settings(config).storage_dir == tmp_path / "a" / "data" / "market"


def test_workflow_contracts():
    # BaseLoader avoids YAML 1.1 interpreting GitHub's `on` as a boolean.
    ci = yaml.load(
        Path(".github/workflows/ci.yml").read_text(encoding="utf-8-sig"), Loader=yaml.BaseLoader
    )
    assert ci["on"] == ["push", "pull_request"]
    steps = ci["jobs"]["checks"]["steps"]
    commands = [s["run"] for s in steps if "run" in s]
    assert all(
        command in commands
        for command in [
            "uv sync --locked",
            "uv run pytest",
            "uv run ruff check .",
            "uv run mypy src",
        ]
    )
    manual = yaml.load(
        Path(".github/workflows/fetch-market-data.yml").read_text(encoding="utf-8-sig"),
        Loader=yaml.BaseLoader,
    )
    assert set(manual["on"]) == {"workflow_dispatch"}
    assert set(manual["on"]["workflow_dispatch"]["inputs"]) == {"market", "interval"}
    assert manual["permissions"] == {"contents": "read"}
    text = Path(".github/workflows/fetch-market-data.yml").read_text(encoding="utf-8-sig")
    assert "pipefail" in text and "actions/upload-artifact@" in text
    assert "git push" not in text and "git commit" not in text


def test_http_429_is_bounded_and_logged(caplog):
    from yfinance.exceptions import YFRateLimitError

    end = datetime.now(UTC)
    ticker = Mock()
    ticker.history.side_effect = YFRateLimitError()
    with (
        patch("trading_bot.provider.yf.Ticker", return_value=ticker),
        patch("trading_bot.provider.time.sleep"),
    ):
        with pytest.raises(DataError, match="YFRateLimitError"):
            YahooFinanceProvider(Settings(retries=2, request_interval=0)).fetch(
                JP,
                end - timedelta(days=2),
                end,
                "1h",
            )
    assert ticker.history.call_count == 3
    assert "YFRateLimitError" in caplog.text


def test_request_spacing():
    end = datetime.now(UTC)
    provider = YahooFinanceProvider(Settings(request_interval=1))
    provider.last_request = 100
    with (
        patch("trading_bot.provider.time.monotonic", return_value=100.2),
        patch("trading_bot.provider.time.sleep") as sleep,
        patch("trading_bot.provider.yf.Ticker") as ticker,
    ):
        ticker.return_value.history.return_value = sample_frame()
        provider.fetch(JP, end - timedelta(days=2), end, "1h")
    assert sleep.call_args.args[0] == pytest.approx(0.8)


def test_future_bars_excluded_and_bad_ranges_rejected():
    with pytest.raises(DataError, match="confirmed"):
        convert_frame(sample_frame().set_axis(pd.DatetimeIndex([NOW])), JP, "1h", NOW)
    provider = YahooFinanceProvider(Settings())
    end = datetime.now(UTC)
    for start, stop in [
        (end, end),
        (end + timedelta(days=1), end),
        (datetime(2026, 10, 10), end),
        (end - timedelta(days=61), end),
    ]:
        with pytest.raises(DataError):
            provider.fetch(JP, start, stop, "1h")


def test_cli_bad_yaml_and_corrupt_parquet(tmp_path, capsys):
    config = tmp_path / "settings.yaml"
    config.write_text("storage_dir: [secret-do-not-disclose\n")
    assert main(["--settings", str(config), "status"]) == 2
    assert "secret-do-not-disclose" not in capsys.readouterr().out
    config.write_text(f"storage_dir: {(tmp_path / 'bars').as_posix()}\n")
    path = ParquetStore(tmp_path / "bars").path(JP, "1h")
    path.parent.mkdir(parents=True)
    path.write_bytes(b"broken")
    assert main(["--settings", str(config), "status"]) == 2
    assert "Error:" in capsys.readouterr().out


def test_cli_partial_failure_and_invalid_arguments(tmp_path, capsys):
    config = tmp_path / "settings.yaml"
    config.write_text(f"storage_dir: {(tmp_path / 'market').as_posix()}\n")
    with patch("trading_bot.cli.YahooFinanceProvider") as provider:
        provider.return_value.fetch.side_effect = DataError("provider returned empty data")
        assert (
            main(
                [
                    "--settings",
                    str(config),
                    "fetch",
                    "--symbol",
                    "7203.T",
                    "--market",
                    "JP",
                    "--interval",
                    "1h",
                ]
            )
            == 1
        )
    assert "FAILED JP/7203.T/1h" in capsys.readouterr().out
    for arguments in [["fetch", "--interval", "5m"], ["fetch", "--symbol", "UNKNOWN"], []]:
        with pytest.raises(SystemExit) as error:
            main(arguments)
        assert error.value.code == 2


def test_storage_permission_denied_before_lock(tmp_path, caplog):
    store = ParquetStore(tmp_path)
    store.save(JP, "1h", sample())
    before = store.path(JP, "1h").read_bytes()
    with patch("trading_bot.storage.os.open", side_effect=PermissionError("denied")):
        with pytest.raises(PermissionError):
            store.save(JP, "1h", sample())
    assert store.path(JP, "1h").read_bytes() == before
    assert "save failed" in caplog.text
    assert not list(tmp_path.rglob(".write.lock"))


def test_status_dates_and_counts_match_disk(tmp_path, capsys):
    config = tmp_path / "settings.yaml"
    config.write_text(f"storage_dir: {tmp_path.as_posix()}\n")
    store = ParquetStore(tmp_path)
    store.save(JP, "1h", sample())
    assert main(["--settings", str(config), "status"]) == 0
    output = capsys.readouterr().out
    row = store.status()[0]
    assert "\t".join(str(value) for value in row.values()) in output
