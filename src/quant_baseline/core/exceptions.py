"""core.exceptions — 领域异常。

设计原则:
    - 每层只抛本层语义的异常，不向上泄漏 requests / duckdb 的原生异常
    - 所有异常继承 QuantBaselineError，便于 CLI 顶层统一捕获并转为退出码
    - 异常对象携带上下文（标的、日期区间、表名），方便日志定位

层级:
    QuantBaselineError
    ├── ConfigError       配置缺失 / 非法 / 未通过校验
    ├── FetchError        取数失败（网络、限流、Token、空数据）
    ├── ProcessError      清洗/加权失败（列缺失、日期不对齐、除零）
    ├── DBWriteError      落库失败（约束冲突、事务回滚）
    └── SchemaError       仓库结构与 docs/db_schema.md 不一致

TODO:
    - 定义基类与上述子类，统一携带 context: dict[str, Any]
"""
