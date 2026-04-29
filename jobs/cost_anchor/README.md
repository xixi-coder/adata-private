# 三锚成本观察池

把定增价、员工持股成本、实控人/高管增持均价统一成“成本锚”，筛出当前价贴近成本锚的 A 股标的。

## 运行

```bash
python3 jobs/cost_anchor/run_daily.py
```

默认输出：

- `jobs/cost_anchor/outputs/cost_anchor_watchlist.csv`：观察池
- `jobs/cost_anchor/outputs/latest_summary.md`：Markdown 摘要
- `jobs/cost_anchor/outputs/shareholder_increase_events.csv`：股东增持事件，`trade_average_price` 为空时需要手工补成本
- `jobs/cost_anchor/outputs/employee_plan_events.csv`：员工持股计划事件，披露均价的会自动进入观察池
- `jobs/cost_anchor/manual_anchors.csv`：手工成本锚模板

## 手工成本锚

员工持股计划、实控人增持公告里的成交均价不总是有免费结构化接口。看到公告后，把成本填到
`manual_anchors.csv`，下一次运行会自动进入同一个筛选器。

关键字段：

- `stock_code`：6 位股票代码
- `stock_name`：股票名称
- `anchor_type`：建议填 `员工持股成本` 或 `实控人增持均价`
- `anchor_price`：公告披露的成交均价/受让价格
- `current_price`：可选；填了会直接计算距离锚位
- `anchor_date`：成本锚日期
- `lockup`：锁定期
- `holder_name`：持有人/增持主体
- `amount`：买入金额
- `source`：公告来源
- `note`：备注

## 参数

```bash
python3 jobs/cost_anchor/run_daily.py --lookback-days 180 --near-low -0.08 --near-high 0.10
```

`near-low` 和 `near-high` 控制当前价距离成本锚的筛选区间。
