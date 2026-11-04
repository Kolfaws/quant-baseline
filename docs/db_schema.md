# Warehouse 多表结构定义

> **状态**：草案 `v0.1.0`（结构定稿前会调整）  
> **对应实现**：`scripts/init_warehouse.py`  
> **对应配置**：`configs/database.yaml`（幂等键、批大小、表前缀）  
> **唯一权威**：本文件的表结构变更必须同步升级 `meta_schema_version` 并记录迁移说明。

本文件定义 `warehouse` 中所有表、视图、索引与写入约定。它是数据层与其他模块之间的**契约**：
代码只能按这里的字段名读写，任何字段增删都必须先改这里。

---

## 1. 设计约定

### 1.1 表命名

| 前缀 | 含义 | 更新频率 | 示例 |
| --- | --- | --- | --- |
| `dim_` | 维度 / 参考数据（标的、成分、定义） | 低（人工或定期刷新） | `dim_benchmark` |
| `fact_` | 事实 / 时序数据，按交易日追加 | 高（每日） | `fact_benchmark_daily` |
| `meta_` | 元数据：版本、同步水位 | 中 | `meta_sync_state` |
| `v_` | 只读视图，不落数据 | — | `v_benchmark_daily` |

### 1.2 字段命名

- 一律小写 `snake_case`，与数据源字段解耦（映射在 `sources/*.py` 内完成）。
- 时间字段只有三种：`trade_date`（交易日，DATE）、`created_at` / `updated_at`（写入时刻，TIMESTAMP）。
- 所有代码字段统一为 `"<6位数字>.<交易所>"`：`600519.SH`、`000001.SZ`、`430047.BJ`。
- 金额/价格不带单位后缀；单位写在字段注释里，不写进字段名。

### 1.3 类型映射

| 语义 | DuckDB | SQLite | 说明 |
| --- | --- | --- | --- |
| 标的 / 基准代码 | `VARCHAR` | `TEXT` | 定长但不强制长度 |
| 交易日 | `DATE` | `TEXT`（ISO `YYYY-MM-DD`） | 不用 TIMESTAMP，规避时区歧义 |
| 价格 | `DOUBLE` | `REAL` | 保留源精度，不做定点化 |
| 权重 | `DECIMAL(18,10)` | `NUMERIC` | 单只权重；同日合计应约等于 1 |
| 涨跌幅 / 收益率 | `DECIMAL(18,8)` | `NUMERIC` | 小数形式：`0.0123` = 1.23% |
| 成交量 | `BIGINT` | `INTEGER` | 单位见各表注释 |
| 成交额 | `DOUBLE` | `REAL` | 单位：元 |
| 布尔标记 | `BOOLEAN` | `INTEGER`（0/1） | 如 `is_suspended` |
| 批次号 | `VARCHAR` | `TEXT` | `run_id`，关联 `etl_run_log` |
| 原始参数 / 扩展 | `VARCHAR`（JSON 文本） | `TEXT` | 便于回溯，不建 JSON 类型 |
| 写入时刻 | `TIMESTAMP` | `TEXT`（ISO） | 默认 `CURRENT_TIMESTAMP` |

### 1.4 主键与幂等

- 每张 `fact_` 表必须有业务主键（PRIMARY KEY 或 UNIQUE），禁止无键追加。
- 统一写入方式：`INSERT ... ON CONFLICT(<keys>) DO UPDATE SET ...`，重复跑不产生重复行。
- 主键一经确定不得随意变更；确需变更时必须走“新表 + 迁移 + 旧表归档”流程。

### 1.5 空值与约束

- 主键、`trade_date`、`run_id` 一律 `NOT NULL`。
- 价格类字段允许 NULL：停牌、未上市、已退市是正常状态，用 `is_suspended` 等标记区分，
  **不要用 0 代替缺失**。
- 权重字段在非再平衡日也落库（漂移后的实际权重），便于复现组合。

---

## 2. 表清单总览

| 表名 | 类型 | 粒度 | 主键 | 说明 |
| --- | --- | --- | --- | --- |
| `meta_schema_version` | 元数据 | 每组件一行 | `(component)` | 结构版本与迁移记录 |
| `dim_benchmark` | 维度 | 每基准一行 | `(benchmark_code)` | 基准定义（外部指数 / 自建合成） |
| `dim_universe` | 维度 | 每标的池一行 | `(universe_code)` | 标的池定义（静态或动态成分源） |
| `dim_universe_member` | 维度 | 每成分×生效区间一行 | `(universe_code, symbol, start_date)` | 成分及进出池区间 |
| `fact_benchmark_daily` | 事实 | 每基准×交易日一行 | `(benchmark_code, trade_date)` | 基准日线点位 |
| `fact_constituent_daily` | 事实 | 每标的池×成分×交易日一行 | `(universe_code, symbol, trade_date)` | 成分日线（计算原料） |
| `fact_benchmark_weight` | 事实 | 每基准×交易日×成分一行 | `(benchmark_code, trade_date, symbol)` | 权重快照 |
| `meta_sync_state` | 元数据 | 每任务×目标一行 | `(job_name, target_key)` | 增量水位 |
| `etl_run_log` | 元数据 | 每次运行一行 | `(run_id)` | 运行日志与结果 |

---

## 3. 各表明细

### 3.1 `meta_schema_version` —— 结构版本

| 字段 | 类型 | 可空 | 默认 | 说明 |
| --- | --- | --- | --- | --- |
| `component` | VARCHAR | 否 | — | 组件名，当前固定 `warehouse` |
| `version` | VARCHAR | 否 | — | 语义化版本，如 `0.1.0` |
| `applied_at` | TIMESTAMP | 否 | `CURRENT_TIMESTAMP` | 该版本写入时刻 |
| `description` | VARCHAR | 是 | NULL | 变更说明 |
| `migration_sql` | VARCHAR | 是 | NULL | 迁移语句（首次建表为 NULL） |

**约定**：`init_warehouse.py` 只允许“升级”不允许“降级”；版本不一致且未显式 `--force` 时报 `SchemaError`。

参考 DDL（其余表同风格，完整 DDL 在 `init_warehouse.py`）：

```sql
CREATE TABLE IF NOT EXISTS meta_schema_version (
    component    VARCHAR      NOT NULL,
    version      VARCHAR      NOT NULL,
    applied_at   TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    description  VARCHAR,
    migration_sql VARCHAR,
    PRIMARY KEY (component)
);
```

### 3.2 `dim_benchmark` —— 基准定义

| 字段 | 类型 | 可空 | 说明 |
| --- | --- | --- | --- |
| `benchmark_code` | VARCHAR | 否 | 内部标识，如 `BI_EQW_CORE` |
| `name` | VARCHAR | 否 | 展示名 |
| `kind` | VARCHAR | 否 | `external`（外部跟踪）/ `synthetic`（自建合成） |
| `category` | VARCHAR | 是 | `index` / `etf` / `stock_basket` |
| `symbol` | VARCHAR | 是 | `external` 专用：被跟踪标的代码 |
| `universe_code` | VARCHAR | 是 | `synthetic` 专用：标的池代码 |
| `source` | VARCHAR | 是 | `tushare` / `akshare` |
| `price_field` | VARCHAR | 是 | `close` / `open` / `vwap` |
| `adjust` | VARCHAR | 是 | `none` / `qfq` / `hfq` |
| `weight_method` | VARCHAR | 是 | `equal` / `market_cap` / `fixed` |
| `weight_field` | VARCHAR | 是 | `total_mv` / `circ_mv`（市值加权用） |
| `weight_lag` | INTEGER | 是 | 市值取 T-N，默认 1 |
| `rebalance_freq` | VARCHAR | 是 | `daily` / `weekly` / `monthly` / `quarterly` / `none` |
| `rebalance_align` | VARCHAR | 是 | `first_trading_day` / `last_trading_day` |
| `base_date` | DATE | 是 | 基准日 |
| `base_value` | DOUBLE | 是 | 基准日点位，默认 1000 |
| `is_active` | BOOLEAN | 否 | 是否纳入日常同步，默认 `TRUE` |
| `created_at` | TIMESTAMP | 否 | 默认 `CURRENT_TIMESTAMP` |
| `updated_at` | TIMESTAMP | 否 | 写入时刷新 |

**来源**：由 `configs/baseline.yaml` 的 `benchmarks` 段同步而来（配置为单一事实来源，DB 为物化副本）。

### 3.3 `dim_universe` —— 标的池定义

| 字段 | 类型 | 可空 | 说明 |
| --- | --- | --- | --- |
| `universe_code` | VARCHAR | 否 | 如 `UNI_CN_A_CORE` |
| `name` | VARCHAR | 否 | 展示名 |
| `description` | VARCHAR | 是 | 用途说明 |
| `member_mode` | VARCHAR | 否 | `static`（手工维护）/ `dynamic`（外部成分源） |
| `constituents_source` | VARCHAR | 是 | `dynamic` 专用，JSON：`{kind, api, index_code, refresh, lookback_days}` |
| `last_refreshed_at` | DATE | 是 | 成分最近一次刷新日 |
| `created_at` | TIMESTAMP | 否 | 默认 `CURRENT_TIMESTAMP` |
| `updated_at` | TIMESTAMP | 否 | 写入时刷新 |

### 3.4 `dim_universe_member` —— 成分与生效区间

| 字段 | 类型 | 可空 | 说明 |
| --- | --- | --- | --- |
| `universe_code` | VARCHAR | 否 | 所属标的池 |
| `symbol` | VARCHAR | 否 | 成分代码 |
| `symbol_name` | VARCHAR | 是 | 成分名称（快照，可能随时间变化） |
| `start_date` | DATE | 否 | 进入标的池的生效日（含） |
| `end_date` | DATE | 是 | 移出生效日（含）；NULL = 仍在池内 |
| `in_weight` | DECIMAL(18,10) | 是 | 进入时权重（仅 `fixed` 方法使用） |
| `created_at` | TIMESTAMP | 否 | 默认 `CURRENT_TIMESTAMP` |

**关键约定**：同一 `symbol` 可以有多行（多次进出池），因此主键必须包含 `start_date`。
查询“某日有效成分”的标准写法：

```sql
SELECT symbol
FROM dim_universe_member
WHERE universe_code = 'UNI_CN_A_CORE'
  AND start_date <= :trade_date
  AND (end_date IS NULL OR end_date >= :trade_date);
```

### 3.5 `fact_benchmark_daily` —— 基准日线

| 字段 | 类型 | 可空 | 说明 |
| --- | --- | --- | --- |
| `benchmark_code` | VARCHAR | 否 | 基准代码 |
| `trade_date` | DATE | 否 | 交易日 |
| `open` | DOUBLE | 是 | 开盘点位 |
| `high` | DOUBLE | 是 | 最高 |
| `low` | DOUBLE | 是 | 最低 |
| `close` | DOUBLE | 否 | 收盘点位（基准日 = `base_value`） |
| `pre_close` | DOUBLE | 是 | 前收盘点位 |
| `pct_chg` | DECIMAL(18,8) | 是 | 当日涨跌幅（小数） |
| `volume` | BIGINT | 是 | 成交量（手 / 份，按标的类型） |
| `amount` | DOUBLE | 是 | 成交额（元） |
| `source` | VARCHAR | 否 | 数据来源 |
| `run_id` | VARCHAR | 否 | 写入批次 |
| `created_at` | TIMESTAMP | 否 | 首次写入时刻 |
| `updated_at` | TIMESTAMP | 否 | 最近覆盖时刻 |

**主键**：`(benchmark_code, trade_date)`。

### 3.6 `fact_constituent_daily` —— 成分日线

| 字段 | 类型 | 可空 | 说明 |
| --- | --- | --- | --- |
| `universe_code` | VARCHAR | 否 | 所属标的池 |
| `symbol` | VARCHAR | 否 | 成分代码 |
| `trade_date` | DATE | 否 | 交易日 |
| `open` | DOUBLE | 是 | 开盘价（已按 `adjust` 处理） |
| `high` | DOUBLE | 是 | 最高价 |
| `low` | DOUBLE | 是 | 最低价 |
| `close` | DOUBLE | 否 | 收盘价（已按 `adjust` 处理） |
| `pre_close` | DOUBLE | 是 | 前收盘价 |
| `pct_chg` | DECIMAL(18,8) | 是 | 涨跌幅（小数） |
| `volume` | BIGINT | 是 | 成交量（手） |
| `amount` | DOUBLE | 是 | 成交额（元） |
| `adj_factor` | DOUBLE | 是 | 复权因子（原始值，便于重算） |
| `total_mv` | DOUBLE | 是 | 总市值（元），市值加权用 |
| `circ_mv` | DOUBLE | 是 | 流通市值（元） |
| `is_suspended` | BOOLEAN | 否 | 是否停牌，默认 `FALSE` |
| `source` | VARCHAR | 否 | 数据来源 |
| `run_id` | VARCHAR | 否 | 写入批次 |
| `created_at` | TIMESTAMP | 否 | 首次写入时刻 |
| `updated_at` | TIMESTAMP | 否 | 最近覆盖时刻 |

**主键**：`(universe_code, symbol, trade_date)`。

> 为什么以 `universe_code` 而非 `benchmark_code` 为主体？同一标的池可被多个基准复用
> （如等权 + 市值加权），成分行情只取一次，避免重复存储。

### 3.7 `fact_benchmark_weight` —— 权重快照

| 字段 | 类型 | 可空 | 说明 |
| --- | --- | --- | --- |
| `benchmark_code` | VARCHAR | 否 | 基准代码 |
| `trade_date` | DATE | 否 | 交易日 |
| `symbol` | VARCHAR | 否 | 成分代码 |
| `weight` | DECIMAL(18,10) | 否 | 该成分当日实际权重（0–1） |
| `target_weight` | DECIMAL(18,10) | 是 | 再平衡日设定的目标权重 |
| `price` | DOUBLE | 是 | 计算所用价格 |
| `market_value` | DOUBLE | 是 | 成分市值（市值加权用） |
| `is_rebalance_date` | BOOLEAN | 否 | 当日是否为再平衡日 |
| `run_id` | VARCHAR | 否 | 写入批次 |
| `created_at` | TIMESTAMP | 否 | 默认 `CURRENT_TIMESTAMP` |

**主键**：`(benchmark_code, trade_date, symbol)`。

**校验规则**：同一 `(benchmark_code, trade_date)` 下 `SUM(weight)` 与 1 的偏差应 `< 1e-8`；
不满足即为 `ProcessError`，不允许写库。

### 3.8 `meta_sync_state` —— 增量水位

| 字段 | 类型 | 可空 | 说明 |
| --- | --- | --- | --- |
| `job_name` | VARCHAR | 否 | 任务名，如 `sync_baseline` |
| `target_key` | VARCHAR | 否 | 目标标识：基准代码或标的池代码 |
| `table_name` | VARCHAR | 否 | 写入的目标表 |
| `start_date` | DATE | 是 | 已同步区间起点 |
| `end_date` | DATE | 是 | 已同步区间终点（水位） |
| `last_trade_date` | DATE | 是 | 最近一次成功写入的交易日 |
| `status` | VARCHAR | 否 | `ok` / `partial` / `failed` |
| `updated_at` | TIMESTAMP | 否 | 写入时刷新 |

**主键**：`(job_name, target_key)`。增量同步据此计算本次区间。

### 3.9 `etl_run_log` —— 运行日志

| 字段 | 类型 | 可空 | 说明 |
| --- | --- | --- | --- |
| `run_id` | VARCHAR | 否 | 批次 ID（时间戳 + 随机后缀） |
| `job_name` | VARCHAR | 否 | 任务名 |
| `target_key` | VARCHAR | 是 | 目标标识 |
| `started_at` | TIMESTAMP | 否 | 开始时刻 |
| `finished_at` | TIMESTAMP | 是 | 结束时刻 |
| `status` | VARCHAR | 否 | `success` / `failed` / `partial` |
| `rows_written` | BIGINT | 是 | 写入行数合计 |
| `error_type` | VARCHAR | 是 | 异常类名 |
| `error_message` | VARCHAR | 是 | 异常摘要（不含敏感信息） |
| `params` | VARCHAR | 是 | 运行参数 JSON |
| `host` | VARCHAR | 是 | 主机名 |

**主键**：`(run_id)`。

---

## 4. 视图

### 4.1 `v_benchmark_daily`

`fact_benchmark_daily` JOIN `dim_benchmark`，补全展示名与类别，供下游直接查询：

```sql
CREATE OR REPLACE VIEW v_benchmark_daily AS
SELECT
    d.benchmark_code,
    d.name,
    d.kind,
    d.category,
    f.trade_date,
    f.open, f.high, f.low, f.close,
    f.pct_chg,
    f.volume, f.amount
FROM fact_benchmark_daily AS f
JOIN dim_benchmark AS d USING (benchmark_code);
```

### 4.2 `v_benchmark_return`

基于窗口函数计算收益率与累计净值（以首个交易日为 1）：

```sql
CREATE OR REPLACE VIEW v_benchmark_return AS
SELECT
    benchmark_code,
    trade_date,
    close,
    close / LAG(close) OVER w - 1                         AS ret_1d,
    close / FIRST_VALUE(close) OVER w                     AS nav_cum,
    close / MAX(close) OVER (PARTITION BY benchmark_code
                             ORDER BY trade_date
                             ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) - 1 AS drawdown
FROM fact_benchmark_daily
WINDOW w AS (PARTITION BY benchmark_code ORDER BY trade_date);
```

> 视图定义也受版本管理：修改视图同样需要升级 `meta_schema_version`。

---

## 5. 索引建议

| 表 | 索引 | 目的 |
| --- | --- | --- |
| `fact_benchmark_daily` | `(trade_date)` | 按日横切、跨基准对比 |
| `fact_constituent_daily` | `(symbol, trade_date)` | 单只股票时序取数 |
| `fact_benchmark_weight` | `(trade_date, symbol)` | 按日查全市场权重 |
| `dim_universe_member` | `(symbol, start_date, end_date)` | 判断某日是否在池 |
| `etl_run_log` | `(job_name, started_at)` | 运行历史排查 |

> DuckDB 对 `PRIMARY KEY` 会自动建索引；SQLite 需显式 `CREATE INDEX IF NOT EXISTS`。
> 索引数量控制在必要范围，过多会拖慢批量写入。

---

## 6. 写入与幂等约定

1. **单次写一个事务**：一张表的一批数据 = 一个事务，失败整体回滚。
2. **批内先去重**：同一批内主键重复只保留最后一条，再入库。
3. **upsert 而非 insert**：统一 `ON CONFLICT DO UPDATE`，重复跑行数不变。
4. **严格列校验**：DataFrame 出现表结构外的列时，`strict_columns=true` 直接失败，
   防止“列名写错但静默丢数据”。
5. **写后校验**：可开启 `verify_after_write`，校验行数与主键唯一性。
6. **跨进程单写者**：DuckDB 以文件为锁单位，同库并发写会失败；写任务串行执行。
7. **水位最后推进**：先写 `fact_*`，再更新 `meta_sync_state`，避免“水位前进但数据没写”。

---

## 7. 版本与迁移

| 版本 | 变更 | 迁移 |
| --- | --- | --- |
| `0.1.0` | 初始结构（本文档全部表） | 无（首次建表） |

迁移流程：

1. 修改本文件，写清新增/变更字段与影响范围；
2. 在 `init_warehouse.py` 增加 `MIGRATIONS` 条目（版本号 + SQL）；
3. 升级 `configs/database.yaml` 的 `schema.version` 与 `migrations`；
4. 在测试库跑 `init` → `verify` → 抽样重算一个基准验证口径未变。

**禁止**：直接改字段类型或用 `ALTER TABLE ... DROP COLUMN` 删列后不记录。
DuckDB 的 `ALTER` 能力有限，结构大改一律走“建新表 → 回填 → 改名”。

---

## 8. 待定项（TODO）

- [ ] **除权处理口径**：自建等权基准用“后复权价”还是“不复权 + 分红再投资”？两者长期收益差异明显，需定稿。
- [ ] **停牌权重**：停牌当日权重剔除后归一，还是保留上一日权重？影响基准波动率。
- [ ] **成分快照频率**：动态池（沪深300）按月快照足够，还是需要按调整公告日精确切换？
- [ ] **ETF 复权**：ETF 分红是否需要复权？当前默认 `none`。
- [ ] **多市场扩展**：港股/美股代码规范（`00700.HK`、`AAPL.US`）是否纳入同一套表。
- [ ] **分区策略**：`fact_constituent_daily` 长期可能上亿行，是否按年拆表或迁移到 Parquet/外部存储。
- [ ] **时区**：所有 `trade_date` 均按 `Asia/Shanghai` 解释，海外数据接入时需重新定义。
