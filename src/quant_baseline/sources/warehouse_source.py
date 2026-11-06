"""sources.warehouse_source — 从 ``market.duckdb`` 读取基准计算所需的全部数据。

本模块是 quant-baseline 的**唯一数据入口**：

* 不联网、不抓行情、不建表；
* 连接由调用方以 ``read_only=True`` 打开；
* 只返回 polars DataFrame，列名与 ``quant_schema.market_ddl`` 对齐。
"""

from __future__ import annotations

import logging
import re
from datetime import date
from typing import Final

import duckdb
import polars as pl

from ..core.exceptions import FetchError, SchemaError

LOG = logging.getLogger("quant_baseline.warehouse_source")

_SYMBOL_RE: Final[re.Pattern[str]] = re.compile(r"^[0-9A-Za-z][0-9A-Za-z.\-]{3,19}$")

_STOCK_SCHEMA: Final[dict[str, pl.DataType]] = {
    "symbol": pl.Utf8(),
    "trade_date": pl.Date(),
    "close": pl.Float64(),
    "pre_close": pl.Float64(),
    "pct_chg": pl.Float64(),
    "adj_factor": pl.Float64(),
    "total_mv": pl.Float64(),
    "circ_mv": pl.Float64(),
}
_INDEX_SCHEMA: Final[dict[str, pl.DataType]] = {
    "symbol": pl.Utf8(),
    "trade_date": pl.Date(),
    "close": pl.Float64(),
    "pre_close": pl.Float64(),
    "pct_chg": pl.Float64(),
}
_WEIGHT_SCHEMA: Final[dict[str, pl.DataType]] = {
    "trade_date": pl.Date(),
    "con_code": pl.Utf8(),
    "weight": pl.Float64(),
}
_DATE_SCHEMA: Final[dict[str, pl.DataType]] = {"trade_date": pl.Date()}

_REQUIRED_TABLES: Final[tuple[str, ...]] = (
    "dim_trade_cal",
    "fact_daily",
    "fact_adj_factor",
    "fact_daily_basic",
    "fact_index_daily",
    "fact_index_weight",
    "fact_fund_daily",
)


def _frame(
    con: duckdb.DuckDBPyConnection,
    sql: str,
    params: list[object],
    schema: dict[str, pl.DataType],
) -> pl.DataFrame:
    cursor = con.execute(sql, params) if params else con.execute(sql)
    rows = cursor.fetchall()
    if not rows:
        return pl.DataFrame(schema=schema)
    return pl.DataFrame(rows, schema=schema, orient="row")


def _symbols(values: tuple[str, ...]) -> tuple[str, ...]:
    for value in values:
        if not isinstance(value, str) or not _SYMBOL_RE.fullmatch(value):
            raise FetchError(f"非法证券代码：{value!r}")
    return tuple(dict.fromkeys(values))


def _placeholders(count: int) -> str:
    return ", ".join("?" for _ in range(count))


class WarehouseSource:
    """market.duckdb 只读访问层。"""

    def __init__(self, con: duckdb.DuckDBPyConnection) -> None:
        self._con = con

    # ------------------------------------------------------------------ 元信息
    def verify_schema(self) -> None:
        """确认 market 库具备基准计算所需的表（不要求表里有数据）。"""
        existing = {
            str(row[0])
            for row in self._con.execute(
                "SELECT table_name FROM duckdb_tables() WHERE schema_name = 'main'"
            ).fetchall()
        }
        missing = [name for name in _REQUIRED_TABLES if name not in existing]
        if missing:
            raise SchemaError(
                f"market.duckdb 缺表 {missing}；请先执行 "
                "data_pipeline/scripts/init_market_db.py init"
            )

    def table_exists(self, name: str) -> bool:
        rows = self._con.execute(
            "SELECT count(*) FROM duckdb_tables() "
            "WHERE schema_name = 'main' AND table_name = ?",
            [name],
        ).fetchall()
        return bool(rows and rows[0][0])

    def table_row_count(self, name: str) -> int:
        if not self.table_exists(name):
            return -1
        # name 只允许来自调用方常量；再做一次白名单校验
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", name):
            raise FetchError(f"非法表名：{name!r}")
        return int(self._con.execute(f'SELECT count(*) FROM "{name}"').fetchall()[0][0])

    # ------------------------------------------------------------------ 日历
    def trading_days(self, start: date, end: date, calendar: str = "SSE") -> list[date]:
        rows = self._con.execute(
            """
            SELECT cal_date FROM dim_trade_cal
            WHERE exchange = ? AND is_open = TRUE AND cal_date BETWEEN ? AND ?
            ORDER BY cal_date
            """,
            [calendar, start, end],
        ).fetchall()
        days = [row[0] for row in rows]
        if days:
            return days
        # 降级：日历表尚未导入时，用日线里出现过的交易日
        LOG.warning(
            "dim_trade_cal 在 %s..%s 无记录，改用 fact_daily 的交易日", start, end
        )
        rows = self._con.execute(
            "SELECT DISTINCT trade_date FROM fact_daily "
            "WHERE trade_date BETWEEN ? AND ? ORDER BY trade_date",
            [start, end],
        ).fetchall()
        return [row[0] for row in rows]

    def latest_trade_date(
        self, calendar: str = "SSE", as_of: date | None = None
    ) -> date | None:
        rows = self._con.execute(
            "SELECT max(cal_date) FROM dim_trade_cal "
            "WHERE exchange = ? AND is_open = TRUE AND cal_date <= coalesce(?, current_date)",
            [calendar, as_of],
        ).fetchall()
        if rows and rows[0][0] is not None:
            return rows[0][0]
        rows = self._con.execute(
            "SELECT max(trade_date) FROM fact_daily WHERE trade_date <= coalesce(?, current_date)",
            [as_of],
        ).fetchall()
        return None if not rows else rows[0][0]

    # ------------------------------------------------------------------ 行情
    def stock_daily(
        self, symbols: tuple[str, ...], start: date, end: date
    ) -> pl.DataFrame:
        symbols = _symbols(symbols)
        if not symbols:
            return pl.DataFrame(schema=_STOCK_SCHEMA)
        sql = f"""
            SELECT d.ts_code AS symbol, d.trade_date, d.close,
                   d.pre_close, d.pct_chg,
                   CAST(f.adj_factor AS DOUBLE) AS adj_factor,
                   CAST(b.total_mv AS DOUBLE) AS total_mv,
                   CAST(b.circ_mv AS DOUBLE) AS circ_mv
            FROM fact_daily d
            LEFT JOIN fact_adj_factor f
              ON f.ts_code = d.ts_code AND f.trade_date = d.trade_date
            LEFT JOIN fact_daily_basic b
              ON b.ts_code = d.ts_code AND b.trade_date = d.trade_date
            WHERE d.ts_code IN ({_placeholders(len(symbols))})
              AND d.trade_date BETWEEN ? AND ?
            ORDER BY d.ts_code, d.trade_date
        """
        params: list[object] = [*symbols, start, end]
        frame = _frame(self._con, sql, params, _STOCK_SCHEMA)
        if frame.is_empty():
            raise FetchError(
                f"fact_daily 在 {start}..{end} 没有 {len(symbols)} 只成分的数据",
                symbols=list(symbols)[:5],
            )
        return frame

    def index_daily(self, index_code: str, start: date, end: date) -> pl.DataFrame:
        _symbols((index_code,))
        frame = _frame(
            self._con,
            """
            SELECT index_code AS symbol, trade_date, close, pre_close, pct_chg
            FROM fact_index_daily
            WHERE index_code = ? AND trade_date BETWEEN ? AND ?
            ORDER BY trade_date
            """,
            [index_code, start, end],
            _INDEX_SCHEMA,
        )
        if frame.is_empty():
            raise FetchError(
                f"fact_index_daily 中 {index_code} 在 {start}..{end} 无数据；"
                "请先在 data_pipeline 导入指数行情",
                index_code=index_code,
            )
        return frame

    def fund_daily(self, fund_code: str, start: date, end: date) -> pl.DataFrame:
        _symbols((fund_code,))
        frame = _frame(
            self._con,
            """
            SELECT ts_code AS symbol, trade_date, close, pre_close, pct_chg
            FROM fact_fund_daily
            WHERE ts_code = ? AND trade_date BETWEEN ? AND ?
            ORDER BY trade_date
            """,
            [fund_code, start, end],
            _INDEX_SCHEMA,
        )
        if frame.is_empty():
            raise FetchError(
                f"fact_fund_daily 中 {fund_code} 在 {start}..{end} 无数据；"
                "请先在 data_pipeline 导入 ETF 行情",
                fund_code=fund_code,
            )
        return frame

    # ------------------------------------------------------------------ 成分
    def index_weights(self, index_code: str, start: date, end: date) -> pl.DataFrame:
        _symbols((index_code,))
        frame = _frame(
            self._con,
            """
            SELECT trade_date, con_code, weight
            FROM fact_index_weight
            WHERE index_code = ? AND trade_date BETWEEN ? AND ?
            ORDER BY trade_date, con_code
            """,
            [index_code, start, end],
            _WEIGHT_SCHEMA,
        )
        if frame.is_empty():
            raise FetchError(
                f"fact_index_weight 中 {index_code} 在 {start}..{end} 无快照；"
                "请先在 data_pipeline 导入指数成分权重",
                index_code=index_code,
            )
        return frame

    def index_snapshot_dates(self, index_code: str) -> list[date]:
        rows = self._con.execute(
            "SELECT DISTINCT trade_date FROM fact_index_weight "
            "WHERE index_code = ? ORDER BY trade_date",
            [index_code],
        ).fetchall()
        return [row[0] for row in rows]


__all__ = ["WarehouseSource"]
