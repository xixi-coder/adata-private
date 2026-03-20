# 分红缓存一次性云同步

该目录提供一个“一次性执行”的 job，用于把 `data/cache/dividend` 批量补齐并上传到 Google Drive。

入口脚本：

- `jobs/dividend_sync/sync_dividend_cache_once.py`

## 用法

全量一次执行（默认）：

```bash
python jobs/dividend_sync/sync_dividend_cache_once.py
```

先小批量试跑：

```bash
python jobs/dividend_sync/sync_dividend_cache_once.py --limit 200 --max-workers 8
```

如果你希望同时把共享缓存包（`three_dim_cache_bundle.tar.gz`）也更新：

```bash
python jobs/dividend_sync/sync_dividend_cache_once.py --sync-shared-cache
```

## 说明

- 默认会先从 Google Drive 下载历史分红缓存包 `dividend_cache_bundle.tar.gz`，再增量更新并回传。
- 同步清单输出到 `data/cache/dividend/dividend_sync_manifest.json`。
- 若 Google Drive 未配置，上传会自动跳过并给出提示。
