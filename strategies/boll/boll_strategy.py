# -*- coding: utf-8 -*-
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from strategies.volatility import QualityGateConfig, VolatilityStrategy, VolatilityStrategyConfig


@dataclass
class BollStrategyConfig:
    quality: QualityGateConfig = field(default_factory=QualityGateConfig)
    universe_size: int | None = 2500
    buy_limit: int = 80
    sell_limit: int = 80
    boll_window: int = 20
    boll_std: float = 2.0
    max_bandwidth_ratio: float = 0.90
    max_mid_slope20: float = 0.08


class BollStrategy(VolatilityStrategy):
    """
    BOLL strategy scanner for range-bound markets.

    The scanner reuses the same quality gate as the volatility strategy, then
    searches for two BOLL structures:
    - lower-band touch with long lower shadow as a buy-watch signal
    - upper-band touch with volume expansion but price stagnation as a sell/risk signal
    """

    def __init__(self, config: BollStrategyConfig | None = None):
        self.boll_config = config or BollStrategyConfig()
        super().__init__(
            VolatilityStrategyConfig(
                quality=self.boll_config.quality,
                universe_size=self.boll_config.universe_size,
            )
        )

    def _append_indicators(self, dfx: pd.DataFrame) -> pd.DataFrame:
        out = super()._append_indicators(dfx)
        window = self.boll_config.boll_window
        std_mult = self.boll_config.boll_std

        out["boll_mid"] = out["close"].rolling(window, min_periods=max(12, window // 2)).mean()
        out["boll_std"] = out["close"].rolling(window, min_periods=max(12, window // 2)).std()
        out["boll_upper"] = out["boll_mid"] + std_mult * out["boll_std"]
        out["boll_lower"] = out["boll_mid"] - std_mult * out["boll_std"]
        out["boll_bandwidth"] = (out["boll_upper"] - out["boll_lower"]) / out["boll_mid"].replace(0, np.nan)
        out["boll_bandwidth_ma60"] = out["boll_bandwidth"].rolling(60, min_periods=30).mean()
        out["boll_bandwidth_ratio"] = out["boll_bandwidth"] / out["boll_bandwidth_ma60"].replace(0, np.nan)
        out["boll_mid_slope20"] = out["boll_mid"] / out["boll_mid"].shift(20) - 1.0
        out["lower_touch"] = (out["low"] <= out["boll_lower"] * 1.01) | (out["close"] <= out["boll_lower"] * 1.03)
        out["upper_touch"] = (out["high"] >= out["boll_upper"] * 0.99) | (out["close"] >= out["boll_upper"] * 0.97)

        candle_range = (out["high"] - out["low"]).replace(0, np.nan)
        out["lower_shadow_ratio"] = ((np.minimum(out["open"], out["close"]) - out["low"]) / candle_range).clip(0, 1)
        out["upper_shadow_ratio"] = ((out["high"] - np.maximum(out["open"], out["close"])) / candle_range).clip(0, 1)
        out["body_ratio"] = ((out["close"] - out["open"]).abs() / candle_range).clip(0, 1)
        out["is_squeeze_parallel"] = (
            (out["boll_bandwidth_ratio"] <= self.boll_config.max_bandwidth_ratio)
            & (out["boll_mid_slope20"].abs() <= self.boll_config.max_mid_slope20)
        )
        return out

    def _score_features(self, features: pd.DataFrame) -> pd.DataFrame:
        out = super()._score_features(features)
        grouped = out.groupby("trade_date", group_keys=False)
        out["boll_buy_score"] = (
            grouped["boll_bandwidth_ratio"].transform(lambda s: self._pct_rank(s, ascending=False)) * 0.25
            + grouped["lower_shadow_ratio"].transform(lambda s: self._pct_rank(s, ascending=True)) * 0.25
            + grouped["close_pos"].transform(lambda s: self._pct_rank(s, ascending=True)) * 0.20
            + grouped.apply(lambda g: self._pct_rank((g["close"] / g["boll_lower"] - 1.0).abs(), ascending=False))
            .reset_index(level=0, drop=True)
            .reindex(out.index)
            * 0.15
            + grouped.apply(lambda g: self._pct_rank(g["ret_1d"], ascending=True))
            .reset_index(level=0, drop=True)
            .reindex(out.index)
            * 0.15
        )
        out["boll_sell_score"] = (
            grouped["amount_ratio5_20"].transform(lambda s: self._pct_rank(s, ascending=True)) * 0.25
            + grouped["upper_shadow_ratio"].transform(lambda s: self._pct_rank(s, ascending=True)) * 0.25
            + grouped.apply(lambda g: self._pct_rank(g["close"] / g["boll_upper"], ascending=True))
            .reset_index(level=0, drop=True)
            .reindex(out.index)
            * 0.20
            + grouped["ret_20d"].transform(lambda s: self._pct_rank(s, ascending=True)) * 0.15
            + grouped.apply(lambda g: self._pct_rank(g["ret_1d"], ascending=False))
            .reset_index(level=0, drop=True)
            .reindex(out.index)
            * 0.15
        )
        return out

    def latest_signals(self, trade_date: str | pd.Timestamp | None = None) -> pd.DataFrame:
        if self.feature_df.empty:
            raise RuntimeError("请先调用 compute_features。")
        features = self.feature_df.copy()
        if trade_date is None:
            signal_date = features["trade_date"].max()
        else:
            requested = pd.to_datetime(trade_date)
            dates = sorted(d for d in features["trade_date"].drop_duplicates().tolist() if d <= requested)
            if not dates:
                raise RuntimeError(f"BOLL策略没有覆盖日期: {trade_date}")
            signal_date = dates[-1]

        day = features[features["trade_date"].eq(signal_date)].copy()
        if day.empty:
            return pd.DataFrame()

        buy = self._select_buy_signals(day)
        sell = self._select_sell_signals(day)
        result = pd.concat([df for df in [buy, sell] if not df.empty], ignore_index=True)
        if result.empty:
            return result
        return result.sort_values(["signal_type", "score"], ascending=[True, False]).reset_index(drop=True)

    def _select_buy_signals(self, day: pd.DataFrame) -> pd.DataFrame:
        mask = (
            day["is_squeeze_parallel"]
            & day["lower_touch"]
            & (day["lower_shadow_ratio"] >= 0.35)
            & (day["close_pos"] >= 0.40)
            & (day["ret_20d"].between(-0.25, 0.18))
            & (day["close"] >= day["boll_lower"] * 0.98)
        )
        selected = day[mask & day["boll_buy_score"].notna()].sort_values("boll_buy_score", ascending=False).head(
            self.boll_config.buy_limit
        )
        return self._format_signals(selected, "下轨止跌观察", "boll_buy_score")

    def _select_sell_signals(self, day: pd.DataFrame) -> pd.DataFrame:
        stagnation = (day["upper_shadow_ratio"] >= 0.28) | (day["close_pos"] <= 0.55) | (day["ret_1d"] <= 0.02)
        mask = (
            day["upper_touch"]
            & (day["amount_ratio5_20"] >= 1.15)
            & stagnation
            & (day["ret_20d"] >= 0.03)
        )
        selected = day[mask & day["boll_sell_score"].notna()].sort_values("boll_sell_score", ascending=False).head(
            self.boll_config.sell_limit
        )
        return self._format_signals(selected, "上轨放量滞涨", "boll_sell_score")

    def _format_signals(self, selected: pd.DataFrame, signal_type: str, score_col: str) -> pd.DataFrame:
        if selected.empty:
            return pd.DataFrame()
        out = selected.copy()
        out["signal_type"] = signal_type
        out["score"] = out[score_col]
        out["risk_level"] = np.select(
            [
                out["drawdown60"].ge(0.30) | out["range_pct"].ge(0.12),
                out["drawdown60"].ge(0.20) | out["range_pct"].ge(0.08),
            ],
            ["高", "中"],
            default="低",
        )
        out["watch_price"] = np.where(
            signal_type == "下轨止跌观察",
            np.maximum(out["boll_lower"], out["close"] * 1.01),
            out["boll_upper"],
        )
        out["invalid_price"] = np.where(
            signal_type == "下轨止跌观察",
            np.minimum(out["low"], out["boll_lower"] * 0.98),
            np.maximum(out["high"], out["boll_upper"] * 1.01),
        )
        out["reason"] = out.apply(self._reason_text, axis=1)
        keep_cols = [
            "trade_date",
            "stock_code",
            "short_name",
            "signal_type",
            "score",
            "risk_level",
            "close",
            "ret_1d",
            "ret_20d",
            "amount_ma20",
            "amount_ratio5_20",
            "boll_mid",
            "boll_upper",
            "boll_lower",
            "boll_bandwidth",
            "boll_bandwidth_ratio",
            "boll_mid_slope20",
            "lower_shadow_ratio",
            "upper_shadow_ratio",
            "close_pos",
            "watch_price",
            "invalid_price",
            "reason",
        ]
        return out[keep_cols]

    @staticmethod
    def _reason_text(row: pd.Series) -> str:
        parts = []
        if pd.notna(row.get("boll_bandwidth_ratio")):
            parts.append(f"布林带宽/60日均值={row['boll_bandwidth_ratio']:.2f}")
        if pd.notna(row.get("boll_mid_slope20")):
            parts.append(f"中轨20日斜率={row['boll_mid_slope20'] * 100:.1f}%")
        if row.get("signal_type") == "下轨止跌观察":
            parts.append(f"下影线占比={row.get('lower_shadow_ratio', 0) * 100:.1f}%")
        else:
            parts.append(f"上影线占比={row.get('upper_shadow_ratio', 0) * 100:.1f}%")
            parts.append(f"5日成交额/20日={row.get('amount_ratio5_20', 0):.2f}")
        return "；".join(parts)
