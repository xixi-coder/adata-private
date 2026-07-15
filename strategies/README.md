# 策略目录说明

`strategies/` 用于存放策略本体、回测脚本和策略说明，不再把这些代码放在 `tests/` 目录。

当前目录：

- `strategies/a_share_allocation/`：A股多风格组合策略，覆盖核心低波、股息增强、趋势动量和短线强势。
- `strategies/three_dim_resonance/`：三维共振策略与解析文档。
- `strategies/short_term/`：短线日线策略、激进版和接力版。
- `strategies/trend/`：趋势突破/回踩日线选股，以及原有周趋势 + 基本面增强回测。
- `strategies/value_v1/`：价值策略第一版及分析文档。
- `strategies/value_v2/`：价值策略第二版及说明文档。
- `strategies/wave/`：强势回踩趋势策略及诊断脚本。

运行入口统一放在 `jobs/`：

- `jobs/three_dim_resonance/`
- `jobs/short_term/`

这样拆分后：

- `strategies/` 负责策略实现与研究输出。
- `jobs/` 负责线上任务入口与调度。
- `tests/` 只保留测试和历史产物，不再承载线上运行脚本。
