# 成本锚观察池 Job

## 作用

成本锚观察池用于寻找当前价格贴近重要成本锚的 A 股标的。成本锚包括定增价、员工持股成本、实控人或高管增持均价。它更适合作为中短线观察池，而不是自动交易任务。

## 入口文件

- `jobs/cost_anchor/run_daily.py`
- `jobs/cost_anchor/README.md`

核心策略逻辑在：

- `strategies/cost_anchor/cost_anchor_strategy.py`

## Workflow

当前没有单独配置 GitHub Actions workflow，主要用于本地或手动执行。

## 运行方式

```bash
python jobs/cost_anchor/run_daily.py
```

可调参数：

```bash
python jobs/cost_anchor/run_daily.py --lookback-days 180 --near-low -0.08 --near-high 0.10
```

## 输入

- 公告/事件数据接口，由 `CostAnchorStrategy` 内部读取。
- 手工成本锚文件：`jobs/cost_anchor/manual_anchors.csv`
- 参数：
  - `--lookback-days`
  - `--near-low`
  - `--near-high`
  - `--min-executive-amount`
  - `--manual-anchor-path`

## 输出

目录：`jobs/cost_anchor/outputs/`

- `cost_anchor_watchlist.csv`：成本锚观察池
- `latest_summary.md`：Markdown 摘要
- `shareholder_increase_events.csv`：股东增持事件
- `employee_plan_events.csv`：员工持股计划事件

手工模板：

- `jobs/cost_anchor/manual_anchors.csv`

## 运行流程

1. 创建或更新手工成本锚模板。
2. 拉取定增、员工持股、股东增持等事件。
3. 合并手工成本锚。
4. 计算当前价相对锚位的距离。
5. 按配置区间筛选观察池。
6. 输出观察池、待补成本事件和 Markdown 摘要。

## 注意事项

- 免费结构化接口不一定给出所有成交均价，部分事件需要人工补入 `manual_anchors.csv`。
- `near-low` 和 `near-high` 控制距离成本锚的筛选范围。
