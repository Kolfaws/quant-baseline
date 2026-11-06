"""processors — 纯计算层（polars）。

    cleaner.py      行情清洗、去重、类型规整
    adjuster.py     收益率口径（pct_chg / hfq / qfq / none）
    weight_calc.py  等权 / 市值加权 / 固定权重；合成基准点位与权重漂移

不查库、不写库、不联网。
"""

from .adjuster import compute_returns
from .cleaner import clean_daily
from .weight_calc import (
    MembershipSnapshot,
    MembershipTimeline,
    build_external_series,
    build_panel,
    build_synthetic_benchmark,
)

__all__ = [
    "MembershipSnapshot",
    "MembershipTimeline",
    "build_external_series",
    "build_panel",
    "build_synthetic_benchmark",
    "clean_daily",
    "compute_returns",
]
