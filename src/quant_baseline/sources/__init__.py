"""sources — 从统一数据底座读取数据。

本层只读 market.duckdb，不联网、不抓行情：

    warehouse_source.WarehouseSource
        交易日历 / 个股（含市值、复权因子）/ 指数 / ETF / 指数成分权重
"""

from .warehouse_source import WarehouseSource

__all__ = ["WarehouseSource"]
