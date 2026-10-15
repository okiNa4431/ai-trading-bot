"""Explicit network audit: temporary data, real CLI commands, no production writes.

Run with `uv run python scripts/verify_live.py`. Outputs ignored data/phase1-audit/ logs.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pandas as pd
import yaml

from trading_bot.config import load_settings, load_universe
from trading_bot.storage import ParquetStore


def run() -> int:
    root = Path(__file__).resolve().parents[1]
    output = root / "data" / "phase1-audit"
    output.mkdir(parents=True, exist_ok=True)
    uv = shutil.which("uv") or str(root / ".tools" / "bin" / "uv.exe")
    env = os.environ.copy()
    if (root / ".runtime").exists():
        env["UV_PYTHON_INSTALL_DIR"] = str(root / ".runtime")
        env["UV_CACHE_DIR"] = str(root / ".uv-cache")
    report: dict[str, object] = {"ran_at": datetime.now(UTC).isoformat(), "python": sys.version}
    commands = [
        ["fetch", "--market", "JP", "--interval", "1d"],
        ["fetch", "--market", "US", "--interval", "1h"],
        ["fetch", "--market", "ALL", "--interval", "1h"],
        ["update", "--market", "ALL"],
        ["status"],
    ]
    try:
        with tempfile.TemporaryDirectory(prefix="live-", dir=output) as directory:
            base = Path(directory)
            config = base / "config"
            config.mkdir()
            settings_path = config / "settings.yaml"
            settings_path.write_text("storage_dir: market\n", encoding="utf-8")
            universe = [
                i
                for i in load_universe(root / "config" / "universe.yaml")
                if i.symbol in {"7203.T", "6758.T", "AAPL", "MSFT"}
            ]
            universe_path = config / "universe.yaml"
            universe_path.write_text(
                yaml.safe_dump([i.model_dump() for i in universe]), encoding="utf-8"
            )
            store = ParquetStore(load_settings(settings_path).storage_dir)
            prefix = [
                uv,
                "run",
                "trading-bot",
                "--settings",
                str(settings_path),
                "--universe",
                str(universe_path),
            ]
            results = []
            previous: dict[tuple[str, str], set[datetime]] = {}
            for number, arguments in enumerate(commands, 1):
                result = subprocess.run(
                    prefix + arguments,
                    cwd=root,
                    env=env,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=300,
                    check=False,
                )
                (output / f"cli-{number}.log").write_text(
                    result.stdout + result.stderr,
                    encoding="utf-8",
                )
                results.append(dict(arguments=arguments, exit_code=result.returncode))
                report["commands"] = results
                assert result.returncode == 0, f"CLI failed: {arguments}; see cli-{number}.log"
                for instrument in universe:
                    for interval in ("1d", "1h"):
                        bars = store.read(instrument, interval)
                        key = (instrument.symbol, interval)
                        stamps = {bar.timestamp for bar in bars}
                        assert len(bars) == len(stamps)
                        assert previous.get(key, set()).issubset(stamps), "history lost"
                        if key in previous:
                            assert len(stamps) == len(previous[key]), "unexpected duplicate growth"
                        if bars:
                            previous[key] = stamps
                rows = store.status()
                if number == 1:
                    assert len(rows) == 2 and all(row["market"] == "JP" for row in rows)
                if number == 2:
                    assert len(rows) == 4
                if number == 3:
                    assert len(rows) == 6
                print(f"PASS CLI {number}: {' '.join(arguments)}", flush=True)
            rows = store.status()
            assert len(rows) == 8
            total = 0
            for instrument in universe:
                for interval in ("1d", "1h"):
                    bars = store.read(instrument, interval)
                    assert len(bars) >= (200 if interval == "1d" else 20)
                    assert all(bar.timestamp.utcoffset() == timedelta(0) for bar in bars)
                    assert all(bar.fetched_at.utcoffset() == timedelta(0) for bar in bars)
                    assert all(not bar.adjusted and bar.source == "yahoo" for bar in bars)
                    assert all(
                        bar.timestamp + timedelta(hours=1) <= bar.fetched_at
                        for bar in bars
                        if interval == "1h"
                    )
                    disk = pd.read_parquet(store.path(instrument, interval))
                    assert len(disk) == len(bars)
                    assert not disk.duplicated(["symbol", "interval", "timestamp"]).any()
                    assert disk.timestamp.is_monotonic_increasing
                    total += len(bars)
            report["partitions"] = rows
            report["total_bars"] = total
            report["result"] = "PASS"
            print(json.dumps(rows, indent=2), flush=True)
    except Exception as exc:
        report["result"] = "FAIL"
        report["error"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        (output / "results.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
