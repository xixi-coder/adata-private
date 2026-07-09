# -*- coding: utf-8 -*-
from __future__ import annotations

import os
import pickle
from typing import Any

import pandas as pd

from jobs.common.a_share_metadata import is_supported_a_share_code, normalize_code


REQUIRED_DAILY_COLUMNS = ["stock_code", "trade_date", "open", "high", "low", "close", "volume", "amount"]
NUMERIC_DAILY_COLUMNS = ["open", "high", "low", "close", "volume", "amount", "turnover_ratio", "pre_close"]


def standardize_daily_df(code: str, df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    out = df.copy()
    if "trade_date" in out.columns:
        out["trade_date"] = pd.to_datetime(out["trade_date"], errors="coerce")
    elif "trade_time" in out.columns:
        out["trade_date"] = pd.to_datetime(out["trade_time"], errors="coerce")
    else:
        return pd.DataFrame()

    out["stock_code"] = out.get("stock_code", code)
    out["stock_code"] = out["stock_code"].map(normalize_code)
    if "pre_close" not in out.columns:
        out["pre_close"] = out.groupby("stock_code")["close"].shift(1) if "close" in out.columns else pd.NA

    if any(col not in out.columns for col in REQUIRED_DAILY_COLUMNS):
        return pd.DataFrame()
    for col in NUMERIC_DAILY_COLUMNS:
        if col in out.columns:
            out[col] = pd.to_numeric(out[col], errors="coerce")
    keep_cols = [col for col in REQUIRED_DAILY_COLUMNS + ["turnover_ratio", "pre_close"] if col in out.columns]
    return (
        out[keep_cols]
        .dropna(subset=["stock_code", "trade_date", "open", "high", "low", "close", "amount"])
        .sort_values("trade_date")
        .drop_duplicates(["stock_code", "trade_date"])
    )


def stock_cache_from_payload(cache: Any) -> dict[str, pd.DataFrame]:
    stock_data = cache.get("stock") if isinstance(cache, dict) and "stock" in cache else cache
    if not isinstance(stock_data, dict):
        raise ValueError("市场缓存结构异常，预期为股票代码到 DataFrame 的映射。")
    return stock_data


def load_stock_cache(path: str) -> dict[str, pd.DataFrame]:
    if not os.path.exists(path):
        raise FileNotFoundError(f"找不到市场缓存文件: {path}")
    with open(path, "rb") as f:
        return stock_cache_from_payload(pickle.load(f))


def build_a_share_panel(
    stock_data: dict[str, pd.DataFrame],
    min_history_days: int = 0,
    universe_size: int | None = None,
) -> pd.DataFrame:
    frames = []
    for raw_code, df in stock_data.items():
        code = normalize_code(raw_code)
        if not is_supported_a_share_code(code):
            continue
        standardized = standardize_daily_df(code, df)
        if standardized.empty or len(standardized) < min_history_days:
            continue
        frames.append(standardized)
    if not frames:
        raise RuntimeError("未从缓存中解析出可用日线数据。")

    panel = pd.concat(frames, ignore_index=True).sort_values(["stock_code", "trade_date"]).reset_index(drop=True)
    if universe_size:
        liquid = panel.groupby("stock_code", sort=False).tail(20).groupby("stock_code")["amount"].mean()
        keep = set(liquid.sort_values(ascending=False).head(universe_size).index)
        panel = panel[panel["stock_code"].isin(keep)].copy()
    return panel.reset_index(drop=True)


def load_a_share_panel(path: str, min_history_days: int = 0, universe_size: int | None = None) -> pd.DataFrame:
    return build_a_share_panel(load_stock_cache(path), min_history_days=min_history_days, universe_size=universe_size)
