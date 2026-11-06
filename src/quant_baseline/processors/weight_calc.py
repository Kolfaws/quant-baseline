"""processors.weight_calc — 基准加权与点位计算（本层核心）。

关键约定（防止前视 / 保证可复现）：

1. **T 日收益使用 T 日前已确定的权重**；再平衡日的目标权重只用 ``T-lag``
   及更早的市值计算（``lag`` 默认 1）。
2. **基准日** ``base_date`` 的点位严格等于 ``base_value``，当日收益记 0。
3. **停牌 / 缺失**：当日收益按 0 处理，权重保留并在组合内自然漂移；
   不对价格做前向填充。
4. **成分进出池**：成员集合变化当日强制再平衡，避免“已退市成分仍占权重”。
5. 每日权重和恒为 1（容差 1e-9），否则 ``ProcessError``。

本模块不查库、不写库，只接收 polars DataFrame 与配置对象。
"""

from __future__ import annotations

import logging
from bisect import bisect_right
from dataclasses import dataclass
from datetime import date
from typing import Final

import polars as pl

from ..core.config import MemberConfig
from ..core.exceptions import ProcessError

LOG = logging.getLogger("quant_baseline.weight_calc")

_WEIGHT_TOLERANCE: Final[float] = 1e-9
_LEVEL_SCHEMA: Final[dict[str, pl.DataType]] = {
    "trade_date": pl.Date(),
    "close": pl.Float64(),
    "ret_1d": pl.Float64(),
    "nav": pl.Float64(),
}
_WEIGHT_SCHEMA: Final[dict[str, pl.DataType]] = {
    "trade_date": pl.Date(),
    "symbol": pl.Utf8(),
    "weight": pl.Float64(),
    "target_weight": pl.Float64(),
    "price": pl.Float64(),
    "market_value": pl.Float64(),
    "is_rebalance_date": pl.Boolean(),
}


@dataclass(frozen=True, slots=True)
class DayPoint:
    ret: float
    close: float
    market_value: float | None


@dataclass(frozen=True, slots=True)
class MembershipSnapshot:
    trade_date: date
    weights: dict[str, float | None]


class MembershipTimeline:
    """把静态区间成员 / 动态快照统一成 ``day -> {symbol: fixed_weight}``。"""

    def __init__(
        self,
        *,
        static_members: tuple[MemberConfig, ...] | None = None,
        snapshots: tuple[MembershipSnapshot, ...] | None = None,
    ) -> None:
        if static_members and snapshots:
            raise ProcessError("MembershipTimeline 不能同时使用静态成员与动态快照")
        self._static = tuple(static_members or ())
        self._snapshots = tuple(
            sorted(snapshots or (), key=lambda item: item.trade_date)
        )
        self._snapshot_dates = [item.trade_date for item in self._snapshots]

    def at(self, day: date) -> dict[str, float | None]:
        if self._static:
            return {
                member.symbol: member.in_weight
                for member in self._static
                if member.start <= day and (member.end is None or day <= member.end)
            }
        index = bisect_right(self._snapshot_dates, day) - 1
        if index < 0:
            return {}
        return dict(self._snapshots[index].weights)

    @property
    def snapshot_dates(self) -> tuple[date, ...]:
        return tuple(self._snapshot_dates)


# ---------------------------------------------------------------------------
# 面板
# ---------------------------------------------------------------------------
def build_panel(
    frame: pl.DataFrame, *, market_cap_field: str = "total_mv"
) -> dict[date, dict[str, DayPoint]]:
    """把长表行情转成 ``{trade_date: {symbol: DayPoint}}``。"""
    if frame.is_empty():
        return {}
    required = {"symbol", "trade_date", "close", "ret_1d"}
    missing = required - set(frame.columns)
    if missing:
        raise ProcessError(f"行情面板缺列 {sorted(missing)}")
    if market_cap_field not in frame.columns:
        raise ProcessError(f"行情面板缺市值列 {market_cap_field!r}")

    panel: dict[date, dict[str, DayPoint]] = {}
    for row in frame.iter_rows(named=True):
        cap = row.get(market_cap_field)
        panel.setdefault(row["trade_date"], {})[row["symbol"]] = DayPoint(
            ret=float(row["ret_1d"]),
            close=float(row["close"]),
            market_value=None if cap is None else float(cap),
        )
    return panel


# ---------------------------------------------------------------------------
# 再平衡日
# ---------------------------------------------------------------------------
def _period_key(day: date, frequency: str) -> tuple[int, ...] | None:
    if frequency == "daily":
        return (day.toordinal(),)
    if frequency == "weekly":
        iso = day.isocalendar()
        return (iso.year, iso.week)
    if frequency == "monthly":
        return (day.year, day.month)
    if frequency == "quarterly":
        return (day.year, (day.month - 1) // 3 + 1)
    return None


def rebalance_dates(
    calendar: list[date], frequency: str, align: str = "first_trading_day"
) -> set[date]:
    """按频率/对齐方式返回再平衡交易日集合。"""
    if frequency == "none":
        return set()
    if frequency == "daily":
        return set(calendar)
    if align not in {"first_trading_day", "last_trading_day"}:
        raise ProcessError(f"未知 rebalance.align={align!r}")

    groups: dict[tuple[int, ...], list[date]] = {}
    for day in calendar:
        key = _period_key(day, frequency)
        if key is not None:
            groups.setdefault(key, []).append(day)
    if align == "first_trading_day":
        return {days[0] for days in groups.values()}
    return {days[-1] for days in groups.values()}


# ---------------------------------------------------------------------------
# 目标权重
# ---------------------------------------------------------------------------
def target_weights(
    *,
    members: dict[str, float | None],
    method: str,
    panel: dict[date, dict[str, DayPoint]],
    reference_day: date,
    available: set[str],
    market_cap_field: str = "total_mv",
    panel_dates: list[date] | None = None,
) -> dict[str, float]:
    """计算某个再平衡日的目标权重；不满足数据的成分会被剔除。"""
    if not members:
        return {}
    points = panel.get(reference_day, {})

    if method == "equal":
        chosen = [symbol for symbol in members if symbol in available]
        if not chosen:
            return {}
        weight = 1.0 / len(chosen)
        return {symbol: weight for symbol in chosen}

    if method == "market_cap":
        caps: dict[str, float] = {}
        for symbol in members:
            point = points.get(symbol)
            if point is None or point.market_value is None:
                point = _latest_point(panel, symbol, reference_day, panel_dates)
            if point is None or point.market_value is None or point.market_value <= 0:
                continue
            caps[symbol] = float(point.market_value)
        total = sum(caps.values())
        if total <= 0:
            return {}
        return {symbol: cap / total for symbol, cap in caps.items()}

    if method == "fixed":
        fixed = {
            symbol: float(weight)
            for symbol, weight in members.items()
            if weight is not None and float(weight) > 0 and symbol in available
        }
        total = sum(fixed.values())
        if total <= 0:
            return {}
        return {symbol: weight / total for symbol, weight in fixed.items()}

    raise ProcessError(f"未知加权方法 {method!r}")


def _latest_point(
    panel: dict[date, dict[str, DayPoint]],
    symbol: str,
    day: date,
    panel_dates: list[date] | None = None,
) -> DayPoint | None:
    """从 ``day`` 往前找最近一个有市值的点（最多 10 个交易日）。"""
    dates = panel_dates if panel_dates is not None else sorted(panel)
    index = bisect_right(dates, day) - 1
    if index < 0:
        return None
    for candidate in reversed(dates[max(0, index - 9) : index + 1]):
        point = panel[candidate].get(symbol)
        if point is not None and point.market_value is not None:
            return point
    return None


# ---------------------------------------------------------------------------
# 合成基准
# ---------------------------------------------------------------------------
def build_synthetic_benchmark(
    panel: dict[date, dict[str, DayPoint]],
    calendar: list[date],
    *,
    base_date: date,
    base_value: float,
    frequency: str,
    align: str,
    method: str,
    timeline: MembershipTimeline,
    market_cap_field: str = "total_mv",
    lag: int = 1,
    label: str = "benchmark",
) -> tuple[pl.DataFrame, pl.DataFrame]:
    """构建合成基准点位与每日权重。

    Returns:
        ``(levels, weights)``；levels 字段为 trade_date/close/ret_1d/nav，
        weights 字段为 trade_date/symbol/weight/target_weight/price/market_value/is_rebalance_date。
    """
    if not calendar:
        raise ProcessError(f"{label} 交易日历为空")
    if base_value <= 0:
        raise ProcessError(f"{label} base_value 必须 > 0")

    full_calendar = sorted(set(calendar))
    start_index = next(
        (index for index, day in enumerate(full_calendar) if day >= base_date), None
    )
    if start_index is None:
        raise ProcessError(f"{label} 在 {base_date} 之后没有交易日")
    start_date = full_calendar[start_index]

    schedule = rebalance_dates(full_calendar, frequency, align)
    available = {symbol for day_points in panel.values() for symbol in day_points}
    panel_dates = sorted(panel)

    levels: list[tuple[date, float, float, float]] = []
    weight_rows: list[tuple] = []
    weights: dict[str, float] = {}
    previous_members: dict[str, float | None] = {}
    level = base_value

    for index in range(start_index, len(full_calendar)):
        day = full_calendar[index]
        members = timeline.at(day)
        member_changed = set(members) != set(previous_members)
        is_rebalance = index == start_index or day in schedule or member_changed
        target: dict[str, float] = {}

        if is_rebalance:
            reference_day = full_calendar[max(0, index - lag)]
            target = target_weights(
                members=members,
                method=method,
                panel=panel,
                reference_day=reference_day,
                available=available,
                market_cap_field=market_cap_field,
                panel_dates=panel_dates,
            )
            if target:
                weights = target
            elif weights:
                LOG.warning(
                    "%s %s 目标权重为空，沿用上一期权重（成分数据缺失）", label, day
                )
            else:
                raise ProcessError(
                    f"{label} 在基准日 {day} 无法计算目标权重；请检查成分行情",
                    members=list(members)[:5],
                )

        points = panel.get(day, {})
        if index == start_index:
            port_ret = 0.0
            level = base_value
        else:
            total_weight = sum(weights.values())
            if total_weight <= 0:
                raise ProcessError(f"{label} {day} 权重合计为 0", day=str(day))
            port_ret = (
                sum(
                    weight * points[symbol].ret
                    for symbol, weight in weights.items()
                    if symbol in points
                )
                / total_weight
            )
            level *= 1.0 + port_ret
            if not (level > 0):
                raise ProcessError(f"{label} {day} 点位非正：{level}")
            # 权重随价格漂移；缺失/停牌成分按 0 收益处理
            drifted = {
                symbol: weight * (1.0 + points[symbol].ret)
                if symbol in points
                else weight
                for symbol, weight in weights.items()
            }
            total = sum(drifted.values())
            if total <= 0:
                raise ProcessError(f"{label} {day} 漂移后权重合计为 0", day=str(day))
            weights = {symbol: value / total for symbol, value in drifted.items()}

        total = sum(weights.values())
        if total == 0 or abs(total - 1.0) > _WEIGHT_TOLERANCE:
            raise ProcessError(
                f"{label} {day} 权重和 {total!r} != 1（容差 {_WEIGHT_TOLERANCE}）",
                day=str(day),
            )

        levels.append((day, level, port_ret, level / base_value))
        for symbol, weight in sorted(weights.items()):
            point = points.get(symbol)
            weight_rows.append(
                (
                    day,
                    symbol,
                    weight,
                    target.get(symbol) if is_rebalance and target else None,
                    None if point is None else point.close,
                    None if point is None else point.market_value,
                    is_rebalance,
                )
            )
        previous_members = members

    LOG.info(
        "%s 完成：%s..%s，%d 个交易日，%d 条权重",
        label,
        start_date,
        full_calendar[-1],
        len(levels),
        len(weight_rows),
    )
    return (
        pl.DataFrame(levels, schema=_LEVEL_SCHEMA, orient="row"),
        pl.DataFrame(weight_rows, schema=_WEIGHT_SCHEMA, orient="row"),
    )


# ---------------------------------------------------------------------------
# 外部基准
# ---------------------------------------------------------------------------
def build_external_series(
    frame: pl.DataFrame,
    *,
    base_date: date,
    label: str = "benchmark",
) -> pl.DataFrame:
    """把指数/ETF 收盘价转成基准点位与净值（base_date 归一为 1.0）。"""
    if frame.is_empty():
        raise ProcessError(f"{label} 行情为空")
    required = {"trade_date", "close"}
    missing = required - set(frame.columns)
    if missing:
        raise ProcessError(f"{label} 缺列 {sorted(missing)}")

    frame = frame.unique(subset=["trade_date"], keep="last").sort("trade_date")
    if "ret_1d" not in frame.columns:
        frame = frame.with_columns(
            (pl.col("close") / pl.col("close").shift(1) - 1.0)
            .fill_null(0.0)
            .alias("ret_1d")
        )

    rows = frame.filter(pl.col("trade_date") >= base_date)
    if rows.is_empty():
        raise ProcessError(f"{label} 在 {base_date} 之后没有数据")
    base_close = float(rows["close"][0])
    if base_close <= 0:
        raise ProcessError(f"{label} 基准日收盘价非正：{base_close}")

    actual_base = rows["trade_date"][0]
    rows = rows.with_columns(
        (pl.col("close") / base_close).alias("nav"),
    )
    # 基准日之前的收益不属于本区间；基准日收益记 0
    rows = rows.with_columns(
        pl.when(pl.col("trade_date") == actual_base)
        .then(0.0)
        .otherwise(pl.col("ret_1d").cast(pl.Float64))
        .alias("ret_1d")
    )
    rows = rows.filter(pl.col("nav").is_not_null() & (pl.col("nav") > 0))
    LOG.info(
        "%s 外部序列：%s..%s，%d 行",
        label,
        actual_base,
        rows["trade_date"][-1],
        rows.height,
    )
    return rows.select("trade_date", "close", "ret_1d", "nav")


__all__ = [
    "DayPoint",
    "MembershipSnapshot",
    "MembershipTimeline",
    "build_external_series",
    "build_panel",
    "build_synthetic_benchmark",
    "rebalance_dates",
    "target_weights",
]
