#!/usr/bin/env python3
"""backfill_history.py — 历史区间回补（按 chunk 分片，幂等，可断点重跑）。

用法：
    python scripts/backfill_history.py --all --start 2015-01-05 --end 2026-11-06
    python scripts/backfill_history.py --benchmark BI_EQW_CORE --start 2018-01-01
    python scripts/backfill_history.py --benchmark BI_HS300 --chunk-days 180

说明：
* 合成基准每次 run 都从 base_date 重算（保证复现），只把请求区间写入库；
* 每片独立 upsert，某片失败不回滚已完成的片；
* 不需要删除旧数据：主键 (benchmark_id, trade_date) 覆盖写。
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

LOG = logging.getLogger("backfill_history")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="backfill_history.py", description="基准历史回补"
    )
    parser.add_argument("--benchmark", action="append", default=None, help="可重复")
    parser.add_argument("--all", action="store_true", help="回补所有启用的基准")
    parser.add_argument("--start", default=None, help="YYYY-MM-DD")
    parser.add_argument("--end", default=None, help="YYYY-MM-DD")
    parser.add_argument("--chunk-days", type=int, default=365)
    parser.add_argument("--profile", default=None)
    parser.add_argument(
        "--log-level", default="INFO", choices=("DEBUG", "INFO", "WARNING", "ERROR")
    )
    return parser


def _chunks(start: date, end: date, days: int) -> list[tuple[date, date]]:
    result: list[tuple[date, date]] = []
    cursor = start
    while cursor <= end:
        stop = min(cursor + timedelta(days=days - 1), end)
        result.append((cursor, stop))
        cursor = stop + timedelta(days=1)
    return result


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(args.log_level)
    settings = load_settings(profile=args.profile)

    try:
        with market_connection(settings.database) as market_con:
            source = WarehouseSource(market_con)
            source.verify_schema()
            latest = source.latest_trade_date(settings.baseline.sync.trade_calendar)
            if latest is None:
                raise RuntimeError("无法确定最新交易日")

            start = (
                date.fromisoformat(args.start)
                if args.start
                else settings.baseline.sync.start_date
            )
            end = date.fromisoformat(args.end) if args.end else latest
            if end < start:
                raise ValueError("--end 必须 >= --start")
            if args.chunk_days < 1:
                raise ValueError("--chunk-days 必须 >= 1")

            with backtest_connection(settings.database) as backtest_con:
                sink = BacktestSink(backtest_con, settings.sink)
                pipeline = BaselinePipeline(source, sink, settings)

                if args.benchmark:
                    benchmarks = [
                        settings.baseline.benchmark(code) for code in args.benchmark
                    ]
                elif args.all or not args.benchmark:
                    benchmarks = settings.baseline.enabled_benchmarks()
                else:
                    benchmarks = []

                failures = 0
                for benchmark in benchmarks:
                    actual_start = max(start, benchmark.base_date)
                    pieces = _chunks(actual_start, end, args.chunk_days)
                    LOG.info(
                        "%s: %s..%s，%d 片",
                        benchmark.code,
                        actual_start,
                        end,
                        len(pieces),
                    )
                    for index, (chunk_start, chunk_end) in enumerate(pieces, start=1):
                        try:
                            report = pipeline.run(
                                benchmark.code, start=chunk_start, end=chunk_end
                            )
                            LOG.info(
                                "  [%d/%d] %s..%s rows=%d %.2fs",
                                index,
                                len(pieces),
                                chunk_start,
                                chunk_end,
                                report.rows_written,
                                report.elapsed_s,
                            )
                        except Exception as exc:  # noqa: BLE001 - 单片隔离
                            failures += 1
                            LOG.error(
                                "  [%d/%d] %s..%s 失败：%s",
                                index,
                                len(pieces),
                                chunk_start,
                                chunk_end,
                                exc,
                            )
                return 1 if failures else 0
    except Exception as exc:  # noqa: BLE001
        LOG.error("回补失败：%s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
