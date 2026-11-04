"""init_warehouse.py — 初始化 warehouse 表结构（建表、建索引、写版本行）。

用法（规划）:
    python scripts/init_warehouse.py                    # 使用 configs/database.yaml 的 active_profile
    python scripts/init_warehouse.py --profile local
    python scripts/init_warehouse.py --db warehouse/baseline.db
    python scripts/init_warehouse.py --check            # 只校验结构，不建表
    python scripts/init_warehouse.py --force            # 结构版本不一致时强制重建（危险）

职责:
    1. 读取 configs/database.yaml，选中 profile 并建立连接
    2. 按 docs/db_schema.md 建表、建索引、建视图（全部 IF NOT EXISTS，幂等）
    3. 写入 meta_schema_version；重复执行只做版本校验
    4. 结构漂移（缺表/缺列/类型不符）时抛 SchemaError 并以非零码退出

退出码: 0 成功 / 1 SchemaError 或数据库错误 / 2 ConfigError

TODO:
    - argparse：--profile / --db / --check / --force / --log-level
    - 实现 init_schema(con) / verify_schema(con) / installed_version(con)
"""
