# API Reference — quant_baseline

> 状态：`v0.1.0` 已实现。以下签名与当前代码一致；变更需同步 README 与
> `docs/db_schema.md`。

## 1. 配置 `quant_baseline.core.config`

```python
load_settings(config_dir: Path | str | None = None,
              profile: str | None = None) -> Settings
```

读取 `configs/baseline.yaml` + `configs/database.yaml`，展开 `${ENV}` 占位符，
严格校验未知字段/枚举/日期。失败抛 `ConfigError`。

`Settings` 字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `baseline` | `BaselineConfig` | 默认值、标的池、基准、同步区间 |
| `database` | `ProfileConfig` | market/backtest 路径、只读开关、资源 |
| `sink` | `SinkConfig` | 表名、幂等键、批大小、写后校验 |
| `config_dir` | `Path` | 配置目录 |

关键子模型：

| 模型 | 字段 |
| --- | --- |
| `DefaultsConfig` | `calendar, price_field, adjust, missing_policy, base_date, base_value, rebalance, weight, warmup_days` |
| `UniverseConfig` | `code, name, members, constituents_source`；`is_dynamic` |
| `BenchmarkConfig` | `code, name, kind, category, symbol, universe, adjust, weight, rebalance, base_date, base_value, is_active` |
| `ProfileConfig` | `name, market_db_path, backtest_db_path, market_read_only, threads, memory_limit, temp_dir` |
| `SinkConfig` | `table, conflict_keys, batch_size, verify_after_write, table_keys` |

`BaselineConfig` 方法：`benchmark(code)`、`universe(code)`、`enabled_benchmarks()`。

## 2. 连接 `quant_baseline.core.db`

```python
connect_market(profile: ProfileConfig) -> duckdb.DuckDBPyConnection   # 强制 read_only
connect_backtest(profile: ProfileConfig) -> duckdb.DuckDBPyConnection

market_connection(profile)    # contextmanager
backtest_connection(profile)  # contextmanager
```

连接器复用 `data_pipeline.db.connect`。`market_read_only=false` 会抛 `ConfigError`。

## 3. 数据源 `quant_baseline.sources.warehouse_source`

```python
WarehouseSource(con)

verify_schema() -> None
trading_days(start, end, calendar="SSE") -> list[date]
latest_trade_date(calendar="SSE", as_of=None) -> date | None
stock_daily(symbols: tuple[str, ...], start, end) -> pl.DataFrame
index_daily(index_code, start, end) -> pl.DataFrame
fund_daily(fund_code, start, end) -> pl.DataFrame
index_weights(index_code, start, end) -> pl.DataFrame
index_snapshot_dates(index_code) -> list[date]
table_exists(name) / table_row_count(name)
```

返回列：

- `stock_daily`：`symbol, trade_date, close, pre_close, pct_chg, adj_factor, total_mv, circ_mv`
- `index_daily` / `fund_daily`：`symbol, trade_date, close, pre_close, pct_chg`
- `index_weights`：`trade_date, con_code, weight`

无数据抛 `FetchError`；表缺失抛 `SchemaError`。

## 4. 处理器 `quant_baseline.processors`

### cleaner

```python
clean_daily(frame, *, table="daily", missing_policy="drop") -> pl.DataFrame
```

去重、类型规整、丢弃 `close<=0`；`forward_fill` 只填充市值/复权因子。

### adjuster

```python
compute_returns(frame, mode="pct_chg") -> pl.DataFrame
```

`mode ∈ {pct_chg, hfq, qfq, none}`；输出 `ret_1d`、`close_adj`。
异常收益（`|ret|>100%`、NaN）抛 `ProcessError`。

### weight_calc

```python
DayPoint(ret, close, market_value)
MembershipSnapshot(trade_date, weights)
MembershipTimeline(*, static_members=None, snapshots=None)
    .at(day) -> dict[str, float | None]

build_panel(frame, *, market_cap_field="total_mv") -> dict[date, dict[str, DayPoint]]
rebalance_dates(calendar, frequency, align="first_trading_day") -> set[date]
target_weights(*, members, method, panel, reference_day, available, market_cap_field)

build_synthetic_benchmark(
    panel, calendar, *, base_date, base_value, frequency, align,
    method, timeline, market_cap_field="total_mv", lag=1, label="benchmark"
) -> tuple[pl.DataFrame, pl.DataFrame]

build_external_series(frame, *, base_date, label="benchmark") -> pl.DataFrame
```

`build_synthetic_benchmark` 返回：

- levels：`trade_date, close, ret_1d, nav`
- weights：`trade_date, symbol, weight, target_weight, price, market_value, is_rebalance_date`

约束：base_date 点位等于 base_value；每日权重和 =1；目标权重只用 `T-lag` 数据。

## 5. 写入 `quant_baseline.sinks.backtest_sink`

```python
BacktestSink(con, settings: SinkConfig)

verify_ready() -> None
table_exists(name) -> bool
write(table, frame, *, conflict_keys=None) -> WriteResult

WriteResult(table: str, rows_written: int)
```

只允许写 `bt_benchmarks` / `bt_baselines` / `bt_baseline_weights` / `bt_compute_log`。
未知列、缺主键列、非白名单表抛 `DBWriteError`；每批一个事务；
`verify_after_write` 时做全表主键唯一性检查。

## 6. 主流程 `quant_baseline.pipeline`

```python
BaselinePipeline(source: WarehouseSource, sink: BacktestSink, settings: Settings)

run(benchmark_id, start=None, end=None) -> RunReport
run_all(trade_date=None) -> list[RunReport]

RunReport(run_id, benchmark_id, start, end, status,
          rows_written, elapsed_s, error)
```

`run`：

1. 解析配置、确定区间与 run_id；
2. 从 `base_date - warmup_days` 读交易历与行情；
3. external：指数/ETF → `build_external_series`；
   synthetic：成分 → 清洗 → 收益 → `build_synthetic_benchmark`；
4. 先写 `bt_benchmarks`，再写 `bt_baselines` / `bt_baseline_weights`；
5. 成功/失败都写 `bt_compute_log`。

`run_all` 跳过 `enabled=false`，单基准异常不影响其他基准。

## 7. 日志与异常

```python
setup_logging(level=None, config_path=None) -> None
get_logger(name) -> logging.Logger
```

异常层级：

```text
QuantBaselineError(RuntimeError)
├── ConfigError(ValueError)
├── FetchError
├── ProcessError
├── DBWriteError
└── SchemaError
```

每个异常携带 `context: dict`，`str(exc)` 会附带上下文。

## 8. CLI

| 脚本 | 参数 | 作用 |
| --- | --- | --- |
| `sync_baseline.py` | `--benchmark --date --profile --dry-run --log-level` | 增量同步；首次自动全量 |
| `backfill_history.py` | `--benchmark`(可重复) `--all --start --end --chunk-days --profile` | 分片回补 |
| `check_connections.py` | `--profile` | 只读检查两库结构/版本/行数 |

退出码：`0` 成功；`1` 业务/数据库异常；`2` 配置或参数非法（argparse）。

## 9. 示例

```python
from datetime import date

from quant_baseline.core.config import load_settings
from quant_baseline.core.db import backtest_connection, market_connection
from quant_baseline.core.logger import setup_logging
from quant_baseline.pipeline import BaselinePipeline
from quant_baseline.sinks.backtest_sink import BacktestSink
from quant_baseline.sources.warehouse_source import WarehouseSource

setup_logging()
settings = load_settings()

with market_connection(settings.database) as market_con:
    source = WarehouseSource(market_con)
    source.verify_schema()
    with backtest_connection(settings.database) as backtest_con:
        sink = BacktestSink(backtest_con, settings.sink)
        pipeline = BaselinePipeline(source, sink, settings)
        report = pipeline.run("BI_EQW_CORE", end=date(2026, 11, 5))
        print(report.status, report.rows_written)
```
