import logging
import math
from datetime import datetime

from pydantic import ValidationError

from trading_bot.config import Instrument, Market
from trading_bot.models import PriceBar
from trading_bot.portfolio.models import utc
from trading_bot.screening.indicators import FeatureUnavailable, calculate
from trading_bot.screening.models import Candidate, Features, ScreeningResult, ScreeningSettings

logger = logging.getLogger(__name__)


def score(
    features: Features, instrument: Instrument, as_of: datetime, settings: ScreeningSettings
) -> tuple[float, dict[str, float]]:
    def clip(value: float) -> float:
        return max(-1, min(1, value))

    threshold = settings.liquidity_jpy if instrument.market == "JP" else settings.liquidity_usd
    components = {
        "trend": clip((features.daily_close / features.sma20 - 1) / 0.1),
        "momentum": clip(features.daily_return / 0.05),
        "volume": clip((features.relative_volume - 1) / 2),
        "liquidity": clip(math.log10(max(features.average_turnover20 / threshold, 1e-12)) / 3),
        "volatility": clip(1 - features.volatility20),
        "quality": clip(
            1
            - 2
            * (as_of - features.hourly_observed_at).total_seconds()
            / settings.max_hourly_age_seconds
        ),
    }
    weights = settings.weights.model_dump()
    return sum(components[key] * weights[key] for key in components) / sum(
        weights.values()
    ), components


def screen(
    universe: list[Instrument],
    data: dict[str, dict[str, list[PriceBar]]],
    held: set[str],
    market: Market,
    as_of: datetime,
    settings: ScreeningSettings,
) -> ScreeningResult:
    as_of = utc(as_of)
    instruments = {item.symbol: item for item in universe if item.market == market}
    if len(instruments) != sum(i.market == market for i in universe):
        raise ValueError("duplicate screening symbol")
    ranked = []
    unavailable = []
    excluded = {}
    for instrument in instruments.values():
        try:
            inputs = data.get(instrument.symbol, {})
            features = calculate(
                instrument, inputs.get("1d", []), inputs.get("1h", []), as_of, settings
            )
            value, components = score(features, instrument, as_of, settings)
            ranked.append(
                Candidate(
                    instrument=instrument,
                    held=instrument.symbol in held,
                    features=features,
                    score=value,
                    components=components,
                )
            )
        except (FeatureUnavailable, ValidationError) as exc:
            reason = (
                str(exc)
                if isinstance(exc, FeatureUnavailable)
                else "invalid/nonfinite computed features"
            )
            excluded[instrument.symbol] = reason
            logger.warning("screen excluded symbol=%s reason=%s", instrument.symbol, reason)
            if instrument.symbol in held:
                unavailable.append(
                    Candidate(
                        instrument=instrument, held=True, features=None, score=None, issue=reason
                    )
                )
    ranked.sort(key=lambda candidate: (-float(candidate.score or 0), candidate.instrument.symbol))
    selected = {candidate.instrument.symbol: candidate for candidate in ranked[: settings.top_k]}
    for candidate in [*ranked, *unavailable]:
        if candidate.held:
            selected[candidate.instrument.symbol] = candidate
    return ScreeningResult(
        market=market,
        as_of=as_of,
        analyzed=len(instruments),
        candidates=list(selected.values()),
        excluded=excluded,
    )
