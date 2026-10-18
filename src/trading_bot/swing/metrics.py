"""Explicit sampling and undefined-statistic handling; never manufacture annual returns."""

from collections.abc import Sequence
from datetime import datetime
from math import sqrt

import numpy as np


def metrics(
    daily_equity: Sequence[float],
    intraday_equity: Sequence[float],
    realized: Sequence[float],
    holding_sessions: Sequence[int],
    costs_jpy: float,
    fills: int,
    crash_returns: Sequence[float],
) -> dict[str, object]:
    equity = np.asarray(daily_equity, dtype=float)
    path = np.asarray(intraday_equity, dtype=float)
    if len(equity) < 2 or not np.isfinite(equity).all() or (equity <= 0).any():
        raise ValueError("need positive finite equity observations")
    if len(path) < 2 or not np.isfinite(path).all() or (path <= 0).any():
        raise ValueError("invalid intraday equity")
    r = equity[1:] / equity[:-1] - 1
    periods = len(r)
    sd = float(r.std(ddof=1)) if periods > 1 else 0
    downside = float(np.sqrt(np.mean(np.minimum(r, 0) ** 2)))
    gains = sum(p for p in realized if p > 0)
    losses = -sum(p for p in realized if p < 0)
    return {
        "cumulative_return": float(equity[-1] / equity[0] - 1),
        "annualized_return": float((equity[-1] / equity[0]) ** (252 / periods) - 1)
        if periods >= 252
        else None,
        "max_drawdown": float(np.min(path / np.maximum.accumulate(path) - 1)),
        "volatility_annualized": sd * sqrt(252) if periods > 1 else None,
        "sharpe_zero_risk_free": float(r.mean() / sd * sqrt(252)) if sd else None,
        "sortino_zero_risk_free": float(r.mean() / downside * sqrt(252)) if downside else None,
        "win_rate": sum(p > 0 for p in realized) / len(realized) if realized else None,
        "profit_factor": gains / losses if losses else None,
        "profit_factor_undefined_no_losses": losses == 0,
        "average_holding_sessions": float(np.mean(holding_sessions)) if holding_sessions else None,
        "annual_trade_count": fills * 252 / periods if periods >= 252 else None,
        "fills": fills,
        "closed_trades": len(realized),
        "costs_jpy": costs_jpy,
        "worst_crash_session_return": min(crash_returns) if crash_returns else None,
        "evaluation_sessions": periods,
        "sampling": "daily-close returns; maximum drawdown uses every observed intraday valuation",
        "limitations": "short samples are descriptive, not evidence of profitability",
    }


def chronological_split(
    dates: Sequence[datetime],
    training_end: datetime,
    evaluation_start: datetime,
) -> tuple[tuple[datetime, ...], tuple[datetime, ...]]:
    if training_end >= evaluation_start or list(dates) != sorted(set(dates)):
        raise ValueError("overlapping or unordered training/evaluation split")
    train = tuple(t for t in dates if t <= training_end)
    test = tuple(t for t in dates if t >= evaluation_start)
    if not train or not test:
        raise ValueError("empty training or evaluation period")
    return train, test


def walk_forward_windows(
    length: int, training: int = 201, evaluation: int = 20
) -> tuple[tuple[int, int, int], ...]:
    if training < 201 or evaluation < 1 or length <= training:
        raise ValueError("invalid walk-forward lengths")
    return tuple(
        (0, start, min(start + evaluation, length)) for start in range(training, length, evaluation)
    )
