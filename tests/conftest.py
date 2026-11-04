"""tests.conftest — Pytest 全局 Fixtures。

计划提供的 fixture:
    config_dir        -> Path，指向仓库内的 configs/（只读）
    settings          -> Settings，加载测试专用配置（tmp_path 下生成，不污染真实库）
    memory_db         -> DuckDB 内存库连接（:memory:），函数级新建、结束关闭
    sqlite_db         -> SQLite :memory: 连接
    initialized_con   -> 已建好全量 schema 的内存库连接
    sample_daily_df   -> 可复用的日线样例 DataFrame（覆盖停牌、缺失、异常值）
    sample_factor_df  -> 复权因子样例
    freezer_date      -> 固定“今天”，保证增量逻辑测试可重复

约定:
    - 测试一律使用内存库或 tmp_path，不读写 warehouse/baseline.db
    - 不访问外网：需要真实数据源的用例标记 @pytest.mark.network

TODO:
    - 落地上面的 fixture
"""
