"""sources.tushare_source — Tushare 数据源实现（主数据源）。

职责:
    封装 tushare pro 接口，产出与 db_schema 对齐的 DataFrame。

计划使用的接口:
    pro_api().trade_cal       交易日历
    pro_api().index_daily     指数日线（000300.SH 等）
    pro_api().fund_daily      ETF 日线（510300.SH 等）
    pro_api().daily           个股日线（不含复权）
    pro_api().adj_factor      复权因子（配合 adjuster 计算前/后复权）
    pro_api().daily_basic     每日指标（total_mv / circ_mv，市值加权用）
    pro_api().index_weight    指数成分权重（动态标的池用）

约定:
    - token 从环境变量 TUSHARE_TOKEN 读取，绝不硬编码、不写日志
    - 按 configs/baseline.yaml 的 sync.chunk_days 分段请求，按 rate_limit 限流
    - 接口字段名 → 仓库列名的映射集中在一张 FIELD_MAP 常量表里

TODO:
    - 实现 TushareSource(AbstractSource)
    - 实现字段映射与分页/分段拉取
"""
