"""Manual Phase 5 entry point: test scopes only, no schedule, no real broker."""

import argparse
import json
import logging
import os
import tempfile
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import yaml

from trading_bot.broker.paper import PaperBroker
from trading_bot.config import load_settings, load_universe
from trading_bot.cycle import TestProposal, run_cycle
from trading_bot.market_inputs import YahooInputs, update_inputs
from trading_bot.operations import Operation, health_summary, market_slot, test_scope
from trading_bot.persistence.supabase_repository import SupabaseAPI, SupabaseError
from trading_bot.phase4_cli import verify
from trading_bot.portfolio.valuation import validate_fx
from trading_bot.provider import YahooFinanceProvider
from trading_bot.storage import ParquetStore
from trading_bot.strategy.models import AISettings


def main():
    for name in ("yfinance", "curl_cffi", "google_genai", "google.genai", "httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.CRITICAL)
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--mode", choices=["cycle", "health", "reconcile", "fixture"], default="health"
    )
    parser.add_argument("--account", default="test-phase5-manual")
    parser.add_argument("--market", choices=["JP", "US"], default="JP")
    parser.add_argument("--kind", choices=["observe", "decision", "execute"], default="observe")
    parser.add_argument("--provider", choices=["mock", "gemini"], default="mock")
    parser.add_argument("--mock-action", choices=["HOLD", "BUY", "SELL"], default="HOLD")
    parser.add_argument("--submit", action="store_true")
    parser.add_argument(
        "--fund-jpy", type=Decimal, choices=[Decimal(0), Decimal("100000")], default=Decimal(0)
    )
    parser.add_argument("--fixture-step", type=int, choices=range(1, 6), default=1)
    parser.add_argument("--run-id", default="inspect")
    parser.add_argument("--reason")
    parser.add_argument("--apply-review", action="store_true")
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    test_scope(args.account)
    api = SupabaseAPI.environment()
    if args.mode == "fixture":
        from phase5_fixture import fixture

        with tempfile.TemporaryDirectory() as directory:
            result = fixture(
                api,
                args.market,
                args.fixture_step,
                directory,
                account=None if args.account == "test-phase5-manual" else args.account,
            )
        print(json.dumps(result))
        return 0
    op = Operation(api, args.account, args.run_id)
    if args.mode in ("health", "reconcile"):
        state = op.read()
        with op.repository.transaction() as tx:
            initialized = tx.initialized()
        if args.apply_review:
            if (
                args.mode != "reconcile"
                or not args.reason
                or os.environ.get("PHASE5_RECOVERY_APPROVED") != "true"
            ):
                raise ValueError("review requires separately enabled administrator recovery")
            if initialized:
                verify(op.repository)
            api.rpc(
                "bot_operation_review",
                {
                    "p_account": args.account,
                    "p_run": args.run_id,
                    "p_reason": args.reason,
                    "p_stop": not args.resume,
                },
            )
            state = op.read()
        now = datetime.now(UTC)
        health = health_summary(state, now)
        preserved = api.rpc("bot_operation_preserved", {})
        print(
            json.dumps(
                {
                    **health,
                    "initialized": initialized,
                    "verified": verify(op.repository)["verified"] if initialized else None,
                    "scheduled": False,
                    "public_promotion": False,
                    "pre_migration_data_preserved": preserved,
                    "order_authorization": "not_granted_by_healthcheck",
                }
            )
        )
        return 1 if health["unresolved"] or state["stopped"] or not preserved else 0
    now = datetime.now(UTC)
    try:
        slot = market_slot(args.market, now, args.kind)
    except ValueError:
        print(json.dumps({"state": "SKIPPED", "reason": "outside market/session window"}))
        return 0
    op = Operation(api, args.account, slot)
    instruments = [
        i
        for i in load_universe(Path("config/universe.yaml"))
        if i.market == args.market
        and i.symbol in ({"9432.T", "7203.T"} if args.market == "JP" else {"AAPL", "MSFT"})
    ]
    # Read is allowed before claim; all mutation is fenced after run_cycle claims.
    with op.repository.transaction() as tx:
        account = tx.account() if tx.initialized() else None
    if account:
        for position in account.positions.values():
            if position.quantity and position.instrument.symbol not in {
                i.symbol for i in instruments
            }:
                instruments.append(position.instrument)
        for row in PaperBroker(op.repository).get_orders():
            if row.status == "PENDING" and row.symbol not in {i.symbol for i in instruments}:
                from trading_bot.config import Instrument

                instruments.append(
                    Instrument(
                        symbol=row.symbol,
                        market=row.market,
                        currency=row.currency,
                        display_name=row.symbol,
                    )
                )
    settings = load_settings(Path("config/settings.yaml")).model_copy(update={"retries": 0})
    ai = AISettings.model_validate(
        yaml.safe_load(Path("config/ai.yaml").read_text(encoding="utf-8"))
    )
    ai = ai.model_copy(update={"retries": 0, "max_requests_per_run": 1})
    yahoo = YahooInputs()
    with tempfile.TemporaryDirectory() as directory:
        store = ParquetStore(Path(directory))

        def prepare(broker):
            at = datetime.now(UTC)
            update_inputs(
                op,
                store,
                instruments,
                YahooFinanceProvider(settings),
                yahoo,
                at,
                broker.get_balance().created_at,
            )
            balance = broker.get_balance()
            if (
                args.market == "US"
                or balance.balances["USD"]
                or any(
                    p.quantity and p.instrument.currency == "USD"
                    for p in balance.positions.values()
                )
            ):
                observed = yahoo.fetch_fx(datetime.now(UTC))
                if observed.observed_at > datetime.now(UTC):
                    raise ValueError("future FX observation")
                validate_fx(observed.rate, datetime.now(UTC), broker.rules)
                op.data("fx:USDJPY", observed.model_dump(mode="json"))
                return observed.rate
            return None

        result = run_cycle(
            op,
            store,
            instruments,
            args.market,
            ai,
            kind=args.kind,
            owner=os.environ.get("GITHUB_RUN_ID", "manual"),
            submit=args.submit,
            provider=args.provider,
            fund_jpy=args.fund_jpy,
            prepare=prepare,
            strategy=TestProposal(args.mock_action) if args.provider == "mock" else None,
        )
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        # No upstream response, key, URL, raw quote or AI content in public Actions logs.
        print(
            json.dumps(
                {
                    "state": "FAILED_OR_UNKNOWN",
                    "error_code": type(exc).__name__,
                    "diagnostic": str(exc) if isinstance(exc, SupabaseError) else "",
                    "action": "inspect retained operation; no automatic replay",
                }
            )
        )
        raise SystemExit(1) from None
