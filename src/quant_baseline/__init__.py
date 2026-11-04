"""quant_baseline — 基准（baseline）数据同步与基准指数计算。

分层结构（单向依赖，禁止下层反向 import 上层）：

    sources/     取数：外部数据源 → 原始 DataFrame
        ↓
    processors/  处理：清洗、复权、交易日对齐、等权/市值加权
        ↓
    sinks/       落库：多表 upsert 到 warehouse（DuckDB / SQLite）

横切能力：

    core/config.py       配置加载与 Pydantic 校验（configs/*.yaml + .env）
    core/logger.py       统一日志（logging.config.dictConfig）
    core/exceptions.py   领域异常（FetchError / ProcessError / DBWriteError）
    pipeline.py          串联 Source → Processor → Sink 的主流程

当前状态：仅搭建结构与配置，业务逻辑待实现。
"""

__version__ = "0.1.0"
