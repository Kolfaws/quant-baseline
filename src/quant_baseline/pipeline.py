"""pipeline — 串联 WarehouseSource → processors → BacktestSink。

唯一知道完整业务流程的模块：

    market.duckdb (只读)
        -> 交易日历 + 成分行情 / 指数行情
        -> 清洗 + 收益口径 + 加权/点位
        -> backtest.duckdb: bt_benchmarks / bt_baselines / bt_baseline_weights / bt_compute_log

幂等：同一 ``(benchmark_id, trade_date)`` 重复执行只覆盖，不追加。
失败：先写 ``bt_compute_log`` 的 failed 记录，再把异常抛给调用方。
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

import polars as pl
from quant_schema.constants import (
    BT_BASELINE_WEIGHTS,
    BT_BASELINES,
    BT_BENCHMARKS,
    BT_COMPUTE_LOG,
)

from .core.config import (
    BenchmarkConfig,
    Settings,
    UniverseConfig,
)
from .core.exceptions import ProcessError
from .processors.adjuster import compute_returns
from .processors.cleaner import clean_daily
from .processors.weight_calc import (
    MembershipSnapshot,
    MembershipTimeline,
    build_external_series,
    build_panel,
    build_synthetic_benchmark,
)
from .sinks.backtest_sink import BacktestSink
from .sources.warehouse_source import WarehouseSource

LOG = logging.getLogger("quant_baseline.pipeline")

_DEFINITION_SCHEMA: dict[str, pl.DataType] = {
    "benchmark_id": pl.Utf8(),
    "name": pl.Utf8(),
    "kind": pl.Utf8(),
    "category": pl.Utf8(),
    "symbol": pl.Utf8(),
    "universe_id": pl.Utf8(),
    "price_field": pl.Utf8(),
    "adjust": pl.Utf8(),
    "weight_method": pl.Utf8(),
    "weight_field": pl.Utf8(),
    "weight_lag": pl.Int32(),
    "rebalance_freq": pl.Utf8(),
    "rebalance_align": pl.Utf8(),
    "base_date": pl.Date(),
    "base_value": pl.Float64(),
    "is_active": pl.Boolean(),
    "updated_at": pl.Datetime("us", "UTC"),
}
_LOG_SCHEMA: dict[str, pl.DataType] = {
    "run_id": pl.Utf8(),
    "job_name": pl.Utf8(),
    "target_key": pl.Utf8(),
    "status": pl.Utf8(),
    "rows_written": pl.Int64(),
    "start_date": pl.Date(),
    "end_date": pl.Date(),
    "error_message": pl.Utf8(),
    "started_at": pl.Datetime("us", "UTC"),
    "finished_at": pl.Datetime("us", "UTC"),
}


@dataclass(frozen=True, slots=True)
class RunReport:
    run_id: str
    benchmark_id: str
    start: date | None
    end: date | None
    status: str
    rows_written: int
    elapsed_s: float
    error: str | None = None


def _now() -> datetime:
    return datetime.now(UTC)


class BaselinePipeline:
    """基准计算主流程。"""

    def __init__(
        self, source: WarehouseSource, sink: BacktestSink, settings: Settings
    ) -> None:
        self._source = source
        self._sink = sink
        self._settings = settings

    # ------------------------------------------------------------------ 入口
    def run(
        self,
        benchmark_id: str,
        start: date | None = None,
        end: date | None = None,
    ) -> RunReport:
        settings = self._settings
        benchmark = settings.baseline.benchmark(benchmark_id)
        calendar_name = settings.baseline.sync.trade_calendar
        started_at = _now()
        started_monotonic = time.monotonic()
        run_id = self._run_id(benchmark_id)

        end_date = (
            end
            or settings.baseline.sync.end_date
            or self._source.latest_trade_date(calendar_name)
        )
        if end_date is None:
            raise ProcessError("无法确定同步结束日：market.duckdb 没有交易日历/日线")
        start_date = start or max(
            settings.baseline.sync.start_date, benchmark.base_date
        )
        if end_date < benchmark.base_date:
            raise ProcessError(
                f"同步区间 {start_date}..{end_date} 早于基准日 {benchmark.base_date}",
                benchmark=benchmark_id,
            )

        warmup_start = benchmark.base_date - timedelta(
            days=int(settings.baseline.defaults.warmup_days)
        )
        calendar = self._source.trading_days(warmup_start, end_date, calendar_name)
        if not calendar:
            raise ProcessError(
                f"{warmup_start}..{end_date} 没有交易日；请检查 market.duckdb 的交易历",
                benchmark=benchmark_id,
            )

        LOG.info(
            "开始 %s (%s): %s..%s run_id=%s",
            benchmark_id,
            benchmark.kind,
            start_date,
            end_date,
            run_id,
        )

        try:
            levels, weights = self._compute(benchmark, calendar, end_date)
            levels = levels.filter(pl.col("trade_date") >= start_date)
            if levels.is_empty():
                raise ProcessError(
                    f"基准 {benchmark_id} 在 {start_date}..{end_date} 没有输出",
                    benchmark=benchmark_id,
                )
            written = self._write(
                benchmark, levels, weights, run_id, start_date, started_at
            )
            report = RunReport(
                run_id=run_id,
                benchmark_id=benchmark_id,
                start=start_date,
                end=end_date,
                status="success",
                rows_written=written,
                elapsed_s=time.monotonic() - started_monotonic,
            )
            LOG.info(
                "完成 %s rows=%d elapsed=%.2fs",
                benchmark_id,
                written,
                report.elapsed_s,
            )
            return report
        except Exception as exc:
            self._log_failure(
                benchmark_id, run_id, start_date, end_date, started_at, exc
            )
            raise

    def run_all(self, trade_date: date | None = None) -> list[RunReport]:
        """计算所有启用的基准；单个失败不影响其他基准。"""
        self._upsert_definitions(self._settings.baseline.enabled_benchmarks())
        reports: list[RunReport] = []
        for benchmark in self._settings.baseline.enabled_benchmarks():
            try:
                reports.append(
                    self.run(benchmark.code, end=trade_date if trade_date else None)
                )
            except Exception as exc:  # noqa: BLE001 - 单基准错误隔离
                LOG.error("基准 %s 失败：%s", benchmark.code, exc)
                reports.append(
                    RunReport(
                        run_id="-",
                        benchmark_id=benchmark.code,
                        start=None,
                        end=trade_date,
                        status="failed",
                        rows_written=0,
                        elapsed_s=0.0,
                        error=str(exc),
                    )
                )
        return reports

    # ------------------------------------------------------------------ 计算
    def _compute(
        self, benchmark: BenchmarkConfig, calendar: list[date], end_date: date
    ) -> tuple[pl.DataFrame, pl.DataFrame | None]:
        if benchmark.kind == "external":
            return self._compute_external(benchmark, calendar, end_date)
        return self._compute_synthetic(benchmark, calendar, end_date)

    def _compute_external(
        self, benchmark: BenchmarkConfig, calendar: list[date], end_date: date
    ) -> tuple[pl.DataFrame, pl.DataFrame | None]:
        assert benchmark.symbol is not None
        warmup_start = calendar[0]
        if benchmark.category == "etf":
            raw = self._source.fund_daily(benchmark.symbol, warmup_start, end_date)
            table = "fact_fund_daily"
        else:
            raw = self._source.index_daily(benchmark.symbol, warmup_start, end_date)
            table = "fact_index_daily"

        cleaned = clean_daily(
            raw,
            table=table,
            missing_policy=self._settings.baseline.defaults.missing_policy,
        )
        frame = compute_returns(cleaned, "pct_chg")
        levels = build_external_series(
            frame, base_date=benchmark.base_date, label=benchmark.code
        )
        actual_base = levels["trade_date"][0]
        base_value = float(levels["close"][0])
        levels = levels.with_columns(
            pl.lit(actual_base).alias("base_date"),
            pl.lit(base_value).alias("base_value"),
            pl.lit(benchmark.kind).alias("kind"),
            pl.lit(benchmark.category).alias("category"),
            pl.lit(f"market.duckdb:{table}:{benchmark.symbol}").alias("source"),
        )
        return levels, None

    def _compute_synthetic(
        self, benchmark: BenchmarkConfig, calendar: list[date], end_date: date
    ) -> tuple[pl.DataFrame, pl.DataFrame | None]:
        assert benchmark.universe is not None
        universe = self._settings.baseline.universe(benchmark.universe)
        warmup_start = calendar[0]
        timeline, symbols = self._load_membership(universe, warmup_start, end_date)

        raw = self._source.stock_daily(symbols, warmup_start, end_date)
        cleaned = clean_daily(
            raw,
            table="fact_daily",
            missing_policy=self._settings.baseline.defaults.missing_policy,
        )
        frame = compute_returns(cleaned, benchmark.adjust)
        assert benchmark.weight is not None
        panel = build_panel(frame, market_cap_field=benchmark.weight.field)
        assert benchmark.rebalance is not None
        levels, weights = build_synthetic_benchmark(
            panel,
            calendar,
            base_date=benchmark.base_date,
            base_value=benchmark.base_value,
            frequency=benchmark.rebalance.frequency,
            align=benchmark.rebalance.align,
            method=benchmark.weight.method,
            timeline=timeline,
            market_cap_field=benchmark.weight.field,
            lag=benchmark.weight.lag,
            label=benchmark.code,
        )
        actual_base = levels["trade_date"][0]
        levels = levels.with_columns(
            pl.lit(actual_base).alias("base_date"),
            pl.lit(float(benchmark.base_value)).alias("base_value"),
            pl.lit(benchmark.kind).alias("kind"),
            pl.lit(benchmark.category).alias("category"),
            pl.lit(f"market.duckdb:synthetic:{benchmark.universe}").alias("source"),
        )
        return levels, weights

    def _load_membership(
        self, universe: UniverseConfig, start: date, end: date
    ) -> tuple[MembershipTimeline, tuple[str, ...]]:
        if not universe.is_dynamic:
            members = tuple(
                member
                for member in universe.members
                if member.start <= end and (member.end is None or member.end >= start)
            )
            if not members:
                raise ProcessError(
                    f"标的池 {universe.code} 在 {start}..{end} 没有有效成分"
                )
            return MembershipTimeline(static_members=members), tuple(
                member.symbol for member in members
            )

        source = universe.constituents_source
        assert source is not None
        snapshots_start = start - timedelta(days=source.lookback_days)
        raw = self._source.index_weights(source.index_code, snapshots_start, end)
        snapshots: list[MembershipSnapshot] = []
        for group in raw.partition_by("trade_date", maintain_order=True):
            snapshot_date = group["trade_date"][0]
            weights = {
                str(row["con_code"]): None
                if row["weight"] is None
                else float(row["weight"])
                for row in group.iter_rows(named=True)
            }
            snapshots.append(
                MembershipSnapshot(trade_date=snapshot_date, weights=weights)
            )
        symbols = tuple(
            sorted({symbol for item in snapshots for symbol in item.weights})
        )
        if not symbols:
            raise ProcessError(f"标的池 {universe.code} 的指数权重快照为空")
        LOG.info(
            "动态标的池 %s：%d 期快照，%d 只成分",
            universe.code,
            len(snapshots),
            len(symbols),
        )
        return MembershipTimeline(snapshots=tuple(snapshots)), symbols

    # ------------------------------------------------------------------ 写入
    def _write(
        self,
        benchmark: BenchmarkConfig,
        levels: pl.DataFrame,
        weights: pl.DataFrame | None,
        run_id: str,
        start_date: date,
        started_at: datetime,
    ) -> int:
        self._upsert_definitions([benchmark])
        now = _now()

        baseline_frame = levels.with_columns(
            pl.lit(benchmark.code).alias("benchmark_id"),
            pl.lit(run_id).alias("run_id"),
            pl.lit(now).alias("updated_at"),
        ).select(
            "benchmark_id",
            "trade_date",
            "close",
            "ret_1d",
            "nav",
            "base_date",
            "base_value",
            "kind",
            "category",
            "source",
            "run_id",
            "updated_at",
        )
        # 这些列名必须与 quant_schema.backtest_ddl.bt_baselines 完全一致
        self._sink.write(BT_BASELINES, baseline_frame)
        weight_rows = 0

        if weights is not None and not weights.is_empty():
            weight_frame = (
                weights.filter(pl.col("trade_date") >= start_date)
                .with_columns(
                    pl.lit(benchmark.code).alias("benchmark_id"),
                    pl.lit(run_id).alias("run_id"),
                )
                .select(
                    "benchmark_id",
                    "trade_date",
                    "symbol",
                    "weight",
                    "target_weight",
                    "price",
                    "market_value",
                    "is_rebalance_date",
                    "run_id",
                )
            )
            self._sink.write(BT_BASELINE_WEIGHTS, weight_frame)
            weight_rows = int(weight_frame.height)

        rows = int(baseline_frame.height) + weight_rows
        self._sink.write(
            BT_COMPUTE_LOG,
            self._log_frame(
                run_id=run_id,
                target_key=benchmark.code,
                status="success",
                rows=rows,
                start_date=start_date,
                end_date=levels["trade_date"][-1],
                started_at=started_at,
                error=None,
            ),
        )
        return rows

    def _upsert_definitions(self, benchmarks: list[BenchmarkConfig]) -> None:
        if not benchmarks:
            return
        now = _now()
        rows = []
        for benchmark in benchmarks:
            weight = benchmark.weight
            rebalance = benchmark.rebalance
            rows.append(
                {
                    "benchmark_id": benchmark.code,
                    "name": benchmark.name,
                    "kind": benchmark.kind,
                    "category": benchmark.category,
                    "symbol": benchmark.symbol,
                    "universe_id": benchmark.universe,
                    "price_field": benchmark.price_field,
                    "adjust": benchmark.adjust,
                    "weight_method": None if weight is None else weight.method,
                    "weight_field": None if weight is None else weight.field,
                    "weight_lag": None if weight is None else weight.lag,
                    "rebalance_freq": None
                    if rebalance is None
                    else rebalance.frequency,
                    "rebalance_align": None if rebalance is None else rebalance.align,
                    "base_date": benchmark.base_date,
                    "base_value": (
                        benchmark.base_value if benchmark.kind == "synthetic" else None
                    ),
                    "is_active": benchmark.is_active,
                    "updated_at": now,
                }
            )
        self._sink.write(BT_BENCHMARKS, pl.DataFrame(rows, schema=_DEFINITION_SCHEMA))

    def _log_frame(
        self,
        *,
        run_id: str,
        target_key: str,
        status: str,
        rows: int,
        start_date: date | None,
        end_date: date | None,
        started_at: datetime,
        error: str | None,
    ) -> pl.DataFrame:
        return pl.DataFrame(
            {
                "run_id": [run_id],
                "job_name": ["baseline"],
                "target_key": [target_key],
                "status": [status],
                "rows_written": [int(rows)],
                "start_date": [start_date],
                "end_date": [end_date],
                "error_message": [None if error is None else error[:1000]],
                "started_at": [started_at],
                "finished_at": [_now()],
            },
            schema=_LOG_SCHEMA,
        )

    def _log_failure(
        self,
        benchmark_id: str,
        run_id: str,
        start_date: date,
        end_date: date,
        started_at: datetime,
        exc: Exception,
    ) -> None:
        try:
            self._sink.write(
                BT_COMPUTE_LOG,
                self._log_frame(
                    run_id=run_id,
                    target_key=benchmark_id,
                    status="failed",
                    rows=0,
                    start_date=start_date,
                    end_date=end_date,
                    started_at=started_at,
                    error=str(exc),
                ),
            )
        except Exception:
            LOG.exception("写 bt_compute_log 失败")

    # ------------------------------------------------------------------ 工具
    @staticmethod
    def _run_id(benchmark_id: str) -> str:
        stamp = _now().strftime("%Y%m%dT%H%M%S")
        suffix = uuid.uuid4().hex[:8]
        return f"{benchmark_id}-{stamp}-{suffix}"


__all__ = ["BaselinePipeline", "RunReport"]
