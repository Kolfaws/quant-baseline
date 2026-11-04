"""sync_baseline.py — 日常增量同步基准数据（写入新表）。

用法（规划）:
    python scripts/sync_baseline.py                          # 同步全部启用基准至最新交易日
    python scripts/sync_baseline.py --benchmark BI_HS300
    python scripts/sync_baseline.py --date 20241101          # 只同步指定交易日
    python scripts/sync_baseline.py --dry-run                # 只打印计划，不写库
    python scripts/sync_baseline.py --profile local

职责:
    1. 读取 configs/baseline.yaml 的 sync 段与基准定义
    2. 从 meta_sync_state 读取水位，按 incremental_lookback_days 计算本次区间
    3. 调用 BaselinePipeline.run() 逐基准执行
    4. 汇总结果写入 etl_run_log，并更新 meta_sync_state

调度建议: 交易日收盘后（约 18:00 之后）由 cron / Windows 任务计划触发，
    重复执行安全（幂等 upsert）。

TODO:
    - argparse：--benchmark / --date / --dry-run / --profile / --log-level
    - 实现水位推进与失败基准的隔离（单基准失败不影响其他）
"""
