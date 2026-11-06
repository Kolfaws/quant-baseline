"""tests.test_config — 配置加载与严格校验。"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from quant_baseline.core.config import PROJECT_ROOT, load_settings
from quant_baseline.core.exceptions import ConfigError

CONFIG_DIR = PROJECT_ROOT / "configs"


@pytest.fixture(autouse=True)
def _db_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """测试不依赖本机 .env；显式注入临时路径。"""
    monkeypatch.setenv("MARKET_DB_PATH", str(tmp_path / "market.duckdb"))
    monkeypatch.setenv("BACKTEST_DB_PATH", str(tmp_path / "backtest.duckdb"))


def test_load_real_config() -> None:
    settings = load_settings()
    assert settings.database.name in {"production", "research"}
    assert settings.database.market_db_path.as_posix().endswith("market.duckdb")
    assert settings.database.backtest_db_path.as_posix().endswith("backtest.duckdb")
    assert settings.database.market_read_only is True
    assert settings.sink.conflict_keys == ("benchmark_id", "trade_date")
    assert settings.sink.batch_size == 10000
    assert settings.baseline.benchmark("BI_HS300").kind == "external"
    assert settings.baseline.benchmark("BI_EQW_CORE").weight.method == "equal"
    assert "BI_EQW_CORE" in {
        item.code for item in settings.baseline.enabled_benchmarks()
    }


def _copy_configs(tmp_path: Path) -> None:
    for name in ("baseline.yaml", "database.yaml", "logging.yaml"):
        shutil.copy(CONFIG_DIR / name, tmp_path / name)


def test_unknown_field_rejected(tmp_path: Path) -> None:
    _copy_configs(tmp_path)
    path = tmp_path / "baseline.yaml"
    path.write_text(path.read_text(encoding="utf-8") + "\nfoo: 1\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="未知字段"):
        load_settings(tmp_path)


def test_bad_conflict_keys_rejected(tmp_path: Path) -> None:
    _copy_configs(tmp_path)
    path = tmp_path / "database.yaml"
    text = path.read_text(encoding="utf-8").replace(
        'conflict_keys: ["benchmark_id", "trade_date"]',
        'conflict_keys: ["benchmark_id"]',
    )
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ConfigError, match="conflict_keys"):
        load_settings(tmp_path)


def test_unknown_universe_rejected(tmp_path: Path) -> None:
    _copy_configs(tmp_path)
    path = tmp_path / "baseline.yaml"
    text = path.read_text(encoding="utf-8").replace(
        "universe: UNI_CN_A_CORE", "universe: UNI_NOT_EXIST"
    )
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ConfigError, match="UNI_NOT_EXIST"):
        load_settings(tmp_path)


def test_profile_selection(tmp_path: Path) -> None:
    _copy_configs(tmp_path)
    settings = load_settings(tmp_path, profile="research")
    assert settings.database.name == "research"
