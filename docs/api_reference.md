# API Reference —— 核心类与方法说明

> **状态**：接口草案（`v0.1.0`）。当前仓库只有占位实现，签名以本文件为准，
> 落地后如有调整需同步更新此处。

本文档描述 `quant_baseline` 的**对外契约**：谁可以被其他模块 import、每个方法吃什么、吐什么、
失败时抛什么异常。内部实现细节（SQL 拼接、分页、重试）不在契约范围内。

---

## 1. 配置层 `quant_baseline.core.config`

### `load_settings(config_dir: Path | None = None) -> Settings`

加载并校验全部配置。

| 参数 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `config_dir` | `Path \| None` | `None` | 配置文件目录；`None` 时用项目根下 `configs/` |

**返回**：`Settings`（不可变），包含三个子配置：

```python
settings.baseline   # BaselineConfig：universes / benchmarks / sync
settings.database   # DatabaseConfig：active_profile / profiles / sink / schema
settings.logging    # LoggingConfig：YAML 原始映射 + 已解析的 level
```

**异常**：`ConfigError` —— 文件缺失、YAML 语法错误、字段未知、枚举值非法、`${VAR}` 未定义。

**约定**：

- 校验是**严格**的（`model_config = ConfigDict(extra="forbid")`），写错字段名会立刻报错，
  而不是静默使用默认值。
- 结果可缓存；测试中用 `config_dir` 注入临时目录，避免污染真实配置。

### 关键模型（规划）

| 模型 | 关键字段 | 校验规则 |
| --- | --- | --- |
| `Settings` | `baseline`, `database`, `logging`, `env` | 三者版本号一致 |
| `BaselineConfig` | `version`, `defaults`, `universes`, `benchmarks`, `sync` | `code` 全局唯一；`kind` 与必填字段匹配 |
| `UniverseConfig` | `code`, `name`, `members`, `constituents_source` | `static` 必须有 `members`，`dynamic` 必须有 `constituents_source` |
| `BenchmarkConfig` | `code`, `kind`, `symbol` / `universe`, `weight`, `rebalance` | `external` 必须有 `symbol`；`synthetic` 必须有 `universe` 且该池存在 |
| `SyncConfig` | `start_date`, `end_date`, `incremental_lookback_days`, `chunk_days` | `start <= end`；`chunk_days > 0` |

---

## 2. 基础设施 `quant_baseline.core`

### `setup_logging(level: str | None = None, config_path: Path | None = None) -> None`

按 `configs/logging.yaml` 初始化日志；`level` 可临时覆盖控制台级别。幂等。

### `get_logger(name: str) -> logging.Logger`

返回 `quant_baseline` 命名空间下的 logger，业务代码统一使用。

### 异常体系 `quant_baseline.core.exceptions`

```text
QuantBaselineError(Exception)
├── ConfigError      配置缺失 / 非法 / 校验失败
├── FetchError       取数失败（网络、限流、Token、空数据）
├── ProcessError     清洗 / 加权失败（列缺失、日期不对齐、除零、权重和不为 1）
├── DBWriteError     落库失败（约束冲突、事务回滚）
└── SchemaError      仓库结构与 docs/db_schema.md 不一致
```

所有异常携带 `context: dict[str, Any]`（如 `benchmark_code`、`trade_date`、`table`），
`__str__` 输出“消息 + 上下文”，便于单行定位。

---

## 3. 数据源层 `quant_baseline.sources`

### `class AbstractSource(ABC)`

| 方法 | 签名 | 返回 |
| --- | --- | --- |
| `fetch_trade_calendar` | `(start: date, end: date)` | `DataFrame[cal_date, is_open]` |
| `fetch_index_daily` | `(symbol: str, start: date, end: date)` | `DataFrame[trade_date, open, high, low, close, volume, amount]` |
| `fetch_etf_daily` | `(symbol: str, start: date, end: date)` | 同上 |
| `fetch_stock_daily` | `(symbols: list[str], start: date, end: date)` | `DataFrame[symbol, trade_date, ...]` |
| `fetch_adj_factor` | `(symbols: list[str], start: date, end: date)` | `DataFrame[symbol, trade_date, adj_factor]` |
| `fetch_daily_basic` | `(symbols: list[str], start: date, end: date)` | `DataFrame[symbol, trade_date, total_mv, circ_mv, ...]` |
| `fetch_index_weight` | `(index_code: str, trade_date: date)` | `DataFrame[symbol, weight]` |

**共同约定**：

- 返回列名直接对齐 `docs/db_schema.md` 的 `fact_*` 字段（小写下划线），不做二次改名。
- 空结果返回**带列名的空 DataFrame**，不抛异常，由调用方决定是否算失败。
- 任何底层异常（HTTP、超时、Token 失效）统一包装为 `FetchError`。
- 实例化时不发请求；token 从 `TUSHARE_TOKEN` 读取，绝不打印。

### `class TushareSource(AbstractSource)`

```python
TushareSource(token: str | None = None, rate_limit: RateLimit | None = None)
```

`token=None` 时读环境变量；缺少 token 立即 `ConfigError`（快速失败，不等到第一次请求）。

### `class AkShareSource(AbstractSource)`

```python
AkShareSource(rate_limit: RateLimit | None = None)
```

可选依赖：未安装 `akshare` 时抛 `ConfigError` 并提示 `pip install -e ".[akshare]"`。
接口契约与 `TushareSource` 完全一致，可互换。

---

## 4. 处理层 `quant_baseline.processors`

### 4.1 `cleaner`

```python
clean(df: pd.DataFrame, *, table: str, keys: list[str],
      missing_policy: str = "drop") -> pd.DataFrame
```

依次执行：去重 → 类型规整 → 缺失处理 → 异常值标记 → 按 `table` 裁剪列。

| 步骤 | 行为 | 失败处理 |
| --- | --- | --- |
| 去重 | 按 `keys` 保留最后一条 | — |
| 类型规整 | `trade_date`→date、价格→float | 无法转换的行剔除并计数 |
| 缺失处理 | `drop` 删行 / `forward_fill` 前向填充 | 全列为空 → `ProcessError` |
| 异常值 | `close<=0`、`high<low`、涨跌幅越界 | 标记 `is_invalid`，不静默丢弃 |
| 列裁剪 | 只保留 schema 定义列 | 缺列 → `ProcessError`；多列 → 按 `strict_columns` 决定 |

### 4.2 `adjuster`

```python
adjust(df: pd.DataFrame, factor: pd.DataFrame, mode: Literal["none", "qfq", "hfq"],
       base_date: date | None = None) -> pd.DataFrame

align_calendar(df: pd.DataFrame, calendar: pd.DataFrame,
               fill: Literal["mark", "drop"] = "mark") -> pd.DataFrame

rebase(series: pd.Series, base_date: date, base_value: float = 1000.0) -> pd.Series
```

- `hfq`：`price * factor(t) / factor(base_date)`；`qfq` 用最新因子归一。
- `align_calendar`：把成分序列对齐到统一交易日历；缺失日标记 `is_suspended=True` 而非填价。
- `rebase`：保证 `series[base_date] == base_value`，否则 `ProcessError`。

### 4.3 `weight_calc`

```python
compute_weights(prices: pd.DataFrame, method: Literal["equal", "market_cap", "fixed"],
                *, members: pd.DataFrame, market_cap: pd.DataFrame | None = None,
                rebalance: RebalanceRule, lag: int = 1) -> pd.DataFrame
```

返回 `DataFrame[trade_date, symbol, weight, target_weight, is_rebalance_date]`。

```python
compute_index_level(prices: pd.DataFrame, weights: pd.DataFrame,
                    *, base_date: date, base_value: float = 1000.0) -> pd.DataFrame
```

返回 `DataFrame[trade_date, close, pct_chg]`，与 `fact_benchmark_daily` 字段对齐。

**必须保证**：

1. 每日权重和为 1（误差 `< 1e-8`），否则 `ProcessError`。
2. 不使用未来信息：`t` 日权重只能来自 `t-1` 及之前的数据。
3. 停牌成分当日收益按 0 处理并重新归一权重（口径见 `db_schema.md` 待定项）。
4. 基准日点位严格等于 `base_value`。

---

## 5. 落库层 `quant_baseline.sinks`

### `class AbstractSink(ABC)`

```python
write(table: str, df: pd.DataFrame, *, conflict_keys: list[str] | None = None) -> WriteResult
exists(table: str) -> bool
row_count(table: str, where: str | None = None) -> int
```

`WriteResult`：`dataclass(rows_written: int, rows_skipped: int, table: str)`。

### `class WarehouseSink(AbstractSink)`

```python
WarehouseSink(con: DuckDBPyConnection | sqlite3.Connection,
              *, settings: SinkConfig, schema: dict[str, TableSpec])
```

- 连接由调用方持有，sink **不关闭**连接。
- `write` 内部按 `settings.batch_size` 分批，整批一个事务，失败整体回滚 → `DBWriteError`。
- `upsert_mode` 决定 SQL 形态（`on_conflict` / `delete_insert` / `append`）。
- `strict_columns=True` 时，df 出现表结构外的列直接 `DBWriteError`。

---

## 6. 主流程 `quant_baseline.pipeline`

### `class BaselinePipeline`

```python
BaselinePipeline(source: AbstractSource, sink: AbstractSink, settings: Settings)

run(benchmark_code: str, start: date | None = None, end: date | None = None) -> RunReport
run_all(trade_date: date | None = None, *, dry_run: bool = False) -> list[RunReport]
```

`RunReport`：`dataclass(run_id, benchmark_code, start, end, status, rows_written, elapsed_s, error)`。

**执行顺序**（`synthetic` 基准）：

```text
1. 解析 dim_benchmark / dim_universe / dim_universe_member（某日有效成分）
2. fetch_stock_daily + fetch_adj_factor + fetch_daily_basic
3. cleaner.clean → adjuster.adjust → adjuster.align_calendar
4. weight_calc.compute_weights → compute_index_level
5. sink.write(fact_constituent_daily)
6. sink.write(fact_benchmark_weight)
7. sink.write(fact_benchmark_daily)
8. 更新 meta_sync_state，写 etl_run_log
```

**幂等性**：同一 `(benchmark_code, start, end)` 重跑，`fact_*` 行数不变；
`meta_sync_state` 与 `etl_run_log` 会新增记录（日志表只追加，这是预期行为）。

**错误隔离**：`run_all` 中单个基准失败不中断其他基准，失败信息写入 `RunReport.error`，
最终整体状态为 `partial`。

---

## 7. 脚本入口（CLI）

| 脚本 | 作用 | 主要参数 |
| --- | --- | --- |
| `scripts/init_warehouse.py` | 建表 / 建索引 / 校验结构 | `--profile` `--db` `--check` `--force` |
| `scripts/sync_baseline.py` | 日常增量同步 | `--benchmark` `--date` `--dry-run` `--profile` |
| `scripts/backfill_history.py` | 历史区间回补 | `--benchmark` `--universe` `--all` `--start` `--end` `--chunk-days` |

**统一退出码**：`0` 成功；`1` 业务/数据库异常；`2` 配置或参数非法。

> 三个脚本都支持 `--log-level`，默认取 `LOG_LEVEL` 环境变量。

---

## 8. 使用示例（目标形态）

```python
from datetime import date

from quant_baseline.core.config import load_settings
from quant_baseline.core.logger import setup_logging
from quant_baseline.sinks.warehouse_sink import WarehouseSink
from quant_baseline.sources.tushare_source import TushareSource
from quant_baseline.pipeline import BaselinePipeline
import duckdb

setup_logging()
settings = load_settings()

con = duckdb.connect(settings.database.profile().path)
source = TushareSource()
sink = WarehouseSink(con, settings=settings.database.sink)

pipeline = BaselinePipeline(source, sink, settings)
report = pipeline.run("BI_EQW_CORE", start=date(2024, 1, 1), end=date(2024, 6, 30))
print(report.status, report.rows_written)
```

> 以上代码为**目标形态**，当前各方法尚未实现。
