"""tests.test_calculators — 等权 / 市值加权 / 再平衡 / 停牌 / 外部基准。

所有用例都是纯函数测试：手写 polars DataFrame，不碰数据库。

PIT 契约（P0-1）：lag 必须 >= 1；再平衡目标权重与动态成分快照都只能用
参考日（T-lag）及更早的数据；equal / fixed 的 ``available`` 是参考日可见
集合，未来上市 / 已退市成分不纳入。
"""

from __future__ import annotations

import logging
from datetime import date

import polars as pl
import pytest

from quant_baseline.core.config import MemberConfig
from quant_baseline.core.exceptions import ProcessError
from quant_baseline.processors.adjuster import compute_returns
from quant_baseline.processors.weight_calc import (
    MembershipSnapshot,
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


def _targets(weights: pl.DataFrame, day: date) -> dict[str, float]:
    rows = weights.filter(pl.col("trade_date") == day)
    return {
        str(symbol): float(target)
        for symbol, target in zip(
            rows["symbol"].to_list(), rows["target_weight"].to_list(), strict=True
        )
        if target is not None
    }


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
    """成员集合变化当日强制再平衡。

    旧实现用 T 日成员集合 + 全样本 available，会让 01-04 才生效的新成分在
    01-04 直接以等权纳入（参考价却是 01-03）。新契约：成员与可用集都按参考日
    （T-lag）取，新成分必须在参考日已生效且可见才会进入目标权重；这里把
    生效日放到 01-03，因此 01-04 的参考日（01-03）能看到成员集合变化并定权。
    """
    frame = _frame(
        [
            ("600000.SH", date(2024, 1, 2), 10.0, 10.0, 0.0, None, None),
            ("600000.SH", date(2024, 1, 3), 10.0, 10.0, 0.0, None, None),
            ("600000.SH", date(2024, 1, 4), 10.0, 10.0, 0.0, None, None),
            # 新成分从 01-03 起有行情，01-04 的参考日可见
            ("600002.SH", date(2024, 1, 3), 30.0, 30.0, 0.0, None, None),
            ("600002.SH", date(2024, 1, 4), 30.0, 30.0, 0.0, None, None),
        ]
    )
    frame = compute_returns(frame, "pct_chg")
    timeline = MembershipTimeline(
        static_members=(
            MemberConfig("600000.SH", date(2024, 1, 2)),
            MemberConfig("600002.SH", date(2024, 1, 3)),
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
    # 01-03 参考 01-02：新成分尚未生效，只持有 600000
    day_two = weights.filter(pl.col("trade_date") == date(2024, 1, 3))
    assert day_two["symbol"].to_list() == ["600000.SH"]
    # 01-04 参考 01-03：成员集合变化 -> 强制再平衡，纳入参考日可见的新成分
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


# ---------------------------------------------------------------------------
# P0-1 PIT 回归：lag>=1 / 动态成分快照日 / 参考日可见集合 / 基准日无 T-1
# ---------------------------------------------------------------------------
def test_lag_zero_rejected() -> None:
    """lag=0 表示用 T 日数据定 T 日权重，直接拒绝（旧行为允许）。"""
    frame = _frame([("600000.SH", date(2024, 1, 2), 10.0, 10.0, 0.0, None, None)])
    frame = compute_returns(frame, "pct_chg")
    with pytest.raises(ProcessError, match="lag"):
        build_synthetic_benchmark(
            build_panel(frame),
            [date(2024, 1, 2)],
            base_date=date(2024, 1, 2),
            base_value=1000.0,
            frequency="monthly",
            align="first_trading_day",
            method="equal",
            timeline=_timeline(("600000.SH",)),
            lag=0,
        )


def test_membership_timeline_never_uses_future_snapshot() -> None:
    """动态快照只能取查询日及以前最近一期；查询日之后的快照不可见。"""
    timeline = MembershipTimeline(
        snapshots=(
            MembershipSnapshot(date(2024, 1, 2), {"600000.SH": 0.4, "600001.SH": 0.6}),
            MembershipSnapshot(date(2024, 2, 1), {"600002.SH": 1.0}),
        )
    )
    assert timeline.at(date(2024, 1, 1)) == {}
    assert timeline.at(date(2024, 1, 31)) == {"600000.SH": 0.4, "600001.SH": 0.6}
    assert timeline.at(date(2024, 2, 1)) == {"600002.SH": 1.0}


def test_dynamic_snapshot_uses_reference_day() -> None:
    """排定再平衡用参考日（T-1）快照；T 日才生效的新成分次日才纳入。"""
    a, b, c = "600000.SH", "600001.SH", "600002.SH"
    calendar = [
        date(2024, 1, 2),
        date(2024, 1, 3),
        date(2024, 1, 31),
        date(2024, 2, 1),
        date(2024, 2, 2),
    ]
    rows = [
        (symbol, day, 10.0, 10.0, 0.0, None, None)
        for symbol in (a, b)
        for day in calendar
    ]
    # 新成分 C 从 02-01 才有行情（02-01 快照才把它纳入）
    rows.extend(
        [
            (c, date(2024, 2, 1), 10.0, 10.0, 0.0, None, None),
            (c, date(2024, 2, 2), 10.0, 10.0, 0.0, None, None),
        ]
    )
    frame = compute_returns(_frame(rows), "pct_chg")
    timeline = MembershipTimeline(
        snapshots=(
            MembershipSnapshot(date(2024, 1, 2), {a: 0.5, b: 0.5}),
            MembershipSnapshot(date(2024, 2, 1), {a: 1 / 3, b: 1 / 3, c: 1 / 3}),
        )
    )
    _, weights = build_synthetic_benchmark(
        build_panel(frame),
        calendar,
        base_date=date(2024, 1, 2),
        base_value=1000.0,
        frequency="monthly",
        align="first_trading_day",
        method="equal",
        timeline=timeline,
        lag=1,
    )
    # 02-01 排定再平衡，参考日 01-31：只能看到 01-02 快照的 A/B
    targets_feb1 = _targets(weights, date(2024, 2, 1))
    assert set(targets_feb1) == {a, b}
    assert targets_feb1[a] == pytest.approx(0.5)
    assert targets_feb1[b] == pytest.approx(0.5)
    # 02-02 参考日 02-01：成分集合变化 -> 强制再平衡，纳入新成分 C
    day_rows = weights.filter(pl.col("trade_date") == date(2024, 2, 2))
    assert bool(day_rows["is_rebalance_date"].all())
    targets_feb2 = _targets(weights, date(2024, 2, 2))
    assert set(targets_feb2) == {a, b, c}
    for symbol in (a, b, c):
        assert targets_feb2[symbol] == pytest.approx(1 / 3)


def test_future_listed_symbol_excluded_from_equal() -> None:
    """未来上市成分在可见前不进入 equal 目标权重（旧行为从基准日起按全样本纳入）。"""
    a, b = "600000.SH", "600001.SH"
    calendar = [
        date(2024, 1, 2),
        date(2024, 1, 3),
        date(2024, 1, 4),
        date(2024, 2, 1),
    ]
    rows = [
        (a, date(2024, 1, 2), 10.0, 10.0, 0.0, None, None),
        (a, date(2024, 1, 3), 11.0, 10.0, 10.0, None, None),
        (a, date(2024, 1, 4), 11.0, 11.0, 0.0, None, None),
        (a, date(2024, 2, 1), 11.0, 11.0, 0.0, None, None),
        # B 从 01-04 才有行情（未来上市 / 数据起点晚于基准日）
        (b, date(2024, 1, 4), 20.0, 20.0, 0.0, None, None),
        (b, date(2024, 2, 1), 20.0, 20.0, 0.0, None, None),
    ]
    frame = compute_returns(_frame(rows), "pct_chg")
    levels, weights = build_synthetic_benchmark(
        build_panel(frame),
        calendar,
        base_date=date(2024, 1, 2),
        base_value=1000.0,
        frequency="monthly",
        align="first_trading_day",
        method="equal",
        timeline=_timeline((a, b)),
    )
    # 基准日 B 尚不可见：只对 A 定权；01-03 A 涨 10% -> 点位 1100（旧行为 1050）
    assert _targets(weights, date(2024, 1, 2)) == pytest.approx({a: 1.0})
    assert set(weights.filter(pl.col("trade_date") == date(2024, 1, 3))["symbol"]) == {
        a
    }
    assert levels["close"].to_list()[1] == pytest.approx(1100.0)
    # 02-01 再平衡参考日 01-04：B 已可见 -> 等权纳入
    assert _targets(weights, date(2024, 2, 1)) == pytest.approx({a: 0.5, b: 0.5})


def test_delisted_symbol_excluded_at_rebalance() -> None:
    """已退市成分在参考日无行情，不再进入 equal 目标权重（旧行为仍按全样本纳入）。"""
    a, b = "600000.SH", "600001.SH"
    calendar = [
        date(2024, 1, 2),
        date(2024, 1, 3),
        date(2024, 1, 4),
        date(2024, 1, 31),
        date(2024, 2, 1),
    ]
    rows = [(a, day, 10.0, 10.0, 0.0, None, None) for day in calendar]
    rows.extend(
        [
            (b, date(2024, 1, 2), 20.0, 20.0, 0.0, None, None),
            (b, date(2024, 1, 3), 20.0, 20.0, 0.0, None, None),
            (b, date(2024, 1, 4), 20.0, 20.0, 0.0, None, None),
            # 01-04 之后 B 再无行情（已退市），但配置的成员区间尚未结束
        ]
    )
    frame = compute_returns(_frame(rows), "pct_chg")
    _, weights = build_synthetic_benchmark(
        build_panel(frame),
        calendar,
        base_date=date(2024, 1, 2),
        base_value=1000.0,
        frequency="monthly",
        align="first_trading_day",
        method="equal",
        timeline=_timeline((a, b)),
    )
    assert _targets(weights, date(2024, 1, 2)) == pytest.approx({a: 0.5, b: 0.5})
    # 02-01 再平衡参考日 01-31：B 已无行情 -> 只保留 A
    assert _targets(weights, date(2024, 2, 1)) == pytest.approx({a: 1.0})
    feb_rows = weights.filter(pl.col("trade_date") == date(2024, 2, 1))
    assert feb_rows["symbol"].to_list() == [a]


def test_base_day_without_t_minus_one_uses_same_day_data(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """日历首日即基准日（没有 T-1）时的边界：用基准日当天数据建初始组合。

    基准日收益恒为 0，组合从次日起承担收益，因此不构成收益前视；同时要
    显式告警 warmup 不足，而不是静默前视。
    """
    a, b = "600000.SH", "600001.SH"
    frame = _frame(
        [
            (a, date(2024, 1, 2), 10.0, 10.0, 0.0, 100.0, 100.0),
            (a, date(2024, 1, 3), 11.0, 10.0, 10.0, 110.0, 110.0),
            (b, date(2024, 1, 2), 20.0, 20.0, 0.0, 300.0, 300.0),
            (b, date(2024, 1, 3), 22.0, 20.0, 10.0, 330.0, 330.0),
        ]
    )
    frame = compute_returns(frame, "pct_chg")
    caplog.set_level(logging.WARNING, logger="quant_baseline.weight_calc")
    levels, weights = build_synthetic_benchmark(
        build_panel(frame),
        [date(2024, 1, 2), date(2024, 1, 3)],
        base_date=date(2024, 1, 2),
        base_value=1000.0,
        frequency="monthly",
        align="first_trading_day",
        method="market_cap",
        timeline=_timeline((a, b)),
        lag=1,
    )
    assert levels["ret_1d"][0] == 0.0
    # 01-02 市值 100/300 -> 0.25/0.75；不会因为缺 T-1 而报错
    assert _targets(weights, date(2024, 1, 2)) == pytest.approx({a: 0.25, b: 0.75})
    assert any(
        "没有交易日" in record.getMessage()
        for record in caplog.records
        if record.name == "quant_baseline.weight_calc"
    )
