# 任务目录说明

`jobs/` 用于放可以直接执行的任务入口和任务公共组件，不再把线上运行入口放在 `tests/` 目录。

目录结构：

- `jobs/common/`：云端同步、Google Drive、打包解包、A 股元数据过滤等公共能力。
- `jobs/three_dim_resonance/`：三维共振策略的云端缓存初始化与每日执行入口。
- `jobs/short_term/`：短线策略缓存初始化与分时扫描入口。

当前三维共振相关入口：

- `jobs/three_dim_resonance/init_cloud_cache.py`：初始化 5 年 A 股非 ST 日线缓存、财务缓存和沪深 300 基准，并同步到 Google Drive。
- `jobs/three_dim_resonance/run_daily.py`：每日下载云端缓存，运行三维共振日策略，生成买卖建议，更新持仓状态并回传云端。
- `jobs/short_term/init_cache.py`：初始化短线策略日线缓存。
- `jobs/short_term/intraday_strategy_live.py`：基于前一交易日日线候选池做当日分时扫描。

对应 GitHub Actions：

- `.github/workflows/three-dim-resonance-cache.yml`：初始化三维共振云端缓存
- `.github/workflows/three-dim-resonance-daily.yml`：运行三维共振日策略
- `.github/workflows/init-short-term-cache.yml`：初始化短线策略缓存
- `.github/workflows/daily-run.yml`：运行短线分时策略

策略核心实现统一放到 `strategies/` 目录：

- `strategies/three_dim_resonance/strategy.py`
- `strategies/short_term/short_term_strategy_code.py`
- `strategies/trend/trend_strategy_code.py`
- `strategies/value_v1/value_strategy_code.py`
- `strategies/value_v2/value_strategy_v2.py`
- `strategies/wave/strategy_trend.py`

这样划分的目的是把“策略实现”和“线上任务入口”从 `tests/` 中拆出来，避免 workflow 继续直接调用测试目录下的脚本。
