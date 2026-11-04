"""sources.akshare_source — AKShare 数据源实现（备选/降级）。

职责:
    tushare 不可用（积分不足、接口变更、限流）时的替代取数实现，
    对外契约与 TushareSource 完全一致，可直接替换。

注意:
    - akshare 是可选依赖，安装方式：pip install -e ".[akshare]"
    - 本模块在导入时不得强制依赖 akshare，应延迟到实例化时 import
    - akshare 接口返回的中文列名必须映射为仓库列名（小写下划线）

计划使用的接口:
    ak.tool_trade_date_hist_sina()            交易日历
    ak.stock_zh_index_daily()                 指数日线
    ak.fund_etf_hist_em()                     ETF 日线
    ak.stock_zh_a_hist()                      个股日线（含 adjust 参数）
    ak.index_stock_cons_weight_csindex()      指数成分权重

TODO:
    - 实现 AkShareSource(AbstractSource)
    - 处理接口不稳定：字段缺失、超时、返回结构变更
"""
