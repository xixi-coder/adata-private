# A 股每日投资复盘 Job

## 作用

A 股每日投资复盘用于盘后生成组合层面的投资复盘。它会更新或读取共享市场缓存，运行 A 股配置/轮动策略，对候选池、已有持仓、市场状态和调仓优先级进行综合输出。

## 入口文件

- `jobs/a_share_allocation/run_daily.py`

核心策略逻辑在：

- `strategies/a_share_allocation/strategy.py`
- `strategies/short_term/short_term_strategy_code.py` 用于可选更新共享日线缓存

## Workflow

- `.github/workflows/a-share-runner.yml`
  - profile：`eod`
  - 单独运行任务：`a_share_review`
  - 脚本内部会再次按 A 股交易日历过滤节假日

## 输入

- 共享缓存包：`three_dim_cache_bundle.tar.gz`
- 市场缓存：`data/cache/full_data_v3_5year.pkl`
- 指数缓存：`data/cache/benchmark_000300.csv`
- 分红缓存：`data/cache/dividend/`
- 财务缓存：`data/cache/finance/`
- 可选持仓配置：环境变量 `A_SHARE_PORTFOLIO_JSON`

## 输出

目录：`jobs/a_share_allocation/outputs/`

- `latest_email_body.txt`：邮件正文
- `latest_summary.json`：运行摘要
- `latest_top_candidates.csv`：中文候选清单
- `latest_top_candidates_raw.csv`：原始候选字段
- `latest_portfolio_review.csv`：持仓逐一复盘
- `latest_metrics.csv`：近 180 日回测指标
- `latest_trades.csv`：回测交易记录
- `latest_equity.csv`：回测净值曲线

## 关键环境变量

- `TRADE_DATE`
- `A_SHARE_REVIEW_ALLOW_ONLINE_UPDATE`
- `A_SHARE_REVIEW_UNIVERSE_SIZE`
- `A_SHARE_REVIEW_MAX_POSITIONS`
- `A_SHARE_REVIEW_REBALANCE_PERIOD`
- `A_SHARE_REVIEW_INCLUDE_DIVIDEND`
- `A_SHARE_REVIEW_MIN_AMOUNT_MA20`
- `A_SHARE_REVIEW_MAX_UPDATE_CODES`
- `A_SHARE_PORTFOLIO_JSON`
- Google Drive OAuth 相关变量

## 运行流程

1. 解析目标交易日，并用交易日历判断是否执行。
2. 从 Google Drive 同步共享缓存。
3. 如允许在线更新，用短线策略的数据加载器补齐日线缓存。
4. 加载 A 股配置策略，计算全市场评分。
5. 取信号日之前最近可用日期作为复盘日期。
6. 跑近 180 日回测，生成风险和收益指标。
7. 生成候选 Top、持仓复盘、市场状态、调仓建议。
8. 写输出文件，workflow 上传 artifact 并发送邮件。

## 注意事项

- 该 job 更偏盘后复盘和组合管理，不是盘中短线信号。
- `A_SHARE_PORTFOLIO_JSON` 未配置时，只输出候选池和市场风险。
- 若开启在线更新，任务耗时和行情接口稳定性会影响完成时间。
