"""tests.test_sinks — backtest.duckdb upsert、批处理、严格列校验。"""

from __future__ import annotations

from datetime import UTC, date, datetime

import duckdb
import polars as pl
import pytest

from quant_baseline.core.exceptions import DBWriteError
from quant_baseline.sinks.backtest_sink import BacktestSink

_BASELINE_SCHEMA = {
    "benchmark_id": pl.Utf8,
    "trade_date": pl.Date,
    "close": pl.Float64,
    "ret_1d": pl.Float64,
    "nav": pl.Float64,
    "base_date": pl.Date,
    "base_value": pl.Float64,
    "kind": pl.Utf8,
    "category": pl.Utf8,
    "source": pl.Utf8,
    "run_id": pl.Utf8,
}


def _baseline_frame(closes: list[float], run_id: str = "run-1") -> pl.DataFrame:
    dates = [date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4)][: len(closes)]
    return pl.DataFrame(
        {
            "benchmark_id": ["BI_TEST"] * len(closes),
            "trade_date": dates,
            "close": closes,
            "ret_1d": [0.0]
            + [closes[i] / closes[i - 1] - 1 for i in range(1, len(closes))],
            "nav": [value / closes[0] for value in closes],
            "base_date": [date(2024, 1, 2)] * len(closes),
            "base_value": [closes[0]] * len(closes),
            "kind": ["synthetic"] * len(closes),
            "category": ["basket"] * len(closes),
            "source": ["test"] * len(closes),
            "run_id": [run_id] * len(closes),
        },
        schema=_BASELINE_SCHEMA,
    )


def test_upsert_is_idempotent(backtest_db, settings) -> None:
    con = duckdb.connect(str(backtest_db))
    try:
        sink = BacktestSink(con, settings.sink)
        sink.write("bt_baselines", _baseline_frame([1000.0, 1100.0, 1210.0]))
        first = con.execute("SELECT count(*) FROM bt_baselines").fetchall()[0][0]
        sink.write(
            "bt_baselines", _baseline_frame([1000.0, 1100.0, 1200.0], run_id="run-2")
        )
        second = con.execute("SELECT count(*) FROM bt_baselines").fetchall()[0][0]
        assert first == 3 and second == 3
        row = con.execute(
            "SELECT close, run_id FROM bt_baselines WHERE trade_date = '2024-01-04'"
        ).fetchall()[0]
        assert row[0] == pytest.approx(1200.0)
        assert row[1] == "run-2"
    finally:
        con.close()


def test_batches_and_log_table(backtest_db, settings) -> None:
    con = duckdb.connect(str(backtest_db))
    try:
        sink = BacktestSink(con, settings.sink)
        # batch_size=2，5 行会拆成 3 批
        frame = _baseline_frame([1000.0, 1100.0, 1210.0])
        extra = frame.slice(0, 2).with_columns(pl.lit("BI_OTHER").alias("benchmark_id"))
        result = sink.write("bt_baselines", pl.concat([frame, extra]))
        assert result.rows_written == 5
        log = pl.DataFrame(
            {
                "run_id": ["run-1"],
                "job_name": ["baseline"],
                "target_key": ["BI_TEST"],
                "status": ["success"],
                "rows_written": [5],
                "start_date": [date(2024, 1, 2)],
                "end_date": [date(2024, 1, 4)],
                "error_message": [None],
                "started_at": [datetime(2024, 1, 2, tzinfo=UTC)],
                "finished_at": [datetime(2024, 1, 2, tzinfo=UTC)],
            },
            schema={
                "run_id": pl.Utf8,
                "job_name": pl.Utf8,
                "target_key": pl.Utf8,
                "status": pl.Utf8,
                "rows_written": pl.Int64,
                "start_date": pl.Date,
                "end_date": pl.Date,
                "error_message": pl.Utf8,
                "started_at": pl.Datetime("us", "UTC"),
                "finished_at": pl.Datetime("us", "UTC"),
            },
        )
        sink.write("bt_compute_log", log)
        assert con.execute("SELECT count(*) FROM bt_compute_log").fetchall()[0][0] == 1
    finally:
        con.close()


def test_unknown_column_rejected(backtest_db, settings) -> None:
    con = duckdb.connect(str(backtest_db))
    try:
        sink = BacktestSink(con, settings.sink)
        frame = _baseline_frame([1000.0]).with_columns(pl.lit(1.0).alias("oops"))
        with pytest.raises(DBWriteError, match="表结构外的列"):
            sink.write("bt_baselines", frame)
    finally:
        con.close()


def test_non_writable_table_rejected(backtest_db, settings) -> None:
    con = duckdb.connect(str(backtest_db))
    try:
        sink = BacktestSink(con, settings.sink)
        with pytest.raises(DBWriteError, match="不允许写表"):
            sink.write("bt_nav", pl.DataFrame({"run_id": ["x"]}))
    finally:
        con.close()
