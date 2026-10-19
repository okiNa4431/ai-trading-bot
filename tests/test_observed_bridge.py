from datetime import timedelta
from unittest.mock import Mock

import pytest

from tests.integration_helpers import observed_fixture
from trading_bot.alpaca import DailyBatch
from trading_bot.swing.models import SwingSettings
from trading_bot.swing.observed import ObservedDailyData


def test_provider_bridge_calls_only_the_existing_approved_fetch_contract():
    data, at = observed_fixture()
    securities = data.securities
    prices = {s.instrument.symbol: data.original[s.instrument.symbol] for s in securities}
    dates = tuple(b.timestamp.date() for b in prices[securities[0].instrument.symbol])
    provider = Mock()
    provider.fetch_many.return_value = DailyBatch(prices, dates, complete_response=True)
    built = ObservedDailyData.collect(
        provider,
        securities,
        data.opens_at[0],
        data.closes_at[-1],
        data.original["SPY"],
        data.fx_observations,
        data.actions,
    )
    result, _, _ = built.screen(at, SwingSettings(), {"S0019"}, at)
    assert any(c.instrument.symbol == "S0019" for c in result.candidates)
    provider.fetch_many.assert_called_once()
    assert not built.rights.external_ai and not built.hourly_available


@pytest.mark.parametrize("failure", ["missing", "duplicate", "unconfirmed", "reference", "fx"])
def test_bad_observed_inputs_rejected(failure):
    data, _ = observed_fixture()
    prices = {
        s.instrument.symbol: list(data.original[s.instrument.symbol]) for s in data.securities
    }
    dates = tuple(b.timestamp.date() for b in prices["S0000"])
    reference = list(data.original["SPY"])
    fx = list(data.fx_observations)
    if failure == "missing":
        prices["S0000"].pop()
    elif failure == "duplicate":
        prices["S0000"].append(prices["S0000"][0])
    elif failure == "unconfirmed":
        prices["S0000"][0] = prices["S0000"][0].model_copy(
            update={"fetched_at": prices["S0000"][0].timestamp}
        )
    elif failure == "reference":
        reference.pop()
    else:
        fx[0] = fx[0].model_copy(update={"source": "fixed-simulation"})
    with pytest.raises(ValueError):
        ObservedDailyData(
            data.securities,
            DailyBatch(prices, dates, complete_response=True),
            reference,
            fx,
            data.actions,
        )


def test_actual_observation_time_and_no_synthetic_fx_or_future_metadata():
    data, at = observed_fixture()
    result, _, _ = data.screen(at, SwingSettings(), set(), at)
    assert all(c.features.fetched_at == data.available[201] for c in result.candidates)
    assert all(c.features.source == "alpaca" for c in result.candidates)
    assert data.fx(at).timestamp <= at
    data.securities = (
        data.securities[0].model_copy(update={"metadata_at": at + timedelta(days=1)}),
        *data.securities[1:],
    )
    with pytest.raises(ValueError, match="future universe"):
        data.screen(at, SwingSettings(), set(), at)
