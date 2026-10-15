"""Command line entry point."""

import argparse
import logging
import sqlite3
import time
from pathlib import Path

import yaml
from pydantic import ValidationError

from trading_bot import ai_cli, paper_cli
from trading_bot.config import load_settings, load_universe
from trading_bot.provider import YahooFinanceProvider
from trading_bot.service import collect
from trading_bot.storage import ParquetStore


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="AI vs オルカン: Phase 1 market data")
    parser.add_argument("--settings", type=Path, default=Path("config/settings.yaml"))
    parser.add_argument("--universe", type=Path, default=Path("config/universe.yaml"))
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("fetch", "update"):
        command = commands.add_parser(name)
        command.add_argument("--market", choices=["JP", "US", "ALL"], default="ALL")
        command.add_argument("--symbol", help="one configured Yahoo symbol")
        command.add_argument("--interval", choices=["1d", "1h"])
    commands.add_parser("status")
    paper_cli.add_parser(commands)
    ai_cli.add_parser(commands)
    args = parser.parse_args(argv)
    try:
        settings = load_settings(args.settings)
        logging.basicConfig(
            level=settings.log_level, format="%(asctime)sZ %(levelname)s %(message)s"
        )
        logging.Formatter.converter = time.gmtime
        # Prevent upstream debug logging of HTTP headers/cookies.
        logging.getLogger("yfinance").setLevel(logging.CRITICAL)
        logging.getLogger("curl_cffi").setLevel(logging.CRITICAL)
        if args.command == "paper":
            return paper_cli.run(args)
        if args.command in ("screen", "ai"):
            return ai_cli.run(args)
        store = ParquetStore(settings.storage_dir)
        if args.command == "status":
            print("symbol\tmarket\tinterval\tcount\toldest\tlatest")
            for row in store.status():
                print("\t".join(str(value) for value in row.values()))
            return 0
        instruments = [
            i
            for i in load_universe(args.universe)
            if args.market == "ALL" or i.market == args.market
        ]
        if args.symbol:
            instruments = [i for i in instruments if i.symbol == args.symbol]
        if not instruments:
            parser.error("no configured instruments match market/symbol")
        intervals = [args.interval] if args.interval else settings.intervals
        failures = collect(
            settings,
            instruments,
            intervals,
            YahooFinanceProvider(settings),
            store,
            update=args.command == "update",
        )
        print(
            f"completed={len(instruments) * len(intervals) - len(failures)} failed={len(failures)}"
        )
        for failure in failures:
            print(f"FAILED {failure}")
        return 1 if failures else 0
    except ValidationError as exc:
        # Pydantic's default string includes input values, potentially secrets.
        details = "; ".join(
            f"{'.'.join(map(str, error['loc'])) or 'configuration'}: {error['type']}"
            for error in exc.errors(include_input=False)
        )
        print(f"Error: validation failed: {details}")
        return 2
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        location = f" at line {mark.line + 1}, column {mark.column + 1}" if mark else ""
        print(f"Error: invalid YAML{location}")
        return 2
    except (OSError, ValueError, sqlite3.Error) as exc:
        print(f"Error: {type(exc).__name__}: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
