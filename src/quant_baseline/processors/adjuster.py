"""processors.adjuster — 收益率口径与复权。

支持四种口径（``baseline.yaml: defaults.adjust``）：

* ``pct_chg``：直接使用行情表里的涨跌幅（Tushare 的 ``pre_close`` 已除权，
  因此 ``pct_chg`` 近似总收益，不依赖 ``fact_adj_factor``）；
* ``hfq``：``close * adj_factor`` 后取环比；
* ``qfq``：``close * adj_factor / last_adj_factor`` 后取环比；
* ``none``：原始收盘价环比（分红日会跳空）。

统一输出 ``ret_1d`` 与 ``close_adj`` 两列。
"""

from __future__ import annotations

import polars as pl

from ..core.exceptions import ProcessError

_MODES = frozenset({"pct_chg", "hfq", "qfq", "none"})


def compute_returns(frame: pl.DataFrame, mode: str = "pct_chg") -> pl.DataFrame:
    """给日线 DataFrame 增加 ``ret_1d`` / ``close_adj``。"""
    if mode not in _MODES:
        raise ProcessError(f"未知 adjust 口径 {mode!r}；允许 {sorted(_MODES)}")
    if frame.is_empty():
        return frame

    required = {"symbol", "trade_date", "close"}
    missing = required - set(frame.columns)
    if missing:
        raise ProcessError(f"日线缺列 {sorted(missing)}")

    frame = frame.sort(["symbol", "trade_date"])

    if mode == "pct_chg":
        if "pct_chg" in frame.columns:
            frame = frame.with_columns(
                (pl.col("pct_chg").cast(pl.Float64) / 100.0).alias("ret_1d")
            )
        else:
            frame = frame.with_columns(
                (pl.col("close") / pl.col("close").shift(1).over("symbol") - 1.0).alias(
                    "ret_1d"
                )
            )
        frame = frame.with_columns(pl.col("close").alias("close_adj"))
    else:
        if "adj_factor" not in frame.columns:
            raise ProcessError(
                f"adjust={mode} 需要 fact_adj_factor，但输入没有 adj_factor 列"
            )
        if mode == "hfq":
            price = pl.col("close") * pl.col("adj_factor")
        elif mode == "qfq":
            last_adj = pl.col("adj_factor").last().over("symbol")
            price = pl.col("close") * pl.col("adj_factor") / last_adj
        else:  # none
            price = pl.col("close")

        frame = frame.with_columns(price.cast(pl.Float64).alias("close_adj"))
        frame = frame.with_columns(
            (
                pl.col("close_adj") / pl.col("close_adj").shift(1).over("symbol") - 1.0
            ).alias("ret_1d")
        )
        if "pct_chg" in frame.columns:
            # 复权因子缺失（停牌 / 数据未导入）时，用涨跌幅兜底
            frame = frame.with_columns(
                pl.when(pl.col("ret_1d").is_null())
                .then(pl.col("pct_chg").cast(pl.Float64) / 100.0)
                .otherwise(pl.col("ret_1d"))
                .alias("ret_1d")
            )

    frame = frame.with_columns(pl.col("ret_1d").fill_null(0.0))
    _validate_returns(frame)
    return frame


def _validate_returns(frame: pl.DataFrame) -> None:
    bad = frame.filter(pl.col("ret_1d").is_null() | ~pl.col("ret_1d").is_finite())
    if not bad.is_empty():
        raise ProcessError(
            f"收益序列存在非法值 {bad.height} 行",
            sample=bad.head(3).to_dicts(),
        )
    extreme = frame.filter(pl.col("ret_1d").abs() > 1.0)
    if not extreme.is_empty():
        raise ProcessError(
            f"收益序列存在 |ret|>100% 的异常值 {extreme.height} 行",
            sample=extreme.head(3).to_dicts(),
        )


__all__ = ["compute_returns"]
