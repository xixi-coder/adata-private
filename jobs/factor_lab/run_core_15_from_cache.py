# -*- coding: utf-8 -*-
from __future__ import annotations
"""
从本地缓存一键运行 A 股核心 15 因子。

输入：
- 日频行情缓存（full_data_v3_5year.pkl）
- 财务缓存目录（finance/*.csv）
- 指数日线（benchmark_000300.csv）

输出：
- ic_summary.csv
- group_summary.csv
- run_meta.json
"""

import argparse
import json
import os
import pickle
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable

import numpy as np
import pandas as pd

from strategies.factor_lab import align_financials_to_daily, run_core_15_pipeline


def _normalize_code(value) -> str:
    if pd.isna(value):
        return ""
    text = str(value).strip()
    if text.endswith(".0"):
        text = text[:-2]
    return text.zfill(6) if text.isdigit() else text


def _is_supported_a_share(code: str) -> bool:
    return isinstance(code, str) and len(code) == 6 and code.isdigit() and not code.startswith(("200", "8", "9"))


def _to_datetime(df: pd.DataFrame, col: str) -> None:
    if col in df.columns and not pd.api.types.is_datetime64_any_dtype(df[col]):
        df[col] = pd.to_datetime(df[col], errors="coerce")


@dataclass
class LoadStats:
    """用于记录本次加载规模，便于回溯运行上下文。"""
    stock_count: int
    trade_rows: int
    finance_rows: int


def _load_stock_panel(
    cache_file: str,
    start_date: str | None = None,
    end_date: str | None = None,
    max_stocks: int | None = None,
) -> pd.DataFrame:
    """读取 full_data 缓存并整理成统一长表（日频行情面板）。"""
    with open(cache_file, "rb") as f:
        cache = pickle.load(f)

    stock_map = cache.get("stock", cache) if isinstance(cache, dict) else {}
    if not isinstance(stock_map, dict):
        raise ValueError(f"Unsupported cache format: {cache_file}")

    selected_items = list(stock_map.items())
    if max_stocks is not None and max_stocks > 0:
        selected_items = selected_items[:max_stocks]

    rows: list[pd.DataFrame] = []
    for code_raw, df in selected_items:
        code = _normalize_code(code_raw)
        if not _is_supported_a_share(code):
            continue
        if not isinstance(df, pd.DataFrame) or df.empty:
            continue

        k = df.copy()
        if "trade_date" not in k.columns:
            if "trade_time" in k.columns:
                k["trade_date"] = pd.to_datetime(k["trade_time"], errors="coerce").dt.strftime("%Y-%m-%d")
            else:
                continue
        k["stock_code"] = code
        k["trade_date"] = pd.to_datetime(k["trade_date"], errors="coerce")
        if start_date:
            k = k[k["trade_date"] >= pd.to_datetime(start_date)]
        if end_date:
            k = k[k["trade_date"] <= pd.to_datetime(end_date)]
        if k.empty:
            continue

        # 只保留因子引擎当前需要的行情字段，避免面板过重。
        keep = [
            "stock_code",
            "trade_date",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "amount",
            "turnover_ratio",
            "pre_close",
        ]
        k = k[[c for c in keep if c in k.columns]].copy()
        for col in ("open", "high", "low", "close", "volume", "amount", "turnover_ratio", "pre_close"):
            if col in k.columns:
                k[col] = pd.to_numeric(k[col], errors="coerce")

        rows.append(k)

    if not rows:
        raise ValueError("No valid stock rows loaded from cache.")

    panel = pd.concat(rows, ignore_index=True)
    panel = panel.sort_values(["stock_code", "trade_date"]).reset_index(drop=True)

    if "turnover_ratio" in panel.columns:
        # 由换手率反推流通股本，再构造市值，给流动性/规模过滤与估值因子使用。
        turnover_decimal = panel["turnover_ratio"] / 100.0
        panel["float_shares"] = np.where(turnover_decimal > 0, panel["volume"] / turnover_decimal, np.nan)
        panel["market_cap"] = panel["close"] * panel["float_shares"]

    return panel


def _load_finance_panel(finance_dir: str, stock_codes: Iterable[str]) -> pd.DataFrame:
    """按股票加载财务快照，并映射为因子模块可识别字段。"""
    frames: list[pd.DataFrame] = []
    use_cols = [
        "stock_code",
        "notice_date",
        "basic_eps",
        "net_asset_ps",
        "total_rev",
        "gross_profit",
        "net_profit_attr_sh",
        "roe_wtd",
        "net_margin",
        "asset_liab_ratio",
        "oper_cf_ps",
    ]

    for code in stock_codes:
        path = os.path.join(finance_dir, f"{code}.csv")
        if not os.path.exists(path):
            continue
        try:
            fdf = pd.read_csv(path, usecols=lambda c: c in use_cols)
        except Exception:
            continue
        if fdf.empty:
            continue
        fdf["stock_code"] = code
        fdf["notice_date"] = pd.to_datetime(fdf.get("notice_date"), errors="coerce")
        frames.append(fdf)

    if not frames:
        return pd.DataFrame(columns=["stock_code", "notice_date"])

    fin = pd.concat(frames, ignore_index=True)
    for col in (
        "basic_eps",
        "net_asset_ps",
        "total_rev",
        "gross_profit",
        "net_profit_attr_sh",
        "roe_wtd",
        "net_margin",
        "asset_liab_ratio",
        "oper_cf_ps",
    ):
        if col in fin.columns:
            fin[col] = pd.to_numeric(fin[col], errors="coerce")

    # 统一字段命名，减少上游来源差异对因子计算的影响。
    fin["revenue"] = fin.get("total_rev")
    fin["profit"] = fin.get("net_profit_attr_sh")
    fin["net_profit"] = fin.get("net_profit_attr_sh")
    if "roe_wtd" in fin.columns:
        fin["roe"] = fin["roe_wtd"] / 100.0
    if "net_margin" in fin.columns:
        fin["net_margin"] = fin["net_margin"] / 100.0
    if "asset_liab_ratio" in fin.columns:
        fin["asset_liability_ratio"] = fin["asset_liab_ratio"] / 100.0

    return fin


def _load_index_df(index_file: str) -> pd.DataFrame:
    idx = pd.read_csv(index_file)
    if "trade_date" not in idx.columns or "close" not in idx.columns:
        raise ValueError(f"Index file must contain trade_date and close: {index_file}")
    idx = idx[["trade_date", "close"]].copy()
    idx["trade_date"] = pd.to_datetime(idx["trade_date"], errors="coerce")
    idx["close"] = pd.to_numeric(idx["close"], errors="coerce")
    idx = idx.dropna(subset=["trade_date", "close"]).sort_values("trade_date").drop_duplicates("trade_date")
    return idx


def _prepare_panel_for_factors(daily_df: pd.DataFrame, finance_df: pd.DataFrame) -> tuple[pd.DataFrame, LoadStats]:
    """把行情与财务对齐，并补出估值/现金流等衍生字段。"""
    if finance_df is not None and not finance_df.empty:
        # 关键：按公告日向后对齐，避免未来函数。
        panel = align_financials_to_daily(daily_df=daily_df, finance_df=finance_df)
    else:
        panel = daily_df.copy()

    # Build valuation and cashflow fields from aligned finance snapshots.
    if "basic_eps" in panel.columns:
        panel["pe"] = np.where(panel["basic_eps"] > 0, panel["close"] / panel["basic_eps"], np.nan)
    if "net_asset_ps" in panel.columns:
        panel["pb"] = np.where(panel["net_asset_ps"] > 0, panel["close"] / panel["net_asset_ps"], np.nan)
    if "market_cap" in panel.columns and "total_rev" in panel.columns:
        panel["ps"] = np.where(panel["total_rev"] > 0, panel["market_cap"] / panel["total_rev"], np.nan)
    if "oper_cf_ps" in panel.columns and "float_shares" in panel.columns:
        panel["operating_cashflow"] = panel["oper_cf_ps"] * panel["float_shares"]

    stats = LoadStats(
        stock_count=int(panel["stock_code"].nunique()),
        trade_rows=int(len(daily_df)),
        finance_rows=int(0 if finance_df is None else len(finance_df)),
    )
    return panel, stats


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run core 15 A-share daily factors from local cache data.")
    parser.add_argument("--cache-file", default="data/cache/full_data_v3_5year.pkl")
    parser.add_argument("--finance-dir", default="data/cache/finance")
    parser.add_argument("--index-file", default="data/cache/benchmark_000300.csv")
    parser.add_argument("--out-dir", default="tests/factor_lab_outputs")
    parser.add_argument("--start-date", default="")
    parser.add_argument("--end-date", default="")
    parser.add_argument("--max-stocks", type=int, default=0, help="Use first N stocks for quick run; 0 = all.")
    parser.add_argument("--horizon", type=int, default=5)
    parser.add_argument("--min-market-cap", type=float, default=3e9)
    parser.add_argument("--min-amount", type=float, default=2e8)
    parser.add_argument("--neutralize", action="store_true", help="Enable industry neutralization (needs industry column).")
    parser.add_argument("--save-panel", action="store_true", help="Save factor panel as pickle.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    os.makedirs(args.out_dir, exist_ok=True)

    daily_df = _load_stock_panel(
        cache_file=args.cache_file,
        start_date=args.start_date or None,
        end_date=args.end_date or None,
        max_stocks=args.max_stocks if args.max_stocks > 0 else None,
    )
    codes = daily_df["stock_code"].dropna().astype(str).unique().tolist()
    finance_df = _load_finance_panel(args.finance_dir, codes)
    index_df = _load_index_df(args.index_file)
    panel, stats = _prepare_panel_for_factors(daily_df, finance_df)

    # 一键执行：因子计算 -> 交易过滤 -> 预处理 -> IC/分组测试。
    result = run_core_15_pipeline(
        panel_df=panel,
        index_df=index_df,
        horizon=args.horizon,
        min_market_cap=args.min_market_cap,
        min_amount=args.min_amount,
        neutralize=args.neutralize,
    )

    ic_path = os.path.join(args.out_dir, "ic_summary.csv")
    group_path = os.path.join(args.out_dir, "group_summary.csv")
    meta_path = os.path.join(args.out_dir, "run_meta.json")
    result["ic_summary"].to_csv(ic_path, index=False, encoding="utf-8-sig")
    result["group_summary"].to_csv(group_path, index=False, encoding="utf-8-sig")

    if args.save_panel:
        panel_path = os.path.join(args.out_dir, "factor_panel.pkl")
        result["panel"].to_pickle(panel_path)

    # run_meta 记录参数、样本规模和 Top 因子，方便批次对比与复现。
    top_ic = result["ic_summary"].head(10).to_dict("records")
    top_ls = result["group_summary"].head(10).to_dict("records")
    meta = {
        "run_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "input": {
            "cache_file": args.cache_file,
            "finance_dir": args.finance_dir,
            "index_file": args.index_file,
            "start_date": args.start_date,
            "end_date": args.end_date,
            "max_stocks": args.max_stocks,
            "horizon": args.horizon,
            "min_market_cap": args.min_market_cap,
            "min_amount": args.min_amount,
            "neutralize": bool(args.neutralize),
        },
        "loaded": {
            "stock_count": stats.stock_count,
            "trade_rows": stats.trade_rows,
            "finance_rows": stats.finance_rows,
            "panel_rows": int(len(panel)),
            "panel_cols": int(panel.shape[1]),
        },
        "output": {
            "factor_count": int(len(result["factors"])),
            "ic_summary_file": ic_path,
            "group_summary_file": group_path,
            "top_ic": top_ic,
            "top_long_short": top_ls,
        },
    }
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)

    print(f"[factor_lab] Done. ic_summary -> {ic_path}")
    print(f"[factor_lab] Done. group_summary -> {group_path}")
    print(f"[factor_lab] Meta -> {meta_path}")


if __name__ == "__main__":
    main()
