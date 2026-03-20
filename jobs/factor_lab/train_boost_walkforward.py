# -*- coding: utf-8 -*-
from __future__ import annotations
"""
Walk-forward（滚动训练/滚动测试）版本：
- 每个窗口仅使用历史数据训练
- 在后续测试窗口打分并做分层评估
- 汇总窗口级与整体级指标
"""

import argparse
import json
import os
from datetime import datetime
from typing import Any

import numpy as np
import pandas as pd

from jobs.factor_lab.run_core_15_from_cache import (
    _load_finance_panel,
    _load_index_df,
    _load_stock_panel,
    _prepare_panel_for_factors,
)
from jobs.factor_lab.train_boost_from_cache import (
    _build_model,
    _feature_importance,
    _group_backtest,
    _regression_metrics,
    _select_top_factors,
)
from strategies.factor_lab import evaluate_factor_ic, run_core_15_pipeline


def _ic_summary_from_subset(
    df: pd.DataFrame,
    factor_cols: list[str],
    horizon: int,
    min_obs: int,
) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for fac in factor_cols:
        res = evaluate_factor_ic(
            panel_df=df,
            factor_col=fac,
            date_col="trade_date",
            stock_col="stock_code",
            horizon=horizon,
            min_obs=min_obs,
        )
        rows.append({k: v for k, v in res.items() if k != "ic_series"})
    out = pd.DataFrame(rows)
    if out.empty:
        return out
    return out.sort_values("ic_mean", ascending=False).reset_index(drop=True)


def _walkforward_dates(
    dates: list[pd.Timestamp],
    train_days: int,
    test_days: int,
    step_days: int,
    max_folds: int = 0,
) -> list[tuple[list[pd.Timestamp], list[pd.Timestamp]]]:
    folds: list[tuple[list[pd.Timestamp], list[pd.Timestamp]]] = []
    if len(dates) < train_days + test_days:
        return folds

    for split_idx in range(train_days, len(dates) - test_days + 1, step_days):
        train_dates = dates[split_idx - train_days : split_idx]
        test_dates = dates[split_idx : split_idx + test_days]
        folds.append((train_dates, test_dates))
        if max_folds > 0 and len(folds) >= max_folds:
            break
    return folds


def _prune_factors_by_coverage(
    ranked_factors: list[str],
    train_df: pd.DataFrame,
    test_df: pd.DataFrame,
    n_factors: int,
    min_coverage: float,
) -> list[str]:
    kept: list[str] = []
    for fac in ranked_factors:
        if fac not in train_df.columns or fac not in test_df.columns:
            continue
        cov_train = float(train_df[fac].notna().mean())
        cov_test = float(test_df[fac].notna().mean())
        if cov_train >= min_coverage and cov_test >= min_coverage:
            kept.append(fac)
        if len(kept) >= n_factors:
            break
    return kept


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Walk-forward training for A-share factor model.")
    parser.add_argument("--cache-file", default="data/cache/full_data_v3_5year.pkl")
    parser.add_argument("--finance-dir", default="data/cache/finance")
    parser.add_argument("--index-file", default="data/cache/benchmark_000300.csv")
    parser.add_argument("--out-dir", default="tests/factor_lab_walkforward_outputs")
    parser.add_argument("--start-date", default="")
    parser.add_argument("--end-date", default="")
    parser.add_argument("--max-stocks", type=int, default=0, help="Use first N stocks for quick run; 0=all.")
    parser.add_argument("--horizon", type=int, default=5)
    parser.add_argument("--n-factors", type=int, default=10)
    parser.add_argument("--min-ic-obs", type=int, default=30)
    parser.add_argument("--min-market-cap", type=float, default=3e9)
    parser.add_argument("--min-amount", type=float, default=2e8)
    parser.add_argument("--neutralize", action="store_true")
    parser.add_argument("--train-days", type=int, default=126)
    parser.add_argument("--test-days", type=int, default=21)
    parser.add_argument("--step-days", type=int, default=21)
    parser.add_argument(
        "--min-folds",
        type=int,
        default=3,
        help="Target minimum number of folds; script will shrink train window when possible.",
    )
    parser.add_argument("--max-folds", type=int, default=0)
    parser.add_argument("--rolling-factor-select", action="store_true", help="Select factors per fold by train IC.")
    parser.add_argument(
        "--min-factor-coverage",
        type=float,
        default=0.2,
        help="Minimum non-null ratio required in both train/test for a selected factor.",
    )
    parser.add_argument("--model", default="auto", choices=["auto", "lightgbm", "xgboost", "linear"])
    parser.add_argument("--random-state", type=int, default=42)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    os.makedirs(args.out_dir, exist_ok=True)

    # 1) 数据 + 因子面板
    daily_df = _load_stock_panel(
        cache_file=args.cache_file,
        start_date=args.start_date or None,
        end_date=args.end_date or None,
        max_stocks=args.max_stocks if args.max_stocks > 0 else None,
    )
    codes = daily_df["stock_code"].dropna().astype(str).unique().tolist()
    finance_df = _load_finance_panel(args.finance_dir, codes)
    index_df = _load_index_df(args.index_file)
    panel, load_stats = _prepare_panel_for_factors(daily_df, finance_df)

    factor_result = run_core_15_pipeline(
        panel_df=panel,
        index_df=index_df,
        horizon=args.horizon,
        min_market_cap=args.min_market_cap,
        min_amount=args.min_amount,
        neutralize=args.neutralize,
    )
    factor_panel = factor_result["panel"].copy()
    factor_panel["trade_date"] = pd.to_datetime(factor_panel["trade_date"], errors="coerce")
    factor_panel = factor_panel.dropna(subset=["trade_date"]).sort_values("trade_date").reset_index(drop=True)
    candidate_factors = [f for f in factor_result["factors"] if f in factor_panel.columns]
    target_col = f"future_ret_{args.horizon}"

    if target_col not in factor_panel.columns:
        raise ValueError(f"Target column not found: {target_col}")
    if not candidate_factors:
        raise ValueError("No candidate factors available for training.")

    dates = sorted(factor_panel["trade_date"].dropna().unique().tolist())
    train_days_used = args.train_days
    folds = _walkforward_dates(
        dates=dates,
        train_days=train_days_used,
        test_days=args.test_days,
        step_days=args.step_days,
        max_folds=args.max_folds,
    )
    # 自动降档：尽量满足最少折数，优先保留测试窗口长度。
    min_train_days = 60
    target_min_folds = max(1, int(args.min_folds))
    if len(folds) < target_min_folds:
        max_train_for_target = len(dates) - args.test_days - args.step_days * (target_min_folds - 1)
        if max_train_for_target >= min_train_days:
            train_days_used = min(train_days_used, int(max_train_for_target))
            folds = _walkforward_dates(
                dates=dates,
                train_days=train_days_used,
                test_days=args.test_days,
                step_days=args.step_days,
                max_folds=args.max_folds,
            )

    if not folds:
        if len(dates) >= min_train_days + args.test_days:
            train_days_used = len(dates) - args.test_days
            folds = _walkforward_dates(
                dates=dates,
                train_days=train_days_used,
                test_days=args.test_days,
                step_days=args.step_days,
                max_folds=args.max_folds,
            )
        if not folds:
            raise ValueError("No walk-forward fold can be formed. Try smaller train/test days or longer date range.")

    fold_metric_rows: list[dict[str, Any]] = []
    fold_group_daily_list: list[pd.DataFrame] = []
    fold_pred_list: list[pd.DataFrame] = []
    fi_rows: list[pd.DataFrame] = []

    fixed_factors: list[str] | None = None
    first_fold_ic: pd.DataFrame | None = None

    for fold_id, (train_dates, test_dates) in enumerate(folds, start=1):
        train_set = set(train_dates)
        test_set = set(test_dates)

        train_df = factor_panel[factor_panel["trade_date"].isin(train_set)].copy()
        test_df = factor_panel[factor_panel["trade_date"].isin(test_set)].copy()
        train_df = train_df.replace([np.inf, -np.inf], np.nan)
        test_df = test_df.replace([np.inf, -np.inf], np.nan)

        if train_df.empty or test_df.empty:
            continue

        # 2) 每个窗口选择因子（或固定首窗口因子）
        if args.rolling_factor_select:
            train_ic = _ic_summary_from_subset(
                df=train_df,
                factor_cols=candidate_factors,
                horizon=args.horizon,
                min_obs=args.min_ic_obs,
            )
            if train_ic.empty:
                continue
            selected_factors = _select_top_factors(train_ic, n_factors=args.n_factors, min_obs=args.min_ic_obs)
        else:
            if fixed_factors is None:
                train_ic = _ic_summary_from_subset(
                    df=train_df,
                    factor_cols=candidate_factors,
                    horizon=args.horizon,
                    min_obs=args.min_ic_obs,
                )
                if train_ic.empty:
                    continue
                first_fold_ic = train_ic.copy()
                fixed_factors = _select_top_factors(train_ic, n_factors=args.n_factors, min_obs=args.min_ic_obs)
            selected_factors = fixed_factors

        selected_factors = _prune_factors_by_coverage(
            ranked_factors=selected_factors,
            train_df=train_df,
            test_df=test_df,
            n_factors=args.n_factors,
            min_coverage=args.min_factor_coverage,
        )
        if len(selected_factors) < 3:
            continue

        train_use = train_df[["stock_code", "trade_date", target_col] + selected_factors].copy()
        test_use = test_df[["stock_code", "trade_date", target_col] + selected_factors].copy()
        train_use = train_use.dropna(subset=[target_col])
        test_use = test_use.dropna(subset=[target_col])
        for fac in selected_factors:
            train_use[fac] = train_use[fac].fillna(0.0)
            test_use[fac] = test_use[fac].fillna(0.0)
        if train_use.empty or test_use.empty:
            continue

        x_train = train_use[selected_factors].astype(float)
        y_train = train_use[target_col].to_numpy(dtype=float)
        x_test = test_use[selected_factors].astype(float)
        y_test = test_use[target_col].to_numpy(dtype=float)

        # 3) 训练与测试
        model_used, model = _build_model(args.model, random_state=args.random_state + fold_id)
        model.fit(x_train, y_train)
        pred_test = model.predict(x_test)
        reg_test = _regression_metrics(y_test, pred_test)

        # 4) 窗口分层回测
        fold_pred = test_use[["stock_code", "trade_date", target_col]].copy()
        fold_pred["pred_score"] = pred_test
        fold_pred["fold_id"] = fold_id
        fold_pred_list.append(fold_pred)

        group_daily, group_summary = _group_backtest(
            pred_df=fold_pred,
            date_col="trade_date",
            score_col="pred_score",
            ret_col=target_col,
            n_groups=10,
            min_obs=30,
        )
        if not group_daily.empty:
            group_daily["fold_id"] = fold_id
            fold_group_daily_list.append(group_daily)

        metric_map = {row["metric"]: row["value"] for _, row in group_summary.iterrows()} if not group_summary.empty else {}
        fold_metric_rows.append(
            {
                "fold_id": fold_id,
                "train_start": pd.Timestamp(train_dates[0]).strftime("%Y-%m-%d"),
                "train_end": pd.Timestamp(train_dates[-1]).strftime("%Y-%m-%d"),
                "test_start": pd.Timestamp(test_dates[0]).strftime("%Y-%m-%d"),
                "test_end": pd.Timestamp(test_dates[-1]).strftime("%Y-%m-%d"),
                "model_used": model_used,
                "n_train_rows": int(len(train_use)),
                "n_test_rows": int(len(test_use)),
                "n_factors": int(len(selected_factors)),
                "test_mae": reg_test.get("mae", np.nan),
                "test_rmse": reg_test.get("rmse", np.nan),
                "test_r2": reg_test.get("r2", np.nan),
                "test_corr": reg_test.get("corr", np.nan),
                "ls_mean": metric_map.get("long_short_mean", np.nan),
                "ls_ir": metric_map.get("long_short_ir", np.nan),
                "ls_win_rate": metric_map.get("long_short_win_rate", np.nan),
                "score_ic_mean": metric_map.get("score_ic_mean", np.nan),
                "score_rank_ic_mean": metric_map.get("score_rank_ic_mean", np.nan),
                "selected_factors": "|".join(selected_factors),
            }
        )

        fi = _feature_importance(model, selected_factors)
        fi["fold_id"] = fold_id
        fi_rows.append(fi)

    if not fold_metric_rows:
        raise ValueError("No valid fold result generated.")

    fold_metrics = pd.DataFrame(fold_metric_rows).sort_values("fold_id").reset_index(drop=True)
    fold_metrics_path = os.path.join(args.out_dir, "wf_fold_metrics.csv")
    fold_metrics.to_csv(fold_metrics_path, index=False, encoding="utf-8-sig")

    all_preds = pd.concat(fold_pred_list, ignore_index=True).sort_values(["trade_date", "stock_code"])
    all_preds_path = os.path.join(args.out_dir, "wf_test_predictions.csv")
    all_preds.to_csv(all_preds_path, index=False, encoding="utf-8-sig")

    all_group_daily = pd.concat(fold_group_daily_list, ignore_index=True).sort_values(["trade_date", "fold_id"])
    all_group_daily_path = os.path.join(args.out_dir, "wf_daily_group_returns.csv")
    all_group_daily.to_csv(all_group_daily_path, index=False, encoding="utf-8-sig")

    fi_all = pd.concat(fi_rows, ignore_index=True)
    fi_mean = (
        fi_all.groupby("factor", as_index=False)["importance"]
        .agg(["mean", "std", "count"])
        .reset_index()
        .rename(columns={"mean": "importance_mean", "std": "importance_std", "count": "fold_count"})
        .sort_values("importance_mean", ascending=False)
        .reset_index(drop=True)
    )
    fi_mean_path = os.path.join(args.out_dir, "wf_feature_importance_mean.csv")
    fi_mean.to_csv(fi_mean_path, index=False, encoding="utf-8-sig")

    # 全样本（日）聚合汇总
    ls = all_group_daily["long_short"].dropna()
    nav = (1.0 + ls).cumprod() if not ls.empty else pd.Series(dtype=float)
    ann_ret = (1.0 + ls.mean()) ** 252 - 1 if not ls.empty else np.nan
    ann_vol = ls.std(ddof=0) * np.sqrt(252) if not ls.empty else np.nan
    ann_sharpe = ann_ret / ann_vol if pd.notna(ann_vol) and ann_vol > 0 else np.nan
    max_dd = float(((nav / nav.cummax()) - 1.0).min()) if not nav.empty else np.nan

    wf_summary = {
        "run_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "input": vars(args),
        "loaded": {
            "stock_count": int(load_stats.stock_count),
            "trade_rows": int(load_stats.trade_rows),
            "finance_rows": int(load_stats.finance_rows),
            "panel_rows": int(len(panel)),
            "factor_panel_rows": int(len(factor_panel)),
        },
        "walkforward": {
            "train_days_used": int(train_days_used),
            "test_days_used": int(args.test_days),
            "step_days_used": int(args.step_days),
            "target_min_folds": int(target_min_folds),
            "fold_count": int(len(fold_metrics)),
            "avg_test_corr": float(fold_metrics["test_corr"].mean()),
            "avg_test_r2": float(fold_metrics["test_r2"].mean()),
            "avg_ls_mean": float(fold_metrics["ls_mean"].mean()),
            "avg_ls_ir": float(fold_metrics["ls_ir"].mean()),
            "avg_ls_win_rate": float(fold_metrics["ls_win_rate"].mean()),
            "all_days_obs": int(len(ls)),
            "all_days_ls_mean": float(ls.mean()) if not ls.empty else np.nan,
            "all_days_ls_ann_ret": float(ann_ret) if pd.notna(ann_ret) else np.nan,
            "all_days_ls_ann_vol": float(ann_vol) if pd.notna(ann_vol) else np.nan,
            "all_days_ls_ann_sharpe_proxy": float(ann_sharpe) if pd.notna(ann_sharpe) else np.nan,
            "all_days_ls_max_drawdown": max_dd,
        },
        "files": {
            "wf_fold_metrics": fold_metrics_path,
            "wf_test_predictions": all_preds_path,
            "wf_daily_group_returns": all_group_daily_path,
            "wf_feature_importance_mean": fi_mean_path,
        },
    }

    if first_fold_ic is not None:
        ic_first_path = os.path.join(args.out_dir, "wf_first_fold_ic_summary.csv")
        first_fold_ic.to_csv(ic_first_path, index=False, encoding="utf-8-sig")
        wf_summary["files"]["wf_first_fold_ic_summary"] = ic_first_path

    summary_path = os.path.join(args.out_dir, "wf_summary.json")
    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump(wf_summary, f, ensure_ascii=False, indent=2)

    print(f"[wf] fold_metrics -> {fold_metrics_path}")
    print(f"[wf] group_daily -> {all_group_daily_path}")
    print(f"[wf] feature_importance_mean -> {fi_mean_path}")
    print(f"[wf] summary -> {summary_path}")


if __name__ == "__main__":
    main()
