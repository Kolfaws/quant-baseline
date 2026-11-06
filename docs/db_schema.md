# quant-baseline 数据契约

> **本项目不建表。**
> `backtest.duckdb` 的 DDL 由 `quant-backtest-engine/scripts/init_backtest_db.py` 拥有，
> 定义在 `quant-db-schema/src/quant_schema/backtest_ddl.py`。本文档说明 quant-baseline 依赖哪些表、
> 写哪些列、主键/幂等键与失败行为，供评审和排查使用。

## 1. 两个库的分工

| 库 | 本项目权限 | 用途 |
| --- | --- | --- |
| `${MARKET_DB_PATH}` | **只读** | 交易日历、个股日线/市值、指数、ETF、指数成分权重 |
| `${BACKTEST_DB_PATH}` | 读写（只 upsert） | 基准定义、基准点位、权重、计算日志 |

`market.duckdb` 内有两个组件：

- `warehouse`（`component='warehouse'`，v1.2.0）：行情基础表；
- `factor`（`component='factor'`，v1.0.0）：因子表。

本项目只读 warehouse 部分，不关心 factor 表。

## 2. 从 market.duckdb 读取的表

| 表 | 用途 | 关键列 |
| --- | --- | --- |
| `dim_trade_cal` | 交易日历 | `exchange`, `cal_date`, `is_open` |
| `fact_daily` | 个股日线 | `ts_code`, `trade_date`, `close`, `pre_close`, `pct_chg` |
| `fact_adj_factor` | 复权因子（可选） | `ts_code`, `trade_date`, `adj_factor` |
| `fact_daily_basic` | 市值（市值加权用） | `ts_code`, `trade_date`, `total_mv`, `circ_mv` |
| `fact_index_daily` | 外部指数基准 | `index_code`, `trade_date`, `close`, `pct_chg` |
| `fact_fund_daily` | 外部 ETF 基准 | `ts_code`, `trade_date`, `close`, `pct_chg` |
| `fact_index_weight` | 动态标的池成分 | `index_code`, `trade_date`, `con_code`, `weight` |

读取规则：

- 连接一律 `read_only=True`；`configs/database.yaml` 的 `market_read_only` 必须为 true。
- 区间无数据时抛 `FetchError`，并在 `bt_compute_log` 写 failed，不静默跳过。
- 不联网；行情缺失先去 `data_pipeline` 补数，而不是在本项目里加数据源。

## 3. backtest.duckdb 中本项目写入的四张表

### 3.1 `bt_benchmarks` —— 基准定义（配置物化）

| 字段 | 来源 |
| --- | --- |
| `benchmark_id` | `baseline.yaml: benchmarks[].code` |
| `name` / `kind` / `category` | 配置 |
| `symbol` | external 的 `symbol` |
| `universe_id` | synthetic 的 `universe` |
| `price_field` / `adjust` | 配置/默认值 |
| `weight_method` / `weight_field` / `weight_lag` | synthetic 权重规则 |
| `rebalance_freq` / `rebalance_align` | synthetic 再平衡规则 |
| `base_date` / `base_value` | 配置 |
| `is_active` | `enabled` |
| `updated_at` | 写入时刻 |

**主键**：`benchmark_id`。配置是唯一事实来源，本表只是副本；每次运行都会刷新。

### 3.2 `bt_baselines` —— 基准日频点位

| 字段 | 说明 |
| --- | --- |
| `benchmark_id` | 基准代码 |
| `trade_date` | 交易日 |
| `close` | 点位：外部=指数/ETF 收盘；合成=base_value 起算 |
| `ret_1d` | 日收益（小数） |
| `nav` | 归一净值，base_date 当日 = 1.0 |
| `base_date` | 实际基准日（配置 base_date 后首个交易日） |
| `base_value` | 外部=基准日收盘价；合成=配置的 base_value |
| `kind` / `category` | 冗余，便于单表查询 |
| `source` | 数据来源标记，如 `market.duckdb:fact_index_daily:000300.SH` |
| `run_id` | 本次计算批次，关联 `bt_compute_log` |
| `created_at` | 首次写入（upsert 不覆盖） |
| `updated_at` | 最近覆盖 |

**主键 / 幂等键**：`(benchmark_id, trade_date)`。
`configs/database.yaml` 的 `sink.conflict_keys` 必须与之一致。

### 3.3 `bt_baseline_weights` —— 合成基准权重

| 字段 | 说明 |
| --- | --- |
| `benchmark_id` / `trade_date` / `symbol` | 联合主键 |
| `weight` | 当日收盘后的实际权重（漂移后），同一日合计为 1 |
| `target_weight` | 再平衡日的目标权重；其他日为 NULL |
| `price` | 当日原始收盘价（未复权） |
| `market_value` | 定权所用市值（T-lag）；等权/无数据时为 NULL |
| `is_rebalance_date` | 当天是否再平衡（基准日/定期/成分变化） |
| `run_id` | 计算批次 |
| `created_at` | 默认 now() |

外部基准不写本表。

### 3.4 `bt_compute_log` —— 计算批次日志

| 字段 | 说明 |
| --- | --- |
| `run_id` | 主键，`<benchmark>-<UTC时间戳>-<8位随机>` |
| `job_name` | 固定 `baseline` |
| `target_key` | `benchmark_id` |
| `status` | `success` / `failed` |
| `rows_written` | 本次写 `bt_baselines` + `bt_baseline_weights` 的行数 |
| `start_date` / `end_date` | 请求区间 |
| `error_message` | 失败摘要（截断到 1000 字符） |
| `started_at` / `finished_at` | 起止时间 |

失败路径：先写 `status='failed'`，再把异常抛给 CLI；单基准失败不影响 `run_all`
中的其他基准。

## 4. 写入顺序

```text
BEGIN（每个表/批次独立事务）
  1. bt_benchmarks         ← 先让定义可见
  2. bt_baselines          ← 点位
  3. bt_baseline_weights   ← 权重（仅 synthetic）
  4. bt_compute_log        ← 最后记录批次结果
COMMIT
```

- 所有写操作走 `INSERT ... BY NAME SELECT * FROM <registered frame> ON CONFLICT`.
- `created_at` 在冲突时不被覆盖；`updated_at` 刷新。
- 每批一个事务，批大小 `sink.batch_size`（默认 10000）。
- 写后校验主键唯一（`verify_after_write: true`）。

## 5. 视图（quant-backtest-engine 提供）

本项目不创建视图，但下游会用到：

### `v_bt_baselines`

```sql
SELECT benchmark_id, name, kind, category, trade_date, close, ret_1d, nav
FROM v_bt_baselines
WHERE benchmark_id = 'BI_HS300'
ORDER BY trade_date;
```

### `v_bt_nav_excess`

把策略 `bt_nav` 与基准 `bt_baselines` 按日对齐，并将基准在回测首日再次归一：

```sql
SELECT trade_date, nav, benchmark_nav, excess_nav, excess_ret_1d
FROM v_bt_nav_excess
WHERE run_id = 'demo_eqw_core'
ORDER BY trade_date;
```

## 6. 校验与失败

| 场景 | 行为 |
| --- | --- |
| backtest 库缺表 / 缺 `backtest` 版本行 | `SchemaError`，提示先跑 `init_backtest_db.py` |
| 配置未知字段 / 非法枚举 | `ConfigError`，启动即失败 |
| market 库缺表 | `SchemaError` |
| 区间无行情 / 指数无数据 | `FetchError`，写 failed 日志 |
| 权重和不为 1 / 基准日无法定权 | `ProcessError`，写 failed 日志 |
| 写入列不在表结构内 | `DBWriteError`，该批回滚 |
| 重复跑同一区间 | 行数不变，`updated_at`/`run_id` 更新 |

## 7. 与 quant-backtest-engine 的边界

- 本项目**不**创建、不修改任何表结构；
- 本项目**不**写 `bt_nav` / `bt_runs` / `bt_run_metrics`；
- 超额计算属于 quant-backtest-engine 的 SQL 视图，本项目只提供基准点位与权重。

## 8. 版本

本契约跟随 `quant-db-schema`：

| 库 | 组件 | 版本 |
| --- | --- | --- |
| market.duckdb | warehouse | 1.2.0 |
| market.duckdb | factor | 1.0.0 |
| backtest.duckdb | backtest | 1.0.0 |

`check_connections.py` 会打印两个库的实际版本与表行数。
