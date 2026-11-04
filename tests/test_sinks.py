"""tests.test_sinks — 多表写入的幂等性与约束测试。

待覆盖:
    1. 幂等：同一批数据写入两次，各表行数不变、内容被更新而非重复
    2. 冲突键：主键冲突时 on_conflict_action=update 覆盖旧值、=ignore 保留旧值
    3. 事务：一批中途失败时整体回滚，库内不残留半批数据
    4. 严格列：出现表结构外的列时抛 DBWriteError（strict_columns=true）
    5. 缺失列：缺列时用 NULL/默认值填充，或按配置报错
    6. delete_insert 模式：区间重算后旧行被正确替换，区间外数据不受影响
    7. 结构校验：verify_schema 能识别缺表、缺列、类型不符
    8. 多表顺序：dim_* 未写入时写 fact_* 应失败（外键/依赖校验）
"""

import pytest

pytest.skip("占位文件：sink 实现后移除本行并补全用例", allow_module_level=True)
