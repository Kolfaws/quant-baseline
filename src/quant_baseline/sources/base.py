"""sources.base — 数据源抽象基类（AbstractSource）。

职责:
    定义所有 Fetcher 的统一契约，使 pipeline 不感知具体数据源。

契约（子类必须实现）:
    fetch_trade_calendar(start, end)        -> DataFrame[cal_date, is_open]
    fetch_index_daily(symbol, start, end)   -> DataFrame[trade_date, open, high, low, close, ...]
    fetch_etf_daily(symbol, start, end)     -> DataFrame[...]
    fetch_stock_daily(symbols, start, end)  -> DataFrame[symbol, trade_date, open, ...]
    fetch_index_weight(index_code, date)    -> DataFrame[symbol, weight]

约定:
    - 入参日期为 "YYYYMMDD" 或 datetime.date，内部统一
    - 输出列名直接对齐 docs/db_schema.md 的 fact 表字段（小写下划线）
    - 空结果不抛异常，返回空 DataFrame + 列名，由上层决定是否算失败
    - 网络/限流/Token 异常 → FetchError
    - 子类通过 config 注入 token、限流、重试策略，构造时不发请求

TODO:
    - 声明 ABC 与抽象方法
    - 提供 _with_retry() 装饰器与限流工具
"""
