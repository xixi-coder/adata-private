# 三维共振任务说明

## 1. 策略是什么

三维共振是一个 A 股日线择时策略，核心是同时满足三类信号后才给出买入建议：

- 形态共振：平台突破或双底突破。
- 指标共振：`close > MA20 > MA60`、MACD 转强、量能放大。
- 资金共振：CMF 和资金净流向同步改善。

卖出建议来自以下退出条件：

- 固定止损
- 移动止盈
- 趋势转弱
- 资金转弱
- 超过最大持仓天数

详细的指标和回测逻辑在：

- `strategies/three_dim_resonance/strategy.py`
- `strategies/three_dim_resonance/策略解析.md`

## 2. 任务入口

- `jobs/three_dim_resonance/init_cloud_cache.py`
  - 初始化最近 5 年的股票日 K、沪深 300 基准、财务缓存。
  - 每次先下载云端缓存，按“差几天补几天”的方式做增量更新（不是全量重抓）。
  - 仅当本次有缓存变化时，才整包回传覆盖云端。
  - 支持 `checkpoint_every` 分段落盘与 `finance_refresh_days` 控制财务刷新频率。

- `jobs/three_dim_resonance/run_daily.py`
  - 下载云端缓存与状态文件。
  - 基于最近一个可用交易日生成买卖建议（仅建议，不执行成交）。
  - 不做在线增量抓取，直接使用云端缓存包中的本地数据。
  - 生成本地输出并上传建议文件。

## 3. 本地生成的文件

缓存目录：`data/cache/`

- `full_data_v3_5year.pkl`：全市场近 5 年日线缓存。
- `benchmark_000300.csv`：沪深 300 基准数据。
- `finance/*.csv`：个股财务核心指标缓存。
- `three_dim_live_state.json`：当前持仓、待执行买单、待执行卖单、已完成交易。
- `three_dim_cache_manifest.json`：本次初始化的缓存摘要。

任务输出目录：`jobs/three_dim_resonance/outputs/`

- `latest_summary.json`：最近一次运行的摘要。
- `latest_email_body.txt`：邮件正文。
- `three_dim_summary_YYYYMMDD.json`：按交易日归档的摘要。

## 4. 上传到 Google Drive 的文件

初始化任务会上传：

- `three_dim_cache_bundle.tar.gz`：`data/cache/` 的整包压缩。
- `three_dim_cache_manifest.json`：缓存构建结果摘要。

日常任务会更新或上传：

- `three_dim_summary_YYYYMMDD.json`
- `three_dim_latest_email.txt`

其中 `three_dim_cache_bundle.tar.gz` 是云端主缓存；买卖建议和邮件正文都以日文件或最新文件方式保存在 Drive 中。

补充说明：

- `three_dim_cache_bundle.tar.gz` 同时也是短线策略使用的共享市场缓存。
- 因此三维共振初始化完成后，短线分时任务会优先复用其中的日 K 和指数缓存，不再单独重复抓取同一批历史数据。

## 5. Workflow 名称

- `初始化三维共振云端缓存`
- `运行三维共振日策略`

这两个 workflow 分别负责“重建/补齐缓存”和“交易日常运行”。
