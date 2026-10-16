import csv
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from trading_bot.benchmark import (
    NAV,
    ManualCSVProvider,
    benchmark_value,
    import_nav,
    initialize_benchmark,
)
from trading_bot.broker.paper import PaperBroker
from trading_bot.dashboard import build_document, publish
from trading_bot.performance import maximum_drawdown, performance, snapshot
from trading_bot.persistence.sqlite_repository import SQLiteRepository
from trading_bot.portfolio.valuation import value_portfolio

START = datetime(2026, 10, 5, 0, tzinfo=UTC)
AT = datetime(2026, 10, 6, 12, tzinfo=UTC)


def nav(**changes):
    return NAV.model_validate(
        {
            "nav_date": "2026-10-06",
            "nav_jpy": "20000",
            "published_at": AT - timedelta(hours=1),
            "fetched_at": AT,
            "source_url": "https://www.am.mufg.jp/fund/253425.html",
            **changes,
        }
    )


class Provider:
    def __init__(self, *rows):
        self.rows = rows

    def observations(self):
        return list(self.rows)


@pytest.fixture
def repo(tmp_path):
    r = SQLiteRepository(tmp_path / "benchmark.sqlite3")
    PaperBroker(r).initialize(START)
    return r


@pytest.mark.parametrize(
    "changes",
    [
        {"nav_jpy": "0"},
        {"nav_jpy": "-1"},
        {"nav_jpy": "NaN"},
        {"nav_jpy": "Infinity"},
        {"fund_id": "ETF-2559"},
        {"source_url": "https://untrusted.invalid/253425"},
        {"published_at": datetime(2026, 10, 5, tzinfo=UTC)},
        {"fetched_at": START},
        {"nav_date": "2026-10-04"},
        {"distribution_jpy": "-1"},
        {"published_at": datetime(2026, 10, 6, 12)},
    ],
)
def test_invalid_nav_is_never_corrected_or_imported(changes):
    with pytest.raises((ValueError, ValidationError)):
        nav(**changes)


def test_nav_import_is_atomic_idempotent_and_revision_is_not_silent(repo):
    import_nav(repo, Provider(nav()))
    import_nav(repo, Provider(nav(fetched_at=AT + timedelta(hours=1))))
    with pytest.raises(ValueError, match="revision"):
        import_nav(repo, Provider(nav(nav_jpy="20001")))
    with repo.transaction() as tx:
        assert len(tx.records("benchmark_prices")) == 1
        assert NAV.model_validate_json(tx.records("benchmark_prices")[0]).nav_jpy == Decimal(
            "20000"
        )


def test_manual_official_source_csv_requires_full_provenance(repo, tmp_path):
    path = tmp_path / "official.csv"
    row = nav().model_dump(mode="json", exclude={"fetched_at"})
    row["derivative_publication_allowed"] = "false"
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(row))
        writer.writeheader()
        writer.writerow(row)
    assert import_nav(repo, ManualCSVProvider(path, AT)) == 1
    assert not ManualCSVProvider(path, AT).observations()[0].derivative_publication_allowed
    path.write_text("fund_id,nav_jpy\n253425,20000\n", encoding="utf-8")
    with pytest.raises(ValueError):
        import_nav(repo, ManualCSVProvider(path, AT))


def test_ten_thousand_units_same_capital_no_double_management_fee(repo):
    import_nav(repo, Provider(nav()))
    investment = initialize_benchmark(repo, date(2026, 10, 6), AT)
    assert investment.units == 500000
    assert investment.invested_jpy == Decimal("1000000")
    assert investment.residual_jpy == 0
    assert initialize_benchmark(repo, date(2026, 10, 6), AT + timedelta(days=1)) == investment
    account = PaperBroker(repo).get_balance()
    value, _, warning = benchmark_value(investment, [nav()], account, AT)
    assert value == Decimal("1000000") and warning is None
    with pytest.raises(ValueError):
        initialize_benchmark(repo, date(2026, 10, 7), AT)


def test_integer_fund_units_leave_cash_residual(repo):
    import_nav(repo, Provider(nav(nav_jpy="33333")))
    investment = initialize_benchmark(repo, date(2026, 10, 6), AT)
    assert investment.invested_jpy + investment.residual_jpy == Decimal("1000000")
    assert 0 <= investment.residual_jpy < Decimal("3.3333")


def test_missing_purchase_nav_never_uses_previous_day(repo):
    import_nav(repo, Provider(nav()))
    with pytest.raises(ValueError, match="missing"):
        initialize_benchmark(
            repo, date(2026, 10, 7), AT + timedelta(days=1), accepted_on=date(2026, 10, 6)
        )
    with pytest.raises(ValueError, match="follow"):
        initialize_benchmark(repo, date(2026, 10, 5), AT)


def test_same_valuation_day_and_publication_are_required(repo):
    import_nav(repo, Provider(nav()))
    investment = initialize_benchmark(repo, date(2026, 10, 6), AT)
    account = PaperBroker(repo).get_balance()
    for when, expected in [
        (AT - timedelta(hours=2), "benchmark_investment_not_available_at_evaluation"),
        (AT + timedelta(days=1), "benchmark_same_date_nav_unavailable"),
    ]:
        value, _, warning = benchmark_value(investment, [nav()], account, when)
        assert value is None and warning == expected
    assert benchmark_value(None, [], account, AT)[0] is None


def test_distributions_do_not_silently_disappear(repo):
    import_nav(repo, Provider(nav()))
    investment = initialize_benchmark(repo, date(2026, 10, 6), AT)
    value, _, warning = benchmark_value(
        investment, [nav(distribution_jpy="100")], PaperBroker(repo).get_balance(), AT
    )
    assert value is None and "distribution" in warning


def test_snapshot_deduplication_comparison_and_revision_audit(repo):
    account = PaperBroker(repo).get_balance()
    valuation = value_portfolio(account, {}, None, AT, PaperBroker(repo).rules)
    initial = snapshot(repo, valuation, AT)
    assert initial.bot_jpy == Decimal("1000000") and initial.benchmark_jpy is None
    assert not initial.complete
    assert snapshot(repo, valuation, AT + timedelta(minutes=1)) == initial
    import_nav(repo, Provider(nav()))
    initialize_benchmark(repo, date(2026, 10, 6), AT)
    with pytest.raises(ValueError, match="audit reason"):
        snapshot(repo, valuation, AT)
    revised = snapshot(repo, valuation, AT, "NAV published and imported")
    assert revised.complete and revised.revision == 2
    assert revised.bot_return == revised.benchmark_return == revised.excess_return == 0
    with repo.transaction() as tx:
        assert len(tx.records("snapshot_revisions")) == 1
        assert len(tx.records("snapshots")) == 1


def test_cumulative_and_excess_returns_are_decimal(repo):
    import_nav(repo, Provider(nav()))
    initialize_benchmark(repo, date(2026, 10, 6), AT)
    later = AT + timedelta(days=1)
    other = nav(
        nav_date=date(2026, 10, 7),
        nav_jpy="22000",
        published_at=later - timedelta(hours=1),
        fetched_at=later,
    )
    import_nav(repo, Provider(other))
    account = PaperBroker(repo).get_balance()
    value = snapshot(
        repo, value_portfolio(account, {}, None, later, PaperBroker(repo).rules), later
    )
    assert value.benchmark_jpy == Decimal("1100000")
    assert value.benchmark_return == Decimal(".1")
    assert value.excess_return == Decimal("-.1")
    assert performance(account, [value], [])["win_rate"] is None


@pytest.mark.parametrize(
    "values,result",
    [
        ([], None),
        (["100"], "0"),
        (["100", "120", "90", "130", "80"], "-0.3846153846153846153846153846"),
        (["100", "80", "90"], "-0.2"),
    ],
)
def test_maximum_drawdown(values, result):
    answer = maximum_drawdown([Decimal(v) for v in values])
    assert answer == (Decimal(result) if result is not None else None)


@pytest.mark.parametrize("value", ["0", "-1", "NaN", "Infinity"])
def test_invalid_drawdown_series(value):
    with pytest.raises(ValueError):
        maximum_drawdown([Decimal(value)])


def test_public_projection_excludes_unlicensed_nav_and_internal_data(repo):
    import_nav(repo, Provider(nav()))
    initialize_benchmark(repo, date(2026, 10, 6), AT)
    account = PaperBroker(repo).get_balance()
    snapshot(repo, value_portfolio(account, {}, None, AT, PaperBroker(repo).rules), AT)
    document = publish(repo, AT)
    assert document.state == "PREPARING" and document.trades == []
    assert document.summary["benchmark_jpy"] is None
    assert document.series[0]["benchmark_jpy"] is None
    raw = document.model_dump_json()
    for forbidden in [
        "nav_jpy",
        "source_url",
        "raw_response",
        "input_hash",
        "cash_ledger",
        "SUPABASE",
    ]:
        assert forbidden not in raw
    assert build_document(repo, AT).summary["bot_jpy"] == "1000000"


def test_authorized_derived_benchmark_can_be_public(repo):
    import_nav(repo, Provider(nav(derivative_publication_allowed=True)))
    initialize_benchmark(repo, date(2026, 10, 6), AT)
    account = PaperBroker(repo).get_balance()
    snapshot(repo, value_portfolio(account, {}, None, AT, PaperBroker(repo).rules), AT)
    assert build_document(repo, AT).summary["benchmark_jpy"] == "1000000"


def test_authorizing_latest_nav_does_not_authorize_purchase_nav(repo):
    import_nav(repo, Provider(nav()))
    initialize_benchmark(repo, date(2026, 10, 6), AT)
    later = AT + timedelta(days=1)
    import_nav(
        repo,
        Provider(
            nav(
                nav_date=date(2026, 10, 7),
                published_at=later,
                fetched_at=later,
                derivative_publication_allowed=True,
            )
        ),
    )
    broker = PaperBroker(repo)
    snapshot(repo, value_portfolio(broker.get_balance(), {}, None, later, broker.rules), later)
    document = build_document(repo, later)
    for key in ["benchmark_jpy", "benchmark_return", "excess_return", "excess_pnl_jpy"]:
        assert document.summary[key] is None
    assert document.series[0]["benchmark_jpy"] is None


def test_snapshot_rejects_arbitrary_tampered_or_future_valuation(repo):
    account = PaperBroker(repo).get_balance()
    valuation = value_portfolio(account, {}, None, AT, PaperBroker(repo).rules)
    with pytest.raises(ValueError):
        snapshot(repo, valuation, AT - timedelta(seconds=1))
    with pytest.raises(ValueError):
        snapshot(repo, valuation.model_copy(update={"total_jpy": Decimal("2000000")}), AT)
