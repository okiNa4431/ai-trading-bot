"""Two-stage, point-in-time daily screening; missing rows never get forward-filled."""

from datetime import datetime, timedelta
from decimal import Decimal

import numpy as np

from trading_bot.portfolio.models import Mark, utc
from trading_bot.screening.models import Candidate, Features, ScreeningResult
from trading_bot.swing.data import SwingData
from trading_bot.swing.models import ScreenSummary, SwingSettings


def screen_daily(
    data: SwingData,
    at: datetime,
    settings: SwingSettings,
    protected: set[str] | None = None,
) -> tuple[ScreeningResult, ScreenSummary]:
    at, protected = utc(at), protected or set()
    end = data.daily_index(at)
    if end < 200 or at - data.closes_at[end] > timedelta(days=7):
        raise ValueError("need 201 available daily sessions without stale history")
    start = end - 200
    c, o = data.closing[start : end + 1], data.opening[start : end + 1]
    h, low, v = data.high[start : end + 1], data.low[start : end + 1], data.volume[start : end + 1]
    valid = np.isfinite(c).all(axis=0) & np.isfinite(o).all(axis=0)
    valid &= np.isfinite(h).all(axis=0) & np.isfinite(low).all(axis=0)
    valid &= np.isfinite(v).all(axis=0) & (v >= 0).all(axis=0)
    valid &= (low > 0).all(axis=0) & (h >= np.maximum(o, c)).all(axis=0)
    valid &= (low <= np.minimum(o, c)).all(axis=0)
    if (
        not np.isfinite(data.market[start : end + 1]).all()
        or (data.market[start : end + 1] <= 0).any()
    ):
        raise ValueError("missing market regime history")
    with np.errstate(divide="ignore", invalid="ignore"):
        returns = c[1:] / c[:-1] - 1
        valid &= (np.abs(returns) <= 0.35).all(axis=0)
        sma20, sma50, sma200 = c[-20:].mean(axis=0), c[-50:].mean(axis=0), c[-200:].mean(axis=0)
        momentum20, momentum60 = c[-1] / c[-21] - 1, c[-1] / c[-61] - 1
        volatility = returns[-20:].std(axis=0, ddof=1) * np.sqrt(252)
        turnover = (c[-20:] * v[-20:]).mean(axis=0)
        gaps = o[-1] / c[-2] - 1
        delta = np.diff(c, axis=0)
        gain, loss = np.maximum(delta, 0), np.maximum(-delta, 0)
        true_range = np.maximum(
            h[1:] - low[1:], np.maximum(abs(h[1:] - c[:-1]), abs(low[1:] - c[:-1]))
        )
        ag, al, atr = gain[:14].mean(axis=0), loss[:14].mean(axis=0), true_range[:14].mean(axis=0)
        for i in range(14, len(delta)):
            ag, al, atr = (
                (ag * 13 + gain[i]) / 14,
                (al * 13 + loss[i]) / 14,
                (atr * 13 + true_range[i]) / 14,
            )
        rsi = np.where(ag + al == 0, 50, np.where(al == 0, 100, 100 - 100 / (1 + ag / al)))
        market_momentum = data.market[end] / data.market[end - 60] - 1
        strength = momentum60 - market_momentum
        scores = (
            0.4 * momentum60
            + 0.25 * strength
            + 0.2 * (c[-1] / sma50 - 1)
            - 0.1 * volatility
            - 0.05 * abs(gaps)
        )
    day = data.closes_at[end].date()
    candidates: dict[str, Candidate] = {}
    eligible: list[tuple[float, str]] = []
    excluded: dict[str, str] = {}
    issues = []
    for i, security in enumerate(data.securities):
        symbol, instrument = security.instrument.symbol, security.instrument
        membership = (
            security.metadata_at <= at
            and security.listed <= day
            and (security.delisted is None or security.delisted > day)
        )
        if not membership and symbol not in protected:
            continue
        reason = None
        if not membership:
            reason = "protected security outside known active membership"
        elif security.kind != "common":
            reason = "ETF/ADR/preferred/special excluded from new entries"
        elif not valid[i]:
            reason = "missing/invalid/discontinuous daily history"
            issues.append(f"{symbol}: incomplete universe coverage; freeze new buys")
        elif c[-1, i] < settings.min_price or turnover[i] < settings.min_turnover:
            reason = "low price or insufficient turnover"
        elif volatility[i] > settings.max_volatility or v[-1, i] == 0:
            reason = "excessive volatility or possible halt"
        features = None
        if membership and valid[i]:
            observed = data.closes_at[end]
            features = Features(
                symbol=symbol,
                hourly_return=None,
                hourly_observed_at=None,
                daily_return=float(returns[-1, i]),
                sma5=float(c[-5:, i].mean()),
                sma20=float(sma20[i]),
                sma50=float(sma50[i]),
                sma200=float(sma200[i]),
                rsi14=float(rsi[i]),
                atr14=float(atr[i]),
                momentum20=float(momentum20[i]),
                momentum60=float(momentum60[i]),
                relative_strength=float(strength[i]),
                relative_volume=float(v[-1, i] / v[-21:-1, i].mean())
                if v[-21:-1, i].mean() > 0
                else 0,
                volatility20=float(volatility[i]),
                average_turnover20=float(turnover[i]),
                daily_close=float(c[-1, i]),
                gap_return=float(gaps[i]),
                sector=security.sector,
                daily_observed_at=observed,
                fetched_at=observed + data.latency,
                source="synthetic" if data.rights.kind == "synthetic" else "alpaca",
                mark=Mark(
                    symbol=symbol,
                    currency=instrument.currency,
                    price=Decimal(str(c[-1, i])),
                    price_at=observed,
                    available_at=observed + data.latency,
                ),
            )
        if reason:
            excluded[symbol] = reason
        candidates[symbol] = Candidate(
            instrument=instrument,
            held=symbol in protected,
            features=features,
            score=float(scores[i]) if valid[i] else None,
            issue=reason,
        )
        if reason is None:
            eligible.append((-float(scores[i]), symbol))
    first = [s for _, s in sorted(eligible)[: settings.stage_one]]
    selected: list[str] = []
    sectors: dict[str, int] = {}
    for symbol in first:
        feature = candidates[symbol].features
        assert feature is not None and feature.sector is not None
        sector = feature.sector
        if sectors.get(sector, 0) >= settings.sector_limit:
            continue
        selected.append(symbol)
        sectors[sector] = sectors.get(sector, 0) + 1
        if len(selected) == settings.stage_two:
            break
    unknown = protected - candidates.keys()
    if unknown:
        raise ValueError(f"protected symbols absent from registry: {sorted(unknown)}")
    selected += sorted(protected - set(selected))
    market_bear = data.market[end] < data.market[end - 199 : end + 1].mean()
    if market_bear:
        issues.append("market below SMA200; freeze new buys")
    return ScreeningResult(
        market="US",
        as_of=at,
        analyzed=len(candidates),
        candidates=[candidates[s] for s in selected],
        excluded=excluded,
    ), ScreenSummary(
        first_stage=first, excluded=excluded, new_buys_allowed=not issues, issues=issues
    )
