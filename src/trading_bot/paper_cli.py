"""Explicit local-only paper commands; no order is filled by buy/sell."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import cast

import yaml
from pydantic import BaseModel

from trading_bot.broker.execution import known_marks
from trading_bot.broker.paper import PaperBroker
from trading_bot.config import Instrument, load_settings, load_universe
from trading_bot.persistence.sqlite_repository import SQLiteRepository
from trading_bot.portfolio.models import Currency, FXRate, Side, TradingRules
from trading_bot.storage import ParquetStore


def add_parser(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    paper = commands.add_parser("paper", help="local simulated cash account")
    paper.add_argument("--db", type=Path, default=Path("data/paper/account.sqlite3"))
    paper.add_argument("--rules", type=Path, default=Path("config/paper.yaml"))
    actions = paper.add_subparsers(dest="paper_action", required=True)
    for name in ("init", "balance", "positions", "orders", "history"):
        command = actions.add_parser(name)
        if name == "init":
            command.add_argument("--at", type=datetime.fromisoformat)
    for side in ("buy", "sell"):
        order = actions.add_parser(side)
        order.add_argument("--symbol", required=True)
        order.add_argument("--quantity", required=True, type=int)
        order.add_argument("--at", type=datetime.fromisoformat)
        order.add_argument("--order-id")
    cancel = actions.add_parser("cancel")
    cancel.add_argument("--order-id", required=True)
    for name in ("execute", "value", "fx"):
        command = actions.add_parser(name)
        command.add_argument("--at", required=True, type=datetime.fromisoformat)
        command.add_argument("--usd-jpy", type=Decimal)
        command.add_argument("--rate-at", type=datetime.fromisoformat)
        if name == "execute":
            command.add_argument("--order-id", required=True)
        if name in ("execute", "value"):
            command.add_argument("--mode", choices=["historical", "paper"], required=True)
            command.add_argument("--interval", choices=["1h", "1d"], default="1h")
        else:
            command.add_argument("--from-currency", choices=["JPY", "USD"], required=True)
            command.add_argument("--amount", type=Decimal, required=True)
            command.add_argument("--exchange-id")


def show(value: object) -> None:
    if isinstance(value, BaseModel):
        print(value.model_dump_json(indent=2))
    elif isinstance(value, list):
        print(
            json.dumps(
                [json.loads(v.model_dump_json()) if isinstance(v, BaseModel) else v for v in value],
                indent=2,
            )
        )
    else:
        print(json.dumps(value, indent=2, default=str))


def run(args: argparse.Namespace) -> int:
    rules = TradingRules.model_validate(yaml.safe_load(args.rules.read_text(encoding="utf-8")))
    broker = PaperBroker(SQLiteRepository(args.db), rules)
    action = args.paper_action
    if action == "init":
        show(broker.initialize(args.at or datetime.now(UTC)))
        return 0
    if action == "history":
        show({key: [json.loads(row) for row in rows] for key, rows in broker.history().items()})
        return 0
    if action in ("balance", "positions", "orders"):
        show(
            {
                "balance": broker.get_balance,
                "positions": broker.get_positions,
                "orders": broker.get_orders,
                "history": broker.history,
            }[action]()
        )
        return 0
    if action == "cancel":
        show(broker.cancel_order(args.order_id))
        return 0
    universe = load_universe(args.universe)
    if action in ("buy", "sell"):
        instrument = next((i for i in universe if i.symbol == args.symbol), None)
        if instrument is None:
            raise ValueError("symbol is not configured in universe")
        order = broker.place_order(
            instrument,
            cast(Side, action.upper()),
            args.quantity,
            args.at or datetime.now(UTC),
            args.order_id,
        )
        show(order)
        return 1 if order.status == "REJECTED" else 0
    fx = None
    if args.usd_jpy is not None:
        if args.rate_at is None:
            raise ValueError("--rate-at is required with --usd-jpy")
        fx = FXRate(usd_jpy=args.usd_jpy, timestamp=args.rate_at, source="fixed-simulation")
        print(f"SIMULATION: fixed USD/JPY={fx.usd_jpy} at={fx.timestamp.isoformat()}")
    if action == "fx":
        if fx is None:
            raise ValueError("FX exchange requires explicit --usd-jpy and --rate-at")
        show(
            broker.exchange(
                cast(Currency, args.from_currency), args.amount, fx, args.at, args.exchange_id
            )
        )
        return 0
    settings = load_settings(args.settings)
    store = ParquetStore(settings.storage_dir)
    instruments: dict[str, Instrument] = {
        p.instrument.symbol: p.instrument for p in broker.get_positions()
    }
    if action == "execute":
        order = broker.get_order(args.order_id)
        instruments[order.symbol] = Instrument(
            symbol=order.symbol,
            market=order.market,
            currency=order.currency,
            display_name=order.symbol,
        )
    data = {s: store.read(i, args.interval) for s, i in instruments.items()}
    print(f"SIMULATION: mode={args.mode} as_of={args.at.isoformat()} interval={args.interval}")
    if action == "execute":
        order = broker.execute(args.order_id, data, fx, args.at, args.mode)
        show(order)
        executions = [e for e in broker.get_executions() if e.order_id == order.order_id]
        show(executions)
        return 1 if order.status == "REJECTED" else 0
    show(
        broker.value(known_marks(list(instruments.values()), data, args.at, args.mode), fx, args.at)
    )
    return 0
