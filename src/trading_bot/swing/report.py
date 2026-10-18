"""Reproducible synthetic comparison and scaling measurements; never a public projection."""

import json
import time
import tracemalloc
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from trading_bot.portfolio.models import TradingRules
from trading_bot.swing.backtest import BacktestResult, run_backtest
from trading_bot.swing.data import alpaca_request_plan, synthetic
from trading_bot.swing.models import SwingSettings
from trading_bot.swing.screening import screen_daily


def scale_benchmark(sizes: tuple[int, ...] = (20, 100, 500, 2000)) -> list[dict[str, object]]:
    result = []
    for size in sizes:
        tracemalloc.start()
        before = time.perf_counter()
        data = synthetic(size, 221)
        generation = time.perf_counter() - before
        before = time.perf_counter()
        screening, summary = screen_daily(data, data.closes_at[-1] + data.latency, SwingSettings())
        elapsed = time.perf_counter() - before
        arrays = [data.opening, data.high, data.low, data.closing, data.volume]
        buffer = pa.BufferOutputStream()
        table = pa.table(
            {
                name: pa.array(values.ravel())
                for name, values in zip(
                    ("open", "high", "low", "close", "volume"), arrays, strict=True
                )
            }
        )
        pq.write_table(table, buffer, compression="zstd")
        parquet_bytes = buffer.tell()
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        encoded = len(screening.model_dump_json().encode())
        result.append(
            {
                "symbols": size,
                "sessions": 221,
                "analyzed": screening.analyzed,
                "first_stage": len(summary.first_stage),
                "second_stage": len(screening.candidates),
                "generation_seconds": generation,
                "screen_seconds": elapsed,
                "tracemalloc_peak_bytes": peak,
                "raw_ohlcv_array_bytes": sum(a.nbytes for a in arrays),
                "parquet_ohlcv_bytes": parquet_bytes,
                "candidate_snapshot_bytes": encoded,
                "api_requests_executed": 0,
                "alpaca_initial_lower_bound": alpaca_request_plan(
                    [s.instrument.symbol for s in data.securities], 221
                ),
                "alpaca_incremental_lower_bound": alpaca_request_plan(
                    [s.instrument.symbol for s in data.securities], 2
                ),
                "measurement_limits": (
                    "Parquet: five numeric columns; excludes keys/indexes/backups; "
                    "tracemalloc is not OS RSS"
                ),
            }
        )
    return result


def comparison(
    root: Path, days: int = 231, settings: SwingSettings | None = None
) -> dict[str, object]:
    settings = settings or SwingSettings()
    if root.exists():
        raise ValueError("new output directory required")
    root.mkdir(parents=True)
    data = synthetic(20, days)
    runs: list[BacktestResult] = []
    for kind in ("rule", "momentum", "gemini_mock", "cash"):
        for mode in ("A", "B", "C"):
            result = run_backtest(
                data,
                root / f"test-swing-{kind}-{mode}",
                mode,
                settings,
                kind,
                rules=TradingRules(spread=Decimal("0.0004"), commission_rate=Decimal("0.0001")),
            )
            runs.append(result)

    def row(kind: str, mode: str) -> dict[str, object]:
        return next(r for r in runs if r.strategy == kind and r.mode == mode).summary

    proxy_return = float(
        data.market[-1] / data.market[200] * data.fx_values[-1] / data.fx_values[200] - 1
    )
    for run in runs:
        value = run.summary.get("cumulative_return")
        run.summary["excess_vs_synthetic_us_index"] = (
            float(str(value)) - proxy_return if value is not None else None
        )
        run.summary["excess_vs_all_country"] = None
    monitor_variants = []
    for response in ("hold", "sell"):
        variant = run_backtest(
            data,
            root / f"test-swing-rule-B-{response}",
            "B",
            settings.model_copy(update={"monitor_response": response}),
            "rule",
            rules=TradingRules(spread=Decimal("0.0004"), commission_rate=Decimal("0.0001")),
        )
        monitor_variants.append(asdict(variant))

    deltas = []
    for kind in ("rule", "momentum", "gemini_mock", "cash"):
        baseline = row(kind, "A")
        for mode in ("B", "C"):
            other = row(kind, mode)
            complete = (
                baseline.get("cumulative_return") is not None
                and other.get("cumulative_return") is not None
            )
            deltas.append(
                {
                    "strategy": kind,
                    "mode": mode,
                    "comparable_complete": complete,
                    "return_delta_vs_A": float(str(other["cumulative_return"]))
                    - float(str(baseline["cumulative_return"]))
                    if complete
                    else None,
                    "drawdown_delta_vs_A": float(str(other["max_drawdown"]))
                    - float(str(baseline["max_drawdown"]))
                    if complete
                    else None,
                    "cost_delta_jpy": float(str(other["costs_jpy"]))
                    - float(str(baseline["costs_jpy"])),
                    "decision_delta": int(str(other["decisions"]))
                    - int(str(baseline["decisions"])),
                    "missed_gain_vs_A": max(
                        0.0,
                        float(str(baseline["cumulative_return"]))
                        - float(str(other["cumulative_return"])),
                    )
                    if complete
                    else None,
                }
            )
    return {
        "data_kind": "synthetic",
        "seed": 551,
        "publish_as_live_profit": False,
        "training": [data.opens_at[0].isoformat(), data.closes_at[200].isoformat()],
        "evaluation": [data.opens_at[201].isoformat(), data.closes_at[-1].isoformat()],
        "fixed_parameters": "config/swing.yaml defaults; no optimization on evaluation",
        "rows": [asdict(r) for r in runs],
        "frequency_deltas": deltas,
        "monitor_variants": monitor_variants,
        "all_country": {
            "status": "NOT_EVALUATED",
            "reason": "no reviewed NAV/application manifest; never substitute synthetic NAV",
        },
        "us_index": {
            "status": "SYNTHETIC_PROXY_GROSS",
            "return": proxy_return,
            "reason": "synthetic equal-weight market proxy, no real index or ETF license claim",
        },
        "recommendation": "A baseline only; B/C not approved from a short synthetic sample",
        "selection_criteria": "out-of-sample loss, cost, coverage; never highest return alone",
        "limitations": [
            "no real Gemini inference",
            "no real market profitability evidence",
            "matched synthetic hourly execution for A/B/C",
            "no actual Supabase capacity measurement",
            "Gemini pretraining may contain future events despite causal prompts",
        ],
    }


def write_report(root: Path, settings: SwingSettings | None = None) -> dict[str, object]:
    settings = settings or SwingSettings()
    result = comparison(root, settings=settings)
    result["settings"] = settings.model_dump()
    result["scaling"] = scale_benchmark(
        tuple(
            sorted(
                {
                    20,
                    min(100, settings.universe_size),
                    min(500, settings.universe_size),
                    settings.universe_size,
                }
            )
        )
    )
    result["research_account_bytes"] = sum(p.stat().st_size for p in root.rglob("*.sqlite3"))
    (root / "report.json").write_text(
        json.dumps(result, default=str, indent=2, allow_nan=False), encoding="utf-8"
    )
    (root / "report.md").write_text(render_report(result), encoding="utf-8")
    return result


def render_report(result: dict[str, Any]) -> str:
    def percent(value: object) -> str:
        return f"{value:.2%}" if isinstance(value, (float, int)) else "未評価"

    lines = [
        "# スイング戦略比較（合成データ専用）",
        "",
        "実相場の利益・実Geminiの実績ではありません。公開ダッシュボードへ混入させません。",
        "",
        f"評価期間（UTC）: {result['evaluation'][0]} ～ {result['evaluation'][1]}",
        "201営業日のウォームアップと未使用評価期間を分離。パラメータ最適化なし。",
        "",
        "| 戦略 | 方式 | 状態 | リターン | 最大DD | コストJPY | 約定 | 判断 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in result["rows"]:
        s = row["summary"]
        lines.append(
            f"| {row['strategy']} | {row['mode']} | {row['status']} | "
            f"{percent(s.get('cumulative_return'))} | {percent(s.get('max_drawdown'))} | "
            f"{s.get('costs_jpy', '未評価')} | {s.get('fills', 0)} | "
            f"{s.get('decisions', 0)} |"
        )
    lines += [
        "",
        "## 性能（実測）",
        "",
        "| 銘柄 | 分析秒 | 追跡メモリbytes | Parquet数値列bytes | 候補snapshot bytes |",
        "| --- | --- | --- | --- | --- |",
    ]
    for row in result.get("scaling", []):
        lines.append(
            f"| {row['symbols']} | {row['screen_seconds']:.4f} | "
            f"{row['tracemalloc_peak_bytes']} | {row['parquet_ohlcv_bytes']} | "
            f"{row['candidate_snapshot_bytes']} |"
        )
    lines += [
        "",
        "追跡メモリはOS RSSではなく、Parquetはキー・索引等を含まない5数値列のみ。",
        "",
        "## 制約",
        "",
        "- 実データ取得・実Gemini要求は0回。Geminiはmock HOLD。",
        "- 実オルカンは適用日・公表時刻・権利が揃った入力を接続するまで未評価。",
        "- 米株指数欄は合成の等ウェイト市場proxyで、実指数ではない。",
        "- 株式戦略はJPY80万円を開始時に明示両替。現金baselineはJPY100万円。",
        "- 年率リターン・年間約定回数は252営業日未満では未評価。",
        "- 比率の時価変動による逸脱は監視対象。新規注文と約定時の制限は維持。",
        "- Aを基準とし、短期合成検証だけでB/Cを採用しない。",
        "- 詳細指標・監視政策の比較・停止理由はreport.jsonを参照。",
        "",
    ]
    return "\n".join(lines)
