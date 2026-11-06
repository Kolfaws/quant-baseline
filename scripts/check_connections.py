#!/usr/bin/env python3
"""check_connections.py — 只读检查两个库的连通性与结构版本。

用法：
    python scripts/check_connections.py
    python scripts/check_connections.py --profile research
"""

from __future__ import annotations

import argparse

from quant_schema.constants import BACKTEST_TABLES, MARKET_WAREHOUSE_TABLES

from quant_baseline.core.config import load_settings
from quant_baseline.core.db import backtest_connection, market_connection
from quant_baseline.core.logger import get_logger, setup_logging
from quant_baseline.sinks.backtest_sink import BacktestSink
from quant_baseline.sources.warehouse_source import WarehouseSource

LOG = get_logger("check_connections")


def _print_table(name: str, con) -> None:
    rows = con.execute(
        "SELECT table_name, estimated_size FROM duckdb_tables() "
        "WHERE schema_name = 'main' ORDER BY table_name"
    ).fetchall()
    LOG.info("  %s: %d 张表", name, len(rows))
    for table, size in rows:
        LOG.info("    %-28s ~%s 行", table, size)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="check_connections.py")
    parser.add_argument("--profile", default=None)
    args = parser.parse_args(argv)
    setup_logging()
    settings = load_settings(profile=args.profile)

    try:
        with market_connection(settings.database) as con:
            source = WarehouseSource(con)
            source.verify_schema()
            versions = con.execute(
                "SELECT version FROM meta_schema_version WHERE component = 'warehouse'"
            ).fetchall()
            LOG.info(
                "market OK  path=%s  warehouse=%s",
                settings.database.market_db_path,
                None if not versions else versions[0][0],
            )
            _print_table("market", con)
            missing = [
                table
                for table in MARKET_WAREHOUSE_TABLES
                if not source.table_exists(table)
            ]
            if missing:
                LOG.error("market 缺表：%s", missing)
                return 1

        with backtest_connection(settings.database) as con:
            sink = BacktestSink(con, settings.sink)
            versions = con.execute(
                "SELECT version FROM meta_schema_version WHERE component = 'backtest'"
            ).fetchall()
            LOG.info(
                "backtest OK  path=%s  backtest=%s",
                settings.database.backtest_db_path,
                None if not versions else versions[0][0],
            )
            _print_table("backtest", con)
            missing = [
                table
                for table in BACKTEST_TABLES
                if not sink.table_exists(table)  # type: ignore[attr-defined]
            ]
            if missing:
                LOG.error("backtest 缺表：%s", missing)
                return 1
        return 0
    except Exception as exc:  # noqa: BLE001
        LOG.error("检查失败：%s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
