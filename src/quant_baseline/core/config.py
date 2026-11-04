"""core.config — 配置加载与校验。

职责:
    1. 读取 configs/baseline.yaml、configs/database.yaml、configs/logging.yaml
    2. 展开 ${ENV_VAR} / ${ENV_VAR:-默认值} 占位符（来自 .env 与系统环境）
    3. 用 Pydantic 模型做严格校验：未知字段、非法枚举值、越界数值一律报错
    4. 输出不可变的强类型配置对象，供全项目统一取用

约定:
    - 配置优先级：真实环境变量 > .env > YAML 默认值
    - 校验失败统一抛 ConfigError（不抛裸 ValueError / KeyError）
    - 单例 + 显式路径：load_settings(config_dir=...) 便于测试注入
    - 不在此处做 IO 重试、不建数据库连接

TODO:
    - 定义 BaseSettings / BaselineConfig / DatabaseConfig / LoggingConfig 模型
    - 实现 load_settings(config_dir: Path | None = None) -> Settings
    - 实现占位符展开函数 _expand_env(value)
"""
