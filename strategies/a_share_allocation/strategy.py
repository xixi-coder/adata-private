# -*- coding: utf-8 -*-
from __future__ import annotations

import argparse
import math
import os
import pickle
import re
from dataclasses import dataclass
from typing import Iterable

import numpy as np
import pandas as pd


@dataclass
class StrategyConfig:
    """A-share allocation defaults tuned for offline research rather than live orders."""

    initial_capital: float = 1_000_000
    max_positions: int = 18
    rebalance_period: int = 20
    min_history_days: int = 120
    min_amount_ma20: float = 80_000_000
    min_price: float = 2.0
    transaction_cost: float = 0.0013
    normal_exposure: float = 0.92
    defensive_exposure: float = 0.45
    stop_loss_pct: float = 0.10
    min_score: float = 45.0
    core_weight: float = 0.35
    dividend_weight: float = 0.20
    trend_weight: float = 0.30
    short_weight: float = 0.15


class AShareAllocationStrategy:
    """
    Multi-style A-share strategy.

    The strategy is intentionally rule-based:
    - core quality proxy: liquidity, low volatility, controlled drawdown
    - dividend proxy: cached trailing dividend yield when available, otherwise low volatility/liquidity
    - trend: 20/60-day relative strength with 60/120-day trend confirmation
    - short-term: 5-day strength and volume expansion, penalizing overheated names
    - market regime: lower target exposure when CSI 300 is below its 120-day MA
    """

    def __init__(self, config: StrategyConfig | None = None):
        self.config = config or StrategyConfig()
        self.base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        self.cache_dir = os.path.join(self.base_dir, "data", "cache")
        self.market_cache_file = os.path.join(self.cache_dir, "full_data_v3_5year.pkl")
        self.benchmark_cache_file = os.path.join(self.cache_dir, "benchmark_000300.csv")
        self.dividend_cache_dir = os.path.join(self.cache_dir, "dividend")

        self.panel_df = pd.DataFrame()
        self.score_df = pd.DataFrame()
        self.benchmark_df = pd.DataFrame()
        self.positions: dict[str, dict[str, float | str]] = {}
        self.cash = float(self.config.initial_capital)
        self.trade_logs: list[dict[str, object]] = []
        self.equity_curve: list[dict[str, object]] = []

    @staticmethod
    def _normalize_code(value) -> str:
        if pd.isna(value):
            return ""
        text = str(value).strip()
        if text.endswith(".0"):
            text = text[:-2]
        return text.zfill(6) if text.isdigit() else text

    @staticmethod
    def _is_supported_equity_code(code: str) -> bool:
        if not (isinstance(code, str) and len(code) == 6 and code.isdigit()):
            return False
        # Exclude B shares and obvious non-common-equity prefixes. Keep STAR/GEM.
        return not code.startswith(("2", "4", "8", "9"))

    @staticmethod
    def _standardize_daily_df(code: str, df: pd.DataFrame) -> pd.DataFrame:
        if df is None or df.empty:
            return pd.DataFrame()
        out = df.copy()
        if "trade_date" not in out.columns:
            date_col = "trade_time" if "trade_time" in out.columns else None
            if date_col is None:
                return pd.DataFrame()
            out["trade_date"] = pd.to_datetime(out[date_col], errors="coerce")
        else:
            out["trade_date"] = pd.to_datetime(out["trade_date"], errors="coerce")

        out["stock_code"] = out.get("stock_code", code)
        out["stock_code"] = out["stock_code"].map(AShareAllocationStrategy._normalize_code)
        required = ["stock_code", "trade_date", "open", "high", "low", "close", "volume", "amount"]
        missing = [col for col in required if col not in out.columns]
        if missing:
            return pd.DataFrame()
        for col in ["open", "high", "low", "close", "volume", "amount", "turnover_ratio", "pre_close"]:
            if col in out.columns:
                out[col] = pd.to_numeric(out[col], errors="coerce")
        keep_cols = [col for col in required + ["turnover_ratio", "pre_close"] if col in out.columns]
        out = out[keep_cols].dropna(subset=["stock_code", "trade_date", "open", "close", "amount"])
        out = out.sort_values("trade_date").drop_duplicates(["stock_code", "trade_date"])
        return out

    @staticmethod
    def _safe_pct_rank(series: pd.Series, ascending: bool = True) -> pd.Series:
        valid = series.replace([np.inf, -np.inf], np.nan)
        if valid.notna().sum() <= 1:
            return pd.Series(50.0, index=series.index)
        return valid.rank(pct=True, ascending=ascending) * 100.0

    @staticmethod
    def _parse_cash_dividend_per_share(plan: str) -> float:
        if not isinstance(plan, str) or not plan.strip():
            return 0.0
        text = plan.replace(" ", "")
        patterns_per_10 = [
            r"每?10股派(?:现金红利)?([0-9]+(?:\.[0-9]+)?)元",
            r"10股派(?:现金红利)?([0-9]+(?:\.[0-9]+)?)元",
            r"10派([0-9]+(?:\.[0-9]+)?)元",
        ]
        for pattern in patterns_per_10:
            match = re.search(pattern, text)
            if match:
                return float(match.group(1)) / 10.0
        match = re.search(r"每股派(?:现金红利)?([0-9]+(?:\.[0-9]+)?)元", text)
        if match:
            return float(match.group(1))
        return 0.0

    @staticmethod
    def _weighted_average(parts: list[tuple[pd.Series, float]]) -> pd.Series:
        numerator = None
        denominator = None
        for series, weight in parts:
            if weight <= 0:
                continue
            values = pd.to_numeric(series, errors="coerce")
            present = values.notna().astype(float) * weight
            contribution = values.fillna(0.0) * weight
            numerator = contribution if numerator is None else numerator + contribution
            denominator = present if denominator is None else denominator + present
        if numerator is None or denominator is None:
            raise ValueError("At least one positive style weight is required.")
        return numerator / denominator.replace(0, np.nan)

    def load_market_cache(self, path: str | None = None, universe_size: int | None = None) -> pd.DataFrame:
        cache_path = path or self.market_cache_file
        if not os.path.exists(cache_path):
            raise FileNotFoundError(f"找不到市场缓存文件: {cache_path}")
        with open(cache_path, "rb") as f:
            cache = pickle.load(f)
        stock_data = cache.get("stock") if isinstance(cache, dict) and "stock" in cache else cache
        if not isinstance(stock_data, dict):
            raise ValueError("市场缓存结构异常，预期为股票代码到 DataFrame 的映射。")

        frames = []
        for code, df in stock_data.items():
            code = self._normalize_code(code)
            if not self._is_supported_equity_code(code):
                continue
            standardized = self._standardize_daily_df(code, df)
            if standardized.empty or len(standardized) < self.config.min_history_days:
                continue
            frames.append(standardized)
        if not frames:
            raise RuntimeError("未从缓存中解析出可用日线数据。")

        panel = pd.concat(frames, ignore_index=True)
        panel = panel.sort_values(["stock_code", "trade_date"]).reset_index(drop=True)
        if universe_size:
            latest = (
                panel.groupby("stock_code", sort=False)
                .tail(20)
                .groupby("stock_code")["amount"]
                .mean()
                .sort_values(ascending=False)
            )
            keep = set(latest.head(universe_size).index)
            panel = panel[panel["stock_code"].isin(keep)].copy()
        self.panel_df = panel
        return self.panel_df

    def set_panel(self, panel_df: pd.DataFrame) -> pd.DataFrame:
        frames = []
        for code, sub in panel_df.groupby("stock_code", sort=False):
            code = self._normalize_code(code)
            standardized = self._standardize_daily_df(code, sub)
            if not standardized.empty:
                frames.append(standardized)
        if not frames:
            raise ValueError("panel_df 中没有可用的股票日线数据。")
        self.panel_df = pd.concat(frames, ignore_index=True).sort_values(["stock_code", "trade_date"])
        return self.panel_df

    def load_benchmark(self, path: str | None = None) -> pd.DataFrame:
        bench_path = path or self.benchmark_cache_file
        if not os.path.exists(bench_path):
            raise FileNotFoundError(f"找不到基准缓存文件: {bench_path}")
        bench = pd.read_csv(bench_path)
        if "trade_date" not in bench.columns or "close" not in bench.columns:
            raise ValueError("基准数据必须包含 trade_date 和 close 列。")
        bench = bench.copy()
        bench["trade_date"] = pd.to_datetime(bench["trade_date"], errors="coerce")
        bench["close"] = pd.to_numeric(bench["close"], errors="coerce")
        bench = bench.dropna(subset=["trade_date", "close"]).sort_values("trade_date").drop_duplicates("trade_date")
        bench["ma120"] = bench["close"].rolling(120, min_periods=60).mean()
        bench["ma60_slope"] = bench["close"].rolling(60, min_periods=30).mean().diff(20)
        self.benchmark_df = bench
        return self.benchmark_df

    def _load_dividend_events(self, codes: Iterable[str]) -> dict[str, pd.DataFrame]:
        events = {}
        if not os.path.isdir(self.dividend_cache_dir):
            return events
        for code in codes:
            path = os.path.join(self.dividend_cache_dir, f"{code}.csv")
            if not os.path.exists(path):
                continue
            try:
                df = pd.read_csv(path)
            except Exception:
                continue
            if df.empty or "ex_dividend_date" not in df.columns:
                continue
            df = df.copy()
            df["ex_dividend_date"] = pd.to_datetime(df["ex_dividend_date"], errors="coerce")
            if "cash_per_share" not in df.columns:
                df["cash_per_share"] = (
                    df["dividend_plan"].map(self._parse_cash_dividend_per_share)
                    if "dividend_plan" in df.columns
                    else 0.0
                )
            df["cash_per_share"] = pd.to_numeric(df["cash_per_share"], errors="coerce").fillna(0.0)
            df = df.dropna(subset=["ex_dividend_date"])
            df = df[df["cash_per_share"] > 0]
            if not df.empty:
                events[code] = df[["ex_dividend_date", "cash_per_share"]].sort_values("ex_dividend_date")
        return events

    def _attach_dividend_yield(self, panel: pd.DataFrame) -> pd.DataFrame:
        out = panel.copy()
        out["dividend_yield_ttm"] = np.nan
        events_by_code = self._load_dividend_events(out["stock_code"].drop_duplicates().tolist())
        if not events_by_code:
            return out

        parts = []
        for code, sub in out.groupby("stock_code", sort=False):
            sub = sub.copy()
            events = events_by_code.get(code)
            if events is None or events.empty:
                parts.append(sub)
                continue
            values = []
            for trade_date, close in zip(sub["trade_date"], sub["close"]):
                start = trade_date - pd.Timedelta(days=365)
                cash = events.loc[
                    (events["ex_dividend_date"] <= trade_date) & (events["ex_dividend_date"] > start),
                    "cash_per_share",
                ].sum()
                values.append(float(cash) / float(close) if close and close > 0 else np.nan)
            sub["dividend_yield_ttm"] = values
            parts.append(sub)
        return pd.concat(parts, ignore_index=True)

    def compute_scores(self, include_dividend: bool = True) -> pd.DataFrame:
        if self.panel_df.empty:
            raise RuntimeError("请先调用 load_market_cache() 或 set_panel()。")
        cfg = self.config
        df = self.panel_df.copy()
        df = df.sort_values(["stock_code", "trade_date"]).reset_index(drop=True)

        for col in ["open", "high", "low", "close", "volume", "amount"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        g = df.groupby("stock_code", sort=False, group_keys=False)
        df["ret_1d"] = g["close"].pct_change()
        df["ret_5"] = df["close"] / g["close"].shift(5) - 1.0
        df["ret_20"] = df["close"] / g["close"].shift(20) - 1.0
        df["ret_60"] = df["close"] / g["close"].shift(60) - 1.0
        df["ma20"] = g["close"].rolling(20, min_periods=10).mean().reset_index(level=0, drop=True)
        df["ma60"] = g["close"].rolling(60, min_periods=30).mean().reset_index(level=0, drop=True)
        df["ma120"] = g["close"].rolling(120, min_periods=60).mean().reset_index(level=0, drop=True)
        df["ma60_slope"] = df["ma60"] - g["ma60"].shift(20)
        df["vol_20"] = g["ret_1d"].rolling(20, min_periods=10).std().reset_index(level=0, drop=True) * math.sqrt(252)
        df["vol_60"] = g["ret_1d"].rolling(60, min_periods=30).std().reset_index(level=0, drop=True) * math.sqrt(252)
        df["amount_ma20"] = g["amount"].rolling(20, min_periods=10).mean().reset_index(level=0, drop=True)
        df["volume_ma5"] = g["volume"].rolling(5, min_periods=3).mean().reset_index(level=0, drop=True)
        df["volume_ma20"] = g["volume"].rolling(20, min_periods=10).mean().reset_index(level=0, drop=True)
        df["volume_ratio_5_20"] = df["volume_ma5"] / df["volume_ma20"].replace(0, np.nan)
        df["amount_ratio1_20"] = df["amount"] / df["amount_ma20"].replace(0, np.nan)
        df["high_60"] = g["high"].rolling(60, min_periods=30).max().reset_index(level=0, drop=True)
        df["close_to_high_60"] = df["close"] / df["high_60"].replace(0, np.nan)
        df["_roll_high_60"] = g["close"].rolling(60, min_periods=30).max().reset_index(level=0, drop=True)
        df["drawdown_60"] = df["close"] / df["_roll_high_60"].replace(0, np.nan) - 1.0
        df["listed_days"] = g.cumcount() + 1
        candle_range = (df["high"] - df["low"]).replace(0, np.nan)
        df["close_pos"] = ((df["close"] - df["low"]) / candle_range).clip(0, 1).fillna(0.5)
        df["upper_shadow_ratio"] = ((df["high"] - np.maximum(df["open"], df["close"])) / candle_range).clip(0, 1)
        df["lower_shadow_ratio"] = ((np.minimum(df["open"], df["close"]) - df["low"]) / candle_range).clip(0, 1)
        df["body_ratio"] = ((df["close"] - df["open"]).abs() / candle_range).clip(0, 1)
        money_flow_multiplier = ((df["close"] - df["low"]) - (df["high"] - df["close"])) / candle_range
        df["money_flow_amount"] = money_flow_multiplier.fillna(0.0).clip(-1.0, 1.0) * df["amount"]
        df["cmf5"] = (
            g["money_flow_amount"].rolling(5, min_periods=3).sum().reset_index(level=0, drop=True)
            / g["amount"].rolling(5, min_periods=3).sum().reset_index(level=0, drop=True).replace(0, np.nan)
        )
        df["cmf20"] = (
            g["money_flow_amount"].rolling(20, min_periods=10).sum().reset_index(level=0, drop=True)
            / g["amount"].rolling(20, min_periods=10).sum().reset_index(level=0, drop=True).replace(0, np.nan)
        )
        signed_amount = np.sign(df["close"] - df.get("pre_close", g["close"].shift(1))) * df["amount"]
        df["signed_amount"] = signed_amount.fillna(0.0)
        df["net_amt3"] = g["signed_amount"].rolling(3, min_periods=2).sum().reset_index(level=0, drop=True)
        df["net_amt5"] = g["signed_amount"].rolling(5, min_periods=3).sum().reset_index(level=0, drop=True)

        if include_dividend:
            df = self._attach_dividend_yield(df)
        elif "dividend_yield_ttm" not in df.columns:
            df["dividend_yield_ttm"] = np.nan

        liquid = (
            (df["listed_days"] >= cfg.min_history_days)
            & (df["amount_ma20"] >= cfg.min_amount_ma20)
            & (df["close"] >= cfg.min_price)
            & df["ma60"].notna()
        )
        trend_ok = (df["close"] > df["ma60"]) & (df["ma60_slope"] > 0)
        not_overheated = df["ret_20"].fillna(0) < 0.35
        df["eligible"] = liquid & trend_ok & not_overheated

        def score_one_day(sub: pd.DataFrame) -> pd.DataFrame:
            sub = sub.copy()
            sub["core_score"] = self._weighted_average(
                [
                    (self._safe_pct_rank(sub["amount_ma20"]), 0.25),
                    (self._safe_pct_rank(sub["vol_60"], ascending=False), 0.35),
                    (self._safe_pct_rank(sub["drawdown_60"]), 0.25),
                    (self._safe_pct_rank(sub["close_to_high_60"]), 0.15),
                ]
            )
            sub["dividend_score"] = self._weighted_average(
                [
                    (self._safe_pct_rank(sub["dividend_yield_ttm"]), 0.50),
                    (self._safe_pct_rank(sub["vol_60"], ascending=False), 0.30),
                    (self._safe_pct_rank(sub["amount_ma20"]), 0.20),
                ]
            )
            sub["trend_score"] = self._weighted_average(
                [
                    (self._safe_pct_rank(sub["ret_60"]), 0.35),
                    (self._safe_pct_rank(sub["ret_20"]), 0.25),
                    (self._safe_pct_rank(sub["ma60_slope"]), 0.20),
                    (self._safe_pct_rank(sub["vol_60"], ascending=False), 0.20),
                ]
            )
            short_raw = self._weighted_average(
                [
                    (self._safe_pct_rank(sub["ret_5"]), 0.35),
                    (self._safe_pct_rank(sub["volume_ratio_5_20"]), 0.30),
                    (self._safe_pct_rank(sub["ret_20"]), 0.20),
                    (self._safe_pct_rank(sub["close_to_high_60"]), 0.15),
                ]
            )
            heat_penalty = np.where(sub["ret_5"] > 0.18, 12.0, 0.0) + np.where(sub["ret_20"] > 0.45, 18.0, 0.0)
            sub["short_score"] = (short_raw - heat_penalty).clip(lower=0.0, upper=100.0)
            sub["final_score"] = self._weighted_average(
                [
                    (sub["core_score"], cfg.core_weight),
                    (sub["dividend_score"], cfg.dividend_weight),
                    (sub["trend_score"], cfg.trend_weight),
                    (sub["short_score"], cfg.short_weight),
                ]
            )
            sub.loc[~sub["eligible"], "final_score"] = np.nan
            return sub

        scored_parts = [score_one_day(sub) for _, sub in df.groupby("trade_date", sort=False)]
        scored = pd.concat(scored_parts, ignore_index=True) if scored_parts else pd.DataFrame()
        scored = scored.drop(columns=["_roll_high_60"], errors="ignore")
        self.score_df = scored.sort_values(["trade_date", "final_score"], ascending=[True, False]).reset_index(drop=True)
        return self.score_df

    def _regime_exposure(self, trade_date: pd.Timestamp) -> float:
        if self.benchmark_df.empty:
            return self.config.normal_exposure
        available = self.benchmark_df[self.benchmark_df["trade_date"] <= trade_date]
        if available.empty:
            return self.config.normal_exposure
        row = available.iloc[-1]
        ma_ok = pd.notna(row["ma120"]) and row["close"] >= row["ma120"]
        slope_ok = pd.notna(row["ma60_slope"]) and row["ma60_slope"] >= 0
        return self.config.normal_exposure if (ma_ok or slope_ok) else self.config.defensive_exposure

    def _pick_targets(self, signal_date: pd.Timestamp, exposure: float) -> list[dict[str, object]]:
        if self.score_df.empty:
            raise RuntimeError("请先调用 compute_scores()。")
        day = self.score_df[self.score_df["trade_date"] == signal_date].copy()
        day = day[day["final_score"].notna() & (day["final_score"] >= self.config.min_score)]
        day = day.sort_values("final_score", ascending=False).head(self.config.max_positions)
        if day.empty:
            return []
        weight = exposure / len(day)
        return [
            {
                "stock_code": row.stock_code,
                "weight": weight,
                "score": float(row.final_score),
                "signal_date": signal_date.strftime("%Y-%m-%d"),
            }
            for row in day.itertuples()
        ]

    def run_backtest(self, start_date: str | None = None, end_date: str | None = None) -> dict[str, float]:
        if self.score_df.empty:
            self.compute_scores()
        df = self.score_df.copy()
        if start_date:
            df = df[df["trade_date"] >= pd.to_datetime(start_date)]
        if end_date:
            df = df[df["trade_date"] <= pd.to_datetime(end_date)]
        trade_dates = sorted(df["trade_date"].drop_duplicates().tolist())
        if len(trade_dates) < 3:
            raise RuntimeError("回测区间交易日太少。")

        prices = {
            date: sub.set_index("stock_code")[["open", "close", "ma60"]]
            for date, sub in self.score_df[self.score_df["trade_date"].isin(trade_dates)].groupby("trade_date")
        }
        self.cash = float(self.config.initial_capital)
        self.positions = {}
        self.trade_logs = []
        self.equity_curve = []

        for idx, today in enumerate(trade_dates):
            today_prices = prices[today]
            total_before_trade = self._mark_to_market(today_prices)

            stop_sells = []
            for code, pos in self.positions.items():
                if code not in today_prices.index:
                    continue
                close = float(today_prices.loc[code, "close"])
                ma60 = float(today_prices.loc[code, "ma60"]) if pd.notna(today_prices.loc[code, "ma60"]) else np.nan
                stop_price = float(pos["buy_price"]) * (1 - self.config.stop_loss_pct)
                if close <= stop_price or (pd.notna(ma60) and close < ma60):
                    stop_sells.append((code, close, "止损" if close <= stop_price else "跌破60日线"))
            for code, price, reason in stop_sells:
                self._sell(code, today, price, reason)

            if idx > 0 and idx % self.config.rebalance_period == 0:
                signal_date = trade_dates[idx - 1]
                exposure = self._regime_exposure(signal_date)
                targets = self._pick_targets(signal_date, exposure)
                target_codes = {str(t["stock_code"]) for t in targets}

                for code in list(self.positions.keys()):
                    if code in target_codes or code not in today_prices.index:
                        continue
                    self._sell(code, today, float(today_prices.loc[code, "open"]), "调仓卖出")

                total_before_trade = self._mark_to_market(today_prices)
                target_budget = total_before_trade * exposure
                for target in targets:
                    code = str(target["stock_code"])
                    if code not in today_prices.index:
                        continue
                    price = float(today_prices.loc[code, "open"])
                    if not np.isfinite(price) or price <= 0:
                        continue
                    target_value = target_budget * float(target["weight"]) / max(exposure, 1e-9)
                    current_shares = float(self.positions.get(code, {}).get("shares", 0.0))
                    current_value = current_shares * price
                    diff_value = target_value - current_value
                    if diff_value <= price * 100:
                        continue
                    shares = int(diff_value / price / 100) * 100
                    self._buy(code, today, price, shares, float(target["score"]))

            close_value = self._mark_to_market(today_prices)
            self.equity_curve.append(
                {
                    "date": today.strftime("%Y-%m-%d"),
                    "total": round(close_value, 2),
                    "cash": round(self.cash, 2),
                    "position_count": len(self.positions),
                }
            )

        return self.metrics()

    def _mark_to_market(self, price_table: pd.DataFrame) -> float:
        market_value = 0.0
        for code, pos in self.positions.items():
            if code in price_table.index:
                market_value += float(pos["shares"]) * float(price_table.loc[code, "close"])
            else:
                market_value += float(pos["shares"]) * float(pos["buy_price"])
        return self.cash + market_value

    def _buy(self, code: str, date: pd.Timestamp, price: float, shares: int, score: float) -> None:
        if shares < 100:
            return
        gross = price * shares
        cost = gross * (1 + self.config.transaction_cost)
        if cost > self.cash:
            shares = int(self.cash / (price * (1 + self.config.transaction_cost)) / 100) * 100
            if shares < 100:
                return
            gross = price * shares
            cost = gross * (1 + self.config.transaction_cost)
        self.cash -= cost
        if code in self.positions:
            pos = self.positions[code]
            old_cost = float(pos["cost"])
            new_shares = float(pos["shares"]) + shares
            pos["buy_price"] = (float(pos["buy_price"]) * float(pos["shares"]) + gross) / new_shares
            pos["shares"] = new_shares
            pos["cost"] = old_cost + cost
        else:
            self.positions[code] = {
                "buy_date": date.strftime("%Y-%m-%d"),
                "buy_price": price,
                "shares": float(shares),
                "cost": cost,
            }
        self.trade_logs.append(
            {
                "date": date.strftime("%Y-%m-%d"),
                "stock_code": code,
                "side": "BUY",
                "price": round(price, 4),
                "shares": shares,
                "amount": round(cost, 2),
                "score": round(score, 2),
                "reason": "组合评分买入",
            }
        )

    def _sell(self, code: str, date: pd.Timestamp, price: float, reason: str) -> None:
        pos = self.positions.get(code)
        if not pos:
            return
        shares = int(float(pos["shares"]))
        gross = price * shares
        net = gross * (1 - self.config.transaction_cost)
        self.cash += net
        profit = net - float(pos["cost"])
        self.trade_logs.append(
            {
                "date": date.strftime("%Y-%m-%d"),
                "stock_code": code,
                "side": "SELL",
                "price": round(price, 4),
                "shares": shares,
                "amount": round(net, 2),
                "profit": round(profit, 2),
                "profit_pct": round(profit / float(pos["cost"]), 6),
                "reason": reason,
            }
        )
        del self.positions[code]

    def metrics(self) -> dict[str, float]:
        if not self.equity_curve:
            return {}
        df = pd.DataFrame(self.equity_curve)
        df["date"] = pd.to_datetime(df["date"])
        df["total"] = pd.to_numeric(df["total"], errors="coerce")
        total_return = float(df["total"].iloc[-1] / self.config.initial_capital - 1.0)
        days = max((df["date"].iloc[-1] - df["date"].iloc[0]).days, 1)
        annual_return = float((1 + total_return) ** (365 / days) - 1) if total_return > -1 else -1.0
        drawdown = df["total"] / df["total"].cummax() - 1.0
        daily_ret = df["total"].pct_change().dropna()
        sharpe = 0.0
        if not daily_ret.empty and daily_ret.std(ddof=0) > 0:
            sharpe = float((daily_ret.mean() * 252 - 0.02) / (daily_ret.std(ddof=0) * math.sqrt(252)))
        sells = pd.DataFrame(self.trade_logs)
        win_rate = 0.0
        if not sells.empty and "profit" in sells.columns:
            sell_profit = pd.to_numeric(sells.loc[sells["side"] == "SELL", "profit"], errors="coerce").dropna()
            if not sell_profit.empty:
                win_rate = float((sell_profit > 0).mean())
        return {
            "final_asset": round(float(df["total"].iloc[-1]), 2),
            "total_return": round(total_return, 6),
            "annual_return": round(annual_return, 6),
            "max_drawdown": round(float(drawdown.min()), 6),
            "sharpe": round(sharpe, 4),
            "win_rate": round(win_rate, 6),
            "trade_count": int(len(self.trade_logs)),
        }

    def save_results(self, out_dir: str) -> dict[str, str]:
        os.makedirs(out_dir, exist_ok=True)
        score_file = os.path.join(out_dir, "a_share_allocation_scores.csv")
        trade_file = os.path.join(out_dir, "a_share_allocation_trades.csv")
        equity_file = os.path.join(out_dir, "a_share_allocation_equity.csv")
        metric_file = os.path.join(out_dir, "a_share_allocation_metrics.csv")

        self.score_df.to_csv(score_file, index=False, encoding="utf-8-sig")
        pd.DataFrame(self.trade_logs).to_csv(trade_file, index=False, encoding="utf-8-sig")
        pd.DataFrame(self.equity_curve).to_csv(equity_file, index=False, encoding="utf-8-sig")
        pd.DataFrame([self.metrics()]).to_csv(metric_file, index=False, encoding="utf-8-sig")
        return {
            "score_file": score_file,
            "trade_file": trade_file,
            "equity_file": equity_file,
            "metric_file": metric_file,
        }


def main() -> None:
    parser = argparse.ArgumentParser(description="A股组合策略实验室：核心低波 + 股息 + 趋势 + 短线强势")
    parser.add_argument("--start", type=str, default=None, help="回测开始日期 YYYY-MM-DD")
    parser.add_argument("--end", type=str, default=None, help="回测结束日期 YYYY-MM-DD")
    parser.add_argument("--universe-size", type=int, default=800, help="按近20日成交额截取股票池")
    parser.add_argument("--max-positions", type=int, default=18, help="最大持仓数")
    parser.add_argument("--rebalance-period", type=int, default=20, help="调仓周期，单位交易日")
    parser.add_argument("--min-amount-ma20", type=float, default=80_000_000, help="最低20日均成交额")
    parser.add_argument("--no-dividend", action="store_true", help="不读取分红缓存，仅使用价格/成交量评分")
    parser.add_argument("--out-dir", type=str, default=os.path.join("tests", "a_share_allocation_backtest"))
    args = parser.parse_args()

    config = StrategyConfig(
        max_positions=args.max_positions,
        rebalance_period=args.rebalance_period,
        min_amount_ma20=args.min_amount_ma20,
    )
    strategy = AShareAllocationStrategy(config)
    strategy.load_market_cache(universe_size=args.universe_size)
    if os.path.exists(strategy.benchmark_cache_file):
        strategy.load_benchmark()
    strategy.compute_scores(include_dividend=not args.no_dividend)
    metrics = strategy.run_backtest(start_date=args.start, end_date=args.end)
    files = strategy.save_results(args.out_dir)

    print("A股组合策略回测完成")
    for key, value in metrics.items():
        print(f"{key}: {value}")
    print(f"结果目录: {args.out_dir}")
    for name, path in files.items():
        print(f"{name}: {path}")


if __name__ == "__main__":
    main()
