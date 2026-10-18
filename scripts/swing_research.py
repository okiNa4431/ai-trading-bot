"""Offline synthetic swing report. No credentials and no live account argument."""

import argparse
from pathlib import Path

import yaml

from trading_bot.swing.models import SwingSettings
from trading_bot.swing.report import write_report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--config", type=Path, default=Path("config/swing.yaml"))
    args = parser.parse_args()
    settings = SwingSettings.model_validate(yaml.safe_load(args.config.read_text(encoding="utf-8")))
    result = write_report(args.output, settings)
    print(f"Synthetic research complete: {args.output / 'report.json'}")
    for row in result["scaling"]:
        print(row)


if __name__ == "__main__":
    main()
