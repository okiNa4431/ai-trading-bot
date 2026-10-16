"""Manual continuation: state lives ONLY in Supabase, fetched quotes are disposable."""

import argparse
import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import yaml

from trading_bot.ai_cli import summary
from trading_bot.broker.paper import PaperBroker
from trading_bot.config import load_settings, load_universe
from trading_bot.dashboard import publish
from trading_bot.orchestration.engine import DecisionEngine
from trading_bot.performance import snapshot
from trading_bot.persistence.decision_repository import DecisionRepository
from trading_bot.persistence.supabase_repository import (
    SupabaseAPI,
    SupabaseError,
    SupabaseRepository,
)
from trading_bot.portfolio.valuation import value_portfolio
from trading_bot.provider import YahooFinanceProvider
from trading_bot.screening.models import ScreeningSettings
from trading_bot.service import collect
from trading_bot.storage import ParquetStore
from trading_bot.strategy.models import AISettings
from trading_bot.strategy.security import redact


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--market", choices=["JP", "US"], default="JP")
    parser.add_argument("--provider", choices=["mock", "gemini"], default="mock")
    args = parser.parse_args()
    output: dict[str, object] = {"status": "FAILED"}
    try:
        api = SupabaseAPI.environment()
        repo = SupabaseRepository(api, "paper-live")
        broker = PaperBroker(repo)
        account = broker.initialize(datetime.now(UTC))
        decisions = DecisionRepository(repo)
        before = len(decisions.usage())
        key = "manual-" + os.environ["GITHUB_RUN_ID"]
        previous = decisions.get(key)
        with tempfile.TemporaryDirectory(prefix="persistent-quotes-") as directory:
            settings = load_settings(Path("config/settings.yaml"))
            settings = settings.model_copy(
                update={"storage_dir": Path(directory), "daily_days": 90, "hourly_days": 7}
            )
            universe = [
                i for i in load_universe(Path("config/universe.yaml")) if i.market == args.market
            ][:2]
            for p in account.positions.values():
                if p.quantity and p.instrument.symbol not in {i.symbol for i in universe}:
                    universe.append(p.instrument)
            store = ParquetStore(Path(directory))
            if not previous and collect(
                settings, universe, ["1d", "1h"], YahooFinanceProvider(settings), store
            ):
                raise ValueError("market data unavailable; no AI called")
            screening = ScreeningSettings.model_validate(
                yaml.safe_load(Path("config/strategy.yaml").read_text(encoding="utf-8"))
            )
            ai = AISettings.model_validate(
                yaml.safe_load(Path("config/ai.yaml").read_text(encoding="utf-8"))
            )
            ai = AISettings.model_validate(
                {**ai.model_dump(), "max_requests_per_run": 1, "retries": 0}
            )
            engine = DecisionEngine(store, broker, decisions, universe, screening, ai)
            record = engine.run(
                args.market,
                previous.as_of if previous else datetime.now(UTC),
                provider=args.provider,
                submit=False,
                decision_id=key,
            )
            if broker.get_balance() != account:
                raise ValueError("dry-run changed account state")
            output = summary(record)
            output.update(
                persistent_api_requests_before=before,
                persistent_api_requests_after=len(decisions.usage()),
                account_created_at=account.created_at.isoformat(),
                dry_run=True,
            )
            # Until marks/FX for held assets are available, don't mint a fresh valuation.
            if not account.positions and account.balances["USD"] == 0:
                now = datetime.now(UTC)
                snapshot(repo, value_portfolio(account, {}, None, now, broker.rules), now)
            publish(repo, datetime.now(UTC))
    except Exception as exc:
        output.update(
            status="FAILED",
            error=str(exc)
            if isinstance(exc, SupabaseError)
            else f"{type(exc).__name__}: manual persistent run stopped safely",
        )
    text = redact(json.dumps(output, ensure_ascii=False, indent=2))
    print(text)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as stream:
            stream.write(
                "## Persistent manual decision (dry-run only)\n\n```json\n" + text + "\n```\n"
            )
    return 0 if output["status"] == "SUCCESS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
