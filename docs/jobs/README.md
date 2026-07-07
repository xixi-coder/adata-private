# Jobs 文档索引

`jobs/` 目录存放可以直接运行的任务入口，以及这些任务共用的云端同步、元数据、打包等公共组件。

## 任务列表

- [短线分时策略](short_term.md)：前一交易日日线选票，交易日盘中拉取 1 分钟分时并生成信号。
- [三维共振策略](three_dim_resonance.md)：初始化共享市场缓存，并按日生成三维共振买卖建议。
- [A 股每日投资复盘](a_share_allocation.md)：组合持仓复盘、候选池排名、市场状态和调仓建议。
- [成本锚观察池](cost_anchor.md)：围绕定增价、员工持股成本、实控人/高管增持均价构建观察池。
- [分红缓存同步](dividend_sync.md)：批量更新分红缓存并同步到 Google Drive。
- [因子实验室](factor_lab.md)：核心因子计算、IC 评估、模型训练、walk-forward 评估和预训练模型诊断。
- [雪球自选股监听](xueqiu_monitor.md)：监听指定雪球用户自选股新增变化，并发送通知。
- [公共组件](common.md)：Google Drive 同步、共享缓存打包、A 股元数据过滤、本地环境变量加载等。

## Workflow 对应关系

| Workflow | 入口脚本 | 说明 |
| --- | --- | --- |
| `运行短线分时策略` | `jobs/short_term/intraday_strategy_live.py` | 上午版短线盘中扫描 |
| `运行短线分时策略-下午版` | `jobs/short_term/intraday_strategy_live.py` | 下午二次确认，当前默认手动触发 |
| `初始化三维共振云端缓存` | `jobs/three_dim_resonance/init_cloud_cache.py` | 构建/增量更新共享市场缓存 |
| `运行三维共振日策略` | `jobs/three_dim_resonance/run_daily.py` | 生成三维共振日线建议 |
| `A股每日投资复盘` | `jobs/a_share_allocation/run_daily.py` | 盘后组合复盘和候选池输出 |
| `雪球关注用户自选股监听` | `jobs/xueqiu_monitor/run.py` | 定时监听雪球用户自选股变化 |

未配置 GitHub Actions 的 job 仍可本地或手动运行，例如 `cost_anchor`、`dividend_sync`、`factor_lab`。
