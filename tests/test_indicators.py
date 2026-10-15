from datetime import timedelta

import numpy as np
import pytest

from tests.ai_helpers import JP, NOW, histories
from trading_bot.screening.indicators import FeatureUnavailable, calculate, confirmed, wilder
from trading_bot.screening.models import ScreeningSettings


def features(data=None, at=NOW):
    data = data or histories()
    return calculate(JP, data["1d"], data["1h"], at, ScreeningSettings())


def test_sma_rsi_atr_hand_calculated():
    value = features()
    assert value.sma5 == 118
    assert value.sma20 == 110.5
    assert value.rsi14 == 100
    assert value.atr14 == 4


@pytest.mark.parametrize("prices,expected", [([100] * 21, 50), (list(range(120, 99, -1)), 0)])
def test_rsi_flat_and_down(prices, expected):
    assert features(histories(prices=prices)).rsi14 == expected


def test_changes_volume_turnover_and_volatility():
    result = features()
    assert result.hourly_return == pytest.approx(1 / 120)
    assert result.daily_return == pytest.approx(1 / 119)
    assert result.relative_volume == 2
    assert result.average_turnover20 == 116500
    returns = np.array([1 / price for price in range(100, 120)])
    assert result.volatility20 == pytest.approx(np.std(returns, ddof=1) * np.sqrt(252))


@pytest.mark.parametrize("kind", ["1d", "1h"])
def test_insufficient_history(kind):
    data = histories()
    data[kind] = data[kind][:1]
    with pytest.raises(FeatureUnavailable, match="need 21"):
        features(data)


@pytest.mark.parametrize(
    "field,value", [("close", float("nan")), ("high", float("inf")), ("open", -1), ("volume", -1)]
)
def test_invalid_observation_is_not_repaired(field, value):
    data = histories()
    data["1d"][0] = data["1d"][0].model_copy(update={field: value})
    with pytest.raises(FeatureUnavailable, match="invalid OHLCV"):
        features(data)


def test_price_discontinuity_and_zero_volume_excluded():
    data = histories(prices=[100] * 20 + [200])
    with pytest.raises(FeatureUnavailable, match="discontinuity"):
        features(data)
    data = histories()
    data["1d"] = [bar.model_copy(update={"volume": 0}) for bar in data["1d"]]
    with pytest.raises(FeatureUnavailable, match="zero historical volume"):
        features(data)


def test_future_fetch_and_unconfirmed_bars_do_not_change_indicators():
    data = histories()
    baseline = features(data)
    future = data["1h"][-1].model_copy(
        update={"timestamp": NOW + timedelta(hours=1), "fetched_at": NOW + timedelta(hours=2)}
    )
    data["1h"].append(future)
    assert features(data) == baseline
    data = histories()
    data["1h"][-1] = data["1h"][-1].model_copy(update={"fetched_at": NOW + timedelta(seconds=1)})
    with pytest.raises(FeatureUnavailable):
        features(data)
    data = histories()
    data["1h"][-1] = data["1h"][-1].model_copy(update={"fetched_at": data["1h"][-1].timestamp})
    with pytest.raises(FeatureUnavailable):
        features(data)


def test_stale_hours_missing_timezone_and_duplicate():
    with pytest.raises(FeatureUnavailable, match="stale hourly"):
        features(at=NOW + timedelta(days=2))
    data = histories()
    data["1h"][0] = data["1h"][0].model_copy(update={"timestamp": NOW.replace(tzinfo=None)})
    with pytest.raises(FeatureUnavailable):
        features(data)
    data = histories()
    data["1d"].append(data["1d"][0])
    with pytest.raises(FeatureUnavailable, match="duplicate"):
        features(data)


def test_wilder_seed_and_smoothing():
    assert wilder(np.array([2.0] * 14 + [16.0])) == 3
    with pytest.raises(FeatureUnavailable):
        wilder(np.array([1.0] * 13))


def test_market_holiday_is_not_fabricated():
    data = histories()
    holiday = data["1h"][0].model_copy(
        update={"timestamp": NOW.replace(month=1, day=1), "fetched_at": NOW}
    )
    assert confirmed([holiday], JP, "1h", NOW) == []
