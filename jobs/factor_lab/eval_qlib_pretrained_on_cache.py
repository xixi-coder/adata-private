# -*- coding: utf-8 -*-
from __future__ import annotations
"""
在 adata 本地缓存数据上直接调用 Qlib 预训练 LSTM/GRU 权重，并输出预测效果评估。

说明
----
- 本脚本会用日线 OHLCV(+VWAP) 复现 Alpha360 风格的 360 维输入特征。
- 仅做推理（inference-only），不会重新训练模型。
- 当前支持 d_feat=6 的预训练非 TS 模型，例如：
  - benchmarks/LSTM/model_lstm_csi300.pkl
  - benchmarks/GRU/model_gru_csi300.pkl
"""

import argparse
import json
import os
from datetime import datetime
from typing import Iterable

import numpy as np
import pandas as pd
import torch
import torch.nn as nn
from numpy.lib.stride_tricks import sliding_window_view

from jobs.factor_lab.run_core_15_from_cache import _load_stock_panel


class _LSTMModel(nn.Module):
    """与 Qlib 预训练权重结构对齐的 LSTM 预测头。"""

    def __init__(self, d_feat: int = 6, hidden_size: int = 64, num_layers: int = 2, dropout: float = 0.0):
        super().__init__()
        # RNN 主体：输入是 [batch, seq_len, d_feat]。
        self.rnn = nn.LSTM(
            input_size=d_feat,
            hidden_size=hidden_size,
            num_layers=num_layers,
            batch_first=True,
            dropout=dropout,
        )
        # 输出层：取最后一个时间步隐状态，映射成 1 个分数。
        self.fc_out = nn.Linear(hidden_size, 1)
        self.d_feat = d_feat

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        # 原始 x 是 [batch, d_feat * seq_len] 的平铺向量，先还原成三维。
        x = x.reshape(len(x), self.d_feat, -1)
        # 调整为 RNN 期望的 [batch, seq_len, d_feat]。
        x = x.permute(0, 2, 1)
        # out[:, -1, :] 表示最后时间步的隐状态。
        out, _ = self.rnn(x)
        return self.fc_out(out[:, -1, :]).squeeze()


class _GRUModel(nn.Module):
    """与 Qlib 预训练权重结构对齐的 GRU 预测头。"""

    def __init__(self, d_feat: int = 6, hidden_size: int = 64, num_layers: int = 2, dropout: float = 0.0):
        super().__init__()
        # GRU 结构参数与预训练权重保持一致。
        self.rnn = nn.GRU(
            input_size=d_feat,
            hidden_size=hidden_size,
            num_layers=num_layers,
            batch_first=True,
            dropout=dropout,
        )
        # 回归输出层。
        self.fc_out = nn.Linear(hidden_size, 1)
        self.d_feat = d_feat

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        # 输入形状转换逻辑与 LSTM 分支一致。
        x = x.reshape(len(x), self.d_feat, -1)
        x = x.permute(0, 2, 1)
        out, _ = self.rnn(x)
        return self.fc_out(out[:, -1, :]).squeeze()


def _make_model(model_type: str, model_path: str, device: str) -> nn.Module:
    """按类型构建模型骨架并加载预训练参数。"""
    # 先构建与权重完全匹配的网络结构。
    if model_type == "lstm":
        model = _LSTMModel(d_feat=6, hidden_size=64, num_layers=2, dropout=0.0)
    elif model_type == "gru":
        model = _GRUModel(d_feat=6, hidden_size=64, num_layers=2, dropout=0.0)
    else:
        raise ValueError(f"Unsupported model type: {model_type}")

    # 权重先加载到 CPU，避免设备不一致报错，再整体搬到目标 device。
    state = torch.load(model_path, map_location="cpu")
    # strict=True 确保结构和参数名完全一致，避免静默错配。
    model.load_state_dict(state, strict=True)
    model = model.to(device)
    # 切到 eval，关闭 dropout/bn 的训练行为。
    model.eval()
    return model


def _build_vwap(df: pd.DataFrame) -> pd.Series:
    """优先使用现成 vwap；否则用 amount/volume 估算；再不行退化为 close。"""
    if "vwap" in df.columns:
        return pd.to_numeric(df["vwap"], errors="coerce")
    if "amount" in df.columns and "volume" in df.columns:
        amount = pd.to_numeric(df["amount"], errors="coerce")
        volume = pd.to_numeric(df["volume"], errors="coerce")
        return np.where(volume > 0, amount / volume, np.nan)
    return pd.to_numeric(df["close"], errors="coerce")


def _alpha360_samples_for_stock(stock_df: pd.DataFrame) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """
    Build Alpha360-like features for one stock:
    [CLOSE59..0, OPEN59..0, HIGH59..0, LOW59..0, VWAP59..0, VOLUME59..0], total 360 dims.

    Returns
    -------
    x: float32 [N, 360]
    trade_dates: datetime64[ns] [N]
    ret_1d: float64 [N]       close(t+1)/close(t)-1
    ret_qlib: float64 [N]     close(t+2)/close(t+1)-1 (qlib default label form)
    """
    # 空输入直接返回空数组，方便上游统一拼接处理。
    if stock_df.empty:
        return np.empty((0, 360), dtype=np.float32), np.array([], dtype="datetime64[ns]"), np.array([]), np.array([])

    # 复制后处理，避免污染原始 DataFrame。
    use = stock_df.copy()
    # 统一日期和数值字段类型，非法值转 NaN。
    use["trade_date"] = pd.to_datetime(use["trade_date"], errors="coerce")
    for col in ("open", "high", "low", "close", "volume", "amount"):
        if col in use.columns:
            use[col] = pd.to_numeric(use[col], errors="coerce")
    # 若无 vwap 字段则按 amount/volume 构造。
    use["vwap"] = _build_vwap(use)
    # 每只股票按交易日排序并去重，确保滑窗连续。
    use = use.sort_values("trade_date").drop_duplicates("trade_date")

    # 转成 numpy 便于后续向量化计算。
    close = use["close"].to_numpy(dtype=float)
    open_ = use["open"].to_numpy(dtype=float) if "open" in use.columns else close.copy()
    high = use["high"].to_numpy(dtype=float) if "high" in use.columns else close.copy()
    low = use["low"].to_numpy(dtype=float) if "low" in use.columns else close.copy()
    volume = use["volume"].to_numpy(dtype=float) if "volume" in use.columns else np.zeros_like(close)
    vwap = use["vwap"].to_numpy(dtype=float)
    dates = use["trade_date"].to_numpy(dtype="datetime64[ns]")

    n = len(use)  # 该股票可用交易日数
    # 至少需要 60 日窗口 + 后续 2 天标签，少于 62 天无法构样本。
    if n < 62:
        return np.empty((0, 360), dtype=np.float32), np.array([], dtype="datetime64[ns]"), np.array([]), np.array([])

    # Keep t in [59, n-3] so that both ret_1d and ret_qlib are available.
    row_idx = np.arange(0, n - 61)  # 每个样本对应 1 个 60 日滑窗
    # t 对应“信号日期”，标签使用 t+1、t+2。
    t_close = close[59 : n - 2]
    t_vol = volume[59 : n - 2]
    t_dates = dates[59 : n - 2]

    # 生成每个字段的 60 日滑窗：形状 [N, 60]。
    close_w = sliding_window_view(close, window_shape=60)[row_idx]
    open_w = sliding_window_view(open_, window_shape=60)[row_idx]
    high_w = sliding_window_view(high, window_shape=60)[row_idx]
    low_w = sliding_window_view(low, window_shape=60)[row_idx]
    vwap_w = sliding_window_view(vwap, window_shape=60)[row_idx]
    volume_w = sliding_window_view(volume, window_shape=60)[row_idx]

    # 价格类特征以当日 close 归一化；成交量以当日 volume 归一化。
    denom_price = t_close[:, None]
    denom_vol = (t_vol + 1e-12)[:, None]

    x = np.concatenate(
        [
            close_w / denom_price,
            open_w / denom_price,
            high_w / denom_price,
            low_w / denom_price,
            vwap_w / denom_price,
            volume_w / denom_vol,
        ],
        axis=1,
    )

    # 两种标签口径：
    # ret_1d: close(t+1)/close(t)-1
    # ret_qlib: close(t+2)/close(t+1)-1
    ret_1d = close[60 : n - 1] / close[59 : n - 2] - 1.0
    ret_qlib = close[61:n] / close[60 : n - 1] - 1.0

    # 样本有效性过滤：去除无穷/空值/异常分母。
    valid = (
        np.isfinite(t_close)
        & (t_close > 0)
        & np.isfinite(t_vol)
        & np.isfinite(ret_1d)
        & np.isfinite(ret_qlib)
        & np.all(np.isfinite(x), axis=1)
    )

    return x[valid].astype(np.float32), t_dates[valid], ret_1d[valid], ret_qlib[valid]


def _batched_predict(model: nn.Module, x: np.ndarray, device: str, batch_size: int) -> np.ndarray:
    """按 batch 推理，避免一次性占用过多显存/内存。"""
    if len(x) == 0:
        return np.array([], dtype=float)
    preds: list[np.ndarray] = []
    # 纯推理场景关闭梯度计算，节省显存和时间。
    with torch.no_grad():
        for i in range(0, len(x), batch_size):
            xb = torch.from_numpy(x[i : i + batch_size]).float().to(device)
            out = model(xb).detach().cpu().numpy()
            preds.append(np.atleast_1d(out))
    return np.concatenate(preds, axis=0)


def _daily_ic(df: pd.DataFrame, target_col: str, min_cs: int) -> pd.DataFrame:
    """逐交易日计算截面 IC 与 RankIC。"""
    rows: list[dict] = []
    for dt, cross in df.groupby("trade_date"):
        # 只保留当天 pred 与目标收益均有效的样本。
        cross = cross[["pred", target_col]].replace([np.inf, -np.inf], np.nan).dropna()
        n = len(cross)
        # 截面样本数不足时跳过该日，避免统计噪声过大。
        if n < min_cs:
            continue
        # Pearson 相关：原值 IC；秩相关（通过 rank 后 Pearson）近似 RankIC。
        ic = cross["pred"].corr(cross[target_col], method="pearson")
        rank_ic = cross["pred"].rank(method="average").corr(cross[target_col].rank(method="average"), method="pearson")
        rows.append({"trade_date": dt, "count": n, "ic": ic, "rank_ic": rank_ic})
    out = pd.DataFrame(rows)
    if not out.empty:
        out = out.sort_values("trade_date").reset_index(drop=True)
    return out


def _daily_group_returns(df: pd.DataFrame, target_col: str, n_groups: int, min_cs: int) -> pd.DataFrame:
    """逐日分组回测：按 pred 分箱并计算 top-bottom 多空收益。"""
    rows: list[dict] = []
    for dt, cross in df.groupby("trade_date"):
        cross = cross[["pred", target_col]].replace([np.inf, -np.inf], np.nan).dropna()
        n = len(cross)
        # 分组数越多，对截面样本数要求越高。
        if n < max(min_cs, n_groups * 2):
            continue
        # 先 rank 再 qcut，减少并列值导致的分箱异常。
        rank = cross["pred"].rank(method="first")
        try:
            grp = pd.qcut(rank, q=n_groups, labels=False, duplicates="drop") + 1
        except ValueError:
            continue
        cross = cross.assign(group=grp.to_numpy(dtype=int))
        gr = cross.groupby("group")[target_col].mean()
        if gr.empty or len(gr) < 2:
            continue
        bottom = float(gr.iloc[0])
        top = float(gr.iloc[-1])
        rows.append(
            {
                "trade_date": dt,
                "count": n,
                "g1": bottom,
                "gN": top,
                "long_short": top - bottom,
            }
        )
    out = pd.DataFrame(rows)
    if not out.empty:
        out = out.sort_values("trade_date").reset_index(drop=True)
    return out


def _summary_from_daily_ic(ic_df: pd.DataFrame) -> dict:
    """把 daily IC 序列聚合成均值/波动/IR。"""
    if ic_df.empty:
        return {
            "obs_days": 0,
            "ic_mean": np.nan,
            "ic_std": np.nan,
            "ic_ir": np.nan,
            "rank_ic_mean": np.nan,
            "rank_ic_std": np.nan,
            "rank_ic_ir": np.nan,
        }
    ic_mean = float(ic_df["ic"].mean())
    ic_std = float(ic_df["ic"].std(ddof=0))
    ric_mean = float(ic_df["rank_ic"].mean())
    ric_std = float(ic_df["rank_ic"].std(ddof=0))
    # IR 年化口径按 252 个交易日。
    ic_ir = ic_mean / ic_std * np.sqrt(252) if ic_std > 0 else np.nan
    ric_ir = ric_mean / ric_std * np.sqrt(252) if ric_std > 0 else np.nan
    return {
        "obs_days": int(len(ic_df)),
        "ic_mean": ic_mean,
        "ic_std": ic_std,
        "ic_ir": float(ic_ir) if pd.notna(ic_ir) else np.nan,
        "rank_ic_mean": ric_mean,
        "rank_ic_std": ric_std,
        "rank_ic_ir": float(ric_ir) if pd.notna(ric_ir) else np.nan,
    }


def _summary_from_group(group_df: pd.DataFrame) -> dict:
    """把 daily 多空序列聚合成日均、年化收益与 Sharpe。"""
    if group_df.empty:
        return {
            "obs_days": 0,
            "ls_mean_daily": np.nan,
            "ls_std_daily": np.nan,
            "ls_ann_return": np.nan,
            "ls_sharpe": np.nan,
        }
    ls_mean = float(group_df["long_short"].mean())
    ls_std = float(group_df["long_short"].std(ddof=0))
    ls_ann = ls_mean * 252  # 线性年化（日均收益 * 252）
    ls_sharpe = ls_mean / ls_std * np.sqrt(252) if ls_std > 0 else np.nan
    return {
        "obs_days": int(len(group_df)),
        "ls_mean_daily": ls_mean,
        "ls_std_daily": ls_std,
        "ls_ann_return": float(ls_ann),
        "ls_sharpe": float(ls_sharpe) if pd.notna(ls_sharpe) else np.nan,
    }


def parse_args() -> argparse.Namespace:
    """命令行参数。"""
    parser = argparse.ArgumentParser(description="Evaluate Qlib pretrained LSTM/GRU on adata cache.")
    # 数据范围与样本控制
    parser.add_argument("--cache-file", default="data/cache/full_data_v3_5year.pkl")
    parser.add_argument("--start-date", default="")
    parser.add_argument("--end-date", default="")
    parser.add_argument("--max-stocks", type=int, default=300, help="0 means all stocks in cache.")
    # 模型与标签口径
    parser.add_argument("--model-type", choices=["lstm", "gru"], default="lstm")
    parser.add_argument("--model-path", default="")
    parser.add_argument("--target", choices=["ret_qlib", "ret_1d"], default="ret_qlib")
    # 评估与推理配置
    parser.add_argument("--min-cross-section", type=int, default=30)
    parser.add_argument("--n-groups", type=int, default=10)
    parser.add_argument("--batch-size", type=int, default=4096)
    parser.add_argument("--device", choices=["auto", "cpu", "cuda"], default="auto")
    # 结果输出目录
    parser.add_argument("--out-dir", default="tests/qlib_pretrained_eval")
    return parser.parse_args()


def main() -> None:
    # 0) 解析参数并准备输出目录
    args = parse_args()
    os.makedirs(args.out_dir, exist_ok=True)

    # 1) 确定模型权重路径（若未显式指定则走默认路径）
    model_path = args.model_path
    if not model_path:
        if args.model_type == "lstm":
            model_path = "/Users/xixi/pythonProject/qlib/examples/benchmarks/LSTM/model_lstm_csi300.pkl"
        else:
            model_path = "/Users/xixi/pythonProject/qlib/examples/benchmarks/GRU/model_gru_csi300.pkl"
    # 权重文件必须存在，否则直接报错。
    if not os.path.exists(model_path):
        raise FileNotFoundError(f"Model file not found: {model_path}")

    # 2) 自动选择设备：auto 且可用 CUDA 时走 GPU，否则走 CPU。
    device = "cuda" if (args.device == "auto" and torch.cuda.is_available()) else args.device
    if device == "auto":
        device = "cpu"

    # 3) 读取股票面板数据（已在共享函数中处理日期与样本范围）。
    daily_df = _load_stock_panel(
        cache_file=args.cache_file,
        start_date=args.start_date or None,
        end_date=args.end_date or None,
        max_stocks=args.max_stocks if args.max_stocks > 0 else None,
    )
    # 无数据时停止，避免输出空文件误导。
    if daily_df.empty:
        raise ValueError("No daily rows loaded from cache.")

    # 4) 构建并加载预训练模型。
    model = _make_model(model_type=args.model_type, model_path=model_path, device=device)

    # 5) 按股票逐只构建 Alpha360 样本并推理，最后再跨股票拼接。
    pred_frames: list[pd.DataFrame] = []
    for code, sub in daily_df.groupby("stock_code", sort=False):
        x, dates, ret_1d, ret_qlib = _alpha360_samples_for_stock(sub)
        if len(x) == 0:
            continue
        pred = _batched_predict(model=model, x=x, device=device, batch_size=args.batch_size)
        pred_frames.append(
            pd.DataFrame(
                {
                    "stock_code": code,
                    "trade_date": pd.to_datetime(dates),
                    "pred": pred.astype(float),
                    "ret_1d": ret_1d.astype(float),
                    "ret_qlib": ret_qlib.astype(float),
                }
            )
        )

    # 若所有股票都未产生有效样本（例如区间太短）则报错。
    if not pred_frames:
        raise ValueError("No valid prediction rows generated. Check input cache and date range.")

    # 6) 拼接最终预测面板，并按日期/股票排序。
    pred_df = pd.concat(pred_frames, ignore_index=True).sort_values(["trade_date", "stock_code"]).reset_index(drop=True)
    target_col = args.target

    # 7) 计算逐日 IC 与逐日分组收益。
    ic_df = _daily_ic(pred_df, target_col=target_col, min_cs=args.min_cross_section)
    group_df = _daily_group_returns(
        pred_df, target_col=target_col, n_groups=args.n_groups, min_cs=args.min_cross_section
    )

    # 8) 准备输出路径。
    pred_path = os.path.join(args.out_dir, "predictions.csv")
    ic_path = os.path.join(args.out_dir, "daily_ic.csv")
    group_path = os.path.join(args.out_dir, "daily_group_returns.csv")
    meta_path = os.path.join(args.out_dir, "summary.json")

    # 9) 落盘明细文件。
    pred_df.to_csv(pred_path, index=False, encoding="utf-8-sig")
    ic_df.to_csv(ic_path, index=False, encoding="utf-8-sig")
    group_df.to_csv(group_path, index=False, encoding="utf-8-sig")

    # 10) 组装汇总元数据，便于复盘参数与指标。
    meta = {
        "run_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "input": {
            "cache_file": args.cache_file,
            "start_date": args.start_date,
            "end_date": args.end_date,
            "max_stocks": args.max_stocks,
            "model_type": args.model_type,
            "model_path": model_path,
            "target": target_col,
            "device": device,
        },
        "dataset": {
            "n_stocks_predicted": int(pred_df["stock_code"].nunique()),
            "n_rows_predicted": int(len(pred_df)),
            "start_date": pd.Timestamp(pred_df["trade_date"].min()).strftime("%Y-%m-%d"),
            "end_date": pd.Timestamp(pred_df["trade_date"].max()).strftime("%Y-%m-%d"),
        },
        "metrics": {
            "ic": _summary_from_daily_ic(ic_df),
            "group_long_short": _summary_from_group(group_df),
        },
        "outputs": {
            "predictions": pred_path,
            "daily_ic": ic_path,
            "daily_group_returns": group_path,
        },
    }
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)

    # 11) 控制台输出关键结果路径与核心指标。
    print(f"[eval] predictions -> {pred_path}")
    print(f"[eval] daily_ic -> {ic_path}")
    print(f"[eval] daily_group_returns -> {group_path}")
    print(f"[eval] summary -> {meta_path}")
    print(f"[eval] ic_mean={meta['metrics']['ic']['ic_mean']:.6f} rank_ic_mean={meta['metrics']['ic']['rank_ic_mean']:.6f}")
    print(
        f"[eval] long_short_daily={meta['metrics']['group_long_short']['ls_mean_daily']:.6f} "
        f"ann={meta['metrics']['group_long_short']['ls_ann_return']:.6f}"
    )


if __name__ == "__main__":
    main()
