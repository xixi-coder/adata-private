# -*- coding: utf-8 -*-
import datetime
import json
import os
import pickle
from zoneinfo import ZoneInfo

os.environ.setdefault("MPLCONFIGDIR", "/tmp/matplotlib")

import matplotlib
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from typing import Optional


matplotlib.use("Agg")
plt.rcParams["font.sans-serif"] = ["Arial Unicode MS", "SimHei"]
plt.rcParams["axes.unicode_minus"] = False


class ThreeDimResonanceStrategy:
    """
    三维共振策略（日线回测）

    买入：形态共振 + 指标共振 + 资金共振 同时满足
    卖出：止损 + 趋势/资金转弱 + 移动止盈 + 持仓时限
    """

    def __init__(
        self,
        initial_capital=1_000_000,
        max_positions=8,
        universe_size=1800,
        max_position_weight=0.20,
        buy_fee_rate=0.0003,
        sell_fee_rate=0.0013,
        stop_loss_pct=0.06,
        trailing_arm_pct=0.12,
        trailing_drawdown_pct=0.06,
        max_hold_days=20,
        breakout_lookback=40,
        double_bottom_lookback=55,
        platform_base_days=20,
        breakout_confirm_pct=0.008,
        breakout_volume_ratio=1.5,
        platform_max_range_pct=0.18,
        breakout_close_pos_min=0.55,
        breakout_max_amount_ratio=6.5,
        min_gain_20d=0.02,
        max_gain_20d=0.45,
    ):
        self.initial_capital = float(initial_capital)
        self.cash = float(initial_capital)
        self.max_positions = int(max_positions)
        self.universe_size = int(universe_size)
        self.max_position_weight = float(max_position_weight)
        self.buy_fee_rate = float(buy_fee_rate)
        self.sell_fee_rate = float(sell_fee_rate)
        self.stop_loss_pct = float(stop_loss_pct)
        self.trailing_arm_pct = float(trailing_arm_pct)
        self.trailing_drawdown_pct = float(trailing_drawdown_pct)
        self.max_hold_days = int(max_hold_days)
        self.breakout_lookback = int(breakout_lookback)
        self.double_bottom_lookback = int(double_bottom_lookback)
        self.platform_base_days = int(platform_base_days)
        self.breakout_confirm_pct = float(breakout_confirm_pct)
        self.breakout_volume_ratio = float(breakout_volume_ratio)
        self.platform_max_range_pct = float(platform_max_range_pct)
        self.breakout_close_pos_min = float(breakout_close_pos_min)
        self.breakout_max_amount_ratio = float(breakout_max_amount_ratio)
        self.min_gain_20d = float(min_gain_20d)
        self.max_gain_20d = float(max_gain_20d)

        base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        self.cache_dir = os.path.join(base_dir, "data", "cache")
        self.full_cache_file = os.path.join(self.cache_dir, "full_data_v2_processed.pkl")
        self.benchmark_file = os.path.join(self.cache_dir, "benchmark_000300.csv")

        self.stock_data = {}
        self.benchmark_df = None
        self.positions = {}
        self.completed_trades = []
        self.equity_curve = []
        self.pending_entries = []

    @staticmethod
    def _is_tradable_a_share_code(code: str) -> bool:
        if not (isinstance(code, str) and len(code) == 6 and code.isdigit()):
            return False
        # 扩大股票范围：主板 + 创业板 + 科创板，先不纳入北交所和 B 股。
        return code.startswith(("600", "601", "603", "605", "000", "001", "002", "003", "300", "301", "688"))

    @staticmethod
    def _now_shanghai() -> datetime.datetime:
        return datetime.datetime.now(ZoneInfo("Asia/Shanghai"))

    def _clock_based_target_date(self, today_str: str) -> str:
        today = pd.to_datetime(today_str, errors="coerce")
        if pd.isna(today):
            return today_str
        if today.weekday() >= 5:
            return (today - pd.tseries.offsets.BDay(1)).strftime("%Y-%m-%d")
        return today.strftime("%Y-%m-%d")

    def _fallback_target_date_from_benchmark(self, today_str: str) -> str:
        candidates = [self._clock_based_target_date(today_str)]
        if os.path.exists(self.benchmark_file):
            try:
                bench = pd.read_csv(self.benchmark_file)
                if not bench.empty and "trade_date" in bench.columns:
                    bench["trade_date"] = bench["trade_date"].astype(str)
                    valid_dates = sorted(d for d in bench["trade_date"].tolist() if d <= today_str)
                    if valid_dates:
                        candidates.append(valid_dates[-1])
            except Exception:
                pass
        return max(candidates) if candidates else today_str

    def _incremental_target_date(self, _adata_module=None) -> str:
        # 为提升 CI 稳定性，目标交易日仅基于本地时钟 + 本地基准推断，
        # 不再依赖在线交易日历接口。
        today_str = self._now_shanghai().strftime("%Y-%m-%d")
        return self._fallback_target_date_from_benchmark(today_str)

    def load_data(self):
        if not os.path.exists(self.full_cache_file):
            raise FileNotFoundError(f"未找到缓存: {self.full_cache_file}")
        if not os.path.exists(self.benchmark_file):
            raise FileNotFoundError(f"未找到基准文件: {self.benchmark_file}")

        print("加载全量股票缓存...")
        with open(self.full_cache_file, "rb") as f:
            cache = pickle.load(f)
        raw_stock = cache["stock"]

        # 日常建议任务固定使用云端缓存中的本地数据，不在此处执行在线增量抓取。
        print("使用云端缓存的本地数据，不执行在线增量更新。")

        print("预处理股票数据并构建股票池...")
        processed = {}
        liquid_scores = []

        for code, df in raw_stock.items():
            if not self._is_tradable_a_share_code(code):
                continue
            if df is None or df.empty or len(df) < 180:
                continue

            need_cols = ["open", "close", "high", "low", "amount", "volume", "pre_close", "trade_time"]
            if any(c not in df.columns for c in need_cols):
                continue

            dfx = df[need_cols].copy()
            for col in ["open", "close", "high", "low", "amount", "volume", "pre_close"]:
                dfx[col] = pd.to_numeric(dfx[col], errors="coerce")

            dfx["trade_date"] = pd.to_datetime(dfx["trade_time"]).dt.strftime("%Y-%m-%d")
            dfx = dfx.sort_values("trade_date").drop_duplicates("trade_date").set_index("trade_date")

            dfx["pct_change"] = (dfx["close"] / dfx["pre_close"] - 1.0) * 100.0
            dfx["ma20"] = dfx["close"].rolling(20).mean()
            dfx["ma60"] = dfx["close"].rolling(60).mean()
            dfx["ma120"] = dfx["close"].rolling(120).mean()
            dfx["amt_ma5"] = dfx["amount"].rolling(5).mean()
            dfx["amt_ma20"] = dfx["amount"].rolling(20).mean()
            dfx["vol_ma20"] = dfx["volume"].rolling(20).mean()

            dfx["ema12"] = dfx["close"].ewm(span=12, adjust=False).mean()
            dfx["ema26"] = dfx["close"].ewm(span=26, adjust=False).mean()
            dfx["dif"] = dfx["ema12"] - dfx["ema26"]
            dfx["dea"] = dfx["dif"].ewm(span=9, adjust=False).mean()
            dfx["macd_hist"] = (dfx["dif"] - dfx["dea"]) * 2.0
            dfx["macd_hist_delta"] = dfx["macd_hist"].diff()
            delta = dfx["close"].diff()
            gain = delta.clip(lower=0.0)
            loss = (-delta.clip(upper=0.0)).replace(0.0, np.nan)
            rs = gain.rolling(6).mean() / loss.rolling(6).mean()
            dfx["rsi6"] = 100.0 - (100.0 / (1.0 + rs))
            dfx["rsi6"] = dfx["rsi6"].fillna(50.0)
            dfx["gain_5d"] = dfx["close"] / dfx["close"].shift(5) - 1.0
            dfx["gain_20d"] = dfx["close"] / dfx["close"].shift(20) - 1.0
            dfx["ma60_delta_5"] = dfx["ma60"] - dfx["ma60"].shift(5)
            range_spread = (dfx["high"] - dfx["low"]).replace(0, np.nan)
            dfx["close_pos"] = ((dfx["close"] - dfx["low"]) / range_spread).clip(lower=0.0, upper=1.0).fillna(0.5)
            dfx["upper_shadow_pct"] = ((dfx["high"] - np.maximum(dfx["open"], dfx["close"])) / dfx["pre_close"]).clip(lower=0.0)
            dfx["amount_ratio20"] = dfx["amount"] / dfx["amt_ma20"]

            # 资金代理：CMF + 净额方向
            spread = (dfx["high"] - dfx["low"]).replace(0, np.nan)
            dfx["mfm"] = ((dfx["close"] - dfx["low"]) - (dfx["high"] - dfx["close"])) / spread
            dfx["mfm"] = dfx["mfm"].fillna(0.0).clip(-1.0, 1.0)
            dfx["mf_amount"] = dfx["mfm"] * dfx["amount"]
            dfx["cmf5"] = dfx["mf_amount"].rolling(5).sum() / dfx["amount"].rolling(5).sum()
            dfx["cmf20"] = dfx["mf_amount"].rolling(20).sum() / dfx["amount"].rolling(20).sum()
            dfx["signed_amount"] = np.sign(dfx["close"] - dfx["pre_close"]) * dfx["amount"]
            dfx["net_amt3"] = dfx["signed_amount"].rolling(3).sum()
            dfx["net_amt5"] = dfx["signed_amount"].rolling(5).sum()
            dfx["net_amt10"] = dfx["signed_amount"].rolling(10).sum()
            up_amt = np.where(dfx["close"] > dfx["pre_close"], dfx["amount"], 0.0)
            dfx["up_amt_ratio5"] = pd.Series(up_amt, index=dfx.index).rolling(5).sum() / dfx["amount"].rolling(5).sum()
            dfx["up_days5"] = (dfx["close"] > dfx["pre_close"]).astype(int).rolling(5).sum()

            # 形态基础：平台突破 + 双底突破
            dfx["prior_high_break"] = dfx["high"].shift(1).rolling(self.breakout_lookback).max()
            dfx["platform_high"] = dfx["high"].shift(1).rolling(self.platform_base_days).max()
            dfx["platform_low"] = dfx["low"].shift(1).rolling(self.platform_base_days).min()
            dfx["local_min"] = (dfx["low"] < dfx["low"].shift(1)) & (dfx["low"] <= dfx["low"].shift(-1))

            dfx = dfx.dropna(
                subset=[
                    "close",
                    "open",
                    "high",
                    "low",
                    "pre_close",
                    "ma20",
                    "ma60",
                    "ma120",
                    "amt_ma20",
                    "dif",
                    "dea",
                    "macd_hist",
                    "cmf5",
                    "cmf20",
                    "rsi6",
                    "gain_5d",
                    "gain_20d",
                    "ma60_delta_5",
                    "up_amt_ratio5",
                    "up_days5",
                    "prior_high_break",
                    "platform_high",
                    "platform_low",
                    "close_pos",
                    "amount_ratio20",
                ]
            )
            if dfx.empty:
                continue

            # 用近60日平均成交额做流动性打分
            liq_score = float(dfx["amount"].rolling(60).mean().iloc[-1]) if len(dfx) >= 60 else float(dfx["amount"].mean())
            processed[code] = dfx
            liquid_scores.append((code, liq_score))

        liquid_scores = sorted(liquid_scores, key=lambda x: x[1], reverse=True)
        selected_codes = {code for code, _ in liquid_scores[: self.universe_size]}
        self.stock_data = {code: df for code, df in processed.items() if code in selected_codes}
        print(f"股票池构建完成: {len(self.stock_data)} 只")

        bench = pd.read_csv(self.benchmark_file)
        bench["trade_date"] = pd.to_datetime(bench["trade_date"]).dt.strftime("%Y-%m-%d")
        bench = bench.sort_values("trade_date").drop_duplicates("trade_date")
        bench["close"] = pd.to_numeric(bench["close"], errors="coerce")
        bench["change_pct"] = pd.to_numeric(bench["change_pct"], errors="coerce")
        bench["ma20"] = bench["close"].rolling(20).mean()
        bench["ma60"] = bench["close"].rolling(60).mean()
        bench = bench.dropna(subset=["close", "ma20", "ma60"])\
            .set_index("trade_date")
        self.benchmark_df = bench
        print(f"基准数据加载完成: {self.benchmark_df.index.min()} ~ {self.benchmark_df.index.max()}")

    def _market_ok(self, date_str: str) -> bool:
        if date_str not in self.benchmark_df.index:
            return False
        idx = self.benchmark_df.index.get_loc(date_str)
        if isinstance(idx, slice):
            idx = idx.stop - 1
        if idx < 5:
            return False
        row = self.benchmark_df.iloc[idx]
        prev5 = self.benchmark_df.iloc[idx - 5]
        return bool(
            (row["close"] > row["ma20"] > row["ma60"])
            and (row["ma20"] >= prev5["ma20"] * 0.995)
            and (row["close"] >= prev5["close"] * 0.97)
            and (row["change_pct"] > -1.2)
        )

    def _breakout_bar_ok(self, row: pd.Series, breakout_level: float) -> bool:
        if breakout_level <= 0:
            return False
        amount_ratio = float(row.get("amount_ratio20", 0.0))
        return bool(
            (row["close"] > breakout_level * (1.0 + self.breakout_confirm_pct))
            and (row["close"] > row["open"])
            and (row["close_pos"] >= self.breakout_close_pos_min)
            and (row["upper_shadow_pct"] <= 0.05)
            and (row["amount"] > row["amt_ma20"] * self.breakout_volume_ratio)
            and (amount_ratio <= self.breakout_max_amount_ratio)
        )

    def _double_bottom_breakout(self, df: pd.DataFrame, idx: int) -> bool:
        # 仅用历史窗口检测，避免未来函数
        start = max(0, idx - self.double_bottom_lookback + 1)
        window = df.iloc[start : idx + 1]
        troughs = window[window["local_min"]]
        if len(troughs) < 2:
            return False

        # 取最近两个低点
        t1 = troughs.iloc[-2]
        t2 = troughs.iloc[-1]
        t1_idx = df.index.get_loc(t1.name)
        t2_idx = df.index.get_loc(t2.name)
        gap = t2_idx - t1_idx
        if gap < 8 or gap > 30:
            return False

        low_diff = abs(t1["low"] - t2["low"]) / max(float(t1["low"]), 1e-9)
        if low_diff > 0.04:
            return False

        mid = df.iloc[t1_idx : t2_idx + 1]
        neckline = float(mid["high"].max())
        low_base = float(min(t1["low"], t2["low"]))
        rebound = neckline / max(low_base, 1e-9) - 1.0
        if rebound < 0.10:
            return False

        row = df.iloc[idx]
        second_bottom_nearby = (idx - t2_idx) <= 15
        breakout_level = neckline
        # 当前确认突破颈线，同时放量，避免形态拖得过久失真。
        return bool(
            second_bottom_nearby
            and self._breakout_bar_ok(row, breakout_level)
            and (row["close"] > row["ma20"])
        )

    def _platform_breakout_signal(self, df: pd.DataFrame, idx: int) -> bool:
        row = df.iloc[idx]
        platform_high = float(row["platform_high"])
        platform_low = float(row["platform_low"])
        if platform_high <= 0 or platform_low <= 0:
            return False

        platform_range = platform_high / platform_low - 1.0
        if platform_range > self.platform_max_range_pct:
            return False

        if row["gain_5d"] > 0.15:
            return False
        if row["gain_20d"] < self.min_gain_20d or row["gain_20d"] > self.max_gain_20d:
            return False

        if idx < self.platform_base_days:
            return False

        base_window = df.iloc[idx - self.platform_base_days : idx]
        base_hits = int((base_window["high"] >= platform_high * 0.985).sum())
        if base_hits < 2:
            return False

        return bool(
            self._breakout_bar_ok(row, platform_high)
        )

    def _evaluate_entry_components(self, df: pd.DataFrame, date_str: str) -> dict:
        idx = df.index.get_loc(date_str)
        if isinstance(idx, slice):
            idx = idx.stop - 1
        row = df.iloc[idx]

        # ---------- 形态共振 ----------
        platform_breakout = self._platform_breakout_signal(df, idx)
        double_bottom = self._double_bottom_breakout(df, idx)
        shape_ok = platform_breakout or double_bottom

        # ---------- 指标共振 ----------
        trend_ok = False
        if idx >= 5:
            ma20_up = bool(row["ma20"] > df.iloc[idx - 1]["ma20"])
            ma60_stable = bool(row["ma60"] >= df.iloc[idx - 5]["ma60"] * 0.99)
            trend_ok = bool(
                (row["close"] > row["ma20"] > row["ma60"])
                and ma60_stable
                and ma20_up
            )
        macd_support = bool(
            (row["dif"] > row["dea"])
            and (
                (row["macd_hist"] > 0)
                or ((row["macd_hist"] > -0.03) and (row["macd_hist"] >= df.iloc[idx - 1]["macd_hist"]))
            )
        ) if idx > 0 else False
        momentum_ok = bool(
            macd_support
            and (50.0 <= row["rsi6"] <= 80.0)
            and (self.min_gain_20d <= row["gain_20d"] <= self.max_gain_20d)
        )
        indicator_ok = trend_ok and momentum_ok

        # ---------- 资金共振 ----------
        capital_ok = bool(
            (row["cmf20"] > 0.03)
            and (row["cmf5"] > max(row["cmf20"] + 0.02, 0.05))
            and (row["net_amt5"] > 0)
            and (row["net_amt10"] > 0)
            and (row["up_amt_ratio5"] >= 0.60)
            and (row["up_days5"] >= 3)
        )

        return {
            "shape_ok": shape_ok,
            "indicator_ok": indicator_ok,
            "capital_ok": capital_ok,
            "platform_breakout": platform_breakout,
            "double_bottom": double_bottom,
            "trend_ok": trend_ok,
            "momentum_ok": momentum_ok,
            "three_dim_ok": bool(shape_ok and indicator_ok and capital_ok),
        }

    def _is_entry_signal(self, code: str, df: pd.DataFrame, date_str: str) -> tuple[bool, dict]:
        meta = self._evaluate_entry_components(df, date_str)
        return bool(meta["three_dim_ok"]), meta

    def _signal_score(self, row: pd.Series, meta: dict) -> float:
        amount_ratio = row["amount"] / row["amt_ma20"] if row["amt_ma20"] > 0 else 0.0
        breakout_anchor = max(float(row.get("prior_high_break", 0.0)), float(row.get("platform_high", 0.0)))
        breakout_strength = row["close"] / breakout_anchor - 1.0 if breakout_anchor > 0 else 0.0
        shape_bonus = 1.0 if meta.get("double_bottom") else 0.0
        amount_penalty = max(amount_ratio - 3.2, 0.0)
        return (
            breakout_strength * 140.0
            + row["macd_hist"] * 2.2
            + row["cmf5"] * 40.0
            + row["close_pos"] * 4.0
            + amount_ratio * 1.0
            + row["pct_change"] * 0.3
            - row["upper_shadow_pct"] * 120.0
            - amount_penalty * 3.0
            + shape_bonus
        )

    def analyze_signal_funnel(self, start_date: str, end_date: str) -> dict:
        if self.benchmark_df is None or not self.stock_data:
            raise RuntimeError("请先调用 load_data()")

        all_dates = [d for d in self.benchmark_df.index if start_date <= d <= end_date]
        market_dates = [d for d in all_dates if self._market_ok(d)]
        stage_counts = {
            "样本交易日": len(all_dates),
            "市场允许开仓日": len(market_dates),
            "扫描样本数": 0,
            "形态通过": 0,
            "指标通过": 0,
            "资金通过": 0,
            "形态+指标": 0,
            "形态+资金": 0,
            "指标+资金": 0,
            "三维共振": 0,
            "平台突破": 0,
            "双底突破": 0,
        }
        failure_counts = {
            "缺形态": 0,
            "缺指标": 0,
            "缺资金": 0,
            "形态后卡指标": 0,
            "形态后卡资金": 0,
            "指标后卡形态": 0,
            "指标后卡资金": 0,
            "资金后卡形态": 0,
            "资金后卡指标": 0,
            "仅缺资金": 0,
            "仅缺指标": 0,
            "仅缺形态": 0,
        }

        for date_str in market_dates:
            for code, df in self.stock_data.items():
                if date_str not in df.index:
                    continue

                meta = self._evaluate_entry_components(df, date_str)
                shape_ok = bool(meta["shape_ok"])
                indicator_ok = bool(meta["indicator_ok"])
                capital_ok = bool(meta["capital_ok"])

                stage_counts["扫描样本数"] += 1
                stage_counts["平台突破"] += int(bool(meta["platform_breakout"]))
                stage_counts["双底突破"] += int(bool(meta["double_bottom"]))
                stage_counts["形态通过"] += int(shape_ok)
                stage_counts["指标通过"] += int(indicator_ok)
                stage_counts["资金通过"] += int(capital_ok)
                stage_counts["形态+指标"] += int(shape_ok and indicator_ok)
                stage_counts["形态+资金"] += int(shape_ok and capital_ok)
                stage_counts["指标+资金"] += int(indicator_ok and capital_ok)
                stage_counts["三维共振"] += int(shape_ok and indicator_ok and capital_ok)

                if not shape_ok:
                    failure_counts["缺形态"] += 1
                if not indicator_ok:
                    failure_counts["缺指标"] += 1
                if not capital_ok:
                    failure_counts["缺资金"] += 1
                if shape_ok and not indicator_ok:
                    failure_counts["形态后卡指标"] += 1
                if shape_ok and not capital_ok:
                    failure_counts["形态后卡资金"] += 1
                if indicator_ok and not shape_ok:
                    failure_counts["指标后卡形态"] += 1
                if indicator_ok and not capital_ok:
                    failure_counts["指标后卡资金"] += 1
                if capital_ok and not shape_ok:
                    failure_counts["资金后卡形态"] += 1
                if capital_ok and not indicator_ok:
                    failure_counts["资金后卡指标"] += 1
                if shape_ok and indicator_ok and not capital_ok:
                    failure_counts["仅缺资金"] += 1
                if shape_ok and capital_ok and not indicator_ok:
                    failure_counts["仅缺指标"] += 1
                if indicator_ok and capital_ok and not shape_ok:
                    failure_counts["仅缺形态"] += 1

        scan_total = max(stage_counts["扫描样本数"], 1)
        pass_rates = {
            "形态通过率(%)": round(stage_counts["形态通过"] / scan_total * 100.0, 3),
            "指标通过率(%)": round(stage_counts["指标通过"] / scan_total * 100.0, 3),
            "资金通过率(%)": round(stage_counts["资金通过"] / scan_total * 100.0, 3),
            "形态+指标通过率(%)": round(stage_counts["形态+指标"] / scan_total * 100.0, 3),
            "形态+资金通过率(%)": round(stage_counts["形态+资金"] / scan_total * 100.0, 3),
            "指标+资金通过率(%)": round(stage_counts["指标+资金"] / scan_total * 100.0, 3),
            "三维共振通过率(%)": round(stage_counts["三维共振"] / scan_total * 100.0, 4),
        }

        return {
            "阶段计数": stage_counts,
            "通过率": pass_rates,
            "失败分布": failure_counts,
        }

    def _current_total_equity(self, date_str: str) -> float:
        market_value = 0.0
        for code, pos in self.positions.items():
            df = self.stock_data.get(code)
            if df is None or date_str not in df.index:
                market_value += pos["shares"] * pos["last_price"]
                continue
            last_price = float(df.loc[date_str, "close"])
            pos["last_price"] = last_price
            market_value += pos["shares"] * last_price
        return self.cash + market_value

    def _close_position(self, code: str, date_str: str, price: float, reason: str):
        pos = self.positions[code]
        gross = pos["shares"] * price
        sell_fee = gross * self.sell_fee_rate
        net = gross - sell_fee
        self.cash += net

        pnl = net - pos["cost"]
        pnl_pct = pnl / pos["cost"] if pos["cost"] > 0 else 0.0

        self.completed_trades.append(
            {
                "股票代码": code,
                "信号日期": pos.get("signal_date", pos["buy_date"]),
                "买入日期": pos["buy_date"],
                "卖出日期": date_str,
                "买入价": round(pos["buy_price"], 3),
                "卖出价": round(price, 3),
                "股数": int(pos["shares"]),
                "持仓天数": int(pos["holding_days"]),
                "盈利金额": round(pnl, 2),
                "盈利率(%)": round(pnl_pct * 100.0, 2),
                "平仓原因": self._label_exit_reason(reason),
                "入场形态": self._label_entry_shape(pos.get("entry_shape", "")),
            }
        )
        del self.positions[code]

    def _exit_reason(self, code: str, df: pd.DataFrame, idx: int, pos: dict) -> Optional[str]:
        row = df.iloc[idx]
        close = float(row["close"])

        # 1) 固定止损
        if close <= pos["buy_price"] * (1.0 - self.stop_loss_pct):
            return "stop_loss"

        # 2) 移动止盈：先触发浮盈阈值，再按回撤卖出
        if close >= pos["buy_price"] * (1.0 + self.trailing_arm_pct):
            pos["trailing_armed"] = True
        if pos.get("trailing_armed", False) and pos["max_close"] > 0:
            retrace = 1.0 - close / pos["max_close"]
            if retrace >= self.trailing_drawdown_pct and close > pos["buy_price"] * 1.02:
                return "trailing_take_profit"

        prev_row = df.iloc[idx - 1] if idx > 0 else row

        # 3) 趋势走弱：MA20跌破 + MACD连续转负
        trend_break = bool((close < row["ma20"]) and (row["macd_hist"] < 0) and (prev_row["macd_hist"] < 0))
        if trend_break:
            return "trend_break"

        # 4) 资金转弱：短中期资金同时转负 + 价格失守MA20
        fund_out = bool((row["cmf5"] < 0) and (row["net_amt3"] < 0) and (row["cmf20"] < 0) and (close < row["ma20"]))
        if fund_out:
            return "capital_outflow"

        # 5) 时间止盈/止损：超过持有上限退出
        if pos["holding_days"] >= self.max_hold_days:
            return "timeout"

        return None

    def run_backtest(self, start_date: str, end_date: str):
        if self.benchmark_df is None or not self.stock_data:
            raise RuntimeError("请先调用 load_data()")

        all_dates = [d for d in self.benchmark_df.index if start_date <= d <= end_date]
        if not all_dates:
            raise ValueError(f"回测区间无有效交易日: {start_date} ~ {end_date}")

        print(f"开始回测: {all_dates[0]} ~ {all_dates[-1]} | 股票池={len(self.stock_data)}")
        self.pending_entries = []

        for date_str in all_dates:
            to_exit = []
            for code, pos in list(self.positions.items()):
                df = self.stock_data.get(code)
                if df is None or date_str not in df.index:
                    continue

                idx = df.index.get_loc(date_str)
                if isinstance(idx, slice):
                    idx = idx.stop - 1
                row = df.iloc[idx]

                pos["holding_days"] += 1
                pos["last_price"] = float(row["close"])
                pos["max_close"] = max(pos["max_close"], pos["last_price"])

                # T+1：买入当天不卖
                if pos["holding_days"] < 1:
                    continue

                reason = self._exit_reason(code, df, idx, pos)
                if reason:
                    to_exit.append((code, float(row["close"]), reason))

            for code, price, reason in to_exit:
                self._close_position(code, date_str, price, reason)

            # 执行上一交易日生成的买单，使用当日开盘价成交。
            today_orders = sorted(self.pending_entries, key=lambda x: x["score"], reverse=True)
            self.pending_entries = []
            if today_orders and len(self.positions) < self.max_positions:
                for order in today_orders:
                    if len(self.positions) >= self.max_positions:
                        break

                    code = order["code"]
                    if code in self.positions:
                        continue

                    df = self.stock_data.get(code)
                    if df is None or date_str not in df.index:
                        continue

                    row = df.loc[date_str]
                    buy_price = float(row["open"])
                    if not np.isfinite(buy_price) or buy_price <= 0:
                        continue

                    slots_left = self.max_positions - len(self.positions)
                    total_equity = self._current_total_equity(date_str)
                    budget = min(self.cash / max(slots_left, 1), total_equity * self.max_position_weight)
                    shares = int(budget / buy_price / 100) * 100
                    if shares < 100:
                        continue

                    gross = shares * buy_price
                    buy_fee = gross * self.buy_fee_rate
                    cost = gross + buy_fee
                    if cost > self.cash:
                        continue

                    self.cash -= cost
                    self.positions[code] = {
                        "buy_date": date_str,
                        "buy_price": buy_price,
                        "shares": int(shares),
                        "cost": float(cost),
                        "holding_days": 0,
                        "max_close": buy_price,
                        "last_price": buy_price,
                        "trailing_armed": False,
                        "entry_shape": order["entry_shape"],
                        "signal_date": order["signal_date"],
                    }

            # 收盘后识别三维共振，挂到下一交易日执行。
            if self._market_ok(date_str) and len(self.positions) < self.max_positions:
                candidates = []
                for code, df in self.stock_data.items():
                    if code in self.positions or date_str not in df.index:
                        continue

                    signal_ok, meta = self._is_entry_signal(code, df, date_str)
                    if not signal_ok:
                        continue

                    row = df.loc[date_str]
                    score = self._signal_score(row, meta)
                    candidates.append(
                        {
                            "code": code,
                            "score": score,
                            "signal_date": date_str,
                            "entry_shape": self._label_entry_shape(
                                "double_bottom" if meta.get("double_bottom") else "platform_breakout"
                            ),
                        }
                    )

                candidates.sort(key=lambda x: x["score"], reverse=True)
                self.pending_entries = candidates[: self.max_positions * 3]

            total = self._current_total_equity(date_str)
            bench_close = float(self.benchmark_df.loc[date_str, "close"])
            if not hasattr(self, "bench_base"):
                self.bench_base = bench_close
            self.equity_curve.append(
                {
                    "日期": date_str,
                    "总资产": total,
                    "基准资产": (bench_close / self.bench_base) * self.initial_capital,
                    "现金": self.cash,
                    "持仓数量": len(self.positions),
                }
            )

        # 回测结束日强制平仓
        last_date = all_dates[-1]
        for code in list(self.positions.keys()):
            df = self.stock_data[code]
            price = float(df.loc[last_date, "close"]) if last_date in df.index else self.positions[code]["last_price"]
            self._close_position(code, last_date, price, "end_of_test")

        print(f"回测结束. 完成交易: {len(self.completed_trades)}")

    def _compute_metrics(self) -> dict:
        eq = pd.DataFrame(self.equity_curve)
        eq["日期"] = pd.to_datetime(eq["日期"])
        eq = eq.sort_values("日期")
        eq["ret"] = eq["总资产"].pct_change().fillna(0.0)
        eq["bench_ret"] = eq["基准资产"].pct_change().fillna(0.0)

        total_return = eq["总资产"].iloc[-1] / self.initial_capital - 1.0
        bench_return = eq["基准资产"].iloc[-1] / self.initial_capital - 1.0
        annual_ret = (1.0 + total_return) ** (252.0 / max(len(eq), 1)) - 1.0
        annual_vol = eq["ret"].std(ddof=0) * np.sqrt(252.0)
        sharpe = (eq["ret"].mean() / eq["ret"].std(ddof=0) * np.sqrt(252.0)) if eq["ret"].std(ddof=0) > 0 else 0.0

        nav = eq["总资产"] / self.initial_capital
        drawdown = nav / nav.cummax() - 1.0
        max_drawdown = float(drawdown.min())

        trades = pd.DataFrame(self.completed_trades)
        if trades.empty:
            win_rate = 0.0
            avg_profit = 0.0
            profit_factor = 0.0
            avg_hold = 0.0
        else:
            win_rate = float((trades["盈利金额"] > 0).mean())
            avg_profit = float(trades["盈利率(%)"].mean())
            gain = float(trades.loc[trades["盈利金额"] > 0, "盈利金额"].sum())
            loss = float(-trades.loc[trades["盈利金额"] < 0, "盈利金额"].sum())
            profit_factor = gain / loss if loss > 0 else 0.0
            avg_hold = float(trades["持仓天数"].mean())

        return {
            "初始资金": round(self.initial_capital, 2),
            "期末总资产": round(float(eq["总资产"].iloc[-1]), 2),
            "策略总收益率(%)": round(total_return * 100.0, 2),
            "基准总收益率(%)": round(bench_return * 100.0, 2),
            "超额收益率(%)": round((total_return - bench_return) * 100.0, 2),
            "年化收益率(%)": round(annual_ret * 100.0, 2),
            "年化波动率(%)": round(annual_vol * 100.0, 2),
            "夏普比率": round(float(sharpe), 3),
            "最大回撤(%)": round(max_drawdown * 100.0, 2),
            "交易总次数": int(len(trades)),
            "交易胜率(%)": round(win_rate * 100.0, 2),
            "平均每次收益(%)": round(avg_profit, 2),
            "盈亏比": round(profit_factor, 3),
            "平均持仓天数": round(avg_hold, 2),
        }

    @staticmethod
    def _first_existing_column(df: pd.DataFrame, candidates):
        for col in candidates:
            if col in df.columns:
                return col
        raise KeyError(f"未找到可用列，候选列: {candidates}")

    @staticmethod
    def _label_exit_reason(reason: str) -> str:
        reason_map = {
            "stop_loss": "固定止损",
            "trailing_take_profit": "移动止盈",
            "trend_break": "趋势走弱",
            "capital_outflow": "资金流出",
            "timeout": "持仓到期",
            "end_of_test": "回测结束平仓",
        }
        return reason_map.get(reason, reason)

    @staticmethod
    def _label_entry_shape(shape: str) -> str:
        shape_map = {
            "platform_breakout": "平台突破",
            "double_bottom": "双底突破",
        }
        return shape_map.get(shape, shape)

    def save_outputs(self, out_dir: str):
        os.makedirs(out_dir, exist_ok=True)

        eq_df = pd.DataFrame(self.equity_curve)
        if not eq_df.empty:
            eq_date_col = self._first_existing_column(eq_df, ["日期", "date"])
            eq_df = eq_df.sort_values(eq_date_col)
        eq_path = os.path.join(out_dir, "three_dim_equity_curve.csv")
        eq_df.to_csv(eq_path, index=False, encoding="utf-8-sig")

        trade_df = pd.DataFrame(self.completed_trades)
        if not trade_df.empty:
            buy_date_col = self._first_existing_column(trade_df, ["买入日期", "buy_date"])
            code_col = self._first_existing_column(trade_df, ["股票代码", "code"])
            trade_df = trade_df.sort_values([buy_date_col, code_col])
        trade_path = os.path.join(out_dir, "three_dim_trade_log.csv")
        trade_df.to_csv(trade_path, index=False, encoding="utf-8-sig")

        metrics = self._compute_metrics()
        metrics_path = os.path.join(out_dir, "three_dim_metrics.json")
        with open(metrics_path, "w", encoding="utf-8") as f:
            json.dump(metrics, f, ensure_ascii=False, indent=2)

        self._plot_report(eq_df, out_dir)

        print("\n关键指标:")
        for k, v in metrics.items():
            print(f"- {k}: {v}")
        print(f"\n输出文件:\n- {eq_path}\n- {trade_path}\n- {metrics_path}")

    def _plot_report(self, eq_df: pd.DataFrame, out_dir: str):
        if eq_df.empty:
            return

        dfx = eq_df.copy()
        date_col = self._first_existing_column(dfx, ["日期", "date"])
        total_col = self._first_existing_column(dfx, ["总资产", "total"])
        benchmark_col = self._first_existing_column(dfx, ["基准资产", "benchmark"])

        dfx[date_col] = pd.to_datetime(dfx[date_col])
        dfx = dfx.sort_values(date_col)
        dfx["nav"] = dfx[total_col] / self.initial_capital
        dfx["bench_nav"] = dfx[benchmark_col] / self.initial_capital
        dfx["drawdown"] = dfx["nav"] / dfx["nav"].cummax() - 1.0

        fig, axes = plt.subplots(2, 1, figsize=(15, 10), gridspec_kw={"height_ratios": [3, 1]})
        axes[0].plot(dfx[date_col], dfx["nav"], label="三维共振策略净值", color="#d35400", linewidth=2.0)
        axes[0].plot(dfx[date_col], dfx["bench_nav"], label="沪深300净值", color="#7f8c8d", linestyle="--")
        axes[0].set_title("三维共振策略回测")
        axes[0].legend(loc="upper left")
        axes[0].grid(alpha=0.3)

        axes[1].fill_between(dfx[date_col], dfx["drawdown"], 0, color="#2980b9", alpha=0.35)
        axes[1].set_title("策略回撤")
        axes[1].grid(alpha=0.3)

        plt.tight_layout()
        fig_path = os.path.join(out_dir, "three_dim_strategy_report.png")
        plt.savefig(fig_path, dpi=120)
        plt.close(fig)


if __name__ == "__main__":
    strategy = ThreeDimResonanceStrategy(
        initial_capital=1_000_000,
        max_positions=6,
        universe_size=1800,
        max_position_weight=0.18,
        stop_loss_pct=0.06,
        trailing_arm_pct=0.12,
        trailing_drawdown_pct=0.06,
        max_hold_days=18,
    )

    strategy.load_data()

    start = "2021-03-01"
    end = "2026-03-12"
    strategy.run_backtest(start, end)

    output_dir = os.path.dirname(os.path.abspath(__file__))
    strategy.save_outputs(output_dir)
