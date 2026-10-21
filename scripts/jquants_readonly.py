"""Aggregate-only entry point. Real execution remains disabled in reviewed config."""

import json
import logging
import os

from trading_bot.jquants import JQuantsProvider, load_policy


def main() -> int:
    previous = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    provider = None
    try:
        provider = JQuantsProvider(
            load_policy(), os.environ.get("JQUANTS_READONLY_APPROVAL", ""), os.environ
        )
        report = provider.run().summary()
        print(json.dumps(report, sort_keys=True))
        return 0 if report["status"] == "READ_ONLY_COMPLETE" else 1
    except Exception:
        print(
            json.dumps(
                dict(
                    phase="JP-2",
                    purpose="diagnostic_only",
                    status="STOPPED",
                    reason="APPROVAL_SCOPE_OR_CONFIGURATION_FAILURE",
                    strategy_approved=False,
                    data_retained=False,
                    requests=provider.guard.requests if provider else 0,
                    checks=dict(
                        scope="FAILED",
                        paging=["NOT_RUN", "NOT_RUN"],
                        quality=["NOT_RUN", "NOT_RUN"],
                        repeat_consistency="NOT_RUN",
                    ),
                )
            )
        )
        return 1
    finally:
        if provider is not None:
            try:
                provider.close()
            except Exception:
                pass
        logging.disable(previous)


if __name__ == "__main__":
    raise SystemExit(main())
