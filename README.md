# Quant Baseline

回测基准（benchmark）计算组件：从统一数据底座读行情，按等权 / 市值加权 / 外部指数
规则合成基准，产出直接写入 `backtest.duckdb`。

> **本项目是纯计算组件。**
> 不抓行情、不建表、不创建独立 `baseline.db`、不提供 SQLite 备选。
> 底层数据由 `data_pipeline` 写入 `market.duckdb`；表结构由 `quant-backtest-engine`
> 的 `init_backtest_db.py` 拥有；本项目的产出 upsert 到 `bt_benchmarks` /
> `bt_baselines` / `bt_baseline_weights` / `bt_compute_log`。

## 1. 数据流

```text
data_pipeline ──写──> ${MARKET_DB_PATH}（warehouse + factor）
                              │ 只读
                              ▼
quant-baseline    读取交易日历 / 个股 / 指数 / ETF / 指数成分
                  清洗 → 收益口径 → 等权/市值加权 → 点位与权重漂移
                              │ upsert
                              ▼
quant-backtest-engine 建表 ─> ${BACKTEST_DB_PATH}
                  读回基准，与策略净值在 SQL 层算超额
```

关键点：

- **单一数据底座**：`market.duckdb` + `backtest.duckdb`，没有第三个库。
- **读写隔离**：`market.duckdb` 以 `read_only=True` 打开；factor_research 写因子时，
  回测/基准仍可并发读（DuckDB 单写多读）。
- **不重复抓数**：基准用的指数、成分、个股价量都已在 market 库中；抓数由
  `data_pipeline/scripts/ingest_market_data.py` 负责。
- **口径可复现**：同一配置重跑，逐位一致；再平衡目标权重只用 `T-lag` 及更早数据。

## 2. 目录

```text
quant-baseline/
├── configs/
│   ├── baseline.yaml             # 标的池 / 基准规则 / 同步区间
│   ├── database.yaml             # market + backtest 路径、sink 约定
│   └── logging.yaml
├── docs/
│   ├── db_schema.md              # 本项目在 backtest.duckdb 中读写的表
│   └── api_reference.md          # 已实现 API
├── scripts/
│   ├── sync_baseline.py          # 日常增量（首次自动全量）
│   ├── backfill_history.py       # 历史分片回补
│   └── check_connections.py      # 只读检查两库结构/版本
├── src/quant_baseline/
│   ├── core/                     # config / db / logger / exceptions
│   ├── sources/warehouse_source.py
│   ├── processors/               # cleaner / adjuster / weight_calc
│   ├── sinks/backtest_sink.py
│   └── pipeline.py
└── tests/                        # 纯函数 + 临时库端到端
```

## 3. 安装

> 依赖只装必要的：`data_pipeline`、`quant-db-schema`（dist 名，包名 `quant_schema`）、`duckdb`、`polars`、`pyarrow`、`pytz`、`pyyaml`。
> 不修改 `pyproject.toml` 的依赖段时，可直接用 `uv add` 注入：

```bash
cd quant-baseline
uv add --editable ../quant-db-schema
uv add --editable ../quant-data-pipeline
uv add duckdb polars pyarrow pytz pyyaml
uv sync
```

> 如果目录名不同，按磁盘实际路径调整 `--editable` 参数。

本项目采用 `src/` 布局，`uv sync` 会以 editable 方式安装 `quant_baseline`；
测试通过 `pyproject.toml` 的 `pythonpath = ["src"]` 解析包，不使用
`sys.path.insert`。

## 4. 配置

### 环境变量

```bash
cp .env.example .env
# 在 .env 中填写本机绝对路径；.env 已被 gitignore
MARKET_DB_PATH=/absolute/path/to/market.duckdb
BACKTEST_DB_PATH=/absolute/path/to/backtest.duckdb
```

### `configs/database.yaml`

```yaml
active_profile: production
profiles:
  production:
    market_db_path: "${MARKET_DB_PATH}"
    backtest_db_path: "${BACKTEST_DB_PATH}"
    market_read_only: true
    threads: 6
    memory_limit: 6GB
sink:
  table: "bt_baselines"
  conflict_keys: ["benchmark_id", "trade_date"]
  batch_size: 10000
```

- `market_read_only` 必须为 `true`；设成 false 会直接 `ConfigError`。
- `sink.conflict_keys` 必须与 `bt_baselines` 主键一致，否则启动即失败。
- 路径不允许写死；必须由 `${MARKET_DB_PATH}` / `${BACKTEST_DB_PATH}` 提供（环境变量优先于 `.env`）。
- 测试库 profile `test` 使用 `${MARKET_TEST_DB_PATH}` / `${BACKTEST_TEST_DB_PATH}`：
  ```bash
  uv run python scripts/sync_baseline.py --profile test
  ```

### `configs/baseline.yaml`

- `defaults`：日历、收益口径、基日/基点、再平衡频率、加权方法、warmup。
- `universes`：
  - 静态：`members` 带 `start`/`end`；
  - 动态：`constituents_source` 指向 market 库的 `fact_index_weight`，不联网。
- `benchmarks`：
  - `kind: external` + `symbol`（指数/ETF）；
  - `kind: synthetic` + `universe`。
- `enabled: false` 的基准不会进入 `run_all`，但可手工指定运行。

当前仓库附带：

| 基准 | 类型 | 状态 | 依赖数据 |
| --- | --- | --- | --- |
| `BI_HS300` | external index | 启用 | `fact_index_daily` |
| `BI_EQW_CORE` | synthetic equal | 启用 | `fact_daily` |
| `BI_MCW_CORE` | synthetic market_cap | 停用 | `fact_daily_basic` |
| `BI_HS300_ETF` | external etf | 停用 | `fact_fund_daily` |
| `BI_EQW_HS300` | synthetic dynamic | 停用 | `fact_index_weight` |

停用的基准不是“没实现”，而是当前 market 库尚未导入对应数据；导入后把
`enabled` 改为 `true` 即可。

## 5. 使用

```bash
# 首次/日常：没有历史则从 sync.start_date 全量，有历史则回看 5 天
uv run python scripts/sync_baseline.py

# 指定基准
uv run python scripts/sync_baseline.py --benchmark BI_HS300

# 历史回补（按 365 天分片，幂等，可断点重跑）
uv run python scripts/backfill_history.py --all --start 2015-01-05 --end 2026-11-06

# 只检查两个库的结构/版本/行数，不写库
uv run python scripts/check_connections.py
```

Makefile：`make sync` / `make backfill START=... END=...` / `make check` / `make test`。

## 6. 计算口径

### 6.1 收益

- `adjust: pct_chg`（默认）：用行情表的涨跌幅；Tushare 的 `pre_close` 已除权，
  因此不依赖 `fact_adj_factor` 也能近似总收益。
- `hfq` / `qfq`：`close * adj_factor`（或除以最新因子）后取环比。
- `none`：原始收盘价环比。

### 6.2 合成基准

1. `base_date` 当日点位 = `base_value`（严格相等），当日收益记 0。
2. 再平衡日目标权重：
   - `equal`：等权，剔除无行情的成分；
   - `market_cap`：用 `T-lag` 市值归一（默认 lag=1，避免前视）；
   - `fixed`：用成员/指数权重归一。
3. 非再平衡日权重随价格漂移：
   `w_i(t) = w_i(t-1) * (1 + r_i(t)) / Σ_j w_j(t-1) * (1 + r_j(t))`。
4. 停牌/缺失：当日收益按 0，权重保留；不填充价格。
5. 成分进出池：成员集合变化当日强制再平衡。
6. 每日 `Σw = 1`（容差 1e-9），否则 `ProcessError`，不写库。

### 6.3 外部基准

- 指数：`close` 直接用指数点位；`nav = close / base_close`。
- ETF：同一逻辑，数据来自 `fact_fund_daily`。
- `bt_baselines.base_date` 存**实际**基准日（配置 base_date 之后的首个交易日）。

## 7. 产出表

本项目**不建表**，只写 backtest.duckdb：

| 表 | 主键 | 说明 |
| --- | --- | --- |
| `bt_benchmarks` | `benchmark_id` | 配置物化 |
| `bt_baselines` | `(benchmark_id, trade_date)` | 点位/净值 |
| `bt_baseline_weights` | `(benchmark_id, trade_date, symbol)` | 每日权重 |
| `bt_compute_log` | `run_id` | 成功/失败日志 |

失败时也会写一条 `status='failed'` 的 `bt_compute_log`，异常继续上抛。
详见 [docs/db_schema.md](docs/db_schema.md)。

## 8. 测试

```bash
uv run --with pytest pytest tests -q
```

- 纯函数用例：等权/市值加权、T-1 市值、月度再平衡、成分进出、停牌、外部基准；
- 写入用例：upsert 幂等、批大小、严格列校验、不可写表拒绝；
- 端到端：临时 market/backtest 库，跑 `BaselinePipeline.run()`。
