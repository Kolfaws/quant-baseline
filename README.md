# Quant Baseline

量化回测基准（Baseline）数据的同步与计算框架。从数据源拉取基准标的（指数/ETF/成分股）行情，按等权或市值加权规则合成合成基准，并以多表结构落库至本地 Warehouse（默认 DuckDB / 备选 SQLite）。

> **当前状态 (`v0.1.0`)**：骨架与配置阶段。`configs/`、`pyproject.toml` 和文档已就绪，`src/`、`scripts/`、`tests/` 下均为占位文件（含 docstring 与 TODO）。各项业务逻辑尚待实现，表结构的权威定义请以 `docs/db_schema.md` 为准。

## 1. 设计目标

做量化的都知道，回测最容易出问题的地方之一就是基准。这个项目的目标很简单：

- **可复现**：相同参数重跑，数据逐位一致，不引入前视偏差。
- **可追溯**：某天基准用了什么成分、权重怎么调的，都能查得到。
- **配置驱动**：换标的池、换加权方式、换数据源，不需要动上下游的 Pipeline 代码。

整体数据流向：

```text
configs/*.yaml  →  sources（取数）  →  processors（清洗/复权/加权）  →  sinks（多表落库）
```

## 2. 目录结构

```text
quant-baseline/
├── configs/                      # 配置中心
│   ├── baseline.yaml             # 标的池 (universes)、基准定义 (benchmarks)、同步控制
│   ├── database.yaml             # warehouse 连接配置与写入约定
│   └── logging.yaml              # 日志配置 (dictConfig)
├── data/                         # 数据中转（不进版本库）
│   ├── raw/
│   └── processed/
├── docs/
│   ├── db_schema.md              # Warehouse 多表结构定义（唯一权威）
│   └── api_reference.md          # 核心类契约
├── scripts/                      # 调度入口（独立执行）
│   ├── init_warehouse.py         # 建表 / 索引 / 结构校验
│   ├── sync_baseline.py          # 日常增量同步
│   └── backfill_history.py       # 历史回补
├── src/quant_baseline/
│   ├── core/                     # config, logger, exceptions
│   ├── sources/                  # base, tushare, akshare
│   ├── processors/               # cleaner, adjuster, weight_calc (等权/市值加权)
│   ├── sinks/                    # base, warehouse_sink (upsert)
│   └── pipeline.py               # Source → Processor → Sink 串联
├── tests/                        # 内存库 + Mock 数据测试
└── warehouse/                    # 新数据仓库 (baseline.db, 已 gitignore)
```

## 3. 快速开始

### 3.1 安装

推荐使用 `uv`（见 `pyproject.toml`）：

```bash
uv sync --extra dev
```

或者用传统的 pip：

```bash
pip install -e ".[dev]"
# 需要备选数据源时安装 akshare
pip install -e ".[dev,akshare]"
```

要求 Python >= 3.10。

### 3.2 环境与初始化

```bash
cp .env.example .env
# 填入 TUSHARE_TOKEN 等必要凭证，数据库默认走本地 warehouse/baseline.db

# 初始化表结构
python scripts/init_warehouse.py --profile local

# 仅校验结构，不建表
python scripts/init_warehouse.py --check
```

### 3.3 数据同步

```bash
# 日常增量（建议收盘后跑）
python scripts/sync_baseline.py

# 指定基准或日期
python scripts/sync_baseline.py --benchmark BI_HS300 --date 20241101

# 历史回补（支持断点续跑）
python scripts/backfill_history.py --all --start 2015-01-01 --end 2024-12-31
```

提供了 `Makefile` 快捷命令：`make install` / `make init` / `make sync` / `make backfill` / `make test` / `make lint`。

## 4. 配置说明

### `configs/baseline.yaml`

分三层：`universes`（标的池）、`benchmarks`（基准规则）、`sync`（同步行为）。
基准支持两种形态：

- `kind: external`：直接跟踪指数（如 `000300.SH`）或 ETF（如 `510300.SH`），不做合成。
- `kind: synthetic`：由标的池按 `equal` / `market_cap` / `fixed` 加权合成，需指定 `base_date` 和 `base_value`。

### `configs/database.yaml`

- `active_profile` 决定连接：`local`（读写 DuckDB）、`remote`（只读远端）、`sqlite`（备选）。
- 敏感信息（如 DB 密码）用 `${ENV_VAR}` 占位，绝不写进 YAML。
- `sink` 段定义了批大小、upsert 模式、幂等键。**注意：`conflict_keys` 必须与 `docs/db_schema.md` 中的主键保持一致。**

## 5. 开发规范 (务必看)

```bash
make install       # 安装依赖
make hooks         # 安装 git 钩子（首次克隆必做）
make lint          # 跑 ruff
make format        # 格式化代码
make test          # 跑 pytest
```

**几条硬性约定：**

1. **钩子必须装**：`.pre-commit-config.yaml` 只是配置，不执行 `make hooks` 或 `pre-commit install`，提交时啥都不会检查。
2. **分支保护**：默认分支受保护，直接往 `main/master` 提交会被拦截。请先切出 `dev` 分支再开发。
3. **日志规范**：一律使用 `logger = get_logger(__name__)`，别用 `print`。
4. **Schema 先行**：加字段、加表，先改 `docs/db_schema.md` 评审，然后再动代码。
5. **测试隔离**：测试代码不允许读写 `warehouse/baseline.db`，必须用内存库和 `tmp_path`。依赖外网的测试记得打 `@pytest.mark.network`。
6. **Windows 编码坑**：用 PowerShell 生成 `requirements.txt` 之类的文件时，用 `Out-File -Encoding utf8` 或 python 脚本。默认的 `>` 会写出带 BOM 的 UTF-16LE，会导致 git 误判为二进制文件，换行符处理也会跟着崩掉。

## 6. 当前实现进度

- [x] 目录与配置（`configs/`、`pyproject.toml`、pre-commit）
- [x] 表结构文档 `docs/db_schema.md` (草案 v0.1.0)
- [ ] `core/` (仅 docstring + TODO)
- [ ] `sources/` (接口已定义，实现待补)
- [ ] `processors/` (加权算法口径待定稿)
- [ ] `sinks/` (upsert 逻辑待补)
- [ ] `pipeline.py` 主流程串联
- [ ] `scripts/` CLI 参数解析
- [ ] `tests/` 用例编写

## 7. 相关文档

- `docs/db_schema.md`：字段定义、主键、索引、写入与迁移约定。
- `docs/api_reference.md`：各层类方法的签名、返回值与异常。

---
END OF FILE
