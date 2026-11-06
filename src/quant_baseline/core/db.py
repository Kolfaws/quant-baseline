"""core.db — market / backtest 双库连接。

* ``market.duckdb`` **强制只读**：data_pipeline 与 factor_research 可能在写；
* ``backtest.duckdb`` 读写：quant-baseline 只 upsert，不建表；
* 连接器复用 ``data_pipeline.db.connect``，三个项目共享同一套 PRAGMA / 权限策略。
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import duckdb
from data_pipeline.db import connect as _duckdb_connect

from .config import ProfileConfig
from .exceptions import ConfigError, FetchError


def _connect(
    path: Path,
    *,
    read_only: bool,
    threads: int,
    memory_limit: str,
    temp_dir: Path | None,
) -> duckdb.DuckDBPyConnection:
    if read_only and not path.is_file():
        raise FetchError(f"DuckDB 文件不存在：{path}", path=str(path))
    try:
        return _duckdb_connect(
            path,
            read_only=read_only,
            threads=threads,
            memory_limit=memory_limit,
            temp_dir=temp_dir,
        )
    except (OSError, ValueError) as exc:
        raise FetchError(f"打开 DuckDB 失败：{path}：{exc}", path=str(path)) from exc


def connect_market(profile: ProfileConfig) -> duckdb.DuckDBPyConnection:
    """只读打开 market.duckdb；忽略 profile 中的 read_only=false。"""
    if not profile.market_read_only:
        raise ConfigError(
            "quant-baseline 必须以只读方式打开 market.duckdb；"
            "请把 profiles.*.market_read_only 设为 true"
        )
    return _connect(
        profile.market_db_path,
        read_only=True,
        threads=profile.threads,
        memory_limit=profile.memory_limit,
        temp_dir=profile.temp_dir,
    )


def connect_backtest(profile: ProfileConfig) -> duckdb.DuckDBPyConnection:
    """读写打开 backtest.duckdb；表必须由 backtest_engine 预先建好。"""
    if not profile.backtest_db_path.parent.is_dir():
        raise FetchError(
            f"backtest.duckdb 目录不存在：{profile.backtest_db_path.parent}；"
            "请先执行 quant-backtest-engine/scripts/init_backtest_db.py init"
        )
    return _connect(
        profile.backtest_db_path,
        read_only=False,
        threads=profile.threads,
        memory_limit=profile.memory_limit,
        temp_dir=profile.temp_dir,
    )


@contextmanager
def market_connection(profile: ProfileConfig) -> Iterator[duckdb.DuckDBPyConnection]:
    con = connect_market(profile)
    try:
        yield con
    finally:
        con.close()


@contextmanager
def backtest_connection(profile: ProfileConfig) -> Iterator[duckdb.DuckDBPyConnection]:
    con = connect_backtest(profile)
    try:
        yield con
    finally:
        con.close()


__all__ = [
    "backtest_connection",
    "connect_backtest",
    "connect_market",
    "market_connection",
]
