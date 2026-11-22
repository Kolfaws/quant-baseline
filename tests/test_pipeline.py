"""tests.test_pipeline — 端到端：market.duckdb -> backtest.duckdb。"""

from __future__ import annotations

from datetime import date

import duckdb
import pytest

from quant_baseline.core.config import (
    BenchmarkConfig,
    ConstituentsSourceConfig,
    MemberConfig,
    RebalanceRule,
    UniverseConfig,
    WeightRule,
)
from quant_baseline.core.db import backtest_connection, market_connection
from quant_baseline.core.exceptions import FetchError
from quant_baseline.pipeline import BaselinePipeline
from quant_baseline.sinks.backtest_sink import BacktestSink
from quant_baseline.sources.warehouse_source import WarehouseSource


def _synthetic(code: str = "BI_EQW_TEST") -> BenchmarkConfig:
    return BenchmarkConfig(
        code=code,
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


def _external(code: str = "BI_EXT", symbol: str = "000300.SH") -> BenchmarkConfig:
    return BenchmarkConfig(
        code=code,
        name="测试指数",
        kind="external",
        category="index",
        symbol=symbol,
        universe=None,
        price_field="close",
        adjust="pct_chg",
        weight=None,
        rebalance=None,
        base_date=date(2024, 1, 2),
        base_value=1000.0,
        is_active=True,
    )


def _run(settings, benchmark_id: str, end: date | None = None):
    with market_connection(settings.database) as market_con:
        source = WarehouseSource(market_con)
        source.verify_schema()
        with backtest_connection(settings.database) as backtest_con:
            sink = BacktestSink(backtest_con, settings.sink)
            pipeline = BaselinePipeline(source, sink, settings)
            return pipeline.run(benchmark_id, end=end)


def test_synthetic_end_to_end(market_db, backtest_db, settings) -> None:
    report = _run(settings, "BI_EQW_TEST", end=date(2024, 2, 1))
    assert report.status == "success"
    assert report.rows_written == 7 + 14

    con = duckdb.connect(str(backtest_db), read_only=True)
    try:
        levels = con.execute(
            "SELECT trade_date, close, nav FROM bt_baselines "
            "WHERE benchmark_id = 'BI_EQW_TEST' ORDER BY trade_date"
        ).fetchall()
        assert len(levels) == 7
        assert levels[0][1] == pytest.approx(1000.0)
        assert levels[1][1] == pytest.approx(1150.0)
        assert levels[-1][1] == pytest.approx(1150.0)

        sums = con.execute(
            "SELECT min(total), max(total) FROM ("
            "  SELECT trade_date, sum(weight) AS total FROM bt_baseline_weights "
            "  WHERE benchmark_id = 'BI_EQW_TEST' GROUP BY trade_date)"
        ).fetchall()[0]
        assert sums[0] == pytest.approx(1.0)
        assert sums[1] == pytest.approx(1.0)

        log = con.execute(
            "SELECT status, rows_written FROM bt_compute_log WHERE target_key = 'BI_EQW_TEST'"
        ).fetchall()[0]
        assert log == ("success", 21)
    finally:
        con.close()


def test_second_run_is_idempotent(market_db, backtest_db, settings) -> None:
    _run(settings, "BI_EQW_TEST", end=date(2024, 2, 1))
    _run(settings, "BI_EQW_TEST", end=date(2024, 2, 1))
    con = duckdb.connect(str(backtest_db), read_only=True)
    try:
        assert con.execute("SELECT count(*) FROM bt_baselines").fetchall()[0][0] == 7
        assert (
            con.execute("SELECT count(*) FROM bt_baseline_weights").fetchall()[0][0]
            == 14
        )
        assert con.execute("SELECT count(*) FROM bt_benchmarks").fetchall()[0][0] == 1
        assert con.execute("SELECT count(*) FROM bt_compute_log").fetchall()[0][0] == 2
    finally:
        con.close()


def test_external_benchmark(market_db, backtest_db, settings_factory) -> None:
    settings = settings_factory(benchmarks=[_external()])
    report = _run(settings, "BI_EXT", end=date(2024, 1, 5))
    assert report.rows_written == 4
    con = duckdb.connect(str(backtest_db), read_only=True)
    try:
        rows = con.execute(
            "SELECT trade_date, close, nav FROM bt_baselines ORDER BY trade_date"
        ).fetchall()
        assert [row[1] for row in rows] == pytest.approx([100.0, 110.0, 121.0, 121.0])
        assert [row[2] for row in rows] == pytest.approx([1.0, 1.1, 1.21, 1.21])
    finally:
        con.close()


def test_dynamic_universe(market_db, backtest_db, settings_factory) -> None:
    universe = UniverseConfig(
        code="UNI_DYN",
        name="动态池",
        description=None,
        members=(),
        constituents_source=ConstituentsSourceConfig(
            kind="warehouse",
            api="index_weight",
            index_code="000300.SH",
            refresh="monthly",
            lookback_days=45,
        ),
    )
    benchmark = BenchmarkConfig(
        code="BI_DYN",
        name="动态等权",
        kind="synthetic",
        category="basket",
        symbol=None,
        universe="UNI_DYN",
        price_field="close",
        adjust="pct_chg",
        weight=WeightRule("equal", "total_mv", 1),
        rebalance=RebalanceRule("monthly", "first_trading_day"),
        base_date=date(2024, 1, 2),
        base_value=1000.0,
        is_active=True,
    )
    settings = settings_factory(benchmarks=[benchmark], universes={"UNI_DYN": universe})
    report = _run(settings, "BI_DYN", end=date(2024, 2, 1))
    assert report.status == "success"
    assert report.rows_written == 7 + 14


def test_dynamic_universe_uses_reference_day_snapshot(
    market_db, backtest_db, settings_factory
) -> None:
    """动态池再平衡用参考日（T-1）成分快照。

    market 库有 01-02 与 02-01 两期指数权重。02-01 排定再平衡时参考日是
    01-31，只能看到 01-02 快照（0.4/0.6），不能预支 02-01 才可见的
    0.5/0.5；旧实现用 T 日快照，会把 02-01 快照直接用于 02-01 定权。
    """
    universe = UniverseConfig(
        code="UNI_DYN",
        name="动态池",
        description=None,
        members=(),
        constituents_source=ConstituentsSourceConfig(
            kind="warehouse",
            api="index_weight",
            index_code="000300.SH",
            refresh="monthly",
            lookback_days=45,
        ),
    )
    benchmark = BenchmarkConfig(
        code="BI_DYN_FIXED",
        name="动态固定权重",
        kind="synthetic",
        category="basket",
        symbol=None,
        universe="UNI_DYN",
        price_field="close",
        adjust="pct_chg",
        weight=WeightRule("fixed", "total_mv", 1),
        rebalance=RebalanceRule("monthly", "first_trading_day"),
        base_date=date(2024, 1, 2),
        base_value=1000.0,
        is_active=True,
    )
    settings = settings_factory(benchmarks=[benchmark], universes={"UNI_DYN": universe})
    report = _run(settings, "BI_DYN_FIXED", end=date(2024, 2, 1))
    assert report.status == "success"

    con = duckdb.connect(str(backtest_db), read_only=True)
    try:
        rows = con.execute(
            "SELECT trade_date, symbol, target_weight FROM bt_baseline_weights "
            "WHERE benchmark_id = 'BI_DYN_FIXED' AND is_rebalance_date "
            "ORDER BY trade_date, symbol"
        ).fetchall()
    finally:
        con.close()
    by_day: dict[date, dict[str, float]] = {}
    for day, symbol, target in rows:
        by_day.setdefault(day, {})[symbol] = float(target)
    # 基准日 01-02：01-02 快照 0.4/0.6
    assert by_day[date(2024, 1, 2)] == pytest.approx(
        {"600000.SH": 0.4, "600001.SH": 0.6}
    )
    # 02-01：参考日 01-31 -> 仍是 01-02 快照，不能预支 02-01 快照的 0.5/0.5
    assert by_day[date(2024, 2, 1)] == pytest.approx(
        {"600000.SH": 0.4, "600001.SH": 0.6}
    )


def test_failure_writes_failed_log(market_db, backtest_db, settings_factory) -> None:
    settings = settings_factory(benchmarks=[_external("BI_MISSING", "999999.SH")])
    with pytest.raises(FetchError):
        _run(settings, "BI_MISSING", end=date(2024, 1, 5))
    con = duckdb.connect(str(backtest_db), read_only=True)
    try:
        row = con.execute(
            "SELECT status FROM bt_compute_log WHERE target_key = 'BI_MISSING'"
        ).fetchall()[0]
        assert row == ("failed",)
    finally:
        con.close()


def test_config_member_intervals_are_respected() -> None:
    member = MemberConfig("600000.SH", date(2024, 2, 1), end=date(2024, 2, 2))
    assert member.start <= date(2024, 2, 1) <= (member.end or date.max)
