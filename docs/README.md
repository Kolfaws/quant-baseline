# quant-baseline 文档索引

面向代码助手按需读取。安装、配置、命令和风险见 [../README.md](../README.md)。

| 文档 | 何时读 | 内容 |
| --- | --- | --- |
| [db_schema.md](db_schema.md) | 改基准表、写入顺序或跨库边界时 | market 读取表、backtest 四张基准表、幂等与校验 |
| [api_reference.md](api_reference.md) | 调用或修改 baseline Python API、CLI 时 | 配置、连接、数据源、处理器、sink、pipeline 接口 |

## 阅读顺序

1. 先读 `../README.md` 了解数据流与运行命令；
2. 只改某个处理器/写入时，直接读对应源码和 `api_reference.md` 对应章节；
3. 涉及表字段、主键、跨库边界时读 `db_schema.md`；
4. 不要从这里推断因子研究口径，因子口径在 `quant-factor/docs/`。
