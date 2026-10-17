"""Phase 5.5 access policy. Researching a provider never activates data collection."""


class MarketAccessDisabled(ValueError):
    pass


def require_live_market_access() -> None:
    # Owner explicitly paused ALL automated real-market collection on 2026-10-17.
    # No environment variable, Workflow input or alternate account overrides it.
    raise MarketAccessDisabled(
        "Real-market collection is disabled pending provider rights and explicit activation"
    )
