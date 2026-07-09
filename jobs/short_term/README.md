# 短线任务说明

`jobs/short_term/` 存放短线策略的线上运行入口。

- `init_cache.py`：初始化日线缓存，供后续分时扫描使用。
- `intraday_strategy_live.py`：基于前一交易日日线候选池，结合当日 1 分钟数据生成分时信号。
- `分时策略说明.md`：分时扫描逻辑说明。

短线策略当前的实战优化方向：

- 候选池分层：把日线候选分成 `弱转强`、`龙头加速`、`分歧修复`、`强势观察`。
- 运行窗口保护：如果 GitHub Actions 延迟到窗口之外，脚本会直接跳过，避免把上午策略跑成下午追高。
- 盘后分时缓存：`minute_cache` profile 会跳过实时窗口，强制拉取候选股当日完整分钟线，用于复盘和样本积累。
- 分时确认更保守：对不同候选类型，自动收紧追高、VWAP 偏离和单分钟脉冲阈值。

共享缓存：

- 短线任务和三维共振任务共用 Google Drive 中的 `three_dim_cache_bundle.tar.gz`。
- 日 K、沪深 300 基准和分钟缓存都写回 `data/cache/` 后再统一上传，避免不同任务重复拉取相同数据。

本地输出目录：

- `jobs/short_term/outputs/latest_candidates.csv`
- `jobs/short_term/outputs/latest_signals.csv`
- `jobs/short_term/outputs/latest_summary.json`
- `jobs/short_term/outputs/latest_summary.txt`

对应 workflow：

- `.github/workflows/a-share-runner.yml`
  - profile：`intraday`
  - profile：`minute_cache`，北京时间工作日 15:35 盘后补采候选股分钟缓存
  - 历史下午版 profile `intraday_pm` 已合并到 `intraday`
  - 手动触发会跳过运行窗口判断，并强制刷新分钟数据
