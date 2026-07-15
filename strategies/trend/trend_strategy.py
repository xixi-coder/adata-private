# -*- coding: utf-8 -*-
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from strategies.volatility import QualityGateConfig, VolatilityStrategy, VolatilityStrategyConfig


@dataclass
class TrendTradingConfig:
    quality: QualityGateConfig = field(default_factory=QualityGateConfig)
    universe_size: int | None = 2500
    breakout_limit: int = 50
    pullback_limit: int = 50
    min_ma60_slope20: float = 0.02
    min_breakout_amount_ratio: float = 1.20
    max_breakout_ret1d: float = 0.085
    max_breakout_distance_ma20: float = 0.15
    max_pullback_distance_ma20: float = 0.035


class TrendTradingStrategy(VolatilityStrategy):
    """A-share trend scanner covering confirmed breakouts and orderly pullbacks."""

    SIGNAL_TYPES = ("趋势突破", "趋势回踩")

    def __init__(self, config: TrendTradingConfig | None = None):
        self.trend_config = config or TrendTradingConfig()
        super().__init__(
            VolatilityStrategyConfig(
                quality=self.trend_config.quality,
                universe_size=self.trend_config.universe_size,
            )
        )

    def _append_indicators(self, dfx: pd.DataFrame) -> pd.DataFrame:
        out = super()._append_indicators(dfx)
        out["ma10"] = out["close"].rolling(10, min_periods=7).mean()
        out["prior_high20"] = out["high"].rolling(20, min_periods=15).max().shift(1)
        out["prior_high60"] = out["high"].rolling(60, min_periods=40).max().shift(1)
        out["ret_10d"] = out["close"] / out["close"].shift(10) - 1.0
        out["ret_60d"] = out["close"] / out["close"].shift(60) - 1.0
        out["trend_alignment"] = (
            (out["close"] > out["ma20"])
            & (out["ma20"] > out["ma60"])
            & (out["ma60"] > out["ma120"])
        )
        out["distance_to_ma20"] = out["close"] / out["ma20"].replace(0, np.nan) - 1.0
        out["distance_to_prior_high20"] = out["close"] / out["prior_high20"].replace(0, np.nan) - 1.0
        out["pullback_to_ma20"] = out["low"] / out["ma20"].replace(0, np.nan) - 1.0
        return out

    def _score_features(self, features: pd.DataFrame) -> pd.DataFrame:
        out = super()._score_features(features)
        grouped = out.groupby("trade_date", group_keys=False)
        out["breakout_score"] = (
            grouped["ret_60d"].transform(lambda s: self._pct_rank(s, ascending=True)) * 0.25
            + grouped["ma60_slope20"].transform(lambda s: self._pct_rank(s, ascending=True)) * 0.25
            + grouped["amount_ratio1_20"].transform(lambda s: self._pct_rank(s, ascending=True)) * 0.25
            + grouped["close_pos"].transform(lambda s: self._pct_rank(s, ascending=True)) * 0.15
            + grouped["drawdown60"].transform(lambda s: self._pct_rank(s, ascending=False)) * 0.10
        )
        out["pullback_score"] = (
            grouped["ma60_slope20"].transform(lambda s: self._pct_rank(s, ascending=True)) * 0.30
            + grouped["ret_60d"].transform(lambda s: self._pct_rank(s, ascending=True)) * 0.20
            + grouped["close_pos"].transform(lambda s: self._pct_rank(s, ascending=True)) * 0.20
            + grouped["distance_to_ma20"].transform(lambda s: self._pct_rank(s.abs(), ascending=False)) * 0.20
            + grouped["amount_ratio5_20"].transform(lambda s: self._pct_rank(s, ascending=True)) * 0.10
        )
        return out

    def _signal_conditions(self, day: pd.DataFrame, signal_type: str) -> tuple[dict[str, pd.Series], pd.Series, str, int]:
        cfg = self.trend_config
        trend_base = {
            "均线多头排列": day["trend_alignment"],
            "60日线向上": day["ma60_slope20"] >= cfg.min_ma60_slope20,
            "中期动量为正": day["ret_60d"] > 0,
        }
        if signal_type == "趋势突破":
            conditions = {
                **trend_base,
                "突破前20日高点": day["close"] > day["prior_high20"],
                "突破放量": day["amount_ratio1_20"] >= cfg.min_breakout_amount_ratio,
                "当日收涨不过热": day["ret_1d"].between(0.0, cfg.max_breakout_ret1d),
                "未远离20日线": day["distance_to_ma20"].between(0.0, cfg.max_breakout_distance_ma20),
                "收盘位置强": day["close_pos"] >= 0.60,
            }
            return conditions, self._and_conditions(conditions), "breakout_score", cfg.breakout_limit
        if signal_type == "趋势回踩":
            conditions = {
                **trend_base,
                "盘中回踩20日线": day["pullback_to_ma20"].between(-0.04, cfg.max_pullback_distance_ma20),
                "收盘守住20日线": day["distance_to_ma20"].between(0.0, cfg.max_pullback_distance_ma20),
                "短线未转弱": day["ret_10d"] > -0.03,
                "收盘位置企稳": day["close_pos"] >= 0.55,
                "非放量下跌": ~((day["ret_1d"] < -0.02) & (day["amount_ratio1_20"] > 1.5)),
            }
            return conditions, self._and_conditions(conditions), "pullback_score", cfg.pullback_limit
        raise ValueError(f"未知趋势信号类型: {signal_type}")

    def latest_signals(self, trade_date: str | pd.Timestamp | None = None) -> pd.DataFrame:
        if self.feature_df.empty:
            raise RuntimeError("请先调用 compute_features。")
        _signal_date, day = self._resolve_signal_day(trade_date)
        frames = [self._select_trend_signal(day, signal_type) for signal_type in self.SIGNAL_TYPES]
        non_empty_frames = [frame for frame in frames if not frame.empty]
        result = pd.concat(non_empty_frames, ignore_index=True) if non_empty_frames else pd.DataFrame()
        if result.empty:
            return result
        return result.sort_values(["signal_type", "score"], ascending=[True, False]).reset_index(drop=True)

    def signal_funnel_summary(self, trade_date: str | pd.Timestamp | None = None) -> dict[str, Any]:
        if self.feature_df.empty:
            raise RuntimeError("请先调用 compute_features。")
        signal_date, day = self._resolve_signal_day(trade_date)
        signals: dict[str, Any] = {}
        for signal_type in self.SIGNAL_TYPES:
            conditions, mask, score_col, limit = self._signal_conditions(day, signal_type)
            selected = day[mask & day[score_col].notna()].sort_values(score_col, ascending=False).head(limit)
            signals[signal_type] = {
                "condition_counts": {name: int(series.fillna(False).sum()) for name, series in conditions.items()},
                "passed_count": int(mask.sum()),
                "selected_limit": int(limit),
                "selected_count": int(len(selected)),
            }
        return {
            "signal_date": pd.to_datetime(signal_date).strftime("%Y-%m-%d"),
            "scanned_count": int(len(day)),
            "signals": signals,
        }

    def _select_trend_signal(self, day: pd.DataFrame, signal_type: str) -> pd.DataFrame:
        _conditions, mask, score_col, limit = self._signal_conditions(day, signal_type)
        selected = day[mask & day[score_col].notna()].sort_values(score_col, ascending=False).head(limit).copy()
        if selected.empty:
            return pd.DataFrame()
        selected["signal_type"] = signal_type
        selected["score"] = selected[score_col]
        selected["risk_level"] = np.select(
            [selected["atr_pct"].ge(0.06) | selected["distance_to_ma20"].ge(0.12), selected["atr_pct"].ge(0.04)],
            ["高", "中"],
            default="低",
        )
        selected["watch_price"] = np.where(
            signal_type == "趋势突破",
            np.maximum(selected["prior_high20"], selected["close"] * 0.99),
            selected["high"],
        )
        selected["invalid_price"] = np.maximum(selected["ma20"] * 0.98, selected["close"] - selected["atr14"] * 1.5)
        selected["reason"] = selected.apply(self._reason_text, axis=1)
        keep = [
            "trade_date", "stock_code", "short_name", "signal_type", "score", "risk_level", "close",
            "ret_1d", "ret_10d", "ret_20d", "ret_60d", "ma20", "ma60", "ma120", "ma60_slope20",
            "amount_ma20", "amount_ratio1_20", "amount_ratio5_20", "distance_to_ma20", "drawdown60",
            "watch_price", "invalid_price", "reason",
        ]
        return selected[keep]

    @staticmethod
    def _reason_text(row: pd.Series) -> str:
        if row["signal_type"] == "趋势突破":
            return (
                f"突破20日高点，成交额为20日均值{row['amount_ratio1_20']:.2f}倍；"
                f"60日涨幅{row['ret_60d'] * 100:.1f}%，60日线斜率{row['ma60_slope20'] * 100:.1f}%"
            )
        return (
            f"多头排列中回踩20日线后收回；距20日线{row['distance_to_ma20'] * 100:.1f}%，"
            f"60日线斜率{row['ma60_slope20'] * 100:.1f}%"
        )
