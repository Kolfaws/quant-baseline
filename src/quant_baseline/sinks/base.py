"""sinks.base — 数据写入抽象基类（AbstractSink）。

职责:
    定义统一的落库契约，使 pipeline 不感知底层是 DuckDB 还是 SQLite。

契约（子类必须实现）:
    write(table, df, conflict_keys) -> int   写入并返回受影响行数
    exists(table) -> bool
    row_count(table, where=None) -> int
    ensure_schema() -> None                  建表/校验结构（委托 init 逻辑）

约定:
    - 幂等：同一批数据重复写入，结果行数不变（ON CONFLICT DO UPDATE）
    - 事务：一次 write 调用 = 一个事务；失败整体回滚并抛 DBWriteError
    - 严格列校验：df 出现表结构外的列时，strict_columns=true 直接失败
    - 批量：按 sink.batch_size 分批，避免单条 INSERT 拖慢
    - 连接由调用方持有，sink 不负责连接生命周期

TODO:
    - 声明 ABC 与抽象方法
    - 定义 WriteResult(rows_written, rows_skipped, table)
"""
