"""Validation of version-controlled research samples, not historical investment universes."""

from collections.abc import Sequence

from trading_bot.config import Instrument


def validate_research_symbols(symbols: Sequence[str], limit: int) -> None:
    if not 1 <= len(symbols) <= limit or len(set(symbols)) != len(symbols):
        raise ValueError("invalid research universe size or duplicates")
    for symbol in symbols:
        Instrument(symbol=symbol, market="US", currency="USD", display_name=symbol)
        if not symbol.isalpha() or not symbol.isascii():
            raise ValueError("research requires reviewed plain US symbols")
