"""core.config — 配置加载与严格校验。

数据来源：

* ``configs/baseline.yaml``：标的池、基准规则、同步区间；
* ``configs/database.yaml``：market / backtest 两个 DuckDB 路径与写入约定；
* ``.env`` / 真实环境变量：路径覆盖（真实环境变量优先）。

不使用 pydantic：只依赖 pyyaml + 标准库，未知字段直接报 ``ConfigError``。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Final

from quant_schema.constants import (
    BT_BASELINE_WEIGHTS,
    BT_BASELINES,
    BT_BASELINES_KEYS,
    BT_BENCHMARKS,
    BT_COMPUTE_LOG,
    CONFLICT_KEYS,
)

from .exceptions import ConfigError

PROJECT_ROOT: Final[Path] = Path(__file__).resolve().parents[3]
_CODE_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z][A-Za-z0-9_]{1,63}$")
_ENV_RE: Final[re.Pattern[str]] = re.compile(
    r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}"
)

REBALANCE_FREQUENCIES: Final[frozenset[str]] = frozenset(
    {"daily", "weekly", "monthly", "quarterly", "none"}
)
REBALANCE_ALIGNS: Final[frozenset[str]] = frozenset(
    {"first_trading_day", "last_trading_day"}
)
WEIGHT_METHODS: Final[frozenset[str]] = frozenset({"equal", "market_cap", "fixed"})
ADJUST_MODES: Final[frozenset[str]] = frozenset({"pct_chg", "hfq", "qfq", "none"})
MISSING_POLICIES: Final[frozenset[str]] = frozenset({"drop", "forward_fill", "hold"})
WRITABLE_TABLES: Final[frozenset[str]] = frozenset(
    {BT_BENCHMARKS, BT_BASELINES, BT_BASELINE_WEIGHTS, BT_COMPUTE_LOG}
)


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------
def _load_dotenv(path: Path | None = None) -> None:
    env_path = path or (PROJECT_ROOT / ".env")
    if not env_path.is_file():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def expand_env(value: str) -> str:
    def replace(match: re.Match[str]) -> str:
        name, default = match.group(1), match.group(2)
        if os.environ.get(name):
            return os.environ[name]
        if default is not None:
            return default
        raise ConfigError(f"环境变量 {name} 未定义，且没有默认值")

    return _ENV_RE.sub(replace, value)


def _resolve_path(raw: str) -> Path:
    path = Path(expand_env(raw)).expanduser()
    return path if path.is_absolute() else (PROJECT_ROOT / path).resolve()


def _mapping(value: Any, path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigError(f"{path} 必须是 mapping，收到 {type(value).__name__}")
    return value


def _sequence(value: Any, path: str) -> list[Any]:
    if not isinstance(value, list):
        raise ConfigError(f"{path} 必须是 list，收到 {type(value).__name__}")
    return value


def _unknown(mapping: dict[str, Any], allowed: set[str], path: str) -> None:
    extra = sorted(set(mapping) - allowed)
    if extra:
        raise ConfigError(f"{path} 存在未知字段 {extra}；允许 {sorted(allowed)}")


def _date(value: Any, path: str) -> date:
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        text = value.strip()
        for fmt in ("%Y-%m-%d", "%Y%m%d"):
            try:
                from datetime import datetime

                return datetime.strptime(text, fmt).date()
            except ValueError:
                continue
    raise ConfigError(f"{path} 不是合法日期：{value!r}（示例 2024-01-05）")


def _optional_date(value: Any, path: str) -> date | None:
    return None if value is None else _date(value, path)


def _int(value: Any, path: str, *, minimum: int | None = None) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ConfigError(f"{path} 必须是 int，收到 {value!r}")
    if minimum is not None and value < minimum:
        raise ConfigError(f"{path} 必须 >= {minimum}，收到 {value}")
    return value


def _float(value: Any, path: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{path} 必须是数字，收到 {value!r}") from exc
    return number


def _bool(value: Any, path: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigError(f"{path} 必须是 bool，收到 {value!r}")
    return value


def _choice(value: Any, choices: frozenset[str], path: str) -> str:
    if not isinstance(value, str) or value not in choices:
        raise ConfigError(f"{path} 必须是 {sorted(choices)} 之一，收到 {value!r}")
    return value


def _code(value: Any, path: str) -> str:
    if not isinstance(value, str) or not _CODE_RE.fullmatch(value):
        raise ConfigError(f"{path} 非法代码：{value!r}（字母开头，字母数字下划线）")
    return value


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ConfigError(f"配置文件不存在：{path}")
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover
        raise ConfigError("缺少 pyyaml：请执行 uv add pyyaml") from exc
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path} YAML 解析失败：{exc}") from exc
    return _mapping(raw, path.name)


# ---------------------------------------------------------------------------
# 配置模型
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class RebalanceRule:
    frequency: str
    align: str


@dataclass(frozen=True, slots=True)
class WeightRule:
    method: str
    field: str = "total_mv"
    lag: int = 1


@dataclass(frozen=True, slots=True)
class MemberConfig:
    symbol: str
    start: date
    end: date | None = None
    name: str | None = None
    in_weight: float | None = None


@dataclass(frozen=True, slots=True)
class ConstituentsSourceConfig:
    kind: str
    api: str
    index_code: str
    refresh: str
    lookback_days: int


@dataclass(frozen=True, slots=True)
class UniverseConfig:
    code: str
    name: str
    description: str | None
    members: tuple[MemberConfig, ...]
    constituents_source: ConstituentsSourceConfig | None

    @property
    def is_dynamic(self) -> bool:
        return self.constituents_source is not None


@dataclass(frozen=True, slots=True)
class BenchmarkConfig:
    code: str
    name: str
    kind: str
    category: str
    symbol: str | None
    universe: str | None
    price_field: str
    adjust: str
    weight: WeightRule | None
    rebalance: RebalanceRule | None
    base_date: date
    base_value: float
    is_active: bool
    description: str | None = None


@dataclass(frozen=True, slots=True)
class DefaultsConfig:
    calendar: str
    price_field: str
    adjust: str
    missing_policy: str
    base_date: date
    base_value: float
    rebalance: RebalanceRule
    weight: WeightRule
    warmup_days: int


@dataclass(frozen=True, slots=True)
class SyncConfig:
    start_date: date
    end_date: date | None
    incremental_lookback_days: int
    chunk_days: int
    trade_calendar: str
    write_mode: str


@dataclass(frozen=True, slots=True)
class BaselineConfig:
    version: int
    defaults: DefaultsConfig
    universes: dict[str, UniverseConfig]
    benchmarks: dict[str, BenchmarkConfig]
    sync: SyncConfig

    def benchmark(self, code: str) -> BenchmarkConfig:
        if code not in self.benchmarks:
            raise ConfigError(f"未知基准 {code!r}；可选 {sorted(self.benchmarks)}")
        return self.benchmarks[code]

    def universe(self, code: str) -> UniverseConfig:
        if code not in self.universes:
            raise ConfigError(f"未知标的池 {code!r}；可选 {sorted(self.universes)}")
        return self.universes[code]

    def enabled_benchmarks(self) -> list[BenchmarkConfig]:
        return [item for item in self.benchmarks.values() if item.is_active]


@dataclass(frozen=True, slots=True)
class SinkConfig:
    table: str
    conflict_keys: tuple[str, ...]
    batch_size: int
    verify_after_write: bool
    table_keys: dict[str, tuple[str, ...]]


@dataclass(frozen=True, slots=True)
class ProfileConfig:
    name: str
    market_db_path: Path
    backtest_db_path: Path
    market_read_only: bool
    threads: int
    memory_limit: str
    temp_dir: Path | None


@dataclass(frozen=True, slots=True)
class Settings:
    baseline: BaselineConfig
    database: ProfileConfig
    sink: SinkConfig
    config_dir: Path


# ---------------------------------------------------------------------------
# baseline.yaml
# ---------------------------------------------------------------------------
_DEFAULTS_KEYS = {
    "calendar",
    "price_field",
    "adjust",
    "missing_policy",
    "base_date",
    "base_value",
    "rebalance",
    "weight",
    "warmup_days",
}


def _parse_rebalance(
    raw: Any, path: str, fallback: RebalanceRule | None = None
) -> RebalanceRule:
    base = fallback or RebalanceRule("monthly", "first_trading_day")
    block = _mapping(raw, path)
    _unknown(block, {"frequency", "align"}, path)
    return RebalanceRule(
        frequency=_choice(
            block.get("frequency", base.frequency),
            REBALANCE_FREQUENCIES,
            f"{path}.frequency",
        ),
        align=_choice(
            block.get("align", base.align), REBALANCE_ALIGNS, f"{path}.align"
        ),
    )


def _parse_weight(
    raw: Any, path: str, fallback: WeightRule | None = None
) -> WeightRule:
    base = fallback or WeightRule("equal", "total_mv", 1)
    block = _mapping(raw, path)
    _unknown(block, {"method", "field", "lag"}, path)
    method = _choice(block.get("method", base.method), WEIGHT_METHODS, f"{path}.method")
    field = block.get("field", base.field)
    if field not in {"total_mv", "circ_mv"}:
        raise ConfigError(f"{path}.field 必须是 total_mv / circ_mv，收到 {field!r}")
    lag = _int(block.get("lag", base.lag), f"{path}.lag", minimum=0)
    return WeightRule(method=method, field=str(field), lag=lag)


def _parse_member(raw: Any, path: str) -> MemberConfig:
    block = _mapping(raw, path)
    _unknown(block, {"symbol", "name", "start", "end", "in_weight"}, path)
    if "symbol" not in block:
        raise ConfigError(f"{path} 缺 symbol")
    if "start" not in block:
        raise ConfigError(f"{path} 缺 start")
    in_weight = block.get("in_weight")
    if in_weight is not None and _float(in_weight, f"{path}.in_weight") <= 0:
        raise ConfigError(f"{path}.in_weight 必须 > 0")
    return MemberConfig(
        symbol=str(block["symbol"]),
        start=_date(block["start"], f"{path}.start"),
        end=_optional_date(block.get("end"), f"{path}.end"),
        name=block.get("name"),
        in_weight=None if in_weight is None else float(in_weight),
    )


def _parse_constituents(raw: Any, path: str) -> ConstituentsSourceConfig:
    block = _mapping(raw, path)
    _unknown(
        block,
        {"kind", "api", "index_code", "refresh", "lookback_days"},
        path,
    )
    kind = block.get("kind", "warehouse")
    if kind != "warehouse":
        raise ConfigError(
            f"{path}.kind 只支持 warehouse（基准项目不抓行情），收到 {kind!r}"
        )
    api = block.get("api", "index_weight")
    if api != "index_weight":
        raise ConfigError(f"{path}.api 只支持 index_weight，收到 {api!r}")
    if "index_code" not in block:
        raise ConfigError(f"{path} 缺 index_code")
    refresh = _choice(
        block.get("refresh", "monthly"),
        frozenset({"daily", "weekly", "monthly", "quarterly"}),
        f"{path}.refresh",
    )
    return ConstituentsSourceConfig(
        kind=kind,
        api=api,
        index_code=str(block["index_code"]),
        refresh=refresh,
        lookback_days=_int(
            block.get("lookback_days", 30), f"{path}.lookback_days", minimum=1
        ),
    )


def _parse_universe(raw: Any, path: str) -> UniverseConfig:
    block = _mapping(raw, path)
    _unknown(
        block,
        {"code", "name", "description", "members", "constituents_source"},
        path,
    )
    if "code" not in block or "name" not in block:
        raise ConfigError(f"{path} 缺 code / name")
    code = _code(block["code"], f"{path}.code")
    members = tuple(
        _parse_member(item, f"{path}.members[{index}]")
        for index, item in enumerate(
            _sequence(block.get("members", []), f"{path}.members")
        )
    )
    source = None
    if block.get("constituents_source") is not None:
        source = _parse_constituents(
            block["constituents_source"], f"{path}.constituents_source"
        )

    if members and source is not None:
        raise ConfigError(f"{path} 不能同时配置 members 与 constituents_source")
    if not members and source is None:
        raise ConfigError(f"{path} 必须提供 members 或 constituents_source")

    for member in members:
        if member.end is not None and member.end < member.start:
            raise ConfigError(
                f"{path} 成分 {member.symbol} 的 end < start",
            )
    return UniverseConfig(
        code=code,
        name=str(block["name"]),
        description=block.get("description"),
        members=members,
        constituents_source=source,
    )


def _parse_benchmark(
    raw: Any, path: str, defaults: DefaultsConfig, universes: dict[str, UniverseConfig]
) -> BenchmarkConfig:
    block = _mapping(raw, path)
    _unknown(
        block,
        {
            "code",
            "name",
            "kind",
            "category",
            "symbol",
            "universe",
            "price_field",
            "adjust",
            "weight",
            "rebalance",
            "base_date",
            "base_value",
            "enabled",
            "description",
        },
        path,
    )
    if "code" not in block or "name" not in block:
        raise ConfigError(f"{path} 缺 code / name")
    code = _code(block["code"], f"{path}.code")
    kind = _choice(
        block.get("kind"), frozenset({"external", "synthetic"}), f"{path}.kind"
    )

    symbol = block.get("symbol")
    universe = block.get("universe")
    weight = None
    rebalance = None
    if kind == "external":
        if not symbol:
            raise ConfigError(f"{path} 是 external，必须提供 symbol")
        if universe:
            raise ConfigError(f"{path} 是 external，不能提供 universe")
        category = block.get("category", "index")
        if category not in {"index", "etf"}:
            raise ConfigError(f"{path}.category 必须是 index / etf，收到 {category!r}")
    else:
        if not universe:
            raise ConfigError(f"{path} 是 synthetic，必须提供 universe")
        if symbol:
            raise ConfigError(f"{path} 是 synthetic，不能提供 symbol")
        if universe not in universes:
            raise ConfigError(f"{path}.universe {universe!r} 不存在")
        category = block.get("category", "basket")
        weight = _parse_weight(
            block.get("weight", {}), f"{path}.weight", fallback=defaults.weight
        )
        rebalance = _parse_rebalance(
            block.get("rebalance", {}), f"{path}.rebalance", fallback=defaults.rebalance
        )

    adjust = _choice(
        block.get("adjust", defaults.adjust), ADJUST_MODES, f"{path}.adjust"
    )
    price_field = block.get("price_field", defaults.price_field)
    if price_field not in {"close", "open", "vwap"}:
        raise ConfigError(f"{path}.price_field 必须是 close / open / vwap")
    base_date = _date(block.get("base_date", defaults.base_date), f"{path}.base_date")
    base_value = _float(
        block.get("base_value", defaults.base_value), f"{path}.base_value"
    )
    if base_value <= 0:
        raise ConfigError(f"{path}.base_value 必须 > 0")

    return BenchmarkConfig(
        code=code,
        name=str(block["name"]),
        kind=kind,
        category=str(category),
        symbol=None if symbol is None else str(symbol),
        universe=None if universe is None else str(universe),
        price_field=str(price_field),
        adjust=adjust,
        weight=weight,
        rebalance=rebalance,
        base_date=base_date,
        base_value=base_value,
        is_active=_bool(block.get("enabled", True), f"{path}.enabled"),
        description=block.get("description"),
    )


def _parse_baseline(raw: dict[str, Any]) -> BaselineConfig:
    _unknown(
        raw, {"version", "defaults", "universes", "benchmarks", "sync"}, "baseline.yaml"
    )
    if raw.get("version") != 1:
        raise ConfigError("baseline.yaml.version 必须为 1")

    defaults_block = _mapping(raw.get("defaults", {}), "defaults")
    _unknown(defaults_block, _DEFAULTS_KEYS, "defaults")
    defaults = DefaultsConfig(
        calendar=str(defaults_block.get("calendar", "SSE")),
        price_field=str(defaults_block.get("price_field", "close")),
        adjust=_choice(
            defaults_block.get("adjust", "pct_chg"), ADJUST_MODES, "defaults.adjust"
        ),
        missing_policy=_choice(
            defaults_block.get("missing_policy", "drop"),
            MISSING_POLICIES,
            "defaults.missing_policy",
        ),
        base_date=_date(
            defaults_block.get("base_date", "2015-01-05"), "defaults.base_date"
        ),
        base_value=_float(
            defaults_block.get("base_value", 1000.0), "defaults.base_value"
        ),
        rebalance=_parse_rebalance(
            defaults_block.get("rebalance", {}), "defaults.rebalance"
        ),
        weight=_parse_weight(defaults_block.get("weight", {}), "defaults.weight"),
        warmup_days=_int(
            defaults_block.get("warmup_days", 30), "defaults.warmup_days", minimum=0
        ),
    )
    if defaults.base_value <= 0:
        raise ConfigError("defaults.base_value 必须 > 0")
    if defaults.price_field not in {"close", "open", "vwap"}:
        raise ConfigError("defaults.price_field 必须是 close / open / vwap")

    universes: dict[str, UniverseConfig] = {}
    for index, item in enumerate(_sequence(raw.get("universes", []), "universes")):
        universe = _parse_universe(item, f"universes[{index}]")
        if universe.code in universes:
            raise ConfigError(f"标的池代码重复：{universe.code}")
        universes[universe.code] = universe
    if not universes:
        raise ConfigError("baseline.yaml 至少需要一个 universe")

    benchmarks: dict[str, BenchmarkConfig] = {}
    for index, item in enumerate(_sequence(raw.get("benchmarks", []), "benchmarks")):
        benchmark = _parse_benchmark(item, f"benchmarks[{index}]", defaults, universes)
        if benchmark.code in benchmarks:
            raise ConfigError(f"基准代码重复：{benchmark.code}")
        benchmarks[benchmark.code] = benchmark
    if not benchmarks:
        raise ConfigError("baseline.yaml 至少需要一个 benchmark")

    sync_block = _mapping(raw.get("sync", {}), "sync")
    _unknown(
        sync_block,
        {
            "start_date",
            "end_date",
            "incremental_lookback_days",
            "chunk_days",
            "trade_calendar",
            "write_mode",
        },
        "sync",
    )
    write_mode = _choice(
        sync_block.get("write_mode", "upsert"),
        frozenset({"upsert", "append"}),
        "sync.write_mode",
    )
    if write_mode != "upsert":
        raise ConfigError("sync.write_mode 只支持 upsert（本项目的幂等约定）")
    sync = SyncConfig(
        start_date=_date(
            sync_block.get("start_date", defaults.base_date), "sync.start_date"
        ),
        end_date=_optional_date(sync_block.get("end_date"), "sync.end_date"),
        incremental_lookback_days=_int(
            sync_block.get("incremental_lookback_days", 5),
            "sync.incremental_lookback_days",
            minimum=0,
        ),
        chunk_days=_int(
            sync_block.get("chunk_days", 365), "sync.chunk_days", minimum=1
        ),
        trade_calendar=str(sync_block.get("trade_calendar", defaults.calendar)),
        write_mode=write_mode,
    )
    if sync.end_date is not None and sync.end_date < sync.start_date:
        raise ConfigError("sync.end_date < sync.start_date")

    return BaselineConfig(
        version=1,
        defaults=defaults,
        universes=universes,
        benchmarks=benchmarks,
        sync=sync,
    )


# ---------------------------------------------------------------------------
# database.yaml
# ---------------------------------------------------------------------------
_PROFILE_KEYS = {
    "market_db_path",
    "backtest_db_path",
    "market_read_only",
    "threads",
    "memory_limit",
    "temp_dir",
}
_SINK_KEYS = {"table", "conflict_keys", "batch_size", "verify_after_write", "tables"}


def _parse_database(
    raw: dict[str, Any], profile: str | None
) -> tuple[ProfileConfig, SinkConfig]:
    _unknown(
        raw,
        {"version", "active_profile", "profiles", "session", "sink"},
        "database.yaml",
    )
    if raw.get("version") != 1:
        raise ConfigError("database.yaml.version 必须为 1")

    profiles = _mapping(raw.get("profiles"), "profiles")
    name = profile or os.environ.get("BASELINE_DB_PROFILE") or raw.get("active_profile")
    if not name:
        raise ConfigError("未指定 active_profile / BASELINE_DB_PROFILE")
    if name not in profiles:
        raise ConfigError(f"profile {name!r} 不存在；可选 {sorted(profiles)}")
    block = _mapping(profiles[name], f"profiles.{name}")
    _unknown(block, _PROFILE_KEYS, f"profiles.{name}")
    missing = [
        key for key in ("market_db_path", "backtest_db_path") if key not in block
    ]
    if missing:
        raise ConfigError(f"profiles.{name} 缺字段 {missing}")
    threads = _int(block.get("threads", 6), f"profiles.{name}.threads", minimum=1)
    if threads > 128:
        raise ConfigError(f"profiles.{name}.threads 必须 <= 128")
    memory_limit = str(block.get("memory_limit", "6GB"))
    if not re.fullmatch(r"^\d+(?:\.\d+)?(?:MB|GB|TB)$", memory_limit, re.IGNORECASE):
        raise ConfigError(f"profiles.{name}.memory_limit 非法：{memory_limit!r}")
    temp_dir = block.get("temp_dir")

    profile_cfg = ProfileConfig(
        name=str(name),
        market_db_path=_resolve_path(str(block["market_db_path"])),
        backtest_db_path=_resolve_path(str(block["backtest_db_path"])),
        market_read_only=_bool(
            block.get("market_read_only", True), f"profiles.{name}.market_read_only"
        ),
        threads=threads,
        memory_limit=memory_limit,
        temp_dir=_resolve_path(str(temp_dir)) if temp_dir else None,
    )

    sink_block = _mapping(raw.get("sink", {}), "sink")
    _unknown(sink_block, _SINK_KEYS, "sink")
    table = sink_block.get("table", BT_BASELINES)
    if table != BT_BASELINES:
        raise ConfigError(f"sink.table 必须是 {BT_BASELINES!r}，收到 {table!r}")
    conflict_keys = tuple(sink_block.get("conflict_keys", BT_BASELINES_KEYS))
    if conflict_keys != tuple(BT_BASELINES_KEYS):
        raise ConfigError(
            f"sink.conflict_keys 必须与主键一致 {list(BT_BASELINES_KEYS)}，"
            f"收到 {list(conflict_keys)}"
        )
    table_keys = {
        name: keys for name, keys in CONFLICT_KEYS.items() if name in WRITABLE_TABLES
    }
    for name, keys in _mapping(sink_block.get("tables", {}), "sink.tables").items():
        if name not in WRITABLE_TABLES:
            raise ConfigError(
                f"sink.tables 含不可写表 {name!r}；允许 {sorted(WRITABLE_TABLES)}"
            )
        if not isinstance(keys, list) or not keys:
            raise ConfigError(f"sink.tables.{name} 必须是非空 list")
        table_keys[str(name)] = tuple(str(key) for key in keys)

    sink = SinkConfig(
        table=str(table),
        conflict_keys=conflict_keys,
        batch_size=_int(
            sink_block.get("batch_size", 10000), "sink.batch_size", minimum=1
        ),
        verify_after_write=_bool(
            sink_block.get("verify_after_write", True), "sink.verify_after_write"
        ),
        table_keys=table_keys,
    )
    return profile_cfg, sink


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
def load_settings(
    config_dir: Path | str | None = None, profile: str | None = None
) -> Settings:
    """加载 baseline.yaml + database.yaml，返回不可变配置对象。"""
    _load_dotenv()
    directory = (
        Path(config_dir) if config_dir is not None else (PROJECT_ROOT / "configs")
    )
    if not directory.is_dir():
        raise ConfigError(f"配置目录不存在：{directory}")

    baseline = _parse_baseline(_load_yaml(directory / "baseline.yaml"))
    database, sink = _parse_database(_load_yaml(directory / "database.yaml"), profile)
    return Settings(
        baseline=baseline, database=database, sink=sink, config_dir=directory
    )


__all__ = [
    "PROJECT_ROOT",
    "BaselineConfig",
    "BenchmarkConfig",
    "ConstituentsSourceConfig",
    "DefaultsConfig",
    "MemberConfig",
    "ProfileConfig",
    "RebalanceRule",
    "Settings",
    "SinkConfig",
    "SyncConfig",
    "UniverseConfig",
    "WeightRule",
    "expand_env",
    "load_settings",
]
