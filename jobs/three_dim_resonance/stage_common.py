# -*- coding: utf-8 -*-
import os
import time

from jobs.common.cloud_cache_sync import download_json_from_drive, sync_cache_from_drive
from jobs.three_dim_resonance.live.strategy import ThreeDimResonanceLiveStrategy


def time_call(name, fn, *args, **kwargs):
    t0 = time.perf_counter()
    result = fn(*args, **kwargs)
    dt = time.perf_counter() - t0
    print(f"[time] {name}: {dt:.3f}s")
    return result, dt


def build_strategy() -> ThreeDimResonanceLiveStrategy:
    return ThreeDimResonanceLiveStrategy(
        initial_capital=1_000_000,
        max_positions=8,
        universe_size=1800,
        max_position_weight=0.20,
    )


def maybe_sync_cloud(strategy: ThreeDimResonanceLiveStrategy):
    # 默认跳过云端同步，避免调试时受网络影响。
    # 设 PROFILE_SKIP_CLOUD=0 可启用。
    skip_cloud = os.getenv("PROFILE_SKIP_CLOUD", "1") == "1"
    if skip_cloud:
        print("[info] PROFILE_SKIP_CLOUD=1，跳过云端同步。")
        return
    time_call(
        "sync_cache_from_drive",
        sync_cache_from_drive,
        strategy.project_root,
        "three_dim_cache_bundle.tar.gz",
        ["data/cache"],
    )
    time_call(
        "download_json_from_drive",
        download_json_from_drive,
        strategy.project_root,
        "three_dim_live_state.json",
        strategy.state_file,
    )
