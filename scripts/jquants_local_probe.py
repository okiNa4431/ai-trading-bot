"""Manual Windows-local aggregate-only probe. No command-line secrets or output files."""

import sys

from trading_bot.jquants_local import main

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
