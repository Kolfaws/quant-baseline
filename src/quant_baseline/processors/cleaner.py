"""processors.cleaner — 数据清洗。

职责:
    把数据源原始输出整理成“可直接进数据库”的干净表。

包含:
    1. 去重：按业务键（如 (symbol, trade_date)）保留最新一条
    2. 缺失值：按 missing_policy 选择 drop / forward_fill；停牌日标记 is_suspended
    3. 类型规整：日期转 date、价格转 float、成交量额转 int/float
    4. 异常值：close <= 0、high < low、涨跌幅超过涨跌停限制等标记/剔除
    5. 列裁剪：只保留 db_schema 定义的列，多余列显式丢弃并记录警告

约定:
    - 纯函数式：输入 DataFrame，返回新 DataFrame，不修改入参
    - 不吞异常：无法修复的数据要么抛 ProcessError，要么进 rejected 清单
    - 每步都可单独调用，便于测试与复用

TODO:
    - dedupe(df, keys) / fill_missing(df, policy)
    - normalize_dtypes(df) / drop_invalid_rows(df)
    - align_columns(df, table_name) -> 依据 db_schema 裁剪
"""
