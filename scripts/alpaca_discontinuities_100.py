"""Separately approved, memory-only discontinuity diagnosis."""

from trading_bot.quality_diagnostic import main

if __name__ == "__main__":
    raise SystemExit(main(continue_discontinuities=True))
