"""No activation flag and no secrets: mock HTTP only."""

import json

from trading_bot.alpaca_probe import mock_probe

if __name__ == "__main__":
    print(json.dumps([mock_probe(size) for size in (20, 100, 500, 2000)], indent=2))
