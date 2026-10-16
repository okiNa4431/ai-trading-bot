"""Explicit persistence, benchmark and publication commands. No scheduler or live broker."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import cast

import yaml

from trading_bot.benchmark import ManualCSVProvider, import_nav, initialize_benchmark
from trading_bot.broker.execution import known_marks
from trading_bot.broker.paper import PaperBroker
from trading_bot.config import Market, load_settings, load_universe
from trading_bot.dashboard import publish
from trading_bot.orchestration.engine import DecisionEngine
from trading_bot.performance import Snapshot, performance, snapshot
from trading_bot.persistence.decision_repository import DecisionRepository
from trading_bot.persistence.sqlite_repository import PortfolioRepository, SQLiteRepository
from trading_bot.persistence.supabase_repository import SupabaseAPI, SupabaseRepository
from trading_bot.portfolio.models import CashEntry, Execution, FXRate
from trading_bot.screening.models import ScreeningSettings
from trading_bot.storage import ParquetStore
from trading_bot.strategy.models import AISettings
from trading_bot.strategy.security import redact


def options(command: argparse.ArgumentParser) -> None:
    command.add_argument("--backend", choices=["sqlite", "supabase"], default="sqlite")
    command.add_argument("--db", type=Path, default=Path("data/paper/account.sqlite3"))
    command.add_argument("--account", default="paper-live")
    command.add_argument("--test-account", action="store_true")


def add_parser(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    for name, actions in {
        "db": ["status", "verify", "initialize", "delete-test"],
        "benchmark": ["initialize", "update", "status"],
        "portfolio": ["snapshot", "performance"],
        "dashboard": ["publish"],
    }.items():
        group = commands.add_parser(name)
        sub = group.add_subparsers(dest="phase4_action", required=True)
        for action in actions:
            command = sub.add_parser(action)
            options(command)
            if name == "benchmark" and action == "initialize":
                command.add_argument("--purchase-date", type=date.fromisoformat, required=True)
            if name == "benchmark" and action == "update":
                command.add_argument("--csv", type=Path, required=True)
            if name == "db" and action == "delete-test":
                command.add_argument("--confirm-account", required=True)
            if name == "portfolio" and action == "snapshot":
                command.add_argument("--at", type=datetime.fromisoformat)
                command.add_argument("--usd-jpy", type=Decimal)
                command.add_argument("--rate-at", type=datetime.fromisoformat)
                command.add_argument("--correction-reason")
    operate = commands.add_parser("operate", help="persistent manual AI decision, dry-run fixed")
    options(operate)
    operate.add_argument("--market", choices=["JP", "US"], required=True)
    operate.add_argument("--provider", choices=["mock", "gemini"], default="mock")
    operate.add_argument("--decision-id")


def repository(args: argparse.Namespace) -> PortfolioRepository:
    if args.backend == "supabase":
        return SupabaseRepository(SupabaseAPI.environment(), args.account, test=args.test_account)
    if args.test_account or args.account != "paper-live":
        raise ValueError("SQLite isolation is by --db path; named account scopes require Supabase")
    return SQLiteRepository(args.db)


def verify(repository: PortfolioRepository) -> dict[str, object]:
    with repository.transaction() as tx:
        account = tx.account()
        entries = [CashEntry.model_validate_json(s) for s in tx.records("cash_ledger")]
        executions = [Execution.model_validate_json(s) for s in tx.records("executions")]
        for c in ("JPY", "USD"):
            if (
                sum((e.amount for e in entries if e.currency == c), Decimal(0))
                != account.balances[c]
            ):
                raise ValueError("cash ledger reconciliation failed")
        quantities: dict[str, int] = {}
        for e in executions:
            quantities[e.symbol] = quantities.get(e.symbol, 0) + (
                e.quantity if e.side == "BUY" else -e.quantity
            )
        if {s: q for s, q in quantities.items() if q} != {
            s: p.quantity for s, p in account.positions.items() if p.quantity
        }:
            raise ValueError("position reconciliation failed")
        return {
            "verified": True,
            "cash_entries": len(entries),
            "executions": len(executions),
            "orders": len(tx.records("orders")),
            "decisions": len(tx.records("decisions")),
            "api_requests": len(tx.records("api_usage")),
        }


def run(args: argparse.Namespace) -> int:
    repo = repository(args)
    broker = PaperBroker(repo)
    now = datetime.now(UTC)
    result: object
    action = getattr(args, "phase4_action", None)
    if args.command == "db":
        if action == "initialize":
            result = broker.initialize(now).model_dump(mode="json")
        elif action == "verify":
            result = verify(repo)
        elif action == "delete-test":
            if not isinstance(repo, SupabaseRepository):
                raise ValueError("test deletion only supports explicitly scoped Supabase accounts")
            repo.delete_test(args.confirm_account)
            result = {"deleted_test_account": args.account}
        else:
            with repo.transaction() as tx:
                result = {
                    "initialized": tx.initialized(),
                    "account": tx.account().model_dump(mode="json") if tx.initialized() else None,
                }
    elif args.command == "benchmark":
        if action == "update":
            result = {"imported": import_nav(repo, ManualCSVProvider(args.csv, now))}
        elif action == "initialize":
            result = initialize_benchmark(repo, args.purchase_date, now).model_dump(mode="json")
        else:
            with repo.transaction() as tx:
                result = {
                    "prices": [json.loads(s) for s in tx.records("benchmark_prices")],
                    "investment": [json.loads(s) for s in tx.records("benchmark_investments")],
                }
    elif args.command == "portfolio":
        if action == "snapshot":
            at = args.at or now
            account = broker.get_balance()
            universe = [p.instrument for p in account.positions.values() if p.quantity]
            store = ParquetStore(load_settings(args.settings).storage_dir)
            marks = known_marks(
                universe, {i.symbol: store.read(i, "1h") for i in universe}, at, "paper"
            )
            fx = (
                FXRate(usd_jpy=args.usd_jpy, timestamp=args.rate_at, source="observed")
                if (args.usd_jpy is not None and args.rate_at is not None)
                else None
            )
            from trading_bot.portfolio.valuation import value_portfolio

            valuation = value_portfolio(account, marks, fx, at, broker.rules)
            result = snapshot(repo, valuation, now, args.correction_reason).model_dump(mode="json")
        else:
            with repo.transaction() as tx:
                result = performance(
                    tx.account(),
                    [Snapshot.model_validate_json(s) for s in tx.records("snapshots")],
                    [Execution.model_validate_json(s) for s in tx.records("executions")],
                )
    elif args.command == "dashboard":
        result = publish(repo, now).model_dump(mode="json")
    else:
        universe = load_universe(args.universe)
        settings = load_settings(args.settings)
        ai = AISettings.model_validate(
            yaml.safe_load(Path("config/ai.yaml").read_text(encoding="utf-8"))
        )
        screening = ScreeningSettings.model_validate(
            yaml.safe_load(Path("config/strategy.yaml").read_text(encoding="utf-8"))
        )
        engine = DecisionEngine(
            ParquetStore(settings.storage_dir),
            broker,
            DecisionRepository(repo),
            universe,
            screening,
            ai,
        )
        from trading_bot.ai_cli import summary

        record = engine.run(
            cast(Market, args.market),
            now,
            provider=args.provider,
            submit=False,
            decision_id=args.decision_id,
        )
        result = summary(record)
        print(redact(json.dumps(result, ensure_ascii=False, default=str)))
        return 0 if record.status == "SUCCESS" else 1
    print(redact(json.dumps(result, ensure_ascii=False, default=str)))
    return 0
