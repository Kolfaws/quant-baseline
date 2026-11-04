"""core.logger — 统一日志入口。

职责:
    1. 加载 configs/logging.yaml，展开占位符后交给 logging.config.dictConfig
    2. 确保日志目录（默认 <项目根>/logs/）存在
    3. 对外提供 get_logger(name) —— 返回带包名前缀的标准 Logger

约定:
    - 业务代码统一 `logger = get_logger(__name__)`，禁止直接 logging.basicConfig
    - 重复调用 set_logger() 是幂等的（先按配置重建 handler）
    - 不在日志里输出 Token、密码、数据库连接串
    - 日志文件的相对路径一律相对于**项目根目录**解析，而不是当前工作目录（cwd）。
      否则从 scripts/ 或任意目录启动时会写到别处，且目录不存在会静默丢失日志。
    - 文件 handler 使用了 delay=true，首次写入才打开文件；因此目录必须在
      dictConfig 之前创建好（mkdir(parents=True, exist_ok=True)）。

TODO:
    - 实现 setup_logging(level: str | None = None, config_path: Path | None = None)
    - 实现 get_logger(name: str) -> logging.Logger
"""
