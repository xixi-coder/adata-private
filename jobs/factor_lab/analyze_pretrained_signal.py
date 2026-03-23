# -*- coding: utf-8 -*-
from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass
from datetime import datetime

import numpy as np
import pandas as pd


def _normalize_code(x: object) -> str:
    # 统一股票代码格式，确保可稳定 join（例如 1 -> 000001）。
    if pd.isna(x):
        return ""
    s = str(x).strip()
    if s.endswith(".0"):
        s = s[:-2]
    return s.zfill(6) if s.isdigit() else s


def _load_gru_predictions(path: str) -> pd.DataFrame:
    df = pd.read_csv(path)
    # 严格校验字段，避免下游评估口径漂移。
    need = {"stock_code", "trade_date", "pred", "ret_qlib", "ret_1d"}
    miss = need - set(df.columns)
    if miss:
        raise ValueError(f"GRU predictions missing columns: {miss}")
    df = df.copy()
    df["stock_code"] = df["stock_code"].map(_normalize_code)
    df["trade_date"] = pd.to_datetime(df["trade_date"], errors="coerce")
    for c in ("pred", "ret_qlib", "ret_1d"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna(subset=["stock_code", "trade_date", "pred", "ret_qlib"]).sort_values(["trade_date", "stock_code"])
    return df


def _load_factor_predictions(path: str) -> pd.DataFrame:
    df = pd.read_csv(path)
    need = {"stock_code", "trade_date", "pred_score"}
    miss = need - set(df.columns)
    if miss:
        raise ValueError(f"Factor predictions missing columns: {miss}")
    df = df.copy()
    df["stock_code"] = df["stock_code"].map(_normalize_code)
    df["trade_date"] = pd.to_datetime(df["trade_date"], errors="coerce")
    df["pred_score"] = pd.to_numeric(df["pred_score"], errors="coerce")
    df = df.dropna(subset=["stock_code", "trade_date", "pred_score"])
    # Walk-forward 可能在同一交易日重复产出，这里保留最后一条（通常是最新折的结果）。
    df = df.sort_values(["trade_date", "stock_code"]).drop_duplicates(["trade_date", "stock_code"], keep="last")
    return df


def _load_benchmark(path: str) -> pd.DataFrame:
    df = pd.read_csv(path)
    if "trade_date" not in df.columns or "close" not in df.columns:
        raise ValueError("benchmark file should include trade_date and close")
    df = df.copy()
    df["trade_date"] = pd.to_datetime(df["trade_date"], errors="coerce")
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    df = df.dropna(subset=["trade_date", "close"]).sort_values("trade_date").drop_duplicates("trade_date")
    # align with ret_qlib label: close(t+2)/close(t+1)-1 at signal date t
    c = df["close"]
    df["bm_ret_qlib"] = c.shift(-2) / c.shift(-1) - 1.0
    return df[["trade_date", "bm_ret_qlib"]]


def _cs_rank_groups(signal: pd.Series, n_groups: int) -> pd.Series:
    # 先做秩排序再分箱，尽量降低截面中并列值导致的空桶问题。
    rank = signal.rank(method="first")
    grp = pd.qcut(rank, q=n_groups, labels=False, duplicates="drop")
    return grp + 1


def _cs_zscore(s: pd.Series) -> pd.Series:
    x = pd.to_numeric(s, errors="coerce")
    mu = x.mean()
    sd = x.std(ddof=0)
    if pd.isna(sd) or sd <= 1e-12:
        return pd.Series(np.zeros(len(x), dtype=float), index=x.index)
    return (x - mu) / sd


@dataclass
class MetricPack:
    days: int
    mean_daily: float
    std_daily: float
    ann_return: float
    sharpe: float


def _ret_metrics(ret: pd.Series) -> MetricPack:
    # 年化口径按 252 个交易日计算。
    x = pd.to_numeric(ret, errors="coerce").dropna()
    if x.empty:
        return MetricPack(0, np.nan, np.nan, np.nan, np.nan)
    mu = float(x.mean())
    sd = float(x.std(ddof=0))
    shp = mu / sd * np.sqrt(252) if sd > 0 else np.nan
    return MetricPack(
        days=int(len(x)),
        mean_daily=mu,
        std_daily=sd,
        ann_return=float(mu * 252),
        sharpe=float(shp) if pd.notna(shp) else np.nan,
    )


def _ic_metrics(df: pd.DataFrame, signal_col: str, ret_col: str = "ret_qlib", min_cs: int = 30) -> dict:
    # 逐日计算截面 IC / RankIC，再做跨日汇总。
    rows = []
    for dt, cross in df.groupby("trade_date"):
        cross = cross[[signal_col, ret_col]].replace([np.inf, -np.inf], np.nan).dropna()
        if len(cross) < min_cs:
            continue
        ic = cross[signal_col].corr(cross[ret_col], method="pearson")
        ric = cross[signal_col].rank(method="average").corr(cross[ret_col].rank(method="average"), method="pearson")
        rows.append((dt, ic, ric, len(cross)))
    ic_df = pd.DataFrame(rows, columns=["trade_date", "ic", "rank_ic", "count"])
    if ic_df.empty:
        return {"obs_days": 0, "ic_mean": np.nan, "rank_ic_mean": np.nan, "ic_ir": np.nan, "rank_ic_ir": np.nan}
    ic_mean = float(ic_df["ic"].mean())
    ric_mean = float(ic_df["rank_ic"].mean())
    ic_std = float(ic_df["ic"].std(ddof=0))
    ric_std = float(ic_df["rank_ic"].std(ddof=0))
    ic_ir = ic_mean / ic_std * np.sqrt(252) if ic_std > 0 else np.nan
    ric_ir = ric_mean / ric_std * np.sqrt(252) if ric_std > 0 else np.nan
    return {
        "obs_days": int(len(ic_df)),
        "ic_mean": ic_mean,
        "rank_ic_mean": ric_mean,
        "ic_ir": float(ic_ir) if pd.notna(ic_ir) else np.nan,
        "rank_ic_ir": float(ric_ir) if pd.notna(ric_ir) else np.nan,
    }


def _build_5group_report(df: pd.DataFrame, signal_col: str, ret_col: str, out_dir: str) -> dict:
    # 5分组（五分位）回测：
    # 每日按信号从低到高分成 5 组，统计各组平均收益，并构造多空收益 G5-G1。
    rows = []
    for dt, cross in df.groupby("trade_date"):
        c = cross[[signal_col, ret_col]].replace([np.inf, -np.inf], np.nan).dropna()
        if len(c) < 30:
            continue
        try:
            grp = _cs_rank_groups(c[signal_col], n_groups=5)
        except ValueError:
            continue
        c = c.assign(group=grp.to_numpy(dtype=int))
        gret = c.groupby("group")[ret_col].mean().sort_index()
        if len(gret) < 5:
            continue
        row = {"trade_date": dt}
        for g in range(1, 6):
            row[f"g{g}"] = float(gret.get(g, np.nan))
        row["long_short_5_1"] = row["g5"] - row["g1"]
        rows.append(row)

    rep = pd.DataFrame(rows).sort_values("trade_date").reset_index(drop=True)
    if rep.empty:
        raise ValueError("5-group report is empty.")

    eq = rep[["trade_date"]].copy()
    for col in ["g1", "g2", "g3", "g4", "g5", "long_short_5_1"]:
        eq[f"eq_{col}"] = (1.0 + rep[col].fillna(0.0)).cumprod()

    rep.to_csv(os.path.join(out_dir, "report_5group_daily.csv"), index=False, encoding="utf-8-sig")
    eq.to_csv(os.path.join(out_dir, "equity_5group.csv"), index=False, encoding="utf-8-sig")

    ls_m = _ret_metrics(rep["long_short_5_1"])
    g5_m = _ret_metrics(rep["g5"])
    return {
        "long_short_5_1": ls_m.__dict__,
        "top_group_g5": g5_m.__dict__,
    }


def _build_top10_reports(df: pd.DataFrame, signal_col: str, ret_col: str, benchmark: pd.DataFrame, out_dir: str) -> dict:
    # Top10（前10%）回测：
    # 每日只做多最高分组（第10组），并计算相对基准超额（hedged_ret=top10-benchmark）。
    rows = []
    for dt, cross in df.groupby("trade_date"):
        c = cross[[signal_col, ret_col]].replace([np.inf, -np.inf], np.nan).dropna()
        if len(c) < 30:
            continue
        try:
            grp = _cs_rank_groups(c[signal_col], n_groups=10)
        except ValueError:
            continue
        c = c.assign(group=grp.to_numpy(dtype=int))
        top = float(c.loc[c["group"] == 10, ret_col].mean())
        rows.append({"trade_date": dt, "top10_ret": top, "count": int(len(c))})
    top = pd.DataFrame(rows).sort_values("trade_date").reset_index(drop=True)
    if top.empty:
        raise ValueError("top10 report is empty")
    top = top.merge(benchmark, on="trade_date", how="left")
    top["hedged_ret"] = top["top10_ret"] - top["bm_ret_qlib"]

    eq = top[["trade_date"]].copy()
    eq["eq_top10_long_only"] = (1.0 + top["top10_ret"].fillna(0.0)).cumprod()
    eq["eq_top10_hedged"] = (1.0 + top["hedged_ret"].fillna(0.0)).cumprod()

    top.to_csv(os.path.join(out_dir, "report_top10_daily.csv"), index=False, encoding="utf-8-sig")
    eq.to_csv(os.path.join(out_dir, "equity_top10.csv"), index=False, encoding="utf-8-sig")

    return {
        "top10_long_only": _ret_metrics(top["top10_ret"]).__dict__,
        "top10_hedged_csi300": _ret_metrics(top["hedged_ret"]).__dict__,
    }


def _build_fusion_report(gru_df: pd.DataFrame, fac_df: pd.DataFrame, out_dir: str) -> dict:
    # 融合评估：
    # 仅在 GRU 与因子预测重叠的股票-日期样本上评估，保证可比性。
    merged = gru_df.merge(
        fac_df[["stock_code", "trade_date", "pred_score"]],
        on=["stock_code", "trade_date"],
        how="inner",
    )
    if merged.empty:
        raise ValueError("No overlap rows between GRU and factor predictions.")

    merged = merged.sort_values(["trade_date", "stock_code"]).reset_index(drop=True)
    # 对两个信号逐日做截面标准化（z-score），再线性融合，避免量纲不一致。
    merged["gru_z"] = merged.groupby("trade_date")["pred"].transform(_cs_zscore)
    merged["fac_z"] = merged.groupby("trade_date")["pred_score"].transform(_cs_zscore)
    merged["fused_eqw"] = 0.5 * merged["gru_z"] + 0.5 * merged["fac_z"]

    # 可选：在重叠样本上做静态权重扫描，仅用于描述性观察最优线性权重。
    best = {"w_gru": np.nan, "w_fac": np.nan, "ann_ls_10": -np.inf}
    for w in np.linspace(0.0, 1.0, 11):
        sc = w * merged["gru_z"] + (1.0 - w) * merged["fac_z"]
        tmp = merged[["trade_date", "ret_qlib"]].copy()
        tmp["score"] = sc.to_numpy()
        rows = []
        for dt, cross in tmp.groupby("trade_date"):
            c = cross[["score", "ret_qlib"]].replace([np.inf, -np.inf], np.nan).dropna()
            if len(c) < 30:
                continue
            try:
                grp = _cs_rank_groups(c["score"], n_groups=10)
            except ValueError:
                continue
            c = c.assign(group=grp.to_numpy(dtype=int))
            g = c.groupby("group")["ret_qlib"].mean().sort_index()
            if len(g) < 10:
                continue
            rows.append(float(g.iloc[-1] - g.iloc[0]))
        if not rows:
            continue
        ann = float(np.mean(rows) * 252)
        if ann > best["ann_ls_10"]:
            best = {"w_gru": float(w), "w_fac": float(1.0 - w), "ann_ls_10": ann}

    def _ls10(signal_col: str) -> dict:
        # 每日计算十分位多空收益：第10组 - 第1组。
        rows = []
        for dt, cross in merged.groupby("trade_date"):
            c = cross[[signal_col, "ret_qlib"]].replace([np.inf, -np.inf], np.nan).dropna()
            if len(c) < 30:
                continue
            try:
                grp = _cs_rank_groups(c[signal_col], n_groups=10)
            except ValueError:
                continue
            c = c.assign(group=grp.to_numpy(dtype=int))
            g = c.groupby("group")["ret_qlib"].mean().sort_index()
            if len(g) < 10:
                continue
            rows.append({"trade_date": dt, "ls10": float(g.iloc[-1] - g.iloc[0])})
        d = pd.DataFrame(rows).sort_values("trade_date")
        if d.empty:
            return {"metrics": _ret_metrics(pd.Series(dtype=float)).__dict__, "daily": d}
        return {"metrics": _ret_metrics(d["ls10"]).__dict__, "daily": d}

    m_gru = _ls10("pred")
    m_fac = _ls10("pred_score")
    m_fused = _ls10("fused_eqw")

    # output artifacts
    merged.to_csv(os.path.join(out_dir, "fusion_overlap_panel.csv"), index=False, encoding="utf-8-sig")
    m_gru["daily"].to_csv(os.path.join(out_dir, "fusion_ls10_gru_daily.csv"), index=False, encoding="utf-8-sig")
    m_fac["daily"].to_csv(os.path.join(out_dir, "fusion_ls10_factor_daily.csv"), index=False, encoding="utf-8-sig")
    m_fused["daily"].to_csv(os.path.join(out_dir, "fusion_ls10_eqw_daily.csv"), index=False, encoding="utf-8-sig")

    eq = pd.DataFrame({"trade_date": sorted(set(m_gru["daily"].get("trade_date", [])) | set(m_fac["daily"].get("trade_date", [])) | set(m_fused["daily"].get("trade_date", [])))})
    for name, daily in [("gru", m_gru["daily"]), ("factor", m_fac["daily"]), ("fused_eqw", m_fused["daily"])]:
        if daily.empty:
            eq[f"eq_{name}"] = np.nan
            continue
        d = daily.copy()
        d[f"eq_{name}"] = (1.0 + d["ls10"].fillna(0.0)).cumprod()
        eq = eq.merge(d[["trade_date", f"eq_{name}"]], on="trade_date", how="left")
    eq.to_csv(os.path.join(out_dir, "fusion_equity_ls10.csv"), index=False, encoding="utf-8-sig")

    return {
        "overlap_rows": int(len(merged)),
        "overlap_stocks": int(merged["stock_code"].nunique()),
        "overlap_dates": int(merged["trade_date"].nunique()),
        "gru_ls10": m_gru["metrics"],
        "factor_ls10": m_fac["metrics"],
        "fused_eqw_ls10": m_fused["metrics"],
        "best_static_weight_scan_on_overlap": best,
        "ic_on_overlap": {
            "gru": _ic_metrics(merged, "pred", "ret_qlib"),
            "factor": _ic_metrics(merged, "pred_score", "ret_qlib"),
            "fused_eqw": _ic_metrics(merged, "fused_eqw", "ret_qlib"),
        },
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Analyze pretrained signal: 5-group/top10/fusion.")
    parser.add_argument(
        "--gru-predictions",
        default="/Users/xixi/pythonProject/adata/tests/qlib_pretrained_eval_gru_300/predictions.csv",
    )
    parser.add_argument(
        "--factor-predictions",
        default="/Users/xixi/pythonProject/adata/tests/factor_lab_walkforward_outputs_full_v2/wf_test_predictions.csv",
    )
    parser.add_argument(
        "--benchmark-file",
        default="/Users/xixi/pythonProject/adata/data/cache/benchmark_000300.csv",
    )
    parser.add_argument(
        "--out-dir",
        default="/Users/xixi/pythonProject/adata/tests/qlib_pretrained_diag_gru300",
    )
    return parser.parse_args()


def main() -> None:
    # 端到端诊断流程：
    # 1) 读取预测与基准数据
    # 2) 生成 5分组、Top10、融合 三类报告
    # 3) 落盘明细与汇总 JSON
    args = parse_args()
    os.makedirs(args.out_dir, exist_ok=True)

    gru_df = _load_gru_predictions(args.gru_predictions)
    fac_df = _load_factor_predictions(args.factor_predictions)
    bm_df = _load_benchmark(args.benchmark_file)

    five = _build_5group_report(gru_df, signal_col="pred", ret_col="ret_qlib", out_dir=args.out_dir)
    top10 = _build_top10_reports(gru_df, signal_col="pred", ret_col="ret_qlib", benchmark=bm_df, out_dir=args.out_dir)
    fusion = _build_fusion_report(gru_df, fac_df, out_dir=args.out_dir)

    summary = {
        "run_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "inputs": {
            "gru_predictions": args.gru_predictions,
            "factor_predictions": args.factor_predictions,
            "benchmark_file": args.benchmark_file,
        },
        "coverage": {
            "gru_rows": int(len(gru_df)),
            "gru_stocks": int(gru_df["stock_code"].nunique()),
            "gru_dates": int(gru_df["trade_date"].nunique()),
            "gru_start": pd.Timestamp(gru_df["trade_date"].min()).strftime("%Y-%m-%d"),
            "gru_end": pd.Timestamp(gru_df["trade_date"].max()).strftime("%Y-%m-%d"),
        },
        "report_1_5group": five,
        "report_2_top10": top10,
        "report_3_fusion": fusion,
        "outputs": {
            "report_5group_daily": os.path.join(args.out_dir, "report_5group_daily.csv"),
            "equity_5group": os.path.join(args.out_dir, "equity_5group.csv"),
            "report_top10_daily": os.path.join(args.out_dir, "report_top10_daily.csv"),
            "equity_top10": os.path.join(args.out_dir, "equity_top10.csv"),
            "fusion_overlap_panel": os.path.join(args.out_dir, "fusion_overlap_panel.csv"),
            "fusion_equity_ls10": os.path.join(args.out_dir, "fusion_equity_ls10.csv"),
        },
    }
    with open(os.path.join(args.out_dir, "diagnostic_summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    print(f"[diag] out_dir -> {args.out_dir}")
    print(f"[diag] 5-group LS ann -> {five['long_short_5_1']['ann_return']:.6f}")
    print(f"[diag] top10 long-only ann -> {top10['top10_long_only']['ann_return']:.6f}")
    print(f"[diag] top10 hedged ann -> {top10['top10_hedged_csi300']['ann_return']:.6f}")
    print(f"[diag] fusion eqw LS10 ann -> {fusion['fused_eqw_ls10']['ann_return']:.6f}")


if __name__ == "__main__":
    main()
