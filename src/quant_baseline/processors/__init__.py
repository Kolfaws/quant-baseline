"""processors — 数据处理层。

输入是 sources 产出的原始 DataFrame，输出是可以直接落库的规整 DataFrame。
本层不做网络请求、不建数据库连接。

子模块:
    cleaner.py      去重、缺失值、异常值、类型规整、停牌标记
    adjuster.py     前/后复权计算、交易日历对齐
    weight_calc.py  等权 / 市值加权基准的计算（本层核心）

依赖方向: processors 可依赖 core 与 sources 的返回类型，但不能被 sources 依赖。
"""

__all__: list[str] = []
