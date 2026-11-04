"""pipeline — 串联 Source → Processor → Sink 的主流程。

这是唯一知道“完整业务流程”的模块：先取什么、再怎么算、最后写哪些表。
所有具体实现都通过构造注入，便于替换数据源与测试。

计划中的两条主流程:

    1. 同步外部基准（index / etf）
       fetch_index_daily / fetch_etf_daily
         → cleaner.clean → adjuster.align_calendar
         → sink.write(fact_benchmark_daily)

    2. 计算合成基准（equal_weight / market_cap）
       fetch_stock_daily(成分) → cleaner
         → adjuster.adjust → weight_calc.compute_weights
         → weight_calc.compute_index_level
         → sink.write(fact_constituent_daily / fact_benchmark_weight / fact_benchmark_daily)

对外入口（规划）:

    class BaselinePipeline:
        def __init__(self, source, sink, settings): ...
        def run(self, benchmark_code: str, start, end) -> RunReport: ...
        def run_all(self, date=None) -> list[RunReport]: ...

约定:
    - 单次 run 是幂等的：失败可重跑，不会产生重复行
    - 每次 run 生成 run_id，写入 etl_run_log，并更新 meta_sync_state
    - 失败时先写日志再抛异常，不静默吞掉

TODO:
    - 定义 RunReport 数据类
    - 实现按 kind（external / synthetic）分派的执行路径
"""
