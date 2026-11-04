"""processors.weight_calc — 基准加权计算（本层核心）。

职责:
    根据标的池成分行情与权重规则，计算合成基准的每日点位与权重快照。

支持的方法:
    equal        等权：每只成分权重 = 1 / N（N 为该日有效成分数）
    market_cap   市值加权：权重 = 成分市值 / 成分市值合计（可用 T-1 市值避免前视）
    fixed        固定权重：权重由配置显式给出（如 60/40 组合）

计算流程（等权示例）:
    1. 取成分在 [start, end] 的日收益 r_i(t) = close_i(t)/close_i(t-1) - 1
    2. 按再平衡频率确定每期的权重 w_i
    3. 组合日收益 R(t) = Σ w_i * r_i(t)
    4. 点位 level(t) = level(t-1) * (1 + R(t))，level(base_date) = base_value

必须处理的边界:
    - 成分当日停牌 / 缺失：权重要么剔除并重新归一，要么置 0（策略需固定一种）
    - 成分进出池：只能在再平衡日调整权重，且不得用未来信息
    - 全部成分缺失：当日点位前值填充并标记，不能出现 NaN
    - 单只权重上限（可选）：避免市值加权被单只巨头主导

TODO:
    - compute_weights(prices, method, **kwargs) -> DataFrame[date, symbol, weight]
    - compute_index_level(prices, weights, base_date, base_value) -> DataFrame
    - 与 docs/db_schema.md 的 fact_benchmark_weight 字段对齐
"""
