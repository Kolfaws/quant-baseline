"""quant_baseline — 回测基准（baseline）计算组件。

定位：
    纯计算组件。底层行情由 ``data_pipeline`` 写入 ``market.duckdb``；
    本项目的产出直接 upsert 到 ``backtest.duckdb``，**不建表、不抓行情**。

分层（单向依赖）：

    sources/     WarehouseSource：只读 market.duckdb
        ↓
    processors/  清洗、收益口径、等权/市值加权、点位与权重漂移
        ↓
    sinks/       BacktestSink：只 upsert backtest.duckdb
        ↓
    pipeline.py  BaselinePipeline：串联并记录 bt_compute_log

横切能力：
    core/config.py       配置加载与严格校验
    core/db.py           market（只读）/ backtest（读写）连接
    core/logger.py       统一日志
    core/exceptions.py   领域异常
"""

__version__ = "0.1.0"

__all__ = ["__version__", "main"]


def main() -> None:  # pragma: no cover - console script 占位
    print(
        "quant-baseline: 纯计算组件，请使用\n"
        "  python scripts/sync_baseline.py\n"
        "  python scripts/backfill_history.py\n"
        "  python scripts/check_connections.py"
    )
