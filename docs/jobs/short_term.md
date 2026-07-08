# 短线分时策略 Job

## 作用

短线分时策略负责做“日线预筛 + 当天 1 分钟分时确认”。它先用前一交易日的日线数据选出值得盯盘的股票，再在交易日盘中拉取分钟数据，确认是否出现开盘区间突破、站稳 VWAP、量能达标等信号。

## 入口文件

- `jobs/short_term/init_cache.py`
- `jobs/short_term/intraday_strategy_live.py`
- `jobs/short_term/分时策略说明.md`

核心策略逻辑在：

- `strategies/short_term/short_term_strategy_code.py`

## 运行方式

初始化或补齐共享日线缓存：

```bash
python jobs/short_term/init_cache.py
```

运行分时扫描：

```bash
python jobs/short_term/intraday_strategy_live.py
```

## Workflow

- `.github/workflows/a-share-runner.yml`
  - profile：`intraday`
  - 下午版 profile：`intraday_pm`
  - 单独运行任务：`short_term_intraday` 或 `short_term_intraday_pm`
  - 上午版默认窗口 `09:45 ~ 10:30`，下午版窗口 `09:30 ~ 14:30`

注意：GitHub Actions schedule 不保证准点触发。运行时间治理建议见 `docs/short_term_strategy_optimization.md`。

## 输入

- 共享缓存包：`three_dim_cache_bundle.tar.gz`
- 本地缓存目录：`data/cache/`
- 日线缓存：`data/cache/full_data_v3_5year.pkl`
- 沪深 300 基准：`data/cache/benchmark_000300.csv`
- 当日 1 分钟行情：通过 `adata.stock.market.get_market_min()` 获取
- 盘中指数分钟线：默认检查 `000300` 和 `399006`

## 输出

目录：`jobs/short_term/outputs/`

- `latest_candidates.csv`：最近一次日线候选池
- `latest_signals.csv`：最近一次分时信号
- `latest_summary.json`：结构化运行摘要
- `latest_summary.txt`：文本摘要
- `候选池_YYYYMMDD_HHMMSS.csv`：候选池归档
- `分时信号_YYYYMMDD_HHMMSS.csv`：信号归档
- `summary_YYYYMMDD_HHMMSS.json/txt`：摘要归档

分钟缓存：

- `data/cache/minute_live/<trade_date>/<stock_code>.csv`

## 关键环境变量

- `GOOGLE_DRIVE_FOLDER_ID`
- `GOOGLE_OAUTH_CLIENT_ID`
- `GOOGLE_OAUTH_CLIENT_SECRET`
- `GOOGLE_OAUTH_REFRESH_TOKEN`
- `INTRADAY_SIGNAL_START_TIME`
- `INTRADAY_SIGNAL_END_TIME`
- `INTRADAY_CONFIRMATION_MINUTES`
- `INTRADAY_MAX_VWAP_EXTENSION_PCT`
- `INTRADAY_MAX_MINUTE_RETURN_PCT`
- `INTRADAY_MAX_OPEN_TO_SIGNAL_PCT`
- `INTRADAY_MAX_OPENING_GAP_PCT`
- `INTRADAY_MIN_MARKET_CHANGE_PCT`

## 运行流程

1. 从 Google Drive 同步共享缓存。
2. 加载日线数据、股票元数据和沪深 300 基准。
3. 判断当天是否交易日。
4. 用前一交易日日线构建候选池。
5. 检查盘中指数环境。
6. 对候选池逐只拉取 1 分钟数据。
7. 生成分时信号、缓存分钟数据、输出摘要。
8. 回传共享缓存，并通过 workflow 上传 artifact 和发送邮件。

## 注意事项

- 当前策略是信号扫描器，不是完整成交系统。
- 历史分钟数据不稳定，因此更适合积累样本后复盘。
- schedule 延迟可能导致上午策略在下午才触发，需要补运行窗口保护。
