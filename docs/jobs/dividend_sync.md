# 分红缓存同步 Job

## 作用

分红缓存同步用于批量抓取和更新 A 股分红数据，并同步到 Google Drive。A 股复盘和高股息相关策略可复用这份缓存，避免每次运行都重新拉取分红接口。

## 入口文件

- `jobs/dividend_sync/sync_dividend_cache_once.py`
- `jobs/dividend_sync/README.md`

## Workflow

当前没有单独配置 GitHub Actions workflow，主要用于本地或手动执行。

## 运行方式

全量或增量更新：

```bash
python jobs/dividend_sync/sync_dividend_cache_once.py
```

小批量试跑：

```bash
python jobs/dividend_sync/sync_dividend_cache_once.py --limit 200 --max-workers 8
```

同步后附带更新共享缓存包：

```bash
python jobs/dividend_sync/sync_dividend_cache_once.py --sync-shared-cache
```

## 输入

- Google Drive 历史分红缓存包：`dividend_cache_bundle.tar.gz`
- 股票元数据：`tests/utils/all_code.csv`
- 分红接口：`adata.stock.market.get_dividend()`

## 输出

本地：

- `data/cache/dividend/<stock_code>.csv`
- `data/cache/dividend/dividend_sync_manifest.json`

Google Drive：

- `dividend_cache_bundle.tar.gz`
- 可选：`three_dim_cache_bundle.tar.gz`

## 关键参数

- `--max-workers`
- `--refresh-days`
- `--limit`
- `--retry`
- `--archive-name`
- `--sync-shared-cache`

## 运行流程

1. 从 Google Drive 下载历史分红缓存。
2. 加载非 ST、非退市、普通 A 股代码列表。
3. 多线程抓取或更新分红 CSV。
4. 避免临时空结果覆盖已有非空文件。
5. 写入同步 manifest。
6. 上传分红缓存包。
7. 如指定 `--sync-shared-cache`，附带上传共享市场缓存包。

## 注意事项

- 默认会跳过 refresh_days 内已更新的文件。
- 任务是一次性同步工具，不建议在盘中高频执行。
