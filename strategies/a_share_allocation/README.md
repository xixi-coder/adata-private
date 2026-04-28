# A股组合策略实验室

这是一个偏稳健的多风格组合策略，用于把“长期核心仓 + 中期趋势 + 少量短线强势”的想法落成可回测规则。

## 策略结构

- 核心低波：成交额、低波动、回撤控制、接近阶段高点。
- 股息增强：读取 `data/cache/dividend/*.csv` 中的近一年现金分红，计算滚动股息率；没有分红缓存时自动退化为低波/流动性代理。
- 趋势动量：20/60 日相对强度、60 日均线斜率、波动惩罚。
- 短线强势：5 日涨幅、量能扩张、接近 60 日高点，同时惩罚过热。
- 市场状态：沪深 300 弱于 120 日均线且 60 日均线斜率走弱时降低目标仓位。

## 运行

```bash
.venv/bin/python -m strategies.a_share_allocation.strategy \
  --start 2025-09-01 \
  --end 2026-03-25 \
  --universe-size 800 \
  --max-positions 18 \
  --rebalance-period 20
```

结果默认输出到 `tests/a_share_allocation_backtest/`：

- `a_share_allocation_scores.csv`
- `a_share_allocation_trades.csv`
- `a_share_allocation_equity.csv`
- `a_share_allocation_metrics.csv`

## 注意

这是研究和回测代码，不是荐股或自动下单系统。A 股 T+1、涨跌停、停牌、滑点、冲击成本、财报修订和退市风险都会让实盘结果偏离回测。实盘前至少需要接入真实可交易状态、涨跌停过滤、停牌过滤和更严格的风控。

## GitHub Actions 每日复盘

已提供 `.github/workflows/a-share-daily-review.yml`，默认北京时间工作日 17:40 触发。Workflow 会先用 A 股交易日历判断是否交易日，遇到节假日会跳过。

需要配置的 GitHub Secrets：

- `GOOGLE_DRIVE_FOLDER_ID`
- `GOOGLE_OAUTH_CLIENT_ID`
- `GOOGLE_OAUTH_CLIENT_SECRET`
- `GOOGLE_OAUTH_REFRESH_TOKEN`
- `MAIL_163_USER`
- `MAIL_163_PASS`
- `MAIL_TO`

可选配置 `A_SHARE_PORTFOLIO_JSON`，用于生成持仓逐一判断。示例：

```json
[
  {"code": "300476", "name": "胜宏科技", "weight": 30.46, "cost": 225.556},
  {"code": "002463", "name": "沪电股份", "weight": 23.99, "cost": 57.923}
]
```

每日输出位于 `jobs/a_share_allocation/outputs/`，并会作为 GitHub Actions artifact 上传：

- `latest_email_body.txt`
- `latest_summary.json`
- `latest_top_candidates.csv`
- `latest_portfolio_review.csv`
- `latest_metrics.csv`
- `latest_trades.csv`
- `latest_equity.csv`
