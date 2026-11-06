#!/usr/bin/env python3
"""sync_baseline.py — 日常增量同步基准点位（写入 backtest.duckdb）。

特点：
* 只读 market.duckdb，只 upsert backtest.duckdb；
* 首次运行（bt_baselines 无该基准数据）自动从 sync.start_date 全量计算；
* 之后按 ``incremental_lookback_days`` 回看，覆盖数据修订；
* 单个基准失败不影响其他基准。

用法：
    python scripts/sync_baseline.py
    python scripts/sync_baseline.py --benchmark BI_HS300
    python scripts/sync_baseline.py --date 2024-11-01 --profile production
    python scripts/sync_baseline.py --dry-run
"""

from __future__ import annotations

import argparse
import logging
from datetime import date, timedelta

from quant_baseline.core.config import load_settings
from quant_baseline.core.db import backtest_connection, market_connection
from quant_baseline.core.logger import setup_logging
from quant_baseline.pipeline import BaselinePipeline
from quant_baseline.sinks.backtest_sink import BacktestSink
from quant_baseline.sources.warehouse_source import WarehouseSource

LOG = logging.getLogger("sync_baseline")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sync_baseline.py", description="基准增量同步"
    )
    parser.add_argument("--benchmark", default=None, help="只同步指定基准")
    parser.add_argument("--date", default=None, help="同步截止日 YYYY-MM-DD")
    parser.add_argument("--profile", default=None)
    parser.add_argument("--dry-run", action="store_true", help="只打印计划，不写库")
    parser.add_argument(
        "--log-level", default="INFO", choices=("DEBUG", "INFO", "WARNING", "ERROR")
    )
    return parser


def _last_written(con, benchmark_id: str) -> date | None:
    rows = con.execute(
        "SELECT max(trade_date) FROM bt_baselines WHERE benchmark_id = ?",
        [benchmark_id],
    ).fetchall()
    return None if not rows else rows[0][0]


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(args.log_level)

    settings = load_settings(profile=args.profile)
    end = date.fromisoformat(args.date) if args.date else None

    try:
        with market_connection(settings.database) as market_con:
            source = WarehouseSource(market_con)
            source.verify_schema()

            with backtest_connection(settings.database) as backtest_con:
                sink = BacktestSink(backtest_con, settings.sink)
                pipeline = BaselinePipeline(source, sink, settings)
                benchmarks = (
                    [settings.baseline.benchmark(args.benchmark)]
                    if args.benchmark
                    else settings.baseline.enabled_benchmarks()
                )
                if not benchmarks:
                    LOG.warning("没有启用的基准；请检查 configs/baseline.yaml")
                    return 0

                failures = 0
                for benchmark in benchmarks:
                    last = _last_written(backtest_con, benchmark.code)
                    if last is None:
                        start = max(
                            settings.baseline.sync.start_date, benchmark.base_date
                        )
                    else:
                        start = last - timedelta(
                            days=settings.baseline.sync.incremental_lookback_days
                        )
                    target_end = end or source.latest_trade_date(
                        settings.baseline.sync.trade_calendar
                    )
                    if target_end is None:
                        raise RuntimeError("无法确定最新交易日")
                    LOG.info(
                        "%s: %s..%s%s",
                        benchmark.code,
                        start,
                        target_end,
                        " (dry-run)" if args.dry_run else "",
                    )
                    if args.dry_run:
                        continue
                    try:
                        pipeline.run(benchmark.code, start=start, end=target_end)
                    except Exception as exc:  # noqa: BLE001 - 单基准隔离
                        failures += 1
                        LOG.error("%s 同步失败：%s", benchmark.code, exc)
                return 1 if failures else 0
    except Exception as exc:  # noqa: BLE001 - CLI 顶层
        LOG.error("同步失败：%s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
