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
    """
    在给定数据子集（通常是训练窗口）上，逐因子计算 IC 摘要并按 ic_mean 排序。
    """
    rows: list[dict[str, Any]] = []
    for fac in factor_cols:
        # evaluate_factor_ic 返回含 ic_series 的完整结构；这里仅保留汇总字段。
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
    """按时间顺序生成 walk-forward 的 (训练日期, 测试日期) 窗口列表。"""
    folds: list[tuple[list[pd.Timestamp], list[pd.Timestamp]]] = []
    # 日期总长度不足以切出 1 折时，直接返回空。
    if len(dates) < train_days + test_days:
        return folds

    # split_idx 表示“测试窗口起点”的索引位置。
    for split_idx in range(train_days, len(dates) - test_days + 1, step_days):
        # 训练集只取 split_idx 之前的历史窗口。
        train_dates = dates[split_idx - train_days : split_idx]
        # 测试集取随后连续 test_days 个交易日。
        test_dates = dates[split_idx : split_idx + test_days]
        folds.append((train_dates, test_dates))
        # 指定 max_folds 时达到上限即停止。
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
    """按训练/测试双侧缺失率过滤候选因子，最多保留 n_factors 个。"""
    kept: list[str] = []
    for fac in ranked_factors:
        # 两侧都必须存在该字段。
        if fac not in train_df.columns or fac not in test_df.columns:
            continue
        # 覆盖率 = 非空样本占比。
        cov_train = float(train_df[fac].notna().mean())
        cov_test = float(test_df[fac].notna().mean())
        if cov_train >= min_coverage and cov_test >= min_coverage:
            kept.append(fac)
        # 达到目标数量后提前退出。
        if len(kept) >= n_factors:
            break
    return kept


def parse_args() -> argparse.Namespace:
    """命令行参数定义。"""
    parser = argparse.ArgumentParser(description="Walk-forward training for A-share factor model.")
    # 数据源与输出目录
    parser.add_argument("--cache-file", default="data/cache/full_data_v3_5year.pkl")
    parser.add_argument("--finance-dir", default="data/cache/finance")
    parser.add_argument("--index-file", default="data/cache/benchmark_000300.csv")
    parser.add_argument("--out-dir", default="tests/factor_lab_walkforward_outputs")
    # 日期与样本范围
    parser.add_argument("--start-date", default="")
    parser.add_argument("--end-date", default="")
    parser.add_argument("--max-stocks", type=int, default=0, help="Use first N stocks for quick run; 0=all.")
    # 目标收益与因子筛选参数
    parser.add_argument("--horizon", type=int, default=5)
    parser.add_argument("--n-factors", type=int, default=10)
    parser.add_argument("--min-ic-obs", type=int, default=30)
    # 交易可行性过滤参数
    parser.add_argument("--min-market-cap", type=float, default=3e9)
    parser.add_argument("--min-amount", type=float, default=2e8)
    parser.add_argument("--neutralize", action="store_true")
    # Walk-forward 窗口参数
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
    # True: 每折都用当折训练集重选因子；False: 固定首折因子列表。
    parser.add_argument("--rolling-factor-select", action="store_true", help="Select factors per fold by train IC.")
    parser.add_argument(
        "--min-factor-coverage",
        type=float,
        default=0.2,
        help="Minimum non-null ratio required in both train/test for a selected factor.",
    )
    # 回归模型类型（auto 会自动回退可用实现）。
    parser.add_argument("--model", default="auto", choices=["auto", "lightgbm", "xgboost", "linear"])
    parser.add_argument("--random-state", type=int, default=42)
    return parser.parse_args()


def main() -> None:
    # 0) 参数与目录准备
    args = parse_args()
    os.makedirs(args.out_dir, exist_ok=True)

    # 1) 数据 + 因子面板
    # 1.1 读取行情长表（可按日期/股票数裁剪）
    daily_df = _load_stock_panel(
        cache_file=args.cache_file,
        start_date=args.start_date or None,
        end_date=args.end_date or None,
        max_stocks=args.max_stocks if args.max_stocks > 0 else None,
    )
    # 1.2 根据股票列表加载财务快照。
    codes = daily_df["stock_code"].dropna().astype(str).unique().tolist()
    finance_df = _load_finance_panel(args.finance_dir, codes)
    # 1.3 读取指数数据（用于部分因子和超额收益计算）。
    index_df = _load_index_df(args.index_file)
    # 1.4 拼接基础面板并返回加载统计。
    panel, load_stats = _prepare_panel_for_factors(daily_df, finance_df)

    # 1.5 一键计算核心因子、过滤、标准化/中性化与前瞻收益列。
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
    # 候选因子限定为 pipeline 实际返回且字段存在的列。
    candidate_factors = [f for f in factor_result["factors"] if f in factor_panel.columns]
    target_col = f"future_ret_{args.horizon}"

    # 核心目标列/候选因子为空时直接失败，避免无意义训练。
    if target_col not in factor_panel.columns:
        raise ValueError(f"Target column not found: {target_col}")
    if not candidate_factors:
        raise ValueError("No candidate factors available for training.")

    # 2) 生成 walk-forward 时间折
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
        # 推导出在给定 test/step 下，为达到最少折数可允许的最大训练窗口。
        max_train_for_target = len(dates) - args.test_days - args.step_days * (target_min_folds - 1)
        if max_train_for_target >= min_train_days:
            # 训练窗口只缩短不放大，尽量保留历史信息。
            train_days_used = min(train_days_used, int(max_train_for_target))
            folds = _walkforward_dates(
                dates=dates,
                train_days=train_days_used,
                test_days=args.test_days,
                step_days=args.step_days,
                max_folds=args.max_folds,
            )

    if not folds:
        # 兜底策略：若仍无折数，尝试“全历史训练 + 最后 test_days 测试”。
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

    # 3) 准备跨折收集容器
    fold_metric_rows: list[dict[str, Any]] = []
    fold_group_daily_list: list[pd.DataFrame] = []
    fold_pred_list: list[pd.DataFrame] = []
    fi_rows: list[pd.DataFrame] = []

    # fixed_factors 用于“非滚动选因子”模式，只在首折确定一次。
    fixed_factors: list[str] | None = None
    # 保存首折 IC 明细，便于结果解释。
    first_fold_ic: pd.DataFrame | None = None

    # 4) 按折滚动训练与测试
    for fold_id, (train_dates, test_dates) in enumerate(folds, start=1):
        # 用 set 加速 isin 判断。
        train_set = set(train_dates)
        test_set = set(test_dates)

        # 切分当折训练/测试样本。
        train_df = factor_panel[factor_panel["trade_date"].isin(train_set)].copy()
        test_df = factor_panel[factor_panel["trade_date"].isin(test_set)].copy()
        # 统一把 inf 处理成 NaN，避免模型/评估报错。
        train_df = train_df.replace([np.inf, -np.inf], np.nan)
        test_df = test_df.replace([np.inf, -np.inf], np.nan)

        # 任一侧为空时，当前折无效，直接跳过。
        if train_df.empty or test_df.empty:
            continue

        # 2) 每个窗口选择因子（或固定首窗口因子）
        if args.rolling_factor_select:
            # 滚动选因子：每折都基于该折训练集重新计算 IC 排名。
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
            # 固定选因子：只在首折计算一次，后续折直接复用。
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

        # 覆盖率裁剪：确保训练与测试期都“有数据可用”。
        selected_factors = _prune_factors_by_coverage(
            ranked_factors=selected_factors,
            train_df=train_df,
            test_df=test_df,
            n_factors=args.n_factors,
            min_coverage=args.min_factor_coverage,
        )
        # 因子过少时模型稳定性差，当前折跳过。
        if len(selected_factors) < 3:
            continue

        # 组装该折模型输入字段。
        train_use = train_df[["stock_code", "trade_date", target_col] + selected_factors].copy()
        test_use = test_df[["stock_code", "trade_date", target_col] + selected_factors].copy()
        # 标签缺失样本不可用于监督训练/评估。
        train_use = train_use.dropna(subset=[target_col])
        test_use = test_use.dropna(subset=[target_col])
        # 特征缺失采用 0 填充（与单独脚本保持一致口径）。
        for fac in selected_factors:
            train_use[fac] = train_use[fac].fillna(0.0)
            test_use[fac] = test_use[fac].fillna(0.0)
        # 再次防御空样本。
        if train_use.empty or test_use.empty:
            continue

        # 构造 numpy 训练/测试矩阵。
        x_train = train_use[selected_factors].astype(float)
        y_train = train_use[target_col].to_numpy(dtype=float)
        x_test = test_use[selected_factors].astype(float)
        y_test = test_use[target_col].to_numpy(dtype=float)

        # 3) 训练与测试
        # 每折随机种子加 fold_id，避免所有折完全同种随机流。
        model_used, model = _build_model(args.model, random_state=args.random_state + fold_id)
        model.fit(x_train, y_train)
        pred_test = model.predict(x_test)
        # 回归指标（MAE/RMSE/R2/Corr）。
        reg_test = _regression_metrics(y_test, pred_test)

        # 4) 窗口分层回测
        # fold_pred 是该折测试期打分明细。
        fold_pred = test_use[["stock_code", "trade_date", target_col]].copy()
        fold_pred["pred_score"] = pred_test
        fold_pred["fold_id"] = fold_id
        fold_pred_list.append(fold_pred)

        # 按分数分十组，统计 top-bottom 多空与 score IC。
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

        # group_summary 转 dict，方便拼到单行 fold 指标里。
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

        # 记录该折特征重要性。
        fi = _feature_importance(model, selected_factors)
        fi["fold_id"] = fold_id
        fi_rows.append(fi)

    # 所有折都被过滤掉时，直接报错。
    if not fold_metric_rows:
        raise ValueError("No valid fold result generated.")

    # 5) 落盘跨折汇总文件
    fold_metrics = pd.DataFrame(fold_metric_rows).sort_values("fold_id").reset_index(drop=True)
    fold_metrics_path = os.path.join(args.out_dir, "wf_fold_metrics.csv")
    fold_metrics.to_csv(fold_metrics_path, index=False, encoding="utf-8-sig")

    # 所有折测试期预测明细（供后续诊断/融合使用）。
    all_preds = pd.concat(fold_pred_list, ignore_index=True).sort_values(["trade_date", "stock_code"])
    all_preds_path = os.path.join(args.out_dir, "wf_test_predictions.csv")
    all_preds.to_csv(all_preds_path, index=False, encoding="utf-8-sig")

    # 所有折逐日分组回测明细（按日期+折号排序）。
    all_group_daily = pd.concat(fold_group_daily_list, ignore_index=True).sort_values(["trade_date", "fold_id"])
    all_group_daily_path = os.path.join(args.out_dir, "wf_daily_group_returns.csv")
    all_group_daily.to_csv(all_group_daily_path, index=False, encoding="utf-8-sig")

    # 6) 聚合跨折特征重要性（均值/标准差/出现折数）。
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
    # long_short 为跨折拼接后的逐日 top-bottom 收益。
    ls = all_group_daily["long_short"].dropna()
    # 净值曲线（复利）。
    nav = (1.0 + ls).cumprod() if not ls.empty else pd.Series(dtype=float)
    # 年化收益采用复利近似口径：(1+日均)^252-1。
    ann_ret = (1.0 + ls.mean()) ** 252 - 1 if not ls.empty else np.nan
    # 年化波动与 Sharpe 代理。
    ann_vol = ls.std(ddof=0) * np.sqrt(252) if not ls.empty else np.nan
    ann_sharpe = ann_ret / ann_vol if pd.notna(ann_vol) and ann_vol > 0 else np.nan
    # 最大回撤来自净值相对历史高点回撤序列最小值。
    max_dd = float(((nav / nav.cummax()) - 1.0).min()) if not nav.empty else np.nan

    # 7) 汇总 JSON：输入参数 + 数据规模 + walk-forward 表现 + 输出文件路径。
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

    # 固定选因子模式下，额外输出首折 IC 摘要。
    if first_fold_ic is not None:
        ic_first_path = os.path.join(args.out_dir, "wf_first_fold_ic_summary.csv")
        first_fold_ic.to_csv(ic_first_path, index=False, encoding="utf-8-sig")
        wf_summary["files"]["wf_first_fold_ic_summary"] = ic_first_path

    # 8) 落盘最终 summary 文件。
    summary_path = os.path.join(args.out_dir, "wf_summary.json")
    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump(wf_summary, f, ensure_ascii=False, indent=2)

    # 9) 控制台打印关键产物路径。
    print(f"[wf] fold_metrics -> {fold_metrics_path}")
    print(f"[wf] group_daily -> {all_group_daily_path}")
    print(f"[wf] feature_importance_mean -> {fi_mean_path}")
    print(f"[wf] summary -> {summary_path}")


if __name__ == "__main__":
    main()
