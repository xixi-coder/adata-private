# -*- coding: utf-8 -*-
import argparse
import datetime
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Optional

import pandas as pd


CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(os.path.dirname(CURRENT_DIR))

if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import adata
from jobs.common.a_share_metadata import (
    is_excluded_short_name,
    is_supported_a_share_code,
    load_stock_metadata,
)
from jobs.common.cloud_cache_sync import (
    SHARED_MARKET_CACHE_ARCHIVE,
    sync_cache_from_drive,
    sync_cache_to_drive,
    write_json,
)

DIVIDEND_CACHE_ARCHIVE = "dividend_cache_bundle.tar.gz"
DIVIDEND_CACHE_REL = "data/cache/dividend"
DIVIDEND_MANIFEST_REL = "data/cache/dividend/dividend_sync_manifest.json"


def _normalize_code(value) -> str:
    text = str(value).strip()
    if text.endswith(".0"):
        text = text[:-2]
    return text.zfill(6) if text.isdigit() else text


class DividendCacheSyncJob:
    def __init__(
        self,
        max_workers: int = 10,
        refresh_days: int = 180,
        limit: int = 0,
        retry: int = 2,
        archive_name: str = DIVIDEND_CACHE_ARCHIVE,
        sync_shared_cache: bool = False,
    ):
        self.max_workers = max(1, int(max_workers))
        self.refresh_days = max(0, int(refresh_days))
        self.limit = max(0, int(limit))
        self.retry = max(1, int(retry))
        self.archive_name = archive_name
        self.sync_shared_cache = bool(sync_shared_cache)

        self.project_root = PROJECT_ROOT
        self.cache_dir = os.path.join(PROJECT_ROOT, "data", "cache")
        self.dividend_dir = os.path.join(self.cache_dir, "dividend")
        self.manifest_path = os.path.join(PROJECT_ROOT, DIVIDEND_MANIFEST_REL)
        os.makedirs(self.dividend_dir, exist_ok=True)

    def _load_codes(self) -> List[str]:
        metadata = load_stock_metadata(self.project_root)
        if metadata:
            codes = []
            for code, meta in metadata.items():
                if not is_supported_a_share_code(code):
                    continue
                if is_excluded_short_name(meta.get("short_name", "")):
                    continue
                codes.append(code)
            codes = sorted(set(codes))
            if codes:
                return codes

        df = adata.stock.info.all_code()
        if df is None or df.empty or "stock_code" not in df.columns:
            raise RuntimeError("无法获取股票代码列表")
        df = df.copy()
        df["stock_code"] = df["stock_code"].map(_normalize_code)
        if "short_name" not in df.columns:
            df["short_name"] = ""
        df["short_name"] = df["short_name"].fillna("").astype(str)
        df = df[df["stock_code"].map(is_supported_a_share_code)]
        df = df[~df["short_name"].map(is_excluded_short_name)]
        return sorted(set(df["stock_code"].tolist()))

    def _is_fresh_file(self, path: str) -> bool:
        if not os.path.exists(path):
            return False
        if self.refresh_days <= 0:
            return False
        file_time = datetime.datetime.fromtimestamp(os.path.getmtime(path))
        age_days = (datetime.datetime.now() - file_time).days
        return age_days < self.refresh_days

    @staticmethod
    def _safe_count_rows(path: str) -> int:
        if not os.path.exists(path):
            return 0
        try:
            df = pd.read_csv(path)
            return len(df)
        except Exception:
            return 0

    @staticmethod
    def _normalize_dividend_df(code: str, df: Optional[pd.DataFrame]) -> pd.DataFrame:
        expected_cols = ["stock_code", "report_date", "dividend_plan", "ex_dividend_date"]
        if df is None or df.empty:
            return pd.DataFrame(columns=expected_cols)
        local_df = df.copy()
        for col in expected_cols:
            if col not in local_df.columns:
                local_df[col] = ""
        local_df["stock_code"] = local_df["stock_code"].fillna("").astype(str)
        local_df.loc[local_df["stock_code"] == "", "stock_code"] = code
        return local_df[expected_cols]

    def _fetch_single_code(self, code: str) -> Dict[str, object]:
        path = os.path.join(self.dividend_dir, f"{code}.csv")
        existing_rows = self._safe_count_rows(path)
        if self._is_fresh_file(path):
            return {"code": code, "status": "skip_fresh", "rows": existing_rows}

        last_exc = None
        for attempt in range(1, self.retry + 1):
            try:
                fetched = adata.stock.market.get_dividend(stock_code=code)
                normalized = self._normalize_dividend_df(code, fetched)

                # 避免临时网络异常导致“非空文件被空结果覆盖”
                if normalized.empty and existing_rows > 0:
                    return {"code": code, "status": "keep_existing", "rows": existing_rows}

                normalized.to_csv(path, index=False, encoding="utf-8-sig")
                if existing_rows == 0 and not normalized.empty:
                    status = "created_non_empty"
                elif existing_rows > 0 and not normalized.empty:
                    status = "updated_non_empty"
                elif existing_rows == 0 and normalized.empty:
                    status = "created_empty"
                else:
                    status = "updated_empty"
                return {"code": code, "status": status, "rows": int(len(normalized))}
            except Exception as exc:
                last_exc = exc
                if attempt < self.retry:
                    time.sleep(0.3 * attempt)

        return {"code": code, "status": "failed", "rows": existing_rows, "error": str(last_exc)}

    def run_once(self):
        t0 = time.perf_counter()
        print("[1/5] 尝试从 Google Drive 恢复历史分红缓存...")
        sync_cache_from_drive(self.project_root, self.archive_name, [DIVIDEND_CACHE_REL, DIVIDEND_MANIFEST_REL])

        print("[2/5] 加载股票列表...")
        codes = self._load_codes()
        if self.limit > 0:
            codes = codes[: self.limit]
        print(f"待处理股票数: {len(codes)}")

        print("[3/5] 批量抓取/更新分红缓存...")
        summary = {
            "skip_fresh": 0,
            "keep_existing": 0,
            "created_non_empty": 0,
            "updated_non_empty": 0,
            "created_empty": 0,
            "updated_empty": 0,
            "failed": 0,
        }
        failures: List[Dict[str, str]] = []
        processed = 0
        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            futures = {executor.submit(self._fetch_single_code, code): code for code in codes}
            for future in as_completed(futures):
                result = future.result()
                status = result.get("status", "failed")
                summary[status] = summary.get(status, 0) + 1
                if status == "failed":
                    failures.append({"code": result["code"], "error": str(result.get("error", ""))})
                processed += 1
                if processed % 200 == 0 or processed == len(codes):
                    print(
                        f"进度 {processed}/{len(codes)} | created={summary['created_non_empty']} "
                        f"updated={summary['updated_non_empty']} failed={summary['failed']}"
                    )

        print("[4/5] 写入同步清单...")
        payload = {
            "synced_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "archive_name": self.archive_name,
            "total_codes": len(codes),
            "max_workers": self.max_workers,
            "refresh_days": self.refresh_days,
            "retry": self.retry,
            "summary": summary,
            "failed_codes": failures[:300],
        }
        write_json(self.manifest_path, payload)

        print("[5/5] 上传分红缓存到 Google Drive...")
        sync_cache_to_drive(self.project_root, self.archive_name, [DIVIDEND_CACHE_REL, DIVIDEND_MANIFEST_REL])

        if self.sync_shared_cache:
            print("附加上传共享缓存包（data/cache 全量）...")
            sync_cache_to_drive(self.project_root, SHARED_MARKET_CACHE_ARCHIVE, ["data/cache"])

        elapsed = time.perf_counter() - t0
        print("执行完成")
        print(f"- 总耗时: {elapsed:.1f}s")
        print(f"- 结果汇总: {summary}")
        if failures:
            print(f"- 失败样本数: {len(failures)}（详见 {self.manifest_path}）")
        else:
            print("- 失败样本数: 0")


def main():
    parser = argparse.ArgumentParser(description="一次性同步分红缓存到 Google Drive")
    parser.add_argument("--max-workers", type=int, default=10, help="并发抓取线程数")
    parser.add_argument("--refresh-days", type=int, default=180, help="本地文件N天内视为新鲜，跳过抓取")
    parser.add_argument("--limit", type=int, default=0, help="限制处理股票数量，0表示全量")
    parser.add_argument("--retry", type=int, default=2, help="单只股票失败重试次数")
    parser.add_argument("--archive-name", type=str, default=DIVIDEND_CACHE_ARCHIVE, help="云端压缩包名称")
    parser.add_argument(
        "--sync-shared-cache",
        action="store_true",
        help="同时上传 shared cache 包（three_dim_cache_bundle.tar.gz）",
    )
    args = parser.parse_args()

    job = DividendCacheSyncJob(
        max_workers=args.max_workers,
        refresh_days=args.refresh_days,
        limit=args.limit,
        retry=args.retry,
        archive_name=args.archive_name,
        sync_shared_cache=args.sync_shared_cache,
    )
    job.run_once()


if __name__ == "__main__":
    main()
