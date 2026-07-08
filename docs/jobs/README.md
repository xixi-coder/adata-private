# Jobs 文档索引

`jobs/` 目录存放可以直接运行的任务入口，以及这些任务共用的云端同步、元数据、打包等公共组件。

## 任务列表

- `jobs/a_share_runner.py`：A 股任务统一调度入口，按 `intraday` / `eod` / `maintenance` profile 编排多个任务。
- [短线分时策略](short_term.md)：前一交易日日线选票，交易日盘中拉取 1 分钟分时并生成信号。
- [三维共振策略](three_dim_resonance.md)：初始化共享市场缓存，并按日生成三维共振买卖建议。
- [A 股每日投资复盘](a_share_allocation.md)：组合持仓复盘、候选池排名、市场状态和调仓建议。
- [成本锚观察池](cost_anchor.md)：围绕定增价、员工持股成本、实控人/高管增持均价构建观察池。
- [分红缓存同步](dividend_sync.md)：批量更新分红缓存并同步到 Google Drive。
- [A 股盘面舆论板块雷达](theme_monitor.md)：盘中调用热榜/人气/概念接口，监控主题升温和降温。
- [因子实验室](factor_lab.md)：核心因子计算、IC 评估、模型训练、walk-forward 评估和预训练模型诊断。
- [雪球自选股监听](xueqiu_monitor.md)：监听指定雪球用户自选股新增变化，并发送通知。
- [公共组件](common.md)：Google Drive 同步、共享缓存打包、A 股元数据过滤、本地环境变量加载等。

## Workflow 对应关系

| Workflow | 入口脚本 | 说明 |
| --- | --- | --- |
| `A股统一任务调度` | `jobs/a_share_runner.py` | 统一定时入口：盘中、盘后、缓存维护通过 profile 调度 |
| `A股盘面舆论板块雷达` | `jobs/theme_monitor/run.py` | 独立盘中热榜/主题雷达，不依赖 K 线缓存 |
| `雪球关注用户自选股监听` | `jobs/xueqiu_monitor/run.py` | 定时监听雪球用户自选股变化 |

未配置 GitHub Actions 的 job 仍可本地或手动运行，例如 `cost_anchor`、`dividend_sync`、`factor_lab`。

## A股统一调度 Profile

```bash
python jobs/a_share_runner.py --profile intraday
python jobs/a_share_runner.py --profile eod
python jobs/a_share_runner.py --profile maintenance
```

- `intraday`：短线分时扫描。日线基座使用上一完整交易日，分时使用当日分钟数据。
- `intraday_pm`：短线分时下午观察窗口。
- `eod`：波动结构、BOLL、A股每日投资复盘、三维共振。
- `maintenance`：共享行情缓存和分红缓存维护。

如需临时只跑部分任务：

```bash
python jobs/a_share_runner.py --profile eod --tasks volatility,boll
```

GitHub Actions 手动运行也统一使用 `A股统一任务调度`，通过 `profile` 或 `tasks` 选择单个策略，不再保留单策略 workflow 文件。

公共交易上下文由 `jobs/common/market_data_context.py` 提供。交易时间内不把当日未收盘 K 线写入日线基座；分时任务通过 `INTRADAY_CACHE_TTL_SECONDS` 控制分钟缓存新鲜度，默认 120 秒。
