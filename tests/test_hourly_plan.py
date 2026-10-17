"""No market/AI connections: synthetic plans across regular, DST and short sessions."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from trading_bot.data_access import MarketAccessDisabled, require_live_market_access
from trading_bot.hourly_plan import FeatureProposal, USHourlyPlan, budget, hourly_features, slots
from trading_bot.persistence.decision_repository import DecisionRepository
from trading_bot.persistence.sqlite_repository import SQLiteRepository
from trading_bot.strategy.models import AISettings

FIVE = ("AAPL", "MSFT", "AMZN", "GOOGL", "META")
TEN = FIVE + ("NVDA", "TSLA", "JPM", "V", "COST")


def test_approved_ai_budget_preserves_existing_reservations(tmp_path):
    settings = AISettings.model_validate(
        yaml.safe_load(Path("config/ai.yaml").read_text(encoding="utf-8"))
    )
    assert (settings.daily_request_limit, settings.monthly_request_limit) == (8, 200)
    defaults = AISettings(model=settings.model, approved_free_models=[settings.model])
    assert defaults.daily_request_limit == 8 and defaults.monthly_request_limit == 200
    ledger = DecisionRepository(SQLiteRepository(tmp_path / "mock-budget.sqlite3"))
    for i in range(6):
        ledger.reserve(f"hour-{i}", settings)
    # Re-open like another runner: reservations survive; no reset or network call.
    fresh = DecisionRepository(SQLiteRepository(tmp_path / "mock-budget.sqlite3"))
    assert len(fresh.usage()) == 6
    fresh.reserve("optional-close", settings)
    fresh.reserve("health-reserve", settings)
    with pytest.raises(ValueError, match="quota exhausted"):
        fresh.reserve("ninth-call", settings)
    assert len(fresh.usage()) == 8


def test_dormant_configuration():
    plan = USHourlyPlan.model_validate(
        yaml.safe_load(Path("config/us-hourly-plan.yaml").read_text(encoding="utf-8"))
    )
    assert plan.mode == "mock" and plan.symbols == FIVE
    with pytest.raises(MarketAccessDisabled):
        require_live_market_access()


@pytest.mark.parametrize(
    "change",
    [
        {"mode": "live"},
        {"market": "JP"},
        {"decision_minutes": 15},
        {"symbols": FIVE[:4]},
        {"symbols": FIVE + FIVE},
        {"symbols": (*FIVE[:4], "7203.T")},
        {"live_enabled": True},
    ],
)
def test_unsafe_plan_rejected(change):
    with pytest.raises(ValidationError):
        USHourlyPlan.model_validate(change)


@pytest.mark.parametrize("symbols,expected,batches", [(FIVE, 273, (6,)), (TEN, 418, (7, 4))])
def test_credit_estimates(symbols, expected, batches):
    result = budget(USHourlyPlan(symbols=symbols), "2026-10-19")
    assert result.collection_slots == 26
    assert result.decision_requests == 6
    assert result.total_credits == expected and result.fits_basic_daily
    assert result.minute_batches == batches


@pytest.mark.parametrize(
    "day,collect,decisions,first",
    [
        ("2026-10-19", 26, 6, "13:45"),
        ("2026-11-02", 26, 6, "14:45"),
        ("2026-11-27", 14, 3, "14:45"),
        ("2026-12-25", 0, 0, None),
        ("2026-10-17", 0, 0, None),
    ],
)
def test_calendar(day, collect, decisions, first):
    schedule = slots(USHourlyPlan(), day)
    assert sum(s.kind == "collect" for s in schedule) == collect
    assert sum(s.kind == "decision" for s in schedule) == decisions
    assert len({s.run_id for s in schedule}) == len(schedule)
    if first:
        assert schedule[0].bar_end.strftime("%H:%M") == first
        assert all(s.due_at - s.bar_end == timedelta(minutes=15) for s in schedule)
    else:
        assert budget(USHourlyPlan(), day).total_credits == 0


def test_five_minute_ten_stocks_exceeds_daily_budget():
    assert not budget(USHourlyPlan(symbols=TEN, poll_minutes=5), "2026-10-19").fits_basic_daily


def test_negative_reserve_rejected():
    with pytest.raises(ValueError):
        budget(USHourlyPlan(), "2026-10-19", -1)


@pytest.mark.parametrize("symbols", [FIVE, TEN])
def test_mock_batched_hourly_decisions_and_repeated_slots(symbols):
    # A test double for future durable reservation, NOT a production memory fallback.
    completed = set()
    calls = []
    plan = USHourlyPlan(symbols=symbols)
    for day in ("2026-10-19", "2026-10-20"):
        for _repeated_workflow in range(2):
            for slot in slots(plan, day):
                if slot.run_id in completed:
                    continue
                if slot.kind == "decision":
                    calls.append(tuple((s, "HOLD") for s in plan.symbols))
                completed.add(slot.run_id)
    assert len(calls) == 12  # One batch/hour, not one request per symbol or per 15m poll.
    assert all(len(c) == len(symbols) for c in calls)


@pytest.mark.parametrize("field", ["price", "close", "volume", "cash", "fx", "quantity"])
def test_minimized_dto_rejects_raw_account_and_market_fields(field):
    with pytest.raises(ValidationError):
        FeatureProposal.model_validate(
            {
                "symbol": "AAPL",
                "return_1h": 0.01,
                "distance_sma20": 0.02,
                "volatility_ratio": 0.03,
                "portfolio_weight": 0.05,
                field: 123,
            }
        )


def test_minimized_dto_valid():
    proposal = FeatureProposal(
        symbol="AAPL",
        return_1h=0.01,
        distance_sma20=0.02,
        volatility_ratio=0.03,
        portfolio_weight=0.05,
    )
    assert set(proposal.model_dump()) == {
        "symbol",
        "return_1h",
        "distance_sma20",
        "volatility_ratio",
        "portfolio_weight",
    }


@pytest.mark.parametrize("failure", ["missing", "duplicate", "future", "nan", "naive", "order"])
def test_synthetic_hour_grid(failure):
    end = datetime(2026, 10, 19, 14, 30, tzinfo=UTC)
    ends = tuple(end - timedelta(minutes=n) for n in (45, 30, 15, 0))
    closes = (100.0, 101.0, 102.0, 103.0)
    assert hourly_features(ends, closes, end, end) == 103.0
    observed = end
    if failure == "missing":
        ends = ends[:-1]
    elif failure == "duplicate":
        ends = (*ends[:3], ends[2])
    elif failure == "future":
        observed -= timedelta(seconds=1)
    elif failure == "nan":
        closes = (100.0, float("nan"), 102.0, 103.0)
    elif failure == "naive":
        ends = tuple(t.replace(tzinfo=None) for t in ends)
    else:
        ends = tuple(reversed(ends))
    with pytest.raises(ValueError):
        hourly_features(ends, closes, end, observed)
