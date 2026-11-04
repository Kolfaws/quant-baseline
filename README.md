# Quant Baseline

基准（baseline）数据的同步与计算框架：从数据源取基准标的行情，按等权 / 市值加权规则合成基准指数，
再以**多表结构**写入本地 warehouse（DuckDB / SQLite）。

> **当前状态**：`v0.1.0` — **仅完成目录骨架与配置**，`src/`、`scripts/`、`tests/` 下全部为占位文件 **Under Development**
> （只有 docstring + TODO），业务逻辑尚未实现。表结构的权威定义见 [docs/db_schema.md](docs/db_schema.md)。

---

## 1. 这个项目解决什么

回测与归因都需要一个“干净的基准”：

- 基准要**可复现**：同一段区间重跑，结果必须逐位一致；
- 基准要**可追溯**：哪天用了哪些成分、多少权重，必须能查；
- 基准要**可扩展**：换数据源、换加权方式、换标的池，都不应改动上下游代码。

因此本项目按三层切分，并用配置文件而非代码来描述“基准是什么”：

```text
configs/*.yaml  →  sources（取数）  →  processors（清洗/复权/加权）  →  sinks（多表落库）
```

---

## 2. 目录结构

```text
quant-baseline/
├── configs/                      # 配置中心（与代码解耦）
│   ├── baseline.yaml             # 基准标的池：universes / benchmarks / sync
│   ├── database.yaml             # warehouse 连接（local / remote / sqlite）与写入约定
│   └── logging.yaml              # 日志格式与级别（dictConfig）
├── data/                         # 数据中转层
│   ├── raw/                      # 原始下载数据（CSV 等，不进版本库）
│   └── processed/                # 清洗后的中间态
├── docs/
│   ├── db_schema.md              # ⭐ warehouse 多表结构定义（唯一权威）
│   └── api_reference.md          # 核心类与方法契约
├── scripts/                      # 调度入口（可独立执行，不依赖包安装）
│   ├── init_warehouse.py         # 建表 / 建索引 / 校验结构
│   ├── sync_baseline.py          # 日常增量同步
│   └── backfill_history.py       # 历史回补
├── src/quant_baseline/
│   ├── core/                     # 基础底座
│   │   ├── config.py             # 配置加载校验（Pydantic）
│   │   ├── logger.py             # 统一日志
│   │   └── exceptions.py         # 领域异常
│   ├── sources/                  # 取数层
│   │   ├── base.py               # AbstractSource
│   │   ├── tushare_source.py     # 主数据源
│   │   └── akshare_source.py     # 备选数据源
│   ├── processors/               # 处理层
│   │   ├── cleaner.py            # 去重 / 缺失 / 异常值
│   │   ├── adjuster.py           # 复权 / 日历对齐
│   │   └── weight_calc.py        # ⭐ 等权 / 市值加权
│   ├── sinks/                    # 落库层
│   │   ├── base.py               # AbstractSink
│   │   └── warehouse_sink.py     # 多表 upsert
│   └── pipeline.py               # Source → Processor → Sink 主流程
├── tests/                        # 测试（内存库 + Mock 数据，不碰真实库）
└── warehouse/                    # 新数据仓库
    └── baseline.db               # 由 init_warehouse.py 生成（已 gitignore）
```

---

## 3. 快速开始

### 3.1 安装

```bash
# 推荐 uv（见 .python-version / pyproject.toml）
uv sync --extra dev

# 或使用现有环境
pip install -e ".[dev]"

# 需要备选数据源 akshare 时
pip install -e ".[dev,akshare]"
```

要求 Python >= 3.10。

### 3.2 配置环境变量

```bash
cp .env.example .env          # PowerShell: Copy-Item .env.example .env
```

至少填写 `TUSHARE_TOKEN`；数据库默认走本地的 `warehouse/baseline.db`。

### 3.3 初始化仓库

```bash
python scripts/init_warehouse.py --profile local
python scripts/init_warehouse.py --check     # 只校验，不建表
```

### 3.4 同步与回补

```bash
# 日常增量（交易日收盘后执行）
python scripts/sync_baseline.py

# 指定基准 / 指定日期
python scripts/sync_baseline.py --benchmark BI_HS300 --date 20241101

# 历史回补（分片执行，可断点续跑）
python scripts/backfill_history.py --all --start 2015-01-01 --end 2024-12-31
```

等价的 Makefile 命令：

```bash
make install     # uv sync --extra dev
make init        # 初始化 warehouse
make sync        # 增量同步
make backfill    # 历史回补
make test        # pytest
make lint        # ruff check
```

---

## 4. 配置说明

### 4.1 `configs/baseline.yaml`

三层结构，**改配置不改代码**：

| 段 | 作用 | 关键字段 |
| --- | --- | --- |
| `universes` | 标的池：成分 + 进出池区间 | `code`、`members`、`constituents_source` |
| `benchmarks` | 基准定义 | `kind`（external / synthetic）、`symbol` 或 `universe`、`weight`、`rebalance` |
| `sync` | 同步行为 | `start_date`、`incremental_lookback_days`、`chunk_days`、`retry`、`rate_limit` |

基准两种形态：

- `kind: external` —— 直接跟踪指数（`000300.SH`）或 ETF（`510300.SH`），不做加权计算；
- `kind: synthetic` —— 由标的池按 `equal` / `market_cap` / `fixed` 加权合成，`base_date` 归一到 `base_value`。

### 4.2 `configs/database.yaml`

- `active_profile` 决定使用哪套连接：`local`（本地 DuckDB 读写）、`remote`（远端只读）、`sqlite`（备选）。
- 敏感信息一律用 `${ENV_VAR}` 占位，由 `.env` 或系统环境注入，**不写死在 YAML**。
- `sink` 段是写入契约：批大小、upsert 模式、幂等键、写后校验。
  其中 `conflict_keys` 必须与 [db_schema.md](docs/db_schema.md) 的主键一致。

### 4.3 `configs/logging.yaml`

标准 `logging.config.dictConfig` 格式：控制台（受 `LOG_LEVEL` 控制）+ 全量日志文件 + 错误日志文件，
均为 10MB × N 份滚动。`logs/` 目录已加入 `.gitignore`。

---

## 5. 开发

```bash
make install       # 安装依赖
make hooks         # 安装 git 钩子（首次克隆后必做，否则提交时不会跑任何检查）
make lint          # ruff check
make format        # ruff format（会改动文件）
make test          # pytest（含覆盖率）
make pre-commit    # 全量跑 pre-commit
```

> **注意**：`.pre-commit-config.yaml` 只是“配置”，真正的钩子要写到 `.git/hooks/pre-commit`。
> 新克隆的仓库不执行 `pre-commit install` 时，`git commit` 不会有任何检查，也不报错。
> 等价于 `pre-commit install`；装完后可用 `pre-commit run --all-files` 做一次全量体检。

约定：

- 代码用 `ruff` 检查与格式化（line-length 88），提交前由 pre-commit 自动执行；
- 默认分支受保护（`no-commit-to-branch` 拦截 `main` / `master`），请先 `git switch -c dev` 再提交；
- 业务代码统一 `logger = get_logger(__name__)`，不直接 `print`；
- 新增字段/表必须先改 [docs/db_schema.md](docs/db_schema.md)，再改代码；
- 测试只用内存库与 `tmp_path`，不读写 `warehouse/baseline.db`，需要外网的用例打 `@pytest.mark.network`。
- 文本文件一律 UTF-8 无 BOM：PowerShell 的 `>` / `Out-File` 默认写 UTF-16LE，
  会让 git 把文件判定为二进制（diff 失效、`.gitattributes` 的换行规则也会失效），
  生成 `requirements.txt` 之类文件请用 `... | Out-File -Encoding utf8` 或 `python -c`。

---

## 6. 实现状态

| 模块 | 状态 | 说明 |
| --- | --- | --- |
| 目录与配置（`configs/`、`pyproject.toml`、pre-commit） | ✅ 完成 | 可直接使用 |
| 表结构文档 `docs/db_schema.md` | ✅ 草案 v0.1.0 | 待评审后定稿 |
| `core/`（config / logger / exceptions） | ⬜ 占位 | 仅 docstring + TODO |
| `sources/` | ⬜ 占位 | 接口已定义，实现待补 |
| `processors/` | ⬜ 占位 | 加权算法口径待定稿 |
| `sinks/` | ⬜ 占位 | upsert 逻辑待补 |
| `pipeline.py` | ⬜ 占位 | 主流程待串联 |
| `scripts/` | ⬜ 占位 | CLI 参数待补 |
| `tests/` | ⬜ 占位 | 用例清单已列出 |

---

## 7. 相关文档

- [Warehouse 多表结构定义](docs/db_schema.md) —— 字段、主键、索引、写入与迁移约定
- [API Reference](docs/api_reference.md) —— 各层类与方法的签名、返回值、异常
