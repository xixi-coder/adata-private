# 短线任务说明

`jobs/short_term/` 存放短线策略的线上运行入口。

- `init_cache.py`：初始化日线缓存，供后续分时扫描使用。
- `intraday_strategy_live.py`：基于前一交易日日线候选池，结合当日 1 分钟数据生成分时信号。
- `分时策略说明.md`：分时扫描逻辑说明。

本地输出目录：

- `jobs/short_term/outputs/latest_candidates.csv`
- `jobs/short_term/outputs/latest_signals.csv`
- `jobs/short_term/outputs/latest_summary.json`
- `jobs/short_term/outputs/latest_summary.txt`

对应 workflow：

- `.github/workflows/init-short-term-cache.yml`
- `.github/workflows/daily-run.yml`
