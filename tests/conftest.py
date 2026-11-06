"""tests.conftest — 内存/临时库 fixtures。

测试隔离约定：
* 所有数据库都建在 ``tmp_path``，绝不读写 <data-root> 下的真实库；
* market.duckdb 结构直接执行 quant_schema 的 DDL（不调用 data_pipeline 脚本）；
* backtest.duckdb 结构同理，保证测试只验证 quant-baseline 的行为。
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import duckdb
import polars as pl
import pytest
from quant_schema.backtest_ddl import BACKTEST_TABLES, BACKTEST_VIEWS
from quant_schema.constants import (
    BACKTEST_COMPONENT,
    BACKTEST_SCHEMA_VERSION,
    CONFLICT_KEYS,
    WAREHOUSE_COMPONENT,
    WAREHOUSE_SCHEMA_VERSION,
)
from quant_schema.market_ddl import (
    FACTOR_TABLES,
    WAREHOUSE_TABLES,
    WAREHOUSE_VIEWS,
)

from quant_baseline.core.config import (
    BaselineConfig,
    BenchmarkConfig,
    DefaultsConfig,
    MemberConfig,
    ProfileConfig,
    RebalanceRule,
    Settings,
    SinkConfig,
    SyncConfig,
    UniverseConfig,
    WeightRule,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent

TRADING_DATES = (
    date(2024, 1, 2),
    date(2024, 1, 3),
    date(2024, 1, 4),
    date(2024, 1, 5),
    date(2024, 1, 8),
    date(2024, 1, 31),
    date(2024, 2, 1),
)


def _build_market_schema(path: Path) -> None:
    con = duckdb.connect(str(path))
    try:
        for table in WAREHOUSE_TABLES + FACTOR_TABLES:
            con.execute(table.ddl())
        for name, sql in WAREHOUSE_VIEWS.items():
            con.execute(f'CREATE OR REPLACE VIEW "{name}" AS {sql}')
        con.execute(
            "INSERT INTO meta_schema_version (component, version) VALUES (?, ?)",
            [WAREHOUSE_COMPONENT, WAREHOUSE_SCHEMA_VERSION],
        )
    finally:
        con.close()


def _seed_market(path: Path) -> None:
    con = duckdb.connect(str(path))
    try:
        con.executemany(
            "INSERT INTO dim_trade_cal (exchange, cal_date, is_open, pretrade_date) "
            "VALUES (?, ?, TRUE, ?)",
            [
                ("SSE", day, TRADING_DATES[index - 1] if index else None)
                for index, day in enumerate(TRADING_DATES)
            ],
        )
        # pct_chg 为百分数，与 Tushare 一致
        daily = [
            ("600000.SH", date(2024, 1, 2), 10.0, 10.0, 0.0),
            ("600000.SH", date(2024, 1, 3), 11.0, 10.0, 10.0),
            ("600000.SH", date(2024, 1, 4), 11.0, 11.0, 0.0),
            ("600000.SH", date(2024, 1, 5), 11.0, 11.0, 0.0),
            ("600000.SH", date(2024, 1, 8), 11.0, 11.0, 0.0),
            ("600000.SH", date(2024, 1, 31), 11.0, 11.0, 0.0),
            ("600000.SH", date(2024, 2, 1), 11.0, 11.0, 0.0),
            ("600001.SH", date(2024, 1, 2), 20.0, 20.0, 0.0),
            ("600001.SH", date(2024, 1, 3), 24.0, 20.0, 20.0),
            ("600001.SH", date(2024, 1, 4), 24.0, 24.0, 0.0),
            ("600001.SH", date(2024, 1, 5), 24.0, 24.0, 0.0),
            ("600001.SH", date(2024, 1, 8), 24.0, 24.0, 0.0),
            ("600001.SH", date(2024, 1, 31), 24.0, 24.0, 0.0),
            ("600001.SH", date(2024, 2, 1), 24.0, 24.0, 0.0),
        ]
        con.executemany(
            "INSERT INTO fact_daily "
            "(ts_code, trade_date, open, high, low, close, pre_close, chg, pct_chg, vol, amount) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, 1000, 100000)",
            [
                (code, day, close, close, close, close, pre_close, pct)
                for code, day, close, pre_close, pct in daily
            ],
        )
        con.executemany(
            "INSERT INTO fact_daily_basic (ts_code, trade_date, total_mv, circ_mv) "
            "VALUES (?, ?, ?, ?)",
            [
                ("600000.SH", date(2024, 1, 2), 100.0, 100.0),
                ("600001.SH", date(2024, 1, 2), 300.0, 300.0),
                ("600000.SH", date(2024, 1, 3), 110.0, 110.0),
                ("600001.SH", date(2024, 1, 3), 360.0, 360.0),
            ],
        )
        con.executemany(
            "INSERT INTO fact_index_daily "
            "(index_code, trade_date, open, high, low, close, pre_close, chg, pct_chg, vol, amount) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, 1000, 100000)",
            [
                ("000300.SH", day, value, value, value, value, value, pct)
                for day, value, pct in (
                    (date(2024, 1, 2), 100.0, 0.0),
                    (date(2024, 1, 3), 110.0, 10.0),
                    (date(2024, 1, 4), 121.0, 10.0),
                    (date(2024, 1, 5), 121.0, 0.0),
                )
            ],
        )
        con.executemany(
            "INSERT INTO fact_index_weight (index_code, trade_date, con_code, weight) "
            "VALUES (?, ?, ?, ?)",
            [
                ("000300.SH", date(2024, 1, 2), "600000.SH", 0.4),
                ("000300.SH", date(2024, 1, 2), "600001.SH", 0.6),
                ("000300.SH", date(2024, 2, 1), "600000.SH", 0.5),
                ("000300.SH", date(2024, 2, 1), "600001.SH", 0.5),
            ],
        )
    finally:
        con.close()


@pytest.fixture()
def market_db(tmp_path: Path) -> Path:
    path = tmp_path / "market.duckdb"
    _build_market_schema(path)
    _seed_market(path)
    return path


@pytest.fixture()
def backtest_db(tmp_path: Path) -> Path:
    path = tmp_path / "backtest.duckdb"
    con = duckdb.connect(str(path))
    try:
        for table in BACKTEST_TABLES:
            con.execute(table.ddl())
        for name, sql in BACKTEST_VIEWS.items():
            con.execute(f'CREATE OR REPLACE VIEW "{name}" AS {sql}')
        con.execute(
            "INSERT INTO meta_schema_version (component, version) VALUES (?, ?)",
            [BACKTEST_COMPONENT, BACKTEST_SCHEMA_VERSION],
        )
    finally:
        con.close()
    return path


def make_settings(
    market_path: Path,
    backtest_path: Path,
    *,
    benchmarks: list[BenchmarkConfig] | None = None,
    universes: dict[str, UniverseConfig] | None = None,
) -> Settings:
    defaults = DefaultsConfig(
        calendar="SSE",
        price_field="close",
        adjust="pct_chg",
        missing_policy="drop",
        base_date=date(2024, 1, 2),
        base_value=1000.0,
        rebalance=RebalanceRule("monthly", "first_trading_day"),
        weight=WeightRule("equal", "total_mv", 1),
        warmup_days=10,
    )
    universe_map = universes or {
        "UNI_TEST": UniverseConfig(
            code="UNI_TEST",
            name="测试池",
            description=None,
            members=(
                MemberConfig("600000.SH", date(2024, 1, 2)),
                MemberConfig("600001.SH", date(2024, 1, 2)),
            ),
            constituents_source=None,
        )
    }
    benchmark_list = benchmarks or [
        BenchmarkConfig(
            code="BI_EQW_TEST",
            name="测试等权",
            kind="synthetic",
            category="basket",
            symbol=None,
            universe="UNI_TEST",
            price_field="close",
            adjust="pct_chg",
            weight=WeightRule("equal", "total_mv", 1),
            rebalance=RebalanceRule("monthly", "first_trading_day"),
            base_date=date(2024, 1, 2),
            base_value=1000.0,
            is_active=True,
        )
    ]
    baseline = BaselineConfig(
        version=1,
        defaults=defaults,
        universes=universe_map,
        benchmarks={item.code: item for item in benchmark_list},
        sync=SyncConfig(
            start_date=date(2024, 1, 2),
            end_date=None,
            incremental_lookback_days=0,
            chunk_days=365,
            trade_calendar="SSE",
            write_mode="upsert",
        ),
    )
    profile = ProfileConfig(
        name="test",
        market_db_path=market_path,
        backtest_db_path=backtest_path,
        market_read_only=True,
        threads=2,
        memory_limit="1GB",
        temp_dir=None,
    )
    sink = SinkConfig(
        table="bt_baselines",
        conflict_keys=("benchmark_id", "trade_date"),
        batch_size=2,
        verify_after_write=True,
        table_keys={
            name: keys
            for name, keys in CONFLICT_KEYS.items()
            if name
            in {
                "bt_benchmarks",
                "bt_baselines",
                "bt_baseline_weights",
                "bt_compute_log",
            }
        },
    )
    return Settings(
        baseline=baseline,
        database=profile,
        sink=sink,
        config_dir=PROJECT_ROOT / "configs",
    )


@pytest.fixture()
def settings(market_db: Path, backtest_db: Path) -> Settings:
    return make_settings(market_db, backtest_db)


@pytest.fixture()
def settings_factory(market_db: Path, backtest_db: Path):
    def factory(benchmarks=None, universes=None) -> Settings:
        return make_settings(
            market_db, backtest_db, benchmarks=benchmarks, universes=universes
        )

    return factory


def utc_now() -> datetime:
    return datetime.now(UTC)


def sample_returns_frame() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "trade_date": list(TRADING_DATES[:4]),
            "ret_1d": [0.0, 0.10, -0.05, 0.0],
        }
    )
