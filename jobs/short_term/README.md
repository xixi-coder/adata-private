# 短线任务说明

`jobs/short_term/` 存放短线策略的线上运行入口。

- `init_cache.py`：初始化日线缓存，供后续分时扫描使用。
- `intraday_strategy_live.py`：基于前一交易日日线候选池，结合当日 1 分钟数据生成分时信号。
- `分时策略说明.md`：分时扫描逻辑说明。

共享缓存：

- 短线任务和三维共振任务共用 Google Drive 中的 `three_dim_cache_bundle.tar.gz`。
- 日 K、沪深 300 基准和分钟缓存都写回 `data/cache/` 后再统一上传，避免不同任务重复拉取相同数据。

本地输出目录：

- `jobs/short_term/outputs/latest_candidates.csv`
- `jobs/short_term/outputs/latest_signals.csv`
- `jobs/short_term/outputs/latest_summary.json`
- `jobs/short_term/outputs/latest_summary.txt`

对应 workflow：

- `.github/workflows/init-short-term-cache.yml`
- `.github/workflows/daily-run.yml`
