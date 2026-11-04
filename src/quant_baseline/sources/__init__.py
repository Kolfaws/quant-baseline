"""sources — 数据源获取层。

本层只负责“把外部数据变成规整的 DataFrame”，不做清洗、不做加权、不落库。
异常统一转换为 FetchError。

子模块:
    base.py             AbstractSource —— 所有数据源的统一契约
    tushare_source.py   主数据源：指数 / ETF 日线、个股日线、指数权重、交易日历
    akshare_source.py   备选数据源：tushare 不可用或积分不足时降级使用

依赖方向: sources 只能依赖 core，不能 import processors / sinks。
"""

__all__: list[str] = []
