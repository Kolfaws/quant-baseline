"""sinks.backtest_sink — 把计算结果幂等写入 ``backtest.duckdb``。

本模块**不建表**：表结构由 ``quant-backtest-engine/scripts/init_backtest_db.py`` 拥有。
写入前会校验：

1. 四张目标表存在且 ``meta_schema_version(component='backtest')`` 有版本行；
2. 目标表在允许写入的白名单内（``bt_benchmarks`` / ``bt_baselines`` /
   ``bt_baseline_weights`` / ``bt_compute_log``）；
3. DataFrame 不含表结构外的列（防止字段漂移被静默丢弃）；
4. 写入后主键无重复。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import duckdb
import polars as pl
from quant_schema.constants import (
    BACKTEST_COMPONENT,
    BT_BASELINE_WEIGHTS,
    BT_BASELINES,
    BT_BENCHMARKS,
    BT_COMPUTE_LOG,
    CONFLICT_KEYS,
)

from ..core.config import SinkConfig
from ..core.exceptions import DBWriteError, SchemaError

LOG = logging.getLogger("quant_baseline.backtest_sink")

_WRITABLE = frozenset(
    {BT_BENCHMARKS, BT_BASELINES, BT_BASELINE_WEIGHTS, BT_COMPUTE_LOG}
)


@dataclass(frozen=True, slots=True)
class WriteResult:
    table: str
    rows_written: int


class BacktestSink:
    """backtest.duckdb 的 upsert 写入器（连接由调用方持有）。"""

    def __init__(self, con: duckdb.DuckDBPyConnection, settings: SinkConfig) -> None:
        self._con = con
        self._settings = settings
        self._columns: dict[str, list[str]] = {}
        self.verify_ready()

    # ------------------------------------------------------------------ 校验
    def verify_ready(self) -> None:
        existing = {
            str(row[0])
            for row in self._con.execute(
                "SELECT table_name FROM duckdb_tables() WHERE schema_name = 'main'"
            ).fetchall()
        }
        missing = [table for table in _WRITABLE if table not in existing]
        if missing:
            raise SchemaError(
                f"backtest.duckdb 缺表 {missing}；请先执行 "
                "quant-backtest-engine/scripts/init_backtest_db.py init"
            )
        row = self._con.execute(
            "SELECT version FROM meta_schema_version WHERE component = ?",
            [BACKTEST_COMPONENT],
        ).fetchall()
        if not row:
            raise SchemaError(
                "backtest 组件未初始化；请先执行 scripts/init_backtest_db.py init"
            )
        LOG.debug("backtest schema version=%s", row[0][0])

    def table_exists(self, table: str) -> bool:
        rows = self._con.execute(
            "SELECT count(*) FROM duckdb_tables() "
            "WHERE schema_name = 'main' AND table_name = ?",
            [table],
        ).fetchall()
        return bool(rows and rows[0][0])

    def _table_columns(self, table: str) -> list[str]:
        if table not in self._columns:
            if table not in _WRITABLE:
                raise DBWriteError(f"不允许写表 {table!r}；允许 {sorted(_WRITABLE)}")
            self._columns[table] = [
                str(row[0])
                for row in self._con.execute(f'DESCRIBE "{table}"').fetchall()
            ]
        return self._columns[table]

    # ------------------------------------------------------------------ 写入
    def write(
        self,
        table: str,
        frame: pl.DataFrame,
        *,
        conflict_keys: tuple[str, ...] | None = None,
    ) -> WriteResult:
        if frame is None or frame.is_empty():
            return WriteResult(table=table, rows_written=0)
        columns = self._table_columns(table)
        keys = tuple(conflict_keys or self._settings.table_keys.get(table, ()))
        if not keys:
            keys = CONFLICT_KEYS.get(table, ())
        if not keys:
            raise DBWriteError(f"{table} 未声明幂等键")

        missing_keys = [key for key in keys if key not in frame.columns]
        if missing_keys:
            raise DBWriteError(f"{table} 缺幂等键列 {missing_keys}")
        unknown = sorted(set(frame.columns) - set(columns))
        if unknown:
            raise DBWriteError(f"{table} 含表结构外的列 {unknown}")

        frame = frame.select([column for column in columns if column in frame.columns])
        frame = frame.unique(subset=list(keys), keep="last")
        update_columns = [
            column
            for column in frame.columns
            if column not in keys and column != "created_at"
        ]
        if update_columns:
            conflict = "DO UPDATE SET " + ", ".join(
                f'"{column}" = excluded."{column}"' for column in update_columns
            )
        else:
            conflict = "DO NOTHING"

        batch_size = self._settings.batch_size
        written = 0
        for offset in range(0, frame.height, batch_size):
            batch = frame.slice(offset, batch_size)
            view_name = f"_bt_sink_{offset}"
            self._con.register(view_name, batch)
            try:
                key_sql = ", ".join(f'"{key}"' for key in keys)
                self._con.execute("BEGIN TRANSACTION")
                self._con.execute(
                    f'INSERT INTO "{table}" BY NAME SELECT * FROM {view_name} '
                    f"ON CONFLICT ({key_sql}) {conflict}"
                )
                self._con.execute("COMMIT")
                written += batch.height
            except Exception as exc:
                try:
                    self._con.execute("ROLLBACK")
                except Exception:  # pragma: no cover - 回滚失败不掩盖原异常
                    LOG.debug("回滚失败", exc_info=True)
                raise DBWriteError(
                    f"写入 {table} 失败（offset={offset}）：{exc}",
                    table=table,
                    rows=batch.height,
                ) from exc
            finally:
                self._con.unregister(view_name)

        if self._settings.verify_after_write:
            self._verify_no_duplicates(table, keys)
        LOG.info("写入 %s：%d 行", table, written)
        return WriteResult(table=table, rows_written=written)

    def _verify_no_duplicates(self, table: str, keys: tuple[str, ...]) -> None:
        key_sql = ", ".join(f'"{key}"' for key in keys)
        row = self._con.execute(
            f'SELECT count(*) FROM (SELECT {key_sql} FROM "{table}" '
            f"GROUP BY {key_sql} HAVING count(*) > 1)"
        ).fetchall()
        if row and int(row[0][0]) > 0:
            raise DBWriteError(
                f"{table} 写入后仍存在 {row[0][0]} 组重复主键 {list(keys)}",
                table=table,
            )


__all__ = ["BacktestSink", "WriteResult"]
