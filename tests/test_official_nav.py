"""Synthetic CSV fixtures only. No real NAV values or external requests in CI."""

import csv
import io
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from trading_bot.benchmark import (
    NAV,
    ApplicationCalendar,
    benchmark_value,
    import_nav,
    initialize_benchmark,
    validate_application,
)
from trading_bot.broker.paper import PaperBroker
from trading_bot.cli import main
from trading_bot.dashboard import build_document
from trading_bot.official_nav import HEADER, OfficialCSVProvider, compare_historical_nav
from trading_bot.performance import snapshot
from trading_bot.persistence.sqlite_repository import SQLiteRepository
from trading_bot.portfolio.valuation import value_portfolio

AT = datetime(2026, 10, 7, 12, tzinfo=UTC)
START = datetime(2026, 10, 5, 0, tzinfo=UTC)
CALENDAR = ApplicationCalendar(
    valid_from=date(2026, 10, 1),
    valid_to=date(2026, 10, 31),
    closed_dates=[date(2026, 10, 1), date(2026, 10, 19)],
    source_url="https://www.am.mufg.jp/fund/253425.html",
    observed_at=AT,
)


def file(
    tmp_path, rows=None, title="ｅＭＡＸＩＳ Ｓｌｉｍ 全世界株式（オール・カントリー）", header=None
):
    """Invented values, following only the official file's schema."""
    stream = io.StringIO(newline="")
    writer = csv.writer(stream)
    writer.writerow([title])
    writer.writerow(HEADER if header is None else header)
    writer.writerows(
        [
            ["2026/10/06", "20000", "20000", "", "1.00"],
            ["2026/10/07", "22000", "22000", "", "1.10"],
        ]
        if rows is None
        else rows
    )
    path = tmp_path / "synthetic-official-format.csv"
    path.write_bytes(stream.getvalue().encode("cp932"))
    return path


def comparison(values, **changes):
    return compare_historical_nav(
        values,
        **{
            "started_at": START,
            "accepted_on": date(2026, 10, 5),
            "purchase_date": date(2026, 10, 6),
            "valuation_date": date(2026, 10, 7),
            "application_calendar": CALENDAR,
            **changes,
        },
    )


def test_official_units_raw_not_reinvested_and_unknown_publication(tmp_path):
    path = file(tmp_path, rows=[["2026/10/06", "20000", "21000", "", "1"]])
    value = OfficialCSVProvider(path, AT).observations()[0]
    assert value.nav_jpy == 20000 and value.units_per_nav == 10000
    assert value.published_at == value.fetched_at == AT
    assert value.publication_time_basis == "observed_by"
    assert value.source_kind == "official_csv" and len(value.source_sha256) == 64
    assert value.distribution_jpy == 0 and not value.derivative_publication_allowed


@pytest.mark.parametrize(
    "row",
    [
        ["2026/10/06", "NaN", "20000", "", "1"],
        ["2026/10/06", "Infinity", "20000", "", "1"],
        ["2026/10/06", "-1", "20000", "", "1"],
        ["2026/10/06", "0", "20000", "", "1"],
        ["2026/10/06", "", "20000", "", "1"],
        ["2026/10/06", "20000.1", "20000", "", "1"],
        ["2026/10/06", "20000", "NaN", "", "1"],
        ["2026/10/06", "20000", "20000", "-1", "1"],
        ["2026/10/06", "20000", "20000", "", "-1"],
        ["2026/10/06", "20000", "20000", "", "Infinity"],
        ["2026/10/06", "20000"],
        ["2026/10/6", "20000", "20000", "", "1"],
        ["2026/10/04", "20000", "20000", "", "1"],
        ["2026/10/08", "20000", "20000", "", "1"],
    ],
)
def test_bad_official_rows_are_rejected(tmp_path, row):
    with pytest.raises(ValueError):
        OfficialCSVProvider(file(tmp_path, [row]), AT).observations()


@pytest.mark.parametrize(
    "kwargs", [{"title": "Other fund"}, {"header": ["date", "price"]}, {"rows": []}]
)
def test_identity_schema_and_empty_file(tmp_path, kwargs):
    with pytest.raises(ValueError):
        OfficialCSVProvider(file(tmp_path, **kwargs), AT).observations()


@pytest.mark.parametrize("dates", [["2026/10/06", "2026/10/06"], ["2026/10/07", "2026/10/06"]])
def test_duplicate_unsorted_dates(tmp_path, dates):
    with pytest.raises(ValueError):
        OfficialCSVProvider(
            file(tmp_path, [[d, "20000", "20000", "", "1"] for d in dates]), AT
        ).observations()


def test_import_preserves_first_observation_and_hash_when_file_grows(tmp_path):
    repo = SQLiteRepository(tmp_path / "private.sqlite3")
    PaperBroker(repo).initialize(START)
    path = file(tmp_path, rows=[["2026/10/06", "20000", "20000", "", "1"]])
    import_nav(repo, OfficialCSVProvider(path, AT))
    with repo.transaction() as tx:
        original = tx.get("benchmark_prices", "2026-10-06")
    path = file(tmp_path)
    import_nav(repo, OfficialCSVProvider(path, AT + timedelta(hours=1)))
    with repo.transaction() as tx:
        assert tx.get("benchmark_prices", "2026-10-06") == original
        assert len(tx.records("benchmark_prices")) == 2
    file(tmp_path, rows=[["2026/10/06", "20001", "20001", "", "1"]])
    with pytest.raises(ValueError, match="revision"):
        import_nav(repo, OfficialCSVProvider(path, AT + timedelta(hours=2)))
    with repo.transaction() as tx:
        assert tx.get("benchmark_prices", "2026-10-06") == original


def test_bad_later_row_import_is_atomic(tmp_path):
    repo = SQLiteRepository(tmp_path / "private.sqlite3")
    PaperBroker(repo).initialize(START)
    path = file(
        tmp_path,
        rows=[
            ["2026/10/06", "20000", "20000", "", "1"],
            ["2026/10/07", "NaN", "20000", "", "1"],
        ],
    )
    with pytest.raises(ValueError):
        import_nav(repo, OfficialCSVProvider(path, AT))
    with repo.transaction() as tx:
        assert tx.records("benchmark_prices") == []


def test_comparison_equal_capital_integer_units_cash_and_fees(tmp_path):
    values = OfficialCSVProvider(file(tmp_path), AT).observations()
    result = comparison(values)
    assert result.units == 500000 and result.residual_jpy == 0
    assert result.initial_jpy == result.cash_scenario_jpy == Decimal("1000000")
    assert result.benchmark_jpy == Decimal("1100000")
    assert result.benchmark_return == Decimal("0.1")
    assert result.cash_scenario_return == 0 and result.excess_cash_return == Decimal("-0.1")
    assert "retrospective" in result.mode and not result.derivative_publication_allowed
    assert result.observed_at == AT
    fractional = comparison(values, initial_jpy=Decimal("1000001"))
    assert fractional.residual_jpy == 1


@pytest.mark.parametrize(
    "changes",
    [
        {"valuation_date": date(2026, 10, 8)},
        {"valuation_date": date(2026, 10, 5)},
        {"initial_jpy": Decimal("NaN")},
        {"initial_jpy": Decimal(0)},
        {"initial_jpy": Decimal("0.01")},
        {"accepted_on": date(2026, 10, 4)},
        {"purchase_date": date(2026, 10, 7)},
    ],
)
def test_invalid_comparison(tmp_path, changes):
    with pytest.raises(ValueError):
        comparison(OfficialCSVProvider(file(tmp_path), AT).observations(), **changes)


def test_distribution_and_unknown_sources_cannot_be_silently_compared(tmp_path):
    values = OfficialCSVProvider(file(tmp_path), AT).observations()
    for altered in [
        values + [values[0]],
        [values[0], values[1].model_copy(update={"distribution_jpy": Decimal(10)})],
        [v.model_copy(update={"source_kind": "manual_declared"}) for v in values],
    ]:
        with pytest.raises(ValueError):
            comparison(altered)


def test_real_csv_no_lookahead_and_public_derivatives_remain_hidden(tmp_path):
    repo = SQLiteRepository(tmp_path / "private.sqlite3")
    broker = PaperBroker(repo)
    account = broker.initialize(START)
    values = OfficialCSVProvider(file(tmp_path), AT).observations()
    import_nav(repo, OfficialCSVProvider(file(tmp_path), AT))
    with pytest.raises(ValueError, match="reviewed"):
        initialize_benchmark(repo, date(2026, 10, 6), AT)
    investment = initialize_benchmark(
        repo,
        date(2026, 10, 6),
        AT,
        accepted_on=date(2026, 10, 5),
        application_calendar=CALENDAR,
    )
    assert benchmark_value(investment, values, account, AT - timedelta(hours=1))[0] is None
    assert benchmark_value(investment, values, account, AT)[0] == Decimal("1100000")
    snapshot(repo, value_portfolio(account, {}, None, AT, broker.rules), AT)
    doc = build_document(repo, AT)
    assert doc.summary["benchmark_jpy"] is None
    assert doc.summary["excess_return"] is None
    assert "source_sha256" not in doc.model_dump_json()


@pytest.mark.parametrize(
    "start,accepted,purchase,valid",
    [
        ("2026-10-16T06:29:59+00:00", "2026-10-16", "2026-10-19", True),
        ("2026-10-16T06:30:00+00:00", "2026-10-16", "2026-10-19", False),
        ("2026-10-16T14:36:07+00:00", "2026-10-19", "2026-10-20", True),
        ("2026-10-16T14:36:07+00:00", "2026-10-16", "2026-10-19", False),
        ("2026-10-17T00:00:00+00:00", "2026-10-19", "2026-10-20", True),
        ("2026-10-09T07:00:00+00:00", "2026-10-13", "2026-10-14", True),
    ],
)
def test_cutoff_weekend_and_domestic_holiday_boundaries(start, accepted, purchase, valid):
    from trading_bot.benchmark import validate_application

    args = datetime.fromisoformat(start), date.fromisoformat(purchase), date.fromisoformat(accepted)
    if valid:
        validate_application(*args)
    else:
        with pytest.raises(ValueError):
            validate_application(*args)


def test_local_compare_cli_never_opens_or_modifies_account(tmp_path, capsys):
    path = file(tmp_path)
    db = tmp_path / "must-not-create.sqlite3"
    calendar = tmp_path / "reviewed-calendar.json"
    calendar.write_text(CALENDAR.model_dump_json(), encoding="utf-8")
    args = [
        "benchmark",
        "compare",
        "--csv",
        str(path),
        "--csv-format",
        "mufg",
        "--db",
        str(db),
        "--started-at",
        START.isoformat(),
        "--accepted-on",
        "2026-10-05",
        "--purchase-date",
        "2026-10-06",
        "--valuation-date",
        "2026-10-07",
    ]
    args += ["--application-calendar", str(calendar)]
    assert main(args) == 0
    assert "retrospective_real_nav_vs_cash_scenario" in capsys.readouterr().out
    assert not db.exists()
    assert main(args + ["--backend", "supabase"]) == 2
    assert not db.exists()


def test_observed_bound_cannot_be_backdated(tmp_path):
    nav = OfficialCSVProvider(file(tmp_path), AT).observations()[0]
    with pytest.raises(ValueError, match="availability"):
        NAV.model_validate({**nav.model_dump(), "published_at": AT - timedelta(hours=1)})


def test_fund_nav_on_exchange_outage_is_valid_but_bank_holidays_are_not(tmp_path):
    # Invented price: the actual defect is confusing an exchange outage with a bank holiday.
    path = file(tmp_path, [["2020/10/01", "20000", "20000", "", "1"]])
    assert OfficialCSVProvider(path, AT).observations()[0].nav_date == date(2020, 10, 1)
    for day in ["2019/05/01", "2026/10/12", "2018/10/30"]:
        with pytest.raises(ValueError):
            OfficialCSVProvider(
                file(tmp_path, [[day, "20000", "20000", "", "1"]]), AT
            ).observations()


def test_reviewed_calendar_blocks_foreign_holiday_and_missing_coverage():
    start = datetime(2026, 10, 16, 14, 36, tzinfo=UTC)
    validate_application(start, date(2026, 10, 21), date(2026, 10, 20), CALENDAR)
    with pytest.raises(ValueError, match="first eligible"):
        validate_application(start, date(2026, 10, 20), date(2026, 10, 19), CALENDAR)
    with pytest.raises(ValueError, match="cover"):
        validate_application(start, date(2026, 11, 2), date(2026, 10, 30), CALENDAR)
    short = CALENDAR.model_copy(update={"shortened_dates": [date(2026, 10, 16)]})
    start = datetime(2026, 10, 16, 2, 30, tzinfo=UTC)
    with pytest.raises(ValueError, match="first eligible"):
        validate_application(start, date(2026, 10, 19), date(2026, 10, 16), short)
    validate_application(start, date(2026, 10, 21), date(2026, 10, 20), short)
