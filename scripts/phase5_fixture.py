"""Explicit synthetic replay across separate manual Workflow runs, never real prices."""

import sys
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from trading_bot.cycle import TestProposal, run_cycle
from trading_bot.operations import Operation
from trading_bot.portfolio.models import FXRate
from trading_bot.storage import ParquetStore


def fixture(api, market, step, directory):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from tests.ai_helpers import JP, NOW, US, ai_settings, histories

    instrument = JP if market == "JP" else US
    account = "test-phase5-fixture-" + market.lower()
    op = Operation(api, account, f"synthetic-fixture:{market}:{step}")
    if step > 1 and not any(
        r["run_id"] == f"synthetic-fixture:{market}:{step - 1}" and r["state"] == "SUCCEEDED"
        for r in op.read()["runs"]
    ):
        raise ValueError("previous fixture step must complete in a separate operation")
    store = ParquetStore(Path(directory))
    for interval, bars in histories(instrument).items():
        store.save(instrument, interval, bars)
    base = histories(instrument)["1h"][-1]
    start = NOW.replace(hour=3, minute=30) if market == "JP" else NOW.replace(hour=13, minute=30)
    fill = NOW.replace(hour=5, minute=35) if market == "JP" else NOW.replace(hour=15, minute=35)
    clock = {
        1: NOW,
        2: fill,
        3: fill + timedelta(minutes=1),
        4: fill + timedelta(minutes=2),
        5: fill.replace(day=16, hour=1) if market == "JP" else fill.replace(hour=17),
    }[step]
    if step >= 2:
        store.save(
            instrument,
            "1h",
            [
                base.model_copy(
                    update={
                        "timestamp": start,
                        "fetched_at": start + timedelta(hours=1, minutes=1),
                        "open": 125.0,
                        "close": 125.0,
                        "high": 126.0,
                        "low": 124.0,
                    }
                )
            ],
        )
    if step >= 2:
        store.save(
            instrument,
            "1h",
            [
                base.model_copy(
                    update={
                        "timestamp": start + timedelta(hours=1),
                        "fetched_at": start + timedelta(hours=2, minutes=1),
                        "open": 125.0,
                        "close": 125.0,
                        "high": 126.0,
                        "low": 124.0,
                    }
                )
            ],
        )
    if step == 5:
        start = start.replace(day=16, hour=0) if market == "JP" else start.replace(hour=16)
        store.save(
            instrument,
            "1h",
            [
                base.model_copy(
                    update={
                        "timestamp": start,
                        "fetched_at": start + timedelta(hours=1, minutes=1),
                        "open": 130.0,
                        "close": 130.0,
                        "high": 131.0,
                        "low": 129.0,
                    }
                )
            ],
        )

    def prepare(broker):
        op.data("synthetic-fixture", {"synthetic": True, "step": step, "market": market})
        return (
            FXRate(usd_jpy=150, timestamp=clock, source="fixed-simulation")
            if market == "US"
            else None
        )

    action = {1: "BUY", 2: "HOLD", 3: "HOLD", 4: "SELL", 5: "HOLD"}[step]
    return run_cycle(
        op,
        store,
        [instrument],
        market,
        ai_settings(),
        kind="decision" if step in (1, 3, 4) else "execute",
        owner="fixture-replay",
        submit=True,
        strategy=TestProposal(action),
        prepare=prepare,
        clock=lambda: clock,
        initialization_at=NOW - timedelta(days=30),
        historical_replay=True,
        fund_jpy=Decimal("100000") if market == "US" and step == 1 else Decimal(0),
    )
