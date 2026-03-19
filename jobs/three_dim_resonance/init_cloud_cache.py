# -*- coding: utf-8 -*-
import datetime
import json
import os
import pickle
import sys
import time
from typing import Optional

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
from jobs.common.cloud_cache_sync import sync_cache_from_drive, sync_cache_to_drive, write_json


def _read_int_env(name: str, default: Optional[int] = None) -> Optional[int]:
    value = os.getenv(name, "").strip()
    if value == "":
        return default
    return int(value)


class FiveYearCloudCacheBuilder:
    def __init__(self):
        self.project_root = PROJECT_ROOT
        self.cache_dir = os.path.join(PROJECT_ROOT, "data", "cache")
        self.finance_dir = os.path.join(self.cache_dir, "finance")
        self.full_cache_file = os.path.join(self.cache_dir, "full_data_v3_5year.pkl")
        self.benchmark_file = os.path.join(self.cache_dir, "benchmark_000300.csv")
        self.manifest_file = os.path.join(self.cache_dir, "three_dim_cache_manifest.json")
        self.metadata = load_stock_metadata(PROJECT_ROOT)

    @staticmethod
    def _five_year_start() -> str:
        return (datetime.datetime.now() - datetime.timedelta(days=365 * 5 + 30)).strftime("%Y-%m-%d")

    def _stock_target_date(self, today_str: str) -> str:
        """
        股票日K补齐目标日：
        - 不抓当天，统一只补到“上一交易日”。
        - 优先使用基准文件里 <= 上一工作日 的最新交易日，避免节假日误判。
        """
        prev_business_day = (pd.to_datetime(today_str) - pd.tseries.offsets.BDay(1)).strftime("%Y-%m-%d")
        if os.path.exists(self.benchmark_file):
            try:
                bench = pd.read_csv(self.benchmark_file)
                if not bench.empty and "trade_date" in bench.columns:
                    bench["trade_date"] = bench["trade_date"].astype(str)
                    valid_dates = sorted(d for d in bench["trade_date"].tolist() if d <= prev_business_day)
                    if valid_dates:
                        return valid_dates[-1]
            except Exception:
                pass
        return prev_business_day

    def _load_cache(self) -> dict:
        if os.path.exists(self.full_cache_file):
            with open(self.full_cache_file, "rb") as f:
                return pickle.load(f)
        return {"stock": {}, "update_meta": {}}

    def _save_cache(self, cache: dict):
        os.makedirs(self.cache_dir, exist_ok=True)
        with open(self.full_cache_file, "wb") as f:
            pickle.dump(cache, f)

    def _valid_codes(self) -> list[str]:
        codes = []
        for code, meta in self.metadata.items():
            if not is_supported_a_share_code(code):
                continue
            if is_excluded_short_name(meta.get("short_name", "")):
                continue
            codes.append(code)
        return sorted(codes)

    def _fetch_stock(self, code: str, existing_df: Optional[pd.DataFrame], target_date: str):
        try:
            if existing_df is not None and not existing_df.empty:
                last_date = pd.to_datetime(existing_df["trade_time"].max()).strftime("%Y-%m-%d")
                if last_date >= target_date:
                    return code, existing_df, False
                new_df = adata.stock.market.get_market(
                    stock_code=code,
                    start_date=last_date,
                    end_date=target_date,
                )
                if new_df is not None and not new_df.empty:
                    merged = pd.concat([existing_df, new_df]).drop_duplicates("trade_time").sort_values("trade_time")
                    return code, merged, True
                return code, existing_df, True
            new_df = adata.stock.market.get_market(
                stock_code=code,
                start_date=self._five_year_start(),
                end_date=target_date,
            )
            if new_df is not None and not new_df.empty:
                return code, new_df, True
        except Exception as exc:
            print(f"[stock fetch failed] {code}: {exc}")
        return code, existing_df, False

    def _refresh_finance(self, code: str, refresh_days: int, today: datetime.datetime) -> bool:
        os.makedirs(self.finance_dir, exist_ok=True)
        path = os.path.join(self.finance_dir, f"{code}.csv")
        if os.path.exists(path):
            file_time = datetime.datetime.fromtimestamp(os.path.getmtime(path))
            if (today - file_time).days < refresh_days:
                return False
        try:
            df = adata.stock.finance.get_core_index(stock_code=code)
            if df is not None and not df.empty:
                df.to_csv(path, index=False, encoding="utf-8-sig")
                return True
        except Exception as exc:
            print(f"[finance fetch failed] {code}: {exc}")
        return False

    def _update_benchmark(self, target_date: str):
        os.makedirs(self.cache_dir, exist_ok=True)
        if os.path.exists(self.benchmark_file):
            bench = pd.read_csv(self.benchmark_file)
        else:
            bench = pd.DataFrame()
        updated = False
        if not bench.empty:
            last_date = str(bench["trade_date"].max())
            if last_date < target_date:
                new_bench = adata.stock.market.get_market_index(
                    index_code="000300",
                    start_date=last_date,
                    end_date=target_date,
                )
                if new_bench is not None and not new_bench.empty:
                    bench = pd.concat([bench, new_bench]).drop_duplicates("trade_date").sort_values("trade_date")
                    bench.to_csv(self.benchmark_file, index=False)
                    updated = True
        else:
            bench = adata.stock.market.get_market_index(
                index_code="000300",
                start_date=self._five_year_start(),
                end_date=target_date,
            )
            if bench is not None and not bench.empty:
                bench.to_csv(self.benchmark_file, index=False)
                updated = True
        return updated

    def build(
        self,
        checkpoint_every: int,
        finance_refresh_days: int,
        auto_commit_minutes: int = 30,
    ) -> dict:
        stage_t0 = time.perf_counter()
        print("[stage] sync_cache_from_drive: start", flush=True)
        sync_cache_from_drive(self.project_root, "three_dim_cache_bundle.tar.gz", ["data/cache"])
        print(f"[stage] sync_cache_from_drive: done ({time.perf_counter() - stage_t0:.1f}s)", flush=True)

        stage_t0 = time.perf_counter()
        print("[stage] load_cache: start", flush=True)
        cache = self._load_cache()
        print(f"[stage] load_cache: done ({time.perf_counter() - stage_t0:.1f}s)", flush=True)
        raw_stock = cache.setdefault("stock", {})
        update_meta = cache.setdefault("update_meta", {})
        finance_last_checked = update_meta.setdefault("finance_last_checked", {})
        auto_commit_count = 0
        cache_dirty_since_last_sync = False
        auto_commit_interval_seconds = max(int(auto_commit_minutes), 0) * 60
        last_auto_commit_ts = time.perf_counter()

        def maybe_auto_commit(reason: str):
            nonlocal auto_commit_count, cache_dirty_since_last_sync, last_auto_commit_ts
            if auto_commit_interval_seconds <= 0:
                return
            if not cache_dirty_since_last_sync:
                return
            now_ts = time.perf_counter()
            if (now_ts - last_auto_commit_ts) < auto_commit_interval_seconds:
                return
            print(f"[stage] auto_commit({reason}): start", flush=True)
            t0 = time.perf_counter()
            self._save_cache(cache)
            sync_cache_to_drive(self.project_root, "three_dim_cache_bundle.tar.gz", ["data/cache"])
            elapsed = time.perf_counter() - t0
            auto_commit_count += 1
            cache_dirty_since_last_sync = False
            last_auto_commit_ts = time.perf_counter()
            print(f"[stage] auto_commit({reason}): done ({elapsed:.1f}s)", flush=True)

        today = datetime.datetime.now()
        today_str = today.strftime("%Y-%m-%d")
        target_date = self._stock_target_date(today_str)
        selected_codes = self._valid_codes()

        # 观察云端缓存当前“落后几天”：以本地 benchmark 最新日期为准。
        cloud_bench_last_date = ""
        if os.path.exists(self.benchmark_file):
            try:
                bench = pd.read_csv(self.benchmark_file)
                if not bench.empty and "trade_date" in bench.columns:
                    cloud_bench_last_date = str(bench["trade_date"].max())
            except Exception:
                cloud_bench_last_date = ""

        if cloud_bench_last_date:
            try:
                gap_days = max((pd.to_datetime(target_date) - pd.to_datetime(cloud_bench_last_date)).days, 0)
            except Exception:
                gap_days = -1
            print(f"cloud_benchmark_last_date={cloud_bench_last_date}, target_date={target_date}, gap_days={gap_days}")
        else:
            print(f"cloud_benchmark_last_date=unknown, target_date={target_date}")

        print(f"selected_codes={len(selected_codes)}")
        updated_stock = 0
        checked_stock = 0
        progress = 0

        # 仅对“缓存末日早于今天”的股票做增量抓取，避免每次全量请求。
        pending_codes = []
        for code in selected_codes:
            existing_df = raw_stock.get(code)
            if existing_df is None or existing_df.empty:
                pending_codes.append(code)
                continue
            try:
                last_date = pd.to_datetime(existing_df["trade_time"].max()).strftime("%Y-%m-%d")
            except Exception:
                pending_codes.append(code)
                continue
            if last_date < target_date:
                pending_codes.append(code)

        print(f"pending_stock_updates={len(pending_codes)}")
        if pending_codes:
            print("[stage] fetch_stock_incremental: start", flush=True)
            stage_t0 = time.perf_counter()
            for completed_fetch, code in enumerate(pending_codes, start=1):
                code, df, checked = self._fetch_stock(code, raw_stock.get(code), target_date)
                if checked:
                    checked_stock += 1
                    progress += 1
                if df is not raw_stock.get(code):
                    raw_stock[code] = df
                    updated_stock += 1
                    cache_dirty_since_last_sync = True
                if completed_fetch % 100 == 0 or completed_fetch == len(pending_codes):
                    print(
                        f"[fetch-progress] completed={completed_fetch}/{len(pending_codes)}, "
                        f"updated={updated_stock}"
                    )
                if checkpoint_every > 0 and progress > 0 and progress % checkpoint_every == 0:
                    self._save_cache(cache)
                maybe_auto_commit("fetch_stock_incremental")
            print(f"[stage] fetch_stock_incremental: done ({time.perf_counter() - stage_t0:.1f}s)", flush=True)

        refreshed_finance = 0
        print("[stage] refresh_finance: start", flush=True)
        stage_t0 = time.perf_counter()
        finance_targets = [
            code for code in selected_codes
            if finance_last_checked.get(code) != today_str
        ]
        for idx, code in enumerate(finance_targets, start=1):
            changed = self._refresh_finance(code, finance_refresh_days, today)
            finance_last_checked[code] = today_str
            refreshed_finance += int(changed)
            cache_dirty_since_last_sync = True
            if idx % 100 == 0 or idx == len(finance_targets):
                print(
                    f"[finance-progress] completed={idx}/{len(finance_targets)}, "
                    f"refreshed={refreshed_finance}"
                )
            if checkpoint_every > 0 and idx % checkpoint_every == 0:
                self._save_cache(cache)
            maybe_auto_commit("refresh_finance")
        print(f"[stage] refresh_finance: done ({time.perf_counter() - stage_t0:.1f}s)", flush=True)

        print("[stage] update_benchmark: start", flush=True)
        stage_t0 = time.perf_counter()
        benchmark_updated = self._update_benchmark(target_date)
        print(f"[stage] update_benchmark: done ({time.perf_counter() - stage_t0:.1f}s)", flush=True)
        cache_changed = bool(updated_stock > 0 or refreshed_finance > 0 or benchmark_updated)
        if cache_changed:
            print("[stage] save_cache: start", flush=True)
            stage_t0 = time.perf_counter()
            self._save_cache(cache)
            print(f"[stage] save_cache: done ({time.perf_counter() - stage_t0:.1f}s)", flush=True)

        manifest = {
            "updated_at": today.strftime("%Y-%m-%d %H:%M:%S"),
            "stock_count": len(raw_stock),
            "selected_code_count": len(selected_codes),
            "pending_stock_update_count": len(pending_codes),
            "updated_stock_count": updated_stock,
            "checked_stock_count": checked_stock,
            "refreshed_finance_count": refreshed_finance,
            "benchmark_updated": benchmark_updated,
            "cache_changed": cache_changed,
            "cache_file": self.full_cache_file,
            "benchmark_file": self.benchmark_file,
            "finance_dir": self.finance_dir,
            "auto_commit_minutes": auto_commit_minutes,
            "auto_commit_count": auto_commit_count,
        }
        write_json(self.manifest_file, manifest)
        if cache_changed:
            # 仅在本次有增量时再整包回传覆盖云端。
            print("[stage] sync_cache_to_drive: start", flush=True)
            stage_t0 = time.perf_counter()
            sync_cache_to_drive(self.project_root, "three_dim_cache_bundle.tar.gz", ["data/cache"])
            print(f"[stage] sync_cache_to_drive: done ({time.perf_counter() - stage_t0:.1f}s)", flush=True)
        else:
            print("本次无缓存变化，跳过云端整包回传。")
        upload_manifest = bool(os.getenv("GOOGLE_DRIVE_FOLDER_ID", "").strip())
        if upload_manifest:
            from jobs.common.cloud_cache_sync import upload_file_to_drive

            print("[stage] upload_manifest: start", flush=True)
            stage_t0 = time.perf_counter()
            upload_file_to_drive(self.manifest_file, "three_dim_cache_manifest.json", mime_type="application/json")
            print(f"[stage] upload_manifest: done ({time.perf_counter() - stage_t0:.1f}s)", flush=True)
        return manifest


if __name__ == "__main__":
    builder = FiveYearCloudCacheBuilder()
    manifest = builder.build(
        checkpoint_every=_read_int_env("CACHE_CHECKPOINT_EVERY", 100),
        finance_refresh_days=_read_int_env("FINANCE_REFRESH_DAYS", 30) or 30,
        auto_commit_minutes=_read_int_env("AUTO_COMMIT_MINUTES", 30) or 30,
    )
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
