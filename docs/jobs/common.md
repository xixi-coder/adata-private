# Jobs 公共组件

## 作用

`jobs/common/` 存放多个 job 共用的基础能力，包括 Google Drive 同步、共享缓存打包、A 股元数据过滤、本地环境变量加载等。它不是独立业务 job，但几乎所有线上任务都会依赖这里的工具。

## 文件说明

- `jobs/common/cloud_cache_sync.py`
  - Google Drive OAuth 连接
  - 文件上传、下载
  - 缓存目录打包和解包
  - 共享缓存包常量 `SHARED_MARKET_CACHE_ARCHIVE`

- `jobs/common/google_drive_store.py`
  - Google Drive 存储封装

- `jobs/common/archive_bundle.py`
  - tar.gz 打包和解包工具

- `jobs/common/a_share_metadata.py`
  - 加载股票元数据
  - 判断普通 A 股代码
  - 过滤 ST、退市、基金、指数等非目标标的

- `jobs/common/local_env.py`
  - 本地运行时加载 `.env.local`

## 共享缓存

当前最重要的共享缓存包是：

- `three_dim_cache_bundle.tar.gz`

它通常包含：

- `data/cache/full_data_v3_5year.pkl`
- `data/cache/benchmark_000300.csv`
- `data/cache/finance/`
- `data/cache/dividend/`
- `data/cache/minute_live/`
- 策略状态文件和 manifest

依赖该缓存的任务：

- 短线分时策略
- 三维共振策略
- A 股每日投资复盘
- 分红缓存同步可选附带更新它

## Google Drive 环境变量

- `GOOGLE_DRIVE_FOLDER_ID`
- `GOOGLE_OAUTH_CLIENT_ID`
- `GOOGLE_OAUTH_CLIENT_SECRET`
- `GOOGLE_OAUTH_REFRESH_TOKEN`

未配置时，部分上传或下载会跳过或失败，具体取决于调用方是否允许降级。

## 注意事项

- 修改共享缓存结构前，需要同时检查所有依赖 job。
- 大文件缓存不应提交进仓库，统一通过 Google Drive 同步。
- 公共组件应保持向后兼容，避免旧 job 无法读取历史缓存。
