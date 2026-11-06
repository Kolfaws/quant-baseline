"""core.logger — 统一日志入口。

* 读取 ``configs/logging.yaml``（标准库 dictConfig 格式）；
* 展开 ``${VAR}`` / ``${VAR:-默认值}``；
* 把 handler 的相对路径按**项目根目录**解析，避免从 scripts/ 启动时写错位置；
* ``get_logger`` 统一加 ``quant_baseline.`` 前缀。
"""

from __future__ import annotations

import logging
import logging.config
import os
from pathlib import Path
from typing import Any

from .config import PROJECT_ROOT, expand_env

_DEFAULT_CONFIG = PROJECT_ROOT / "configs" / "logging.yaml"
_CONFIGURED = False


def _expand(value: Any) -> Any:
    if isinstance(value, str):
        return expand_env(value)
    if isinstance(value, dict):
        return {key: _expand(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_expand(item) for item in value]
    return value


def setup_logging(
    level: str | None = None, config_path: Path | str | None = None
) -> None:
    """按 logging.yaml 初始化日志；重复调用幂等。"""
    global _CONFIGURED
    path = Path(config_path) if config_path is not None else _DEFAULT_CONFIG
    if not path.is_file():
        logging.basicConfig(
            level=getattr(
                logging, (level or os.getenv("LOG_LEVEL", "INFO")).upper(), logging.INFO
            ),
            format="%(asctime)s %(levelname)-8s | %(name)s - %(message)s",
        )
        _CONFIGURED = True
        return

    try:
        import yaml
    except ImportError:  # pragma: no cover
        logging.basicConfig(level=logging.INFO)
        return

    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    config = _expand(raw)
    for handler in config.get("handlers", {}).values():
        filename = handler.get("filename")
        if not filename:
            continue
        target = Path(filename)
        if not target.is_absolute():
            target = PROJECT_ROOT / target
        target.parent.mkdir(parents=True, exist_ok=True)
        handler["filename"] = str(target)

    logging.config.dictConfig(config)
    _CONFIGURED = True
    if level:
        logging.getLogger("quant_baseline").setLevel(level.upper())


def get_logger(name: str) -> logging.Logger:
    """返回 ``quant_baseline.*`` 命名空间下的 logger。"""
    if name.startswith("quant_baseline"):
        return logging.getLogger(name)
    return logging.getLogger(f"quant_baseline.{name}")


__all__ = ["get_logger", "setup_logging"]
