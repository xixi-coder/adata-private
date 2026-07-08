# A 股盘面舆论板块雷达 Job

## 作用

盘面舆论板块雷达用于交易时间内快速观察市场正在发酵的主题。它不读取共享 K 线缓存，也不写入 `full_data_v3_5year.pkl`，只调用热榜、人气榜、概念/行业热度接口，输出主题热度、代表股和升温/降温状态。

## 入口文件

- `jobs/theme_monitor/run.py`
- `strategies/theme_monitor/theme_monitor_strategy.py`

## Workflow

- `.github/workflows/theme-monitor.yml`
  - 北京时间 09:35、10:30、13:30、14:30 运行
  - 支持手动触发
  - 会提交 `jobs/theme_monitor/state/latest_snapshot.json`，用于跨运行对比主题升温/降温

## 输入

- 同花顺热股榜：`adata.sentiment.hot.hot_rank_100_ths()`
- 同花顺热门概念：`adata.sentiment.hot.hot_concept_20_ths(plate_type=1)`
- 同花顺热门行业：`adata.sentiment.hot.hot_concept_20_ths(plate_type=2)`
- 东方财富人气榜：`adata.sentiment.hot.pop_rank_100_east()`

## 输出

目录：`jobs/theme_monitor/outputs/`

- `latest_summary.json`
- `latest_email_body.txt`
- `latest_hot_stocks.csv`
- `latest_hot_concepts.csv`
- `latest_hot_industries.csv`
- `latest_popularity_stocks.csv`
- `latest_theme_radar.csv`

状态文件：

- `jobs/theme_monitor/state/latest_snapshot.json`

## 评分逻辑

第一版主题分：

- 概念/行业热榜分：40%
- 热股标签频次：30%
- 主题内热股平均涨跌幅：15%
- 东方财富人气榜共振：10%
- 资金流确认：预留 5%

状态包括：

- `新晋升温`
- `快速升温`
- `持续发酵`
- `降温`
- `震荡观察`

## 注意事项

- 这是方向雷达，不直接给买卖点。
- 盘中数据来自公开热榜接口，适合分钟级观察，不适合当作毫秒级实时行情。
- 后续可加概念资金流作为资金确认分。
