"""core.exceptions — 领域异常。

约定：

* 每层只抛本层语义的异常，不向上泄漏 duckdb / yaml 的原生异常；
* 所有异常继承 ``QuantBaselineError``，CLI 顶层可统一转退出码；
* 异常携带 ``context``，单行日志即可定位基准、日期、表名。
"""

from __future__ import annotations

from typing import Any


class QuantBaselineError(RuntimeError):
    """基准计算异常基类。"""

    def __init__(self, message: str, **context: Any) -> None:
        self.message = message
        self.context = context
        super().__init__(self._render())

    def _render(self) -> str:
        if not self.context:
            return self.message
        extra = " ".join(f"{key}={value!r}" for key, value in self.context.items())
        return f"{self.message} [{extra}]"

    def __str__(self) -> str:
        return self._render()


class ConfigError(QuantBaselineError, ValueError):
    """配置缺失或非法。"""


class FetchError(QuantBaselineError):
    """从 market.duckdb 读取数据失败（表缺失、区间无数据）。"""


class ProcessError(QuantBaselineError):
    """清洗 / 加权 / 净值计算失败。"""


class DBWriteError(QuantBaselineError):
    """写 backtest.duckdb 失败。"""


class SchemaError(QuantBaselineError):
    """backtest.duckdb 结构与 quant-db-schema（quant_schema 包）契约不一致。"""


__all__ = [
    "ConfigError",
    "DBWriteError",
    "FetchError",
    "ProcessError",
    "QuantBaselineError",
    "SchemaError",
]
