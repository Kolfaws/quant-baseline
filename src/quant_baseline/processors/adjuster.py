"""processors.adjuster — 复权与交易日历对齐。

职责:
    1. 复权：用 adj_factor 计算前复权（qfq）/ 后复权（hfq）价格序列
    2. 日历对齐：把成分行情对齐到统一交易日历，补齐/标记停牌
    3. 基准日归一：把价格序列换算成“基准日 = base_value”的点位序列

关键公式（后复权）:
    hfq_price(t) = close(t) * adj_factor(t) / adj_factor(base_date)
    前复权则除以最新 adj_factor，使最新价与真实价一致。

注意:
    - 自建基准默认使用“不复权价 + 除权除息再投资”还是“后复权价”必须明确，
      否则会在除权日产生虚假跳空（详见 docs/db_schema.md 的待定项）
    - 停牌日不得前向填充价格后参与收益计算，否则会低估波动

TODO:
    - adjust(df, factor_df, mode) -> DataFrame
    - align_calendar(df, calendar) -> DataFrame
    - rebase_to_base(series, base_date, base_value) -> Series
"""
