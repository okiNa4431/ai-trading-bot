"""Manual-only, disposable dry-run smoke test. No paper orders or artifacts."""

import argparse
import json
import logging
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import yaml

from trading_bot.ai_cli import summary
from trading_bot.broker.paper import PaperBroker
from trading_bot.config import load_settings, load_universe
from trading_bot.orchestration.engine import DecisionEngine
from trading_bot.persistence.decision_repository import DecisionRepository
from trading_bot.persistence.sqlite_repository import SQLiteRepository
from trading_bot.provider import YahooFinanceProvider
from trading_bot.screening.models import ScreeningSettings
from trading_bot.service import collect
from trading_bot.storage import ParquetStore
from trading_bot.strategy.models import AISettings
from trading_bot.strategy.security import redact


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--market", choices=["JP", "US"], default="JP")
    parser.add_argument("--mode", choices=["mock", "gemini"], default="mock")
    args = parser.parse_args()
    for name in ("yfinance", "curl_cffi", "google_genai", "httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.CRITICAL)
    output: dict[str, object] = {
        "status": "FAILED",
        "market": args.market,
        "run_at": datetime.now(UTC).isoformat(),
    }
    try:
        with tempfile.TemporaryDirectory(prefix="ai-smoke-") as directory:
            base = Path(directory)
            settings = load_settings(Path("config/settings.yaml"))
            settings = settings.model_copy(
                update={"storage_dir": base / "market", "daily_days": 90, "hourly_days": 7}
            )
            universe = [
                i for i in load_universe(Path("config/universe.yaml")) if i.market == args.market
            ][:2]
            repository = SQLiteRepository(base / "disposable.sqlite3")
            decisions = DecisionRepository(repository)
            broker = PaperBroker(repository)
            initial = broker.initialize(datetime.now(UTC))
            store = ParquetStore(settings.storage_dir)
            failures = collect(
                settings, universe, ["1d", "1h"], YahooFinanceProvider(settings), store
            )
            if failures:
                raise ValueError("market data acquisition failed; AI not called")
            ai = AISettings.model_validate(
                yaml.safe_load(Path("config/ai.yaml").read_text(encoding="utf-8"))
            )
            ai = AISettings.model_validate(
                {**ai.model_dump(), "max_requests_per_run": 1, "retries": 0}
            )
            screening = ScreeningSettings.model_validate(
                yaml.safe_load(
                    Path("config/strategy.yaml").read_text(encoding="utf-8"),
                )
            )
            engine = DecisionEngine(store, broker, decisions, universe, screening, ai)
            record = engine.run(args.market, datetime.now(UTC), provider=args.mode, submit=False)
            output = summary(record)
            if broker.get_orders() or broker.get_executions() or broker.get_balance() != initial:
                raise ValueError("smoke test invariant violated: paper account changed")
            if len(decisions.usage()) > (1 if args.mode == "gemini" else 0):
                raise ValueError("smoke test request limit violated")
    except Exception as exc:
        # Never expose arbitrary SDK or filesystem exception messages.
        output.update(status="FAILED", error=f"{type(exc).__name__}: smoke test aborted safely")
    text = redact(json.dumps(output, ensure_ascii=False, indent=2))
    print("PHASE3_SUMMARY_JSON=" + redact(json.dumps(output, ensure_ascii=False)))
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with Path(path).open("a", encoding="utf-8") as stream:
            stream.write(
                "## Gemini decision smoke test — disposable dry-run\n\n```json\n" + text + "\n```\n"
            )
    return 0 if output.get("status") == "SUCCESS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
