"""backfill_history.py — 历史数据回补（针对新标的或新表）。

用法（规划）:
    python scripts/backfill_history.py --benchmark BI_EQW_CORE --start 2015-01-01
    python scripts/backfill_history.py --all --start 2018-01-01 --end 2020-12-31
    python scripts/backfill_history.py --universe UNI_CN_A_CORE --chunk-days 365

职责:
    1. 按日期分片（chunk_days）逐段回补，避免单次请求过大、内存打爆
    2. 每片独立事务：单片失败只回滚该片，可断点续跑
    3. 回补完成后再推进 meta_sync_state 水位
    4. 与 sync_baseline 共用 BaselinePipeline，保证计算口径完全一致

注意:
    - 回补不等同于“覆盖”：upsert 只覆盖区间内已有行，不删除区间外数据
    - 若标的结构变化（新增成分/改名），需先更新 dim_* 表再回补 fact 表

TODO:
    - argparse：--benchmark / --universe / --all / --start / --end / --chunk-days
    - 实现分片循环、进度输出与断点续跑
"""
