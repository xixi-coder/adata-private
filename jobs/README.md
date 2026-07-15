# 任务目录说明

`jobs/` 用于放可以直接执行的任务入口和任务公共组件，不再把线上运行入口放在 `tests/` 目录。

目录结构：

- `jobs/common/`：云端同步、Google Drive、打包解包、A 股元数据过滤等公共能力。
- `jobs/three_dim_resonance/`：三维共振策略的云端缓存初始化与每日执行入口。
- `jobs/short_term/`：短线策略分时扫描入口。
- `jobs/theme_monitor/`：盘面舆论和板块主题雷达入口。
- `jobs/theme_rotation_workflow/`：把主题雷达归并为科技、创新药、高股息等主线轮动仓位计划。
- `jobs/trend/`：趋势突破与趋势回踩的盘后选股扫描。
- `jobs/dividend_sync/`：分红缓存一次性增量同步到 Google Drive 的入口。

当前三维共振相关入口：

- `jobs/a_share_runner.py`：A 股统一调度入口，通过 `--profile intraday/minute_cache/eod/maintenance` 编排盘中、盘后和缓存维护任务。
- `jobs/three_dim_resonance/init_cloud_cache.py`：初始化 5 年 A 股非 ST 日线缓存、财务缓存和沪深 300 基准，并同步到 Google Drive。
- `jobs/three_dim_resonance/run_daily.py`：每日下载云端缓存，运行三维共振日策略，生成买卖建议，更新持仓状态并回传云端。
- `jobs/short_term/init_cache.py`：初始化短线策略日线缓存。
- `jobs/short_term/intraday_strategy_live.py`：基于前一交易日日线候选池做当日分时扫描。
- `jobs/theme_monitor/run.py`：盘中热榜、人气榜、概念/行业雷达，不依赖共享 K 线缓存。
- `jobs/theme_rotation_workflow/run.py`：主线轮动 workflow，输出主线/副主线/观察/回避和目标仓位区间。
- `jobs/dividend_sync/sync_dividend_cache_once.py`：一次性抓取/更新分红缓存并上传到 Google Drive（可选附带更新共享缓存包）。

共享缓存说明：

- 三维共振和短线任务共用同一个 Google Drive 缓存包 `three_dim_cache_bundle.tar.gz`。
- 共享缓存位于 `data/cache/`，里面同时保存日 K 原始缓存、沪深 300 基准、财务缓存、分钟缓存和策略状态文件。
- 日常任务默认优先读取共享缓存，避免同一份日 K 数据被不同 job 重复从行情接口拉取。

对应 GitHub Actions：

- `.github/workflows/a-share-runner.yml`：统一任务调度（定时入口）
- `.github/workflows/theme-monitor.yml`：盘面舆论板块雷达
- `.github/workflows/theme-rotation-workflow.yml`：主线轮动 workflow，收盘后输出科技/创新药/高股息等篮子计划

其中 `a-share-runner.yml` 承接新的定时调度：

- `intraday`：盘中分时扫描，分钟缓存默认 120 秒 TTL；手动触发时强制使用最新分时数据。
- `minute_cache`：盘后补采短线候选股当日 1 分钟数据，跳过实时运行窗口并强制刷新分钟缓存。
- `eod`：盘后波动、BOLL、趋势交易、三维共振和主线轮动。持仓复盘不再默认运行，仍可在 `tasks` 中指定 `a_share_review` 手动执行。
- `maintenance`：共享行情缓存和分红缓存维护，安排在盘后复盘前执行。

手动只跑单个策略时，在 `统一任务调度` 的 `tasks` 输入里填写任务名，例如 `volatility,boll`。手动触发 `intraday` 会跳过运行窗口判断，并强制刷新分钟数据。

策略核心实现统一放到 `strategies/` 目录：

- `strategies/three_dim_resonance/strategy.py`
- `strategies/short_term/short_term_strategy_code.py`
- `strategies/trend/trend_strategy_code.py`
- `strategies/value_v1/value_strategy_code.py`
- `strategies/value_v2/value_strategy_v2.py`
- `strategies/wave/strategy_trend.py`

这样划分的目的是把“策略实现”和“线上任务入口”从 `tests/` 中拆出来，避免 workflow 继续直接调用测试目录下的脚本。
