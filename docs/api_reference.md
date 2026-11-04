# API Reference —— 核心类与方法说明

> **状态**：接口草案（`v0.1.0`）。当前仓库仅有占位实现，函数签名以本文件为准。正式落地后如有调整，需同步更新。

本文档定义 `quant_baseline` 的对外接口。内部实现细节（如 SQL 拼接、网络分页、重试机制）不在接口约束范围内。

## 1. 配置层 `quant_baseline.core.config`

### `load_settings(config_dir: Path | None = None) -> Settings`

加载并校验全部配置。

| 参数 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `config_dir` | `Path \| None` | `None` | 配置文件目录；`None` 时使用项目根目录下的 `configs/` |

**返回**：不可变对象 `Settings`，包含三个子配置：

- `settings.baseline`：`BaselineConfig`（universes / benchmarks / sync）
- `settings.database`：`DatabaseConfig`（active_profile / profiles / sink / schema）
- `settings.logging`：`LoggingConfig`（YAML 原始映射 + 已解析的 level）

**异常**：`ConfigError`。触发条件包括文件缺失、YAML 语法错误、存在未知字段、枚举值非法或环境变量未定义。

**约定**：

- 校验是严格的（`extra="forbid"`），字段名拼写错误会直接报错，不会静默使用默认值。
- 结果支持缓存。测试中通过 `config_dir` 注入临时目录，避免污染真实配置。

### 核心模型定义

| 模型 | 关键字段 | 校验规则 |
| --- | --- | --- |
| `Settings` | `baseline`, `database`, `logging`, `env` | 三者版本号必须一致 |
| `BaselineConfig` | `version`, `defaults`, `universes`, `benchmarks`, `sync` | `code` 全局唯一；`kind` 与必填字段匹配 |
| `UniverseConfig` | `code`, `name`, `members`, `constituents_source` | `static` 必须有 `members`；`dynamic` 必须有 `constituents_source` |
| `BenchmarkConfig` | `code`, `kind`, `symbol` / `universe`, `weight`, `rebalance` | `external` 必须有 `symbol`；`synthetic` 必须有 `universe` 且该标的池存在 |
| `SyncConfig` | `start_date`, `end_date`, `incremental_lookback_days`, `chunk_days` | `start <= end`；`chunk_days > 0` |

## 2. 基础设施 `quant_baseline.core`

### `setup_logging(level: str | None = None, config_path: Path | None = None) -> None`

基于 `configs/logging.yaml` 初始化日志系统。`level` 可用于临时覆盖控制台级别。该方法幂等。

### `get_logger(name: str) -> logging.Logger`

获取 `quant_baseline` 命名空间下的 logger，业务代码需统一使用此方法获取日志句柄。

### 异常体系 `quant_baseline.core.exceptions`

```text
QuantBaselineError(Exception)
├── ConfigError      配置缺失 / 非法 / 校验失败
├── FetchError       数据获取失败（网络、限流、Token失效、空数据）
├── ProcessError     清洗/加权失败（列缺失、日期未对齐、除零、权重和不为1）
├── DBWriteError     落库失败（约束冲突、事务回滚）
└── SchemaError      仓库结构与 db_schema.md 不一致
```

所有异常均携带 `context: dict[str, Any]`（如 `benchmark_code`、`trade_date`、`table`），`__str__` 方法会输出消息及上下文，便于单行日志定位。

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

**实现约定**：

- 返回的列名必须与 `db_schema.md` 中的 `fact_*` 字段（小写下划线）对齐，禁止在 Source 层重命名。
- 查无数据时，返回包含列名的空 DataFrame，不抛异常（由调用方决定如何处理）。
- 底层异常（HTTP 错误、超时、Token 失效）需统一包装为 `FetchError` 抛出。
- 初始化时不建立网络连接。Token 从 `TUSHARE_TOKEN` 读取，禁止在日志中打印。

### 数据源实现

```python
TushareSource(token: str | None = None, rate_limit: RateLimit | None = None)
```

`token=None` 时读取环境变量。缺失 token 时立即抛出 `ConfigError`（快速失败）。

```python
AkShareSource(rate_limit: RateLimit | None = None)
```

可选依赖。未安装 `akshare` 时抛出 `ConfigError`，并提示安装命令 `pip install -e ".[akshare]"`。接口定义与 `TushareSource` 完全一致，可无缝替换。

## 4. 处理层 `quant_baseline.processors`

### 4.1 `cleaner`

```python
clean(df: pd.DataFrame, *, table: str, keys: list[str], missing_policy: str = "drop") -> pd.DataFrame
```

执行顺序：去重 -> 类型规整 -> 缺失处理 -> 异常值标记 -> 按 schema 裁剪列。

- **去重**：按 `keys` 排序，保留最后一条。
- **类型规整**：`trade_date` 转为 date，价格转为 float。无法转换的行会剔除并计数。
- **缺失处理**：`drop` 删除整行；`forward_fill` 前向填充。若关键列全空，抛出 `ProcessError`。
- **异常值**：如 `close<=0`、`high<low`、涨跌幅越界。仅打上 `is_invalid` 标记，不静默丢弃。
- **列裁剪**：只保留 schema 定义的列。缺列抛 `ProcessError`；多列根据 `strict_columns` 配置决定是否报错。

### 4.2 `adjuster`

```python
adjust(df: pd.DataFrame, factor: pd.DataFrame, mode: Literal["none", "qfq", "hfq"], base_date: date | None = None) -> pd.DataFrame
align_calendar(df: pd.DataFrame, calendar: pd.DataFrame, fill: Literal["mark", "drop"] = "mark") -> pd.DataFrame
rebase(series: pd.Series, base_date: date, base_value: float = 1000.0) -> pd.Series
```

- `hfq`（后复权）：`price * factor(t) / factor(base_date)`；`qfq`（前复权）使用最新因子归一。
- `align_calendar`：将成分序列对齐至统一交易日历。缺失日标记 `is_suspended=True`，不填充价格。
- `rebase`：确保 `series[base_date] == base_value`，否则抛出 `ProcessError`。

### 4.3 `weight_calc`

```python
compute_weights(prices: pd.DataFrame, method: Literal["equal", "market_cap", "fixed"], *, members: pd.DataFrame, market_cap: pd.DataFrame | None = None, rebalance: RebalanceRule, lag: int = 1) -> pd.DataFrame
```

返回字段：`trade_date, symbol, weight, target_weight, is_rebalance_date`。

```python
compute_index_level(prices: pd.DataFrame, weights: pd.DataFrame, *, base_date: date, base_value: float = 1000.0) -> pd.DataFrame
```

返回字段：`trade_date, close, pct_chg`，需与 `fact_benchmark_daily` 字段对齐。

**计算约束（务必保证）**：

1. 每日权重之和为 1（容差 `< 1e-8`），否则抛 `ProcessError`。
2. 严禁使用未来数据：`t` 日权重仅能基于 `t-1` 及之前的数据计算。
3. 停牌成分当日收益按 0 处理，并重新归一化权重。
4. 基准日点位必须严格等于 `base_value`。

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
WarehouseSink(con: DuckDBPyConnection | sqlite3.Connection, *, settings: SinkConfig, schema: dict[str, TableSpec])
```

- 数据库连接由调用方管理，Sink 内部**不**关闭连接。
- `write` 方法内按 `settings.batch_size` 分批提交，单批为一个事务，失败整体回滚并抛 `DBWriteError`。
- `upsert_mode` 决定 SQL 形态（`on_conflict` / `delete_insert` / `append`）。
- `strict_columns=True` 时，DataFrame 若包含表结构外的列，直接抛 `DBWriteError`。

## 6. 主流程 `quant_baseline.pipeline`

### `class BaselinePipeline`

```python
BaselinePipeline(source: AbstractSource, sink: AbstractSink, settings: Settings)

run(benchmark_code: str, start: date | None = None, end: date | None = None) -> RunReport
run_all(trade_date: date | None = None, *, dry_run: bool = False) -> list[RunReport]
```

`RunReport`：`dataclass(run_id, benchmark_code, start, end, status, rows_written, elapsed_s, error)`。

**执行顺序（针对 synthetic 基准）**：

1. 解析基准、标的池及当日有效成分。
2. 取数：`fetch_stock_daily` + `fetch_adj_factor` + `fetch_daily_basic`。
3. 清洗加工：`cleaner.clean` -> `adjuster.adjust` -> `adjuster.align_calendar`。
4. 计算：`weight_calc.compute_weights` -> `compute_index_level`。
5. 落库：`fact_constituent_daily` -> `fact_benchmark_weight` -> `fact_benchmark_daily`。
6. 记录水位与日志：更新 `meta_sync_state`，写入 `etl_run_log`。

**幂等性**：同一参数（`benchmark_code, start, end`）重复执行，`fact_*` 表行数不变。`meta_sync_state` 与 `etl_run_log` 会追加新记录（符合日志表预期）。

**错误隔离**：`run_all` 中单个基准失败不影响其他基准。错误信息捕获至 `RunReport.error`，整体状态标记为 `partial`。

## 7. 脚本入口（CLI）

| 脚本 | 作用 | 主要参数 |
| --- | --- | --- |
| `scripts/init_warehouse.py` | 建表 / 建索引 / 校验结构 | `--profile` `--db` `--check` `--force` |
| `scripts/sync_baseline.py` | 日常增量同步 | `--benchmark` `--date` `--dry-run` `--profile` |
| `scripts/backfill_history.py` | 历史区间回补 | `--benchmark` `--universe` `--all` `--start` `--end` `--chunk-days` |

**退出码**：`0` 成功；`1` 业务或数据库异常；`2` 配置或参数非法。
三个脚本均支持 `--log-level` 参数，默认读取 `LOG_LEVEL` 环境变量。

## 8. 使用示例（目标形态）

```python
from datetime import date
import duckdb

from quant_baseline.core.config import load_settings
from quant_baseline.core.logger import setup_logging
from quant_baseline.sinks.warehouse_sink import WarehouseSink
from quant_baseline.sources.tushare_source import TushareSource
from quant_baseline.pipeline import BaselinePipeline

setup_logging()
settings = load_settings()

con = duckdb.connect(settings.database.profile().path)
source = TushareSource()
sink = WarehouseSink(con, settings=settings.database.sink)

pipeline = BaselinePipeline(source, sink, settings)
report = pipeline.run("BI_EQW_CORE", start=date(2024, 1, 1), end=date(2024, 6, 30))
print(report.status, report.rows_written)
```

> 注：当前各方法尚未完全实现，以上代码为目标调用形态。

---
END OF FILE
