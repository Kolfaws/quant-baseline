"""sinks — 数据持久化层。

本层只负责“把规整好的 DataFrame 按约定写进 warehouse”，不做任何业务计算。

子模块:
    base.py             AbstractSink —— 写入契约
    warehouse_sink.py   多表 upsert 实现（DuckDB / SQLite）

依赖方向: sinks 可依赖 core，不能 import sources / processors。
"""

__all__: list[str] = []
