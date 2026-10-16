"""Only isolated persistent test accounts; no Gemini and no market data publication."""

import argparse
import json
import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from trading_bot.broker.paper import PaperBroker
from trading_bot.config import Instrument
from trading_bot.models import PriceBar
from trading_bot.persistence.decision_repository import DecisionRepository
from trading_bot.persistence.supabase_repository import (
    SupabaseAPI,
    SupabaseError,
    SupabaseRepository,
)
from trading_bot.phase4_cli import verify
from trading_bot.strategy.models import AISettings, DecisionRun


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--account", default="test-phase4-smoke")
    args = parser.parse_args()
    result: dict[str, object] = {"status": "FAILED"}
    try:
        api = SupabaseAPI.environment()
        repo = SupabaseRepository(api, args.account, test=True)
        broker = PaperBroker(repo)
        initial = broker.initialize(datetime(2026, 10, 4, tzinfo=UTC))
        # Synthetic prices are allowed ONLY for this explicitly isolated test account.
        at = datetime(2026, 10, 5, tzinfo=UTC)
        instrument = Instrument(
            symbol="TEST.T", market="JP", currency="JPY", display_name="SYNTHETIC TEST"
        )
        order = broker.place_order(
            instrument, "BUY", 100, at - timedelta(seconds=1), "smoke-buy-v1"
        )
        bar = PriceBar(
            symbol="TEST.T",
            market="JP",
            currency="JPY",
            interval="1h",
            timestamp=at,
            open=100,
            high=100,
            low=100,
            close=100,
            volume=1000,
            fetched_at=at + timedelta(hours=1),
        )
        filled = broker.execute(order.order_id, {"TEST.T": [bar]}, None, at + timedelta(hours=1))
        assert filled.status == "FILLED"
        restarted = PaperBroker(SupabaseRepository(api, args.account, test=True))
        assert restarted.get_balance().balances["JPY"] == Decimal("989990")
        assert len(restarted.get_executions()) == 1
        assert (
            restarted.execute(order.order_id, {"TEST.T": [bar]}, None, at + timedelta(hours=1))
            == filled
        )
        before = api.rpc("bot_read", {"p_account": args.account})
        try:
            api.rpc(
                "bot_commit",
                {
                    "p_account": args.account,
                    "p_test": True,
                    "p_version": before["version"],
                    "p_value": {**before["account"], "balances": {"JPY": "1", "USD": "0"}},
                    "p_writes": [],
                },
            )
        except ValueError:
            pass
        else:
            raise AssertionError("invalid ledger accepted")
        assert api.rpc("bot_read", {"p_account": args.account}) == before
        decisions = DecisionRepository(repo)
        decision = DecisionRun(
            decision_id="quota-smoke-v1",
            started_at=datetime.now(UTC),
            as_of=at,
            market="JP",
            provider="mock",
            model="quota-test-no-api",
            prompt_version="smoke",
            input_hash="test",
            account_hash="test",
        )
        decisions.claim(decision)
        usage = decisions.usage("quota-smoke-v1")
        if not usage:
            reservation = decisions.reserve(
                "quota-smoke-v1",
                AISettings(
                    model="quota-test-no-api",
                    approved_free_models=["quota-test-no-api"],
                    daily_request_limit=5,
                    monthly_request_limit=100,
                ),
            )
            decisions.save_usage(
                reservation.model_copy(update={"status": "ERROR", "error": "test-no-api"})
            )
        assert (
            len(
                DecisionRepository(SupabaseRepository(api, args.account, test=True)).usage(
                    "quota-smoke-v1"
                )
            )
            == 1
        )
        public = SupabaseAPI.environment(public=True)
        assert isinstance(public.request("GET", "bot_dashboard?select=id,document"), list)
        # Test all mutation verbs against the public table, even if currently empty.
        denied = 0
        for method, path, body in [
            ("POST", "rpc/bot_read", {"p_account": args.account}),
            ("POST", "rpc/bot_delete_test", {"p_account": args.account}),
            ("POST", "bot_dashboard", {"id": "paper-live", "document": {}}),
            ("PATCH", "bot_dashboard?id=eq.paper-live", {"document": {}}),
            ("DELETE", "bot_dashboard?id=eq.paper-live", None),
        ]:
            try:
                public.request(method, path, body)
            except ValueError:
                denied += 1
            else:
                raise AssertionError("anonymous internal access or public mutation permitted")
        result = {
            "status": "SUCCESS",
            "test_account": args.account,
            "reused_account": initial.created_at.isoformat(),
            **verify(repo),
            "cash_jpy": str(restarted.get_balance().balances["JPY"]),
            "public_permissions_denied": denied,
            "real_api_requests": 0,
            "synthetic_test_data": True,
        }
    except Exception as exc:
        result["error"] = (
            str(exc)
            if isinstance(exc, SupabaseError)
            else (f"{type(exc).__name__}: connection/schema/permission verification incomplete")
        )
    text = json.dumps(result, ensure_ascii=False, indent=2)
    print(text)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as stream:
            stream.write("## Phase 4 isolated persistence smoke\n\n```json\n" + text + "\n```\n")
    return 0 if result["status"] == "SUCCESS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
