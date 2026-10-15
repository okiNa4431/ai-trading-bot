import pytest

from tests.ai_helpers import JP, NOW, histories
from trading_bot.config import Instrument
from trading_bot.screening.indicators import calculate
from trading_bot.screening.models import ScreeningSettings, Weights
from trading_bot.screening.screener import score, screen


def test_score_components_and_configurable_weights():
    settings = ScreeningSettings(
        weights=Weights(trend=0, momentum=1, volume=0, liquidity=0, volatility=0, quality=0)
    )
    data = histories()
    features = calculate(JP, data["1d"], data["1h"], NOW, settings)
    value, components = score(features, JP, NOW, settings)
    assert value == pytest.approx((1 / 119) / 0.05)
    assert all(-1 <= component <= 1 for component in components.values())
    assert score(features, JP, NOW, ScreeningSettings())[0] != value
    with pytest.raises(ValueError):
        Weights(trend=0, momentum=0, volume=0, liquidity=0, volatility=0, quality=0)


def seven():
    instruments = [
        Instrument(symbol=f"{1000 + i}.T", market="JP", currency="JPY", display_name="Synthetic")
        for i in range(7)
    ]
    data = {}
    for index, instrument in enumerate(instruments):
        data[instrument.symbol] = {
            interval: [
                bar.model_copy(
                    update={
                        key: getattr(bar, key) * (index + 1)
                        for key in ("open", "high", "low", "close")
                    }
                )
                for bar in bars
            ]
            for interval, bars in histories(instrument).items()
        }
    return instruments, data


def test_ranking_top_k_and_forced_holding_without_duplicates():
    instruments, data = seven()
    result = screen(instruments, data, {instruments[0].symbol}, "JP", NOW, ScreeningSettings())
    symbols = [c.instrument.symbol for c in result.candidates]
    assert symbols[:5] == [i.symbol for i in reversed(instruments[2:])]
    assert instruments[0].symbol in symbols
    assert len(symbols) == len(set(symbols)) == 6


def test_unavailable_holding_still_evaluated_and_unheld_excluded():
    instruments, data = seven()
    data[instruments[0].symbol] = {}
    data[instruments[1].symbol] = {}
    result = screen(instruments, data, {instruments[0].symbol}, "JP", NOW, ScreeningSettings())
    held = next(c for c in result.candidates if c.instrument.symbol == instruments[0].symbol)
    assert held.held and held.features is None and held.issue
    assert instruments[1].symbol not in [c.instrument.symbol for c in result.candidates]


def test_screen_duplicate_symbol_rejected():
    with pytest.raises(ValueError, match="duplicate"):
        screen([JP, JP], {}, set(), "JP", NOW, ScreeningSettings())
