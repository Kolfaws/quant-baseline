"""tests.test_calculators — 等权 / 市值加权 / 再平衡 / 停牌 / 外部基准。

所有用例都是纯函数测试：手写 polars DataFrame，不碰数据库。
"""

from __future__ import annotations

from datetime import date

import polars as pl
import pytest

from quant_baseline.core.config import MemberConfig
from quant_baseline.core.exceptions import ProcessError
from quant_baseline.processors.adjuster import compute_returns
from quant_baseline.processors.weight_calc import (
    MembershipTimeline,
    build_external_series,
    build_panel,
    build_synthetic_benchmark,
    rebalance_dates,
)

_SCHEMA = {
    "symbol": pl.Utf8,
    "trade_date": pl.Date,
    "close": pl.Float64,
    "pre_close": pl.Float64,
    "pct_chg": pl.Float64,
    "total_mv": pl.Float64,
    "circ_mv": pl.Float64,
}


def _frame(rows: list[tuple]) -> pl.DataFrame:
    return pl.DataFrame(rows, schema=_SCHEMA, orient="row")


def _timeline(
    symbols: tuple[str, ...] = ("600000.SH", "600001.SH"),
) -> MembershipTimeline:
    return MembershipTimeline(
        static_members=tuple(
            MemberConfig(symbol, date(2024, 1, 2)) for symbol in symbols
        )
    )


def test_equal_weight_and_drift() -> None:
    frame = _frame(
        [
            ("600000.SH", date(2024, 1, 2), 10.0, 10.0, 0.0, None, None),
            ("600000.SH", date(2024, 1, 3), 11.0, 10.0, 10.0, None, None),
            ("600000.SH", date(2024, 1, 4), 11.0, 11.0, 0.0, None, None),
            ("600000.SH", date(2024, 1, 5), 9.9, 11.0, -10.0, None, None),
            ("600001.SH", date(2024, 1, 2), 20.0, 20.0, 0.0, None, None),
            ("600001.SH", date(2024, 1, 3), 24.0, 20.0, 20.0, None, None),
            ("600001.SH", date(2024, 1, 4), 24.0, 24.0, 0.0, None, None),
            ("600001.SH", date(2024, 1, 5), 24.0, 24.0, 0.0, None, None),
        ]
    )
    frame = compute_returns(frame, "pct_chg")
    calendar = [date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4), date(2024, 1, 5)]
    levels, weights = build_synthetic_benchmark(
        build_panel(frame),
        calendar,
        base_date=date(2024, 1, 2),
        base_value=1000.0,
        frequency="monthly",
        align="first_trading_day",
        method="equal",
        timeline=_timeline(),
    )

    assert levels["close"].to_list() == pytest.approx([1000.0, 1150.0, 1150.0, 1095.0])
    assert levels["ret_1d"].to_list() == pytest.approx(
        [0.0, 0.15, 0.0, -0.0478260869565]
    )

    sums = (
        weights.group_by("trade_date")
        .agg(pl.col("weight").sum().alias("total"))
        .sort("trade_date")
    )
    assert sums["total"].to_list() == pytest.approx([1.0] * 4)

    last = weights.filter(pl.col("trade_date") == date(2024, 1, 5))
    values = dict(zip(last["symbol"].to_list(), last["weight"].to_list()))
    # 收盘后权重 = 期初权重 × (1+当日收益) 归一
    assert values["600000.SH"] == pytest.approx(0.478260869565 * 0.9 / 0.952173913043)
    assert values["600001.SH"] == pytest.approx(0.521739130434 / 0.952173913043)

    rebalance_rows = weights.filter(pl.col("is_rebalance_date"))
    assert set(rebalance_rows["trade_date"].to_list()) == {date(2024, 1, 2)}
    assert rebalance_rows["target_weight"].to_list() == pytest.approx([0.5, 0.5])


def test_market_cap_uses_t_minus_one() -> None:
    frame = _frame(
        [
            ("600000.SH", date(2024, 1, 2), 10.0, 10.0, 0.0, 100.0, 100.0),
            ("600000.SH", date(2024, 1, 3), 10.0, 10.0, 0.0, 110.0, 110.0),
            ("600000.SH", date(2024, 1, 4), 11.0, 10.0, 10.0, 120.0, 120.0),
            ("600001.SH", date(2024, 1, 2), 20.0, 20.0, 0.0, 300.0, 300.0),
            ("600001.SH", date(2024, 1, 3), 20.0, 20.0, 0.0, 360.0, 360.0),
            ("600001.SH", date(2024, 1, 4), 20.0, 20.0, 0.0, 360.0, 360.0),
        ]
    )
    frame = compute_returns(frame, "pct_chg")
    levels, weights = build_synthetic_benchmark(
        build_panel(frame),
        [date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4)],
        base_date=date(2024, 1, 3),
        base_value=1000.0,
        frequency="monthly",
        align="first_trading_day",
        method="market_cap",
        timeline=_timeline(),
        lag=1,
    )
    # 基准日 01-03 用 01-02 的市值：100 / 300 -> 0.25 / 0.75
    base_weights = weights.filter(pl.col("trade_date") == date(2024, 1, 3))
    values = dict(
        zip(base_weights["symbol"].to_list(), base_weights["weight"].to_list())
    )
    assert values["600000.SH"] == pytest.approx(0.25)
    assert values["600001.SH"] == pytest.approx(0.75)
    # 01-04 只有 A 涨 10%：组合 +2.5%
    assert levels["close"].to_list() == pytest.approx([1000.0, 1025.0])


def test_monthly_rebalance_resets_drift() -> None:
    frame = _frame(
        [
            ("600000.SH", date(2024, 1, 2), 10.0, 10.0, 0.0, None, None),
            ("600000.SH", date(2024, 1, 3), 11.0, 10.0, 10.0, None, None),
            ("600000.SH", date(2024, 1, 31), 11.0, 11.0, 0.0, None, None),
            ("600000.SH", date(2024, 2, 1), 11.0, 11.0, 0.0, None, None),
            ("600001.SH", date(2024, 1, 2), 20.0, 20.0, 0.0, None, None),
            ("600001.SH", date(2024, 1, 3), 20.0, 20.0, 0.0, None, None),
            ("600001.SH", date(2024, 1, 31), 20.0, 20.0, 0.0, None, None),
            ("600001.SH", date(2024, 2, 1), 20.0, 20.0, 0.0, None, None),
        ]
    )
    frame = compute_returns(frame, "pct_chg")
    calendar = [
        date(2024, 1, 2),
        date(2024, 1, 3),
        date(2024, 1, 31),
        date(2024, 2, 1),
    ]
    _, weights = build_synthetic_benchmark(
        build_panel(frame),
        calendar,
        base_date=date(2024, 1, 2),
        base_value=1000.0,
        frequency="monthly",
        align="first_trading_day",
        method="equal",
        timeline=_timeline(),
    )
    before = weights.filter(pl.col("trade_date") == date(2024, 1, 31))
    values = dict(zip(before["symbol"].to_list(), before["weight"].to_list()))
    assert values["600000.SH"] > 0.5  # 1 月 A 跑赢，权重漂移

    after = weights.filter(pl.col("trade_date") == date(2024, 2, 1))
    assert set(after["target_weight"].to_list()) == {0.5}
    assert after["weight"].to_list() == pytest.approx([0.5, 0.5])


def test_member_entry_forces_rebalance() -> None:
    frame = _frame(
        [
            ("600000.SH", date(2024, 1, 2), 10.0, 10.0, 0.0, None, None),
            ("600000.SH", date(2024, 1, 3), 10.0, 10.0, 0.0, None, None),
            ("600000.SH", date(2024, 1, 4), 10.0, 10.0, 0.0, None, None),
            ("600002.SH", date(2024, 1, 4), 30.0, 30.0, 0.0, None, None),
        ]
    )
    frame = compute_returns(frame, "pct_chg")
    timeline = MembershipTimeline(
        static_members=(
            MemberConfig("600000.SH", date(2024, 1, 2)),
            MemberConfig("600002.SH", date(2024, 1, 4)),
        )
    )
    _, weights = build_synthetic_benchmark(
        build_panel(frame),
        [date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4)],
        base_date=date(2024, 1, 2),
        base_value=1000.0,
        frequency="monthly",
        align="first_trading_day",
        method="equal",
        timeline=timeline,
    )
    entry = weights.filter(pl.col("trade_date") == date(2024, 1, 4))
    assert entry.height == 2
    assert bool(entry["is_rebalance_date"].all())
    assert entry["weight"].to_list() == pytest.approx([0.5, 0.5])


def test_suspended_symbol_earns_zero_and_weight_stays() -> None:
    frame = _frame(
        [
            ("600000.SH", date(2024, 1, 2), 10.0, 10.0, 0.0, None, None),
            ("600000.SH", date(2024, 1, 3), 10.0, 10.0, 0.0, None, None),
            ("600000.SH", date(2024, 1, 4), 11.0, 10.0, 10.0, None, None),
            ("600001.SH", date(2024, 1, 2), 20.0, 20.0, 0.0, None, None),
            ("600001.SH", date(2024, 1, 3), 20.0, 20.0, 0.0, None, None),
            # 600001.SH 在 01-04 停牌（无行）
        ]
    )
    frame = compute_returns(frame, "pct_chg")
    levels, weights = build_synthetic_benchmark(
        build_panel(frame),
        [date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4)],
        base_date=date(2024, 1, 2),
        base_value=1000.0,
        frequency="monthly",
        align="first_trading_day",
        method="equal",
        timeline=_timeline(),
    )
    assert levels["close"].to_list() == pytest.approx([1000.0, 1000.0, 1050.0])
    last = weights.filter(pl.col("trade_date") == date(2024, 1, 4))
    values = dict(zip(last["symbol"].to_list(), last["weight"].to_list()))
    assert values["600000.SH"] == pytest.approx(0.5 * 1.1 / 1.05)
    assert values["600001.SH"] == pytest.approx(0.5 / 1.05)


def test_external_series_rebases_at_base_date() -> None:
    frame = pl.DataFrame(
        {
            "trade_date": [date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4)],
            "close": [100.0, 110.0, 121.0],
            "ret_1d": [0.0, 0.10, 0.10],
        },
        schema={"trade_date": pl.Date, "close": pl.Float64, "ret_1d": pl.Float64},
    )
    series = build_external_series(frame, base_date=date(2024, 1, 3))
    assert series["nav"].to_list() == pytest.approx([1.0, 1.1])
    assert series["ret_1d"].to_list() == pytest.approx([0.0, 0.10])


def test_rebalance_dates_last_trading_day() -> None:
    calendar = [
        date(2024, 1, 30),
        date(2024, 1, 31),
        date(2024, 2, 1),
        date(2024, 2, 2),
    ]
    assert rebalance_dates(calendar, "monthly", "first_trading_day") == {
        date(2024, 1, 30),
        date(2024, 2, 1),
    }
    assert rebalance_dates(calendar, "monthly", "last_trading_day") == {
        date(2024, 1, 31),
        date(2024, 2, 2),
    }


def test_empty_target_raises() -> None:
    frame = _frame([("600000.SH", date(2024, 1, 2), 10.0, 10.0, 0.0, None, None)])
    frame = compute_returns(frame, "pct_chg")
    with pytest.raises(ProcessError):
        build_synthetic_benchmark(
            build_panel(frame),
            [date(2024, 1, 2)],
            base_date=date(2024, 1, 2),
            base_value=1000.0,
            frequency="monthly",
            align="first_trading_day",
            method="equal",
            timeline=_timeline(("999999.SH",)),
        )
