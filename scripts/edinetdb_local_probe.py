"""Offline JP-3 preflight; no API key needed and no live mode exists."""

import sys

from trading_bot.edinetdb import main

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
