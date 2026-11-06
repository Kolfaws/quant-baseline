"""processors.cleaner — 行情清洗（polars）。

只做“输入 DataFrame 的规整”，不查库、不写库：

1. 列名校验与类型规整；
2. 按业务键去重（保留最后一条）；
3. 丢弃 ``close <= 0`` 的脏行；
4. 缺失策略：``drop`` 丢弃无收盘价行；``hold`` 保留（权重计算按 0 收益处理）；
   ``forward_fill`` 仅对市值/复权因子前向填充，不伪造价格。
"""

from __future__ import annotations

from typing import Final

import polars as pl

from ..core.exceptions import ProcessError

REQUIRED_COLUMNS: Final[tuple[str, ...]] = ("symbol", "trade_date", "close")
_NUMERIC_COLUMNS: Final[tuple[str, ...]] = (
    "close",
    "pre_close",
    "pct_chg",
    "adj_factor",
    "total_mv",
    "circ_mv",
    "ret_1d",
)


def _ensure_columns(frame: pl.DataFrame, required: tuple[str, ...], table: str) -> None:
    missing = [column for column in required if column not in frame.columns]
    if missing:
        raise ProcessError(f"{table} 缺列 {missing}", table=table)


def clean_daily(
    frame: pl.DataFrame,
    *,
    table: str = "daily",
    missing_policy: str = "drop",
) -> pl.DataFrame:
    """清洗个股 / 指数日线；返回按 ``(symbol, trade_date)`` 排序的 DataFrame。"""
    if frame is None:
        raise ProcessError(f"{table} 为空（None）", table=table)
    if frame.is_empty():
        return frame

    _ensure_columns(frame, REQUIRED_COLUMNS, table)
    if missing_policy not in {"drop", "forward_fill", "hold"}:
        raise ProcessError(f"未知 missing_policy={missing_policy!r}", table=table)

    expressions = [
        pl.col("symbol").cast(pl.Utf8),
        pl.col("trade_date").cast(pl.Date),
    ]
    for column in _NUMERIC_COLUMNS:
        if column in frame.columns:
            expressions.append(pl.col(column).cast(pl.Float64))
    frame = frame.with_columns(expressions)

    frame = frame.filter(pl.col("close").is_not_null() & (pl.col("close") > 0))
    frame = frame.unique(subset=["symbol", "trade_date"], keep="last").sort(
        ["symbol", "trade_date"]
    )

    if missing_policy == "drop":
        frame = frame.filter(pl.col("close").is_not_null())
    elif missing_policy == "forward_fill":
        fill_targets = [
            column
            for column in ("adj_factor", "total_mv", "circ_mv")
            if column in frame.columns
        ]
        if fill_targets:
            frame = frame.with_columns(
                [
                    pl.col(column).forward_fill().over("symbol")
                    for column in fill_targets
                ]
            )

    return frame


def attach_returns(frame: pl.DataFrame) -> pl.DataFrame:
    """确保存在 ``ret_1d`` 列；缺失时用 ``pct_chg/100`` 或收盘价比值兜底。"""
    if "ret_1d" in frame.columns:
        return frame
    if "pct_chg" in frame.columns:
        return frame.with_columns((pl.col("pct_chg") / 100.0).alias("ret_1d"))
    return frame.with_columns(
        (pl.col("close") / pl.col("close").shift(1).over("symbol") - 1.0).alias(
            "ret_1d"
        )
    )


__all__ = ["REQUIRED_COLUMNS", "attach_returns", "clean_daily"]
