# 三维共振策略 Job

## 作用

三维共振策略是一个 A 股日线择时任务。它在形态、指标、资金三个维度同时转强时给出买入建议，并用固定止损、移动止盈、趋势走弱、资金流出、持仓到期等条件给出卖出建议。

同时，三维共振的缓存初始化任务也是项目里的共享市场缓存构建器，短线策略和 A 股复盘都会复用它生成的 `data/cache/`。

## 入口文件

- `jobs/three_dim_resonance/init_cloud_cache.py`
- `jobs/three_dim_resonance/run_daily.py`
- `jobs/three_dim_resonance/live/strategy.py`
- `jobs/three_dim_resonance/live/data_date_mixin.py`
- `jobs/three_dim_resonance/live/execution_mixin.py`
- `jobs/three_dim_resonance/live/output_mixin.py`
- `jobs/three_dim_resonance/live/state_mixin.py`

核心策略逻辑在：

- `strategies/three_dim_resonance/strategy.py`
- `strategies/three_dim_resonance/策略解析.md`

## Workflow

- `.github/workflows/a-share-runner.yml`
  - 缓存维护 profile：`maintenance`
  - 日常建议 profile：`eod`
  - 单独运行任务：`shared_cache` 或 `three_dim`

## 输入

缓存初始化任务输入：

- 股票元数据：`tests/utils/all_code.csv`
- 行情接口：`adata.stock.market.get_market()`
- 沪深 300 指数接口：`adata.stock.market.get_market_index()`
- 财务接口：`adata.stock.finance.get_core_index()`
- Google Drive 共享缓存包：`three_dim_cache_bundle.tar.gz`

日常建议任务输入：

- `data/cache/full_data_v3_5year.pkl`
- `data/cache/benchmark_000300.csv`
- `data/cache/three_dim_live_state.json`
- 可选环境变量 `TRADE_DATE`

## 输出

缓存目录：

- `data/cache/full_data_v3_5year.pkl`
- `data/cache/benchmark_000300.csv`
- `data/cache/benchmark_399006.csv`
- `data/cache/benchmark_000688.csv`
- `data/cache/finance/*.csv`
- `data/cache/three_dim_cache_manifest.json`
- `data/cache/three_dim_live_state.json`

任务输出：

- `jobs/three_dim_resonance/outputs/latest_summary.json`
- `jobs/three_dim_resonance/outputs/latest_email_body.txt`
- `jobs/three_dim_resonance/outputs/three_dim_summary_YYYYMMDD.json`

Google Drive：

- `three_dim_cache_bundle.tar.gz`
- `three_dim_cache_manifest.json`
- `three_dim_summary_YYYYMMDD.json`
- `three_dim_latest_email.txt`

## 运行流程

缓存初始化：

1. 从 Google Drive 下载已有缓存包。
2. 计算日线补齐目标日期。
3. 增量更新股票日 K、沪深 300/创业板指/科创50 和财务缓存。
4. 分批 checkpoint 落盘。
5. 写入 manifest。
6. 有变化时重新打包上传。

日常建议：

1. 下载共享缓存和状态文件。
2. 加载日线数据和基准。
3. 解析目标交易日。
4. 扫描持仓，生成卖出建议。
5. 扫描股票池，生成买入候选。
6. 写本地输出和邮件正文。

## 注意事项

- 当前日常任务是建议模式，不自动执行真实成交。
- 三维共振缓存包是多个 job 的共享底座，改缓存结构前要同步检查短线和 A 股复盘任务。
- 缓存初始化任务耗时较长，workflow timeout 设置为 360 分钟。
