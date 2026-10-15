"""Mock-by-default AI commands; real communication and paper submission are independent flags."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import yaml

from trading_bot.broker.paper import PaperBroker
from trading_bot.config import Interval, load_settings, load_universe
from trading_bot.orchestration.engine import DecisionEngine
from trading_bot.persistence.decision_repository import DecisionRepository
from trading_bot.persistence.sqlite_repository import SQLiteRepository
from trading_bot.portfolio.models import FXRate, TradingRules
from trading_bot.screening.models import ScreeningSettings
from trading_bot.screening.screener import screen
from trading_bot.storage import ParquetStore
from trading_bot.strategy.models import AISettings, DecisionRun
from trading_bot.strategy.security import redact


def add_parser(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    screening = commands.add_parser("screen")
    screening.add_argument("--market", required=True, choices=["JP", "US"])
    screening.add_argument("--at", type=datetime.fromisoformat)
    screening.add_argument("--strategy-config", type=Path, default=Path("config/strategy.yaml"))
    screening.add_argument("--db", type=Path, default=Path("data/paper/account.sqlite3"))
    ai = commands.add_parser("ai")
    actions = ai.add_subparsers(dest="ai_action", required=True)
    for name in ("history", "usage", "decide"):
        command = actions.add_parser(name)
        command.add_argument("--db", type=Path, default=Path("data/paper/account.sqlite3"))
        if name != "decide":
            continue
        command.add_argument("--market", required=True, choices=["JP", "US"])
        command.add_argument("--provider", choices=["mock", "gemini"], default="mock")
        command.add_argument("--at", type=datetime.fromisoformat)
        command.add_argument("--decision-id")
        command.add_argument("--strategy-config", type=Path, default=Path("config/strategy.yaml"))
        command.add_argument("--ai-config", type=Path, default=Path("config/ai.yaml"))
        command.add_argument("--paper-config", type=Path, default=Path("config/paper.yaml"))
        command.add_argument("--usd-jpy", type=Decimal)
        command.add_argument("--rate-at", type=datetime.fromisoformat)
        permission = command.add_mutually_exclusive_group()
        permission.add_argument("--dry-run", action="store_true")
        permission.add_argument("--submit-orders", action="store_true")


def summary(record: DecisionRun) -> dict[str, object]:
    context = record.context
    return {
        "decision_id": record.decision_id,
        "run_at": record.started_at.isoformat(),
        "as_of": record.as_of.isoformat(),
        "market": record.market,
        "provider": record.provider,
        "model": record.model,
        "status": record.status,
        "analyzed": context.screening.analyzed if context else 0,
        "candidates": [
            {"symbol": c.instrument.symbol, "score": c.score, "held": c.held, "issue": c.issue}
            for c in context.screening.candidates
        ]
        if context
        else [],
        "result": record.result.model_dump(mode="json") if record.result else None,
        "action_counts": {
            action: sum(d.action == action for d in record.result.decisions)
            for action in ("BUY", "SELL", "HOLD")
        }
        if record.result
        else {},
        "plan": record.plan.model_dump(mode="json") if record.plan else None,
        "registered_orders": [order.model_dump(mode="json") for order in record.registered_orders],
        "api_requests": len(record.usage),
        "usage": [usage.model_dump(mode="json") for usage in record.usage],
        "error": record.error,
        "registration_error": record.registration_error,
    }


def run(args: argparse.Namespace) -> int:
    repository = SQLiteRepository(args.db)
    decisions = DecisionRepository(repository)
    if args.command == "ai" and args.ai_action in ("history", "usage"):
        value = (
            [summary(record) for record in decisions.history()]
            if args.ai_action == "history"
            else [row.model_dump(mode="json") for row in decisions.usage()]
        )
        print(redact(json.dumps(value, ensure_ascii=False, indent=2)))
        return 0
    universe = load_universe(args.universe)
    settings = load_settings(args.settings)
    store = ParquetStore(settings.storage_dir)
    screening = ScreeningSettings.model_validate(
        yaml.safe_load(args.strategy_config.read_text(encoding="utf-8"))
    )
    as_of = args.at or datetime.now(UTC)
    if args.command == "screen":
        with repository.transaction() as tx:
            account = tx.account() if tx.initialized() else None
        instruments = {i.symbol: i for i in universe}
        held = set()
        if account:
            for position in account.positions.values():
                if position.quantity:
                    held.add(position.instrument.symbol)
                    instruments[position.instrument.symbol] = position.instrument
        intervals: list[Interval] = ["1d", "1h"]
        data = {
            s: {str(interval): store.read(i, interval) for interval in intervals}
            for s, i in instruments.items()
            if i.market == args.market
        }
        result = screen(list(instruments.values()), data, held, args.market, as_of, screening)
        print(result.model_dump_json(indent=2))
        return 0
    ai = AISettings.model_validate(yaml.safe_load(args.ai_config.read_text(encoding="utf-8")))
    rules = TradingRules.model_validate(
        yaml.safe_load(args.paper_config.read_text(encoding="utf-8"))
    )
    fx = None
    if args.usd_jpy is not None:
        if args.rate_at is None:
            raise ValueError("--rate-at is required for fixed simulation FX")
        fx = FXRate(usd_jpy=args.usd_jpy, timestamp=args.rate_at, source="fixed-simulation")
    if args.decision_id and args.at is None:
        saved = decisions.get(args.decision_id)
        if saved:
            as_of = saved.as_of
    engine = DecisionEngine(
        store, PaperBroker(repository, rules), decisions, universe, screening, ai
    )
    record = engine.run(args.market, as_of, fx, args.provider, args.submit_orders, args.decision_id)
    print(redact(json.dumps(summary(record), ensure_ascii=False, indent=2)))
    return 1 if record.status == "FAILED" else 0
