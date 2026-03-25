# -*- coding: utf-8 -*-
from __future__ import annotations
"""
基于缓存数据训练 LightGBM/XGBoost（自动从 ic_summary 选择前 N 因子）。

输出：
- ic_summary.csv
- selected_factors.json
- feature_importance.csv
- test_predictions.csv
- test_daily_group_returns.csv
- test_group_summary.csv
- model_metrics.json
- run_meta.json
"""

import argparse
import json
import os
from dataclasses import dataclass
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
from strategies.factor_lab import run_core_15_pipeline


@dataclass
class SplitData:
    """时间切分结果容器：训练/验证/测试三个子集。"""
    train: pd.DataFrame
    valid: pd.DataFrame
    test: pd.DataFrame


class NumpyLinearRegressor:
    """无第三方依赖的最小回归回退模型（仅用于 auto 兜底）。"""

    def __init__(self) -> None:
        self.coef_: np.ndarray | None = None
        self.intercept_: float = 0.0

    def fit(self, x: np.ndarray, y: np.ndarray) -> "NumpyLinearRegressor":
        # 最小二乘：在特征前拼一列常数项求解截距+系数。
        x = np.asarray(x, dtype=float)
        y = np.asarray(y, dtype=float)
        x_aug = np.column_stack([np.ones(len(x)), x])
        beta, *_ = np.linalg.lstsq(x_aug, y, rcond=None)
        self.intercept_ = float(beta[0])
        self.coef_ = beta[1:]
        return self

    def predict(self, x: np.ndarray) -> np.ndarray:
        # 预测前确保已经 fit 过。
        if self.coef_ is None:
            raise ValueError("Model is not fitted yet.")
        x = np.asarray(x, dtype=float)
        return self.intercept_ + x @ self.coef_


def _build_model(model_name: str, random_state: int = 42) -> tuple[str, Any]:
    """
    返回 (model_used, model_instance)。
    - auto: lightgbm -> xgboost -> 线性回退
    """
    name = model_name.lower().strip()
    if name in ("lightgbm", "lgbm", "auto"):
        try:
            import lightgbm as lgb  # type: ignore

            # 这套参数偏稳健，适合日频截面回归的默认起点。
            model = lgb.LGBMRegressor(
                objective="regression",
                n_estimators=500,
                learning_rate=0.03,
                num_leaves=31,
                subsample=0.85,
                colsample_bytree=0.85,
                reg_alpha=0.05,
                reg_lambda=0.2,
                random_state=random_state,
                verbosity=-1,
            )
            return "lightgbm", model
        except Exception:
            # 显式指定 lightgbm 时，导入失败应直接抛错。
            if name in ("lightgbm", "lgbm"):
                raise

    if name in ("xgboost", "xgb", "auto"):
        try:
            import xgboost as xgb  # type: ignore

            # LightGBM 不可用时，XGBoost 作为次选。
            model = xgb.XGBRegressor(
                objective="reg:squarederror",
                n_estimators=500,
                learning_rate=0.03,
                max_depth=6,
                subsample=0.85,
                colsample_bytree=0.85,
                reg_alpha=0.05,
                reg_lambda=0.2,
                random_state=random_state,
                n_jobs=4,
                verbosity=0,
            )
            return "xgboost", model
        except Exception:
            # 显式指定 xgboost 时，导入失败应直接抛错。
            if name in ("xgboost", "xgb"):
                raise

    if name in ("linear", "auto"):
        # 最终兜底：纯 numpy 线性回归，确保脚本可运行。
        return "linear_fallback", NumpyLinearRegressor()

    raise ValueError(f"Unsupported model option: {model_name}")


def _select_top_factors(ic_summary: pd.DataFrame, n_factors: int, min_obs: int = 30) -> list[str]:
    """按 IC 均值选择前 N 个因子，优先保留观测天数足够的因子。"""
    use = ic_summary.copy()
    if "obs_days" in use.columns:
        use = use[use["obs_days"] >= min_obs]
    if use.empty:
        # 如果严格过滤后为空，则回退到原始 IC 表，避免流程中断。
        use = ic_summary.copy()
    use = use.sort_values("ic_mean", ascending=False)
    factors = use["factor"].dropna().astype(str).head(n_factors).tolist()
    if not factors:
        raise ValueError("No valid factors selected from ic_summary.")
    return factors


def _time_split(df: pd.DataFrame, date_col: str, valid_days: int, test_days: int) -> SplitData:
    """按时间先后切分训练/验证/测试，严格避免未来信息泄露。"""
    dates = sorted(pd.to_datetime(df[date_col], errors="coerce").dropna().unique())
    min_train_days = 20
    if len(dates) < min_train_days + 2:
        raise ValueError(f"Not enough dates for split. got={len(dates)}")

    # 当样本日期不够时，自动收缩验证/测试窗口，优先保留训练窗口。
    max_holdout = len(dates) - min_train_days
    req_holdout = valid_days + test_days
    if req_holdout > max_holdout:
        # 保底分配：test 至少 1 天，valid 至少 1 天（若可行）。
        test_days = max(1, min(test_days, max_holdout // 2 if max_holdout >= 2 else 1))
        valid_days = max(1, max_holdout - test_days) if max_holdout >= 2 else 0

    test_set = set(dates[-test_days:])  # 最后 test_days 天
    valid_set = set(dates[-(test_days + valid_days) : -test_days]) if valid_days > 0 else set()

    test_df = df[df[date_col].isin(test_set)].copy()
    valid_df = df[df[date_col].isin(valid_set)].copy()
    train_df = df[(~df[date_col].isin(test_set)) & (~df[date_col].isin(valid_set))].copy()

    if train_df.empty or test_df.empty:
        raise ValueError("Split failed: train/test is empty.")
    return SplitData(train=train_df, valid=valid_df, test=test_df)


def _regression_metrics(y_true: np.ndarray, y_pred: np.ndarray) -> dict[str, float]:
    """回归评估：MAE / RMSE / R2 / Pearson Corr。"""
    y_true = np.asarray(y_true, dtype=float)
    y_pred = np.asarray(y_pred, dtype=float)
    err = y_true - y_pred
    mae = float(np.mean(np.abs(err)))
    rmse = float(np.sqrt(np.mean(err**2)))
    denom = float(np.sum((y_true - np.mean(y_true)) ** 2))
    r2 = float(1.0 - np.sum(err**2) / denom) if denom > 0 else np.nan
    corr = float(np.corrcoef(y_true, y_pred)[0, 1]) if len(y_true) > 2 else np.nan
    return {"mae": mae, "rmse": rmse, "r2": r2, "corr": corr}


def _feature_importance(model: Any, feature_cols: list[str]) -> pd.DataFrame:
    """抽取模型特征重要性；线性模型用 |coef| 近似。"""
    if hasattr(model, "feature_importances_"):
        imp = np.asarray(getattr(model, "feature_importances_"), dtype=float)
    elif hasattr(model, "coef_"):
        imp = np.abs(np.asarray(getattr(model, "coef_"), dtype=float))
    else:
        imp = np.zeros(len(feature_cols), dtype=float)
    out = pd.DataFrame({"factor": feature_cols, "importance": imp})
    return out.sort_values("importance", ascending=False).reset_index(drop=True)


def _group_backtest(
    pred_df: pd.DataFrame,
    date_col: str,
    score_col: str,
    ret_col: str,
    n_groups: int = 10,
    min_obs: int = 30,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    对预测分数做逐日分组回测：
    - 输出逐日分组收益明细 daily
    - 输出汇总指标 summary
    """
    rows: list[dict[str, Any]] = []

    for dt_value, cross in pred_df.groupby(date_col, sort=False):
        # 当天有效样本不足时跳过。
        cross = cross[[score_col, ret_col]].replace([np.inf, -np.inf], np.nan).dropna()
        if len(cross) < max(n_groups, min_obs):
            continue
        # 先 rank 再 qcut，减少并列值导致的分箱异常。
        rank = cross[score_col].rank(method="first")
        try:
            grp = pd.qcut(rank, q=n_groups, labels=False, duplicates="drop") + 1
        except ValueError:
            continue
        cross = cross.assign(_group=grp.values)
        grp_ret = cross.groupby("_group")[ret_col].mean()

        row: dict[str, Any] = {"trade_date": dt_value, "count": int(len(cross))}
        # 逐组平均收益 + 多空（最高组减最低组）+ 当日 score IC。
        for i in range(1, n_groups + 1):
            row[f"group_{i}"] = float(grp_ret.get(i, np.nan))
        row["long_short"] = row.get(f"group_{n_groups}", np.nan) - row.get("group_1", np.nan)
        row["score_ic"] = float(cross[score_col].corr(cross[ret_col]))
        row["score_rank_ic"] = float(cross[score_col].rank().corr(cross[ret_col].rank()))
        rows.append(row)

    daily = pd.DataFrame(rows).sort_values("trade_date").reset_index(drop=True)
    if daily.empty:
        return daily, pd.DataFrame([{"metric": "obs_days", "value": 0}])

    # 净值序列：方便看分组与多空策略曲线形态。
    daily["long_short_nav"] = (1.0 + daily["long_short"].fillna(0.0)).cumprod()
    if "group_1" in daily.columns:
        daily["group_1_nav"] = (1.0 + daily["group_1"].fillna(0.0)).cumprod()
    if f"group_{n_groups}" in daily.columns:
        daily[f"group_{n_groups}_nav"] = (1.0 + daily[f"group_{n_groups}"].fillna(0.0)).cumprod()

    ls_mean = float(daily["long_short"].mean())
    ls_std = float(daily["long_short"].std(ddof=0))
    ls_ir = ls_mean / ls_std * np.sqrt(252) if ls_std > 0 else np.nan  # 年化 IR 近似

    # 汇总指标：样本天数、均值/波动/IR、胜率、IC、各组均值。
    metrics = [
        {"metric": "obs_days", "value": float(len(daily))},
        {"metric": "long_short_mean", "value": ls_mean},
        {"metric": "long_short_std", "value": ls_std},
        {"metric": "long_short_ir", "value": float(ls_ir) if pd.notna(ls_ir) else np.nan},
        {"metric": "long_short_win_rate", "value": float((daily["long_short"] > 0).mean())},
        {"metric": "score_ic_mean", "value": float(daily["score_ic"].mean())},
        {"metric": "score_rank_ic_mean", "value": float(daily["score_rank_ic"].mean())},
    ]
    for i in range(1, n_groups + 1):
        col = f"group_{i}"
        if col in daily.columns:
            metrics.append({"metric": f"{col}_mean", "value": float(daily[col].mean())})

    return daily, pd.DataFrame(metrics)


def parse_args() -> argparse.Namespace:
    """命令行参数定义。"""
    parser = argparse.ArgumentParser(description="Train LightGBM/XGBoost from A-share factor cache.")
    # 数据与输出
    parser.add_argument("--cache-file", default="data/cache/full_data_v3_5year.pkl")
    parser.add_argument("--finance-dir", default="data/cache/finance")
    parser.add_argument("--index-file", default="data/cache/benchmark_000300.csv")
    parser.add_argument("--out-dir", default="tests/factor_lab_model_outputs")
    # 样本范围
    parser.add_argument("--start-date", default="")
    parser.add_argument("--end-date", default="")
    parser.add_argument("--max-stocks", type=int, default=0, help="Use first N stocks for quick run; 0=all.")
    # 目标与特征筛选参数
    parser.add_argument("--horizon", type=int, default=5)
    parser.add_argument("--n-factors", type=int, default=10)
    parser.add_argument("--min-ic-obs", type=int, default=30)
    # 可交易性过滤参数
    parser.add_argument("--min-market-cap", type=float, default=3e9)
    parser.add_argument("--min-amount", type=float, default=2e8)
    parser.add_argument("--neutralize", action="store_true")
    # 时间切分参数
    parser.add_argument("--valid-days", type=int, default=60)
    parser.add_argument("--test-days", type=int, default=60)
    # 模型参数
    parser.add_argument("--model", default="auto", choices=["auto", "lightgbm", "xgboost", "linear"])
    parser.add_argument("--random-state", type=int, default=42)
    return parser.parse_args()


def main() -> None:
    # 0) 参数与输出目录
    args = parse_args()
    os.makedirs(args.out_dir, exist_ok=True)

    # 1) 数据加载与因子流水线
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
    factor_panel = factor_result["panel"]
    ic_summary = factor_result["ic_summary"].copy()
    # 先落盘 IC，便于直接核对选因子依据。
    ic_path = os.path.join(args.out_dir, "ic_summary.csv")
    ic_summary.to_csv(ic_path, index=False, encoding="utf-8-sig")

    # 2) 自动选因子
    selected_factors = _select_top_factors(ic_summary, n_factors=args.n_factors, min_obs=args.min_ic_obs)
    selected_path = os.path.join(args.out_dir, "selected_factors.json")
    with open(selected_path, "w", encoding="utf-8") as f:
        json.dump({"factors": selected_factors}, f, ensure_ascii=False, indent=2)

    # 3) 建模样本
    target_col = f"future_ret_{args.horizon}"
    # 使用目标列 + 入选因子构建监督学习样本。
    model_df = factor_panel[["stock_code", "trade_date", target_col] + selected_factors].copy()
    # 模型训练不接受 NaN/Inf，先统一清洗。
    model_df = model_df.replace([np.inf, -np.inf], np.nan).dropna(subset=[target_col] + selected_factors)
    if model_df.empty:
        raise ValueError("Model dataset is empty after dropna.")
    model_df["trade_date"] = pd.to_datetime(model_df["trade_date"], errors="coerce")
    model_df = model_df.dropna(subset=["trade_date"]).sort_values("trade_date").reset_index(drop=True)

    # 时间切分：训练在前，验证/测试在后，严格时序。
    split = _time_split(model_df, date_col="trade_date", valid_days=args.valid_days, test_days=args.test_days)
    x_train = split.train[selected_factors].astype(float)
    y_train = split.train[target_col].to_numpy(dtype=float)
    x_valid = split.valid[selected_factors].astype(float)
    y_valid = split.valid[target_col].to_numpy(dtype=float)
    x_test = split.test[selected_factors].astype(float)
    y_test = split.test[target_col].to_numpy(dtype=float)

    # 4) 训练模型
    model_used, model = _build_model(args.model, random_state=args.random_state)
    fit_kwargs: dict[str, Any] = {}
    # 树模型可附带验证集用于训练日志/早停接口兼容。
    if model_used in ("lightgbm", "xgboost") and len(split.valid) > 0:
        fit_kwargs = {"eval_set": [(x_valid, y_valid)]}
    model.fit(x_train, y_train, **fit_kwargs) if fit_kwargs else model.fit(x_train, y_train)

    # 5) 预测与评估
    pred_train = model.predict(x_train)
    pred_valid = model.predict(x_valid) if len(split.valid) > 0 else np.array([])
    pred_test = model.predict(x_test)

    metrics = {
        "model_used": model_used,
        "train": _regression_metrics(y_train, pred_train),
        "valid": _regression_metrics(y_valid, pred_valid) if len(pred_valid) > 0 else {},
        "test": _regression_metrics(y_test, pred_test),
    }
    metrics_path = os.path.join(args.out_dir, "model_metrics.json")
    with open(metrics_path, "w", encoding="utf-8") as f:
        json.dump(metrics, f, ensure_ascii=False, indent=2)

    # 6) 特征重要性
    fi = _feature_importance(model, selected_factors)
    fi_path = os.path.join(args.out_dir, "feature_importance.csv")
    fi.to_csv(fi_path, index=False, encoding="utf-8-sig")

    # 7) 测试期分层回测（基于预测分数）
    test_pred_df = split.test[["stock_code", "trade_date", target_col]].copy()
    test_pred_df["pred_score"] = pred_test
    pred_path = os.path.join(args.out_dir, "test_predictions.csv")
    test_pred_df.to_csv(pred_path, index=False, encoding="utf-8-sig")

    daily_group, group_summary = _group_backtest(
        pred_df=test_pred_df,
        date_col="trade_date",
        score_col="pred_score",
        ret_col=target_col,
        n_groups=10,
        min_obs=30,
    )
    group_daily_path = os.path.join(args.out_dir, "test_daily_group_returns.csv")
    group_summary_path = os.path.join(args.out_dir, "test_group_summary.csv")
    daily_group.to_csv(group_daily_path, index=False, encoding="utf-8-sig")
    group_summary.to_csv(group_summary_path, index=False, encoding="utf-8-sig")

    # 8) 汇总元信息，便于结果复盘与可重复实验。
    meta = {
        "run_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "input": vars(args),
        "loaded": {
            "stock_count": load_stats.stock_count,
            "trade_rows": load_stats.trade_rows,
            "finance_rows": load_stats.finance_rows,
            "panel_rows": int(len(panel)),
            "factor_panel_rows": int(len(factor_panel)),
            "model_rows": int(len(model_df)),
        },
        "selected_factors": selected_factors,
        "files": {
            "ic_summary": ic_path,
            "selected_factors": selected_path,
            "feature_importance": fi_path,
            "test_predictions": pred_path,
            "test_daily_group_returns": group_daily_path,
            "test_group_summary": group_summary_path,
            "model_metrics": metrics_path,
        },
    }
    meta_path = os.path.join(args.out_dir, "run_meta.json")
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)

    print(f"[model_train] model={model_used}")
    print(f"[model_train] feature_importance -> {fi_path}")
    print(f"[model_train] group_summary -> {group_summary_path}")
    print(f"[model_train] meta -> {meta_path}")


if __name__ == "__main__":
    main()
