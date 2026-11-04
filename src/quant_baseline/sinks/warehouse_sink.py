"""sinks.warehouse_sink — warehouse 多表写入实现。

职责:
    针对 DuckDB / SQLite 的 upsert 写库逻辑，字段与 docs/db_schema.md 一一对应。

写入策略（由 configs/database.yaml 的 sink 段控制）:
    on_conflict     INSERT ... ON CONFLICT(keys) DO UPDATE  —— 默认，幂等
    delete_insert   先按键删除区间内旧行，再整批插入 —— 适合整段重算
    append          纯追加，不去重 —— 仅用于日志类表

多表写入顺序（受外键/依赖约束）:
    meta_schema_version → dim_benchmark / dim_universe / dim_universe_member
    → fact_constituent_daily → fact_benchmark_weight → fact_benchmark_daily
    → meta_sync_state / etl_run_log（最后记录水位）

约定:
    - 列名 → 表字段名映射由 db_schema 驱动，不做隐式猜测
    - 写入前按 conflict_keys 去重（同一批内重复只保留最后一条）
    - 每张表写入后可选校验：行数、主键唯一、NOT NULL 满足
    - DuckDB 跨进程单写者：写锁冲突时按 session.lock_wait_seconds 重试

TODO:
    - 实现 WarehouseSink(AbstractSink)
    - 实现 _build_upsert_sql(table, columns, conflict_keys)
    - 实现 verify_write(table, expected_rows)
"""
