"""sinks — 把计算结果幂等写入 backtest.duckdb。

本层不建表：

    backtest_sink.BacktestSink
        只允许写 bt_benchmarks / bt_baselines / bt_baseline_weights / bt_compute_log
"""

from .backtest_sink import BacktestSink, WriteResult

__all__ = ["BacktestSink", "WriteResult"]
