# -*- coding: utf-8 -*-
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from jobs.common.a_share_metadata import is_excluded_short_name, is_supported_a_share_code, normalize_code
from jobs.common.a_share_panel import load_a_share_panel, standardize_daily_df


@dataclass
class QualityGateConfig:
    min_history_days: int = 180
    min_listing_days: int = 180
    min_amount_ma20: float = 100_000_000
    min_valid_days20: int = 16
    min_price: float = 3.0
    max_drawdown60: float = 0.40
    max_limit_like_days20: int = 5
    max_extreme_range_days20: int = 4
    min_mine_score: float = 75.0


@dataclass
class VolatilityStrategyConfig:
    quality: QualityGateConfig = field(default_factory=QualityGateConfig)
    universe_size: int | None = 2500
    squeeze_limit: int = 80
    expansion_limit: int = 80
    anomaly_limit: int = 40
    min_expansion_amount_ratio1_20: float = 1.5
    min_squeeze_ma60_slope20: float = -0.02
    min_squeeze_close_to_ma120: float = -0.02


class VolatilityStrategy:
    """
    Daily A-share volatility structure scanner.

    The scanner intentionally runs quality gates before ranking volatility signals.
    This keeps ST, delisting-risk, illiquid, newly listed, and structurally broken
    names out of the final research list.
    """

    def __init__(self, config: VolatilityStrategyConfig | None = None):
        self.config = config or VolatilityStrategyConfig()
        self.base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        self.cache_dir = os.path.join(self.base_dir, "data", "cache")
        self.market_cache_file = os.path.join(self.cache_dir, "full_data_v3_5year.pkl")
        self.panel_df = pd.DataFrame()
        self.feature_df = pd.DataFrame()
        self.quality_report: dict[str, Any] = {}

    @staticmethod
    def _standardize_daily_df(code: str, df: pd.DataFrame) -> pd.DataFrame:
        return standardize_daily_df(code, df)

    @staticmethod
    def _pct_rank(series: pd.Series, ascending: bool = True) -> pd.Series:
        values = pd.to_numeric(series, errors="coerce").replace([np.inf, -np.inf], np.nan)
        if values.notna().sum() <= 1:
            return pd.Series(50.0, index=series.index)
        return values.rank(pct=True, ascending=ascending) * 100.0

    def load_market_cache(self, path: str | None = None) -> pd.DataFrame:
        cache_path = path or self.market_cache_file
        self.panel_df = load_a_share_panel(cache_path, universe_size=self.config.universe_size)
        return self.panel_df

    def set_panel(self, panel_df: pd.DataFrame) -> pd.DataFrame:
        frames = []
        for code, sub in panel_df.groupby("stock_code", sort=False):
            standardized = self._standardize_daily_df(normalize_code(code), sub)
            if not standardized.empty:
                frames.append(standardized)
        if not frames:
            raise ValueError("panel_df 中没有可用的股票日线数据。")
        self.panel_df = pd.concat(frames, ignore_index=True).sort_values(["stock_code", "trade_date"])
        return self.panel_df

    def compute_features(
        self,
        stock_meta: dict[str, dict[str, Any]] | None = None,
        mine_risks: dict[str, dict[str, Any]] | None = None,
    ) -> pd.DataFrame:
        if self.panel_df.empty:
            raise RuntimeError("请先加载或设置 panel_df。")
        stock_meta = stock_meta or {}
        mine_risks = mine_risks or {}
        quality = self.config.quality

        frames = []
        reject_counts: dict[str, int] = {}
        total_codes = 0
        accepted_codes = 0

        for code, raw in self.panel_df.groupby("stock_code", sort=False):
            total_codes += 1
            code = normalize_code(code)
            meta = stock_meta.get(code, {})
            short_name = str(meta.get("short_name") or "").strip()
            reject_reasons = self._quality_reject_reasons(code, raw, meta, mine_risks.get(code), short_name)
            if reject_reasons:
                for reason in reject_reasons:
                    reject_counts[reason] = reject_counts.get(reason, 0) + 1
                continue

            dfx = raw.copy().sort_values("trade_date").reset_index(drop=True)
            dfx = self._append_indicators(dfx)
            dfx["short_name"] = short_name
            accepted_codes += 1
            frames.append(dfx)

        if frames:
            features = pd.concat(frames, ignore_index=True).sort_values(["stock_code", "trade_date"])
            features = self._score_features(features)
        else:
            features = pd.DataFrame()

        self.feature_df = features
        self.quality_report = {
            "initial_stock_count": total_codes,
            "accepted_stock_count": accepted_codes,
            "rejected_stock_count": total_codes - accepted_codes,
            "reject_counts": reject_counts,
            "quality_gate": {
                "min_history_days": quality.min_history_days,
                "min_listing_days": quality.min_listing_days,
                "min_amount_ma20": quality.min_amount_ma20,
                "min_valid_days20": quality.min_valid_days20,
                "min_price": quality.min_price,
                "max_drawdown60": quality.max_drawdown60,
                "max_limit_like_days20": quality.max_limit_like_days20,
                "max_extreme_range_days20": quality.max_extreme_range_days20,
                "min_mine_score": quality.min_mine_score,
            },
        }
        return self.feature_df

    def _quality_reject_reasons(
        self,
        code: str,
        raw: pd.DataFrame,
        meta: dict[str, Any],
        mine_risk: dict[str, Any] | None,
        short_name: str,
    ) -> list[str]:
        quality = self.config.quality
        reasons = []
        dfx = raw.copy().sort_values("trade_date")
        if not is_supported_a_share_code(code):
            reasons.append("非支持A股代码")
        if is_excluded_short_name(short_name):
            reasons.append("ST退市或非普通股")
        if len(dfx) < quality.min_history_days:
            reasons.append("历史数据不足")

        list_date = pd.to_datetime(meta.get("list_date"), errors="coerce")
        latest_date = pd.to_datetime(dfx["trade_date"].max(), errors="coerce")
        if pd.notna(list_date) and pd.notna(latest_date) and (latest_date - list_date).days < quality.min_listing_days:
            reasons.append("上市时间不足")

        last = self._append_indicators(dfx).iloc[-1]
        if pd.notna(last.get("close")) and float(last["close"]) < quality.min_price:
            reasons.append("股价过低")
        if pd.isna(last.get("amount_ma20")) or float(last["amount_ma20"]) < quality.min_amount_ma20:
            reasons.append("流动性不足")
        if pd.isna(last.get("valid_days20")) or int(last["valid_days20"]) < quality.min_valid_days20:
            reasons.append("有效交易日不足")
        if pd.notna(last.get("drawdown60")) and float(last["drawdown60"]) > quality.max_drawdown60:
            reasons.append("近60日回撤过大")
        if pd.notna(last.get("limit_like_days20")) and int(last["limit_like_days20"]) > quality.max_limit_like_days20:
            reasons.append("极端涨跌停过多")
        if pd.notna(last.get("extreme_range_days20")) and int(last["extreme_range_days20"]) > quality.max_extreme_range_days20:
            reasons.append("极端振幅过多")

        if mine_risk:
            score = pd.to_numeric(pd.Series([mine_risk.get("score")]), errors="coerce").iloc[0]
            reason_text = str(mine_risk.get("reason") or "")
            major_words = ("退市", "立案", "调查", "诉讼", "资金占用", "违规担保", "债务", "处罚", "冻结")
            if pd.notna(score) and float(score) < quality.min_mine_score:
                reasons.append("扫雷分过低")
            elif any(word in reason_text for word in major_words):
                reasons.append("重大风险项")
        return reasons

    @staticmethod
    def _append_indicators(dfx: pd.DataFrame) -> pd.DataFrame:
        out = dfx.copy().sort_values("trade_date").reset_index(drop=True)
        out["pre_close"] = out["pre_close"].fillna(out["close"].shift(1))
        out["ret_1d"] = out["close"] / out["pre_close"] - 1.0
        out["ret_5d"] = out["close"] / out["close"].shift(5) - 1.0
        out["ret_20d"] = out["close"] / out["close"].shift(20) - 1.0
        out["ma20"] = out["close"].rolling(20, min_periods=15).mean()
        out["ma60"] = out["close"].rolling(60, min_periods=40).mean()
        out["ma120"] = out["close"].rolling(120, min_periods=80).mean()
        out["ma60_slope20"] = out["ma60"] / out["ma60"].shift(20) - 1.0
        out["amount_ma20"] = out["amount"].rolling(20, min_periods=10).mean()
        out["amount_ma5"] = out["amount"].rolling(5, min_periods=3).mean()
        out["valid_days20"] = out["amount"].gt(0).rolling(20, min_periods=1).sum()

        prev_close = out["pre_close"].replace(0, np.nan)
        true_range = pd.concat(
            [
                out["high"] - out["low"],
                (out["high"] - prev_close).abs(),
                (out["low"] - prev_close).abs(),
            ],
            axis=1,
        ).max(axis=1)
        out["atr14"] = true_range.rolling(14, min_periods=10).mean()
        out["atr_pct"] = out["atr14"] / out["close"].replace(0, np.nan)
        out["range_pct"] = (out["high"] - out["low"]) / out["pre_close"].replace(0, np.nan)
        out["range_ma20"] = out["range_pct"].rolling(20, min_periods=10).mean()
        out["range_ma60"] = out["range_pct"].rolling(60, min_periods=30).mean()
        out["volatility20"] = out["ret_1d"].rolling(20, min_periods=12).std()
        out["volatility60"] = out["ret_1d"].rolling(60, min_periods=30).std()
        out["high20"] = out["high"].rolling(20, min_periods=12).max()
        out["low20"] = out["low"].rolling(20, min_periods=12).min()
        out["high60"] = out["high"].rolling(60, min_periods=30).max()
        out["low60"] = out["low"].rolling(60, min_periods=30).min()
        out["drawdown60"] = 1.0 - out["close"] / out["high60"].replace(0, np.nan)
        out["squeeze_ratio"] = out["range_ma20"] / out["range_ma60"].replace(0, np.nan)
        out["amount_ratio1_20"] = out["amount"] / out["amount_ma20"].replace(0, np.nan)
        out["amount_ratio5_20"] = out["amount_ma5"] / out["amount_ma20"].replace(0, np.nan)
        out["close_to_high20"] = out["close"] / out["high20"].replace(0, np.nan)
        out["close_to_ma20"] = out["close"] / out["ma20"].replace(0, np.nan) - 1.0
        out["close_to_ma60"] = out["close"] / out["ma60"].replace(0, np.nan) - 1.0
        out["close_to_ma120"] = out["close"] / out["ma120"].replace(0, np.nan) - 1.0
        out["limit_like_days20"] = out["ret_1d"].abs().ge(0.095).rolling(20, min_periods=1).sum()
        out["extreme_range_days20"] = out["range_pct"].ge(0.16).rolling(20, min_periods=1).sum()
        out["close_pos"] = ((out["close"] - out["low"]) / (out["high"] - out["low"]).replace(0, np.nan)).clip(0, 1)
        return out

    def _score_features(self, features: pd.DataFrame) -> pd.DataFrame:
        out = features.copy()
        grouped = out.groupby("trade_date", group_keys=False)
        out["squeeze_score"] = (
            grouped["squeeze_ratio"].transform(lambda s: self._pct_rank(s, ascending=False)) * 0.45
            + grouped.apply(lambda g: self._pct_rank(g["volatility20"] / g["volatility60"], ascending=False))
            .reset_index(level=0, drop=True)
            .reindex(out.index)
            * 0.25
            + grouped["amount_ratio5_20"].transform(lambda s: self._pct_rank(s, ascending=True)) * 0.15
            + grouped.apply(lambda g: self._pct_rank(g["close_to_ma60"].abs(), ascending=False))
            .reset_index(level=0, drop=True)
            .reindex(out.index)
            * 0.15
        )
        out["expansion_score"] = (
            grouped["range_pct"].transform(lambda s: self._pct_rank(s, ascending=True)) * 0.30
            + grouped["amount_ratio1_20"].transform(lambda s: self._pct_rank(s, ascending=True)) * 0.25
            + grouped["amount_ratio5_20"].transform(lambda s: self._pct_rank(s, ascending=True)) * 0.10
            + grouped["close_to_high20"].transform(lambda s: self._pct_rank(s, ascending=True)) * 0.20
            + grouped["ret_1d"].transform(lambda s: self._pct_rank(s, ascending=True)) * 0.15
        )
        out["anomaly_score"] = (
            grouped["range_pct"].transform(lambda s: self._pct_rank(s, ascending=True)) * 0.35
            + grouped["amount_ratio5_20"].transform(lambda s: self._pct_rank(s, ascending=True)) * 0.30
            + grouped.apply(lambda g: self._pct_rank(g["ret_1d"].abs(), ascending=True))
            .reset_index(level=0, drop=True)
            .reindex(out.index)
            * 0.20
            + grouped["drawdown60"].transform(lambda s: self._pct_rank(s, ascending=True)) * 0.15
        )
        return out

    def latest_signals(self, trade_date: str | pd.Timestamp | None = None) -> pd.DataFrame:
        if self.feature_df.empty:
            raise RuntimeError("请先调用 compute_features。")
        signal_date, day = self._resolve_signal_day(trade_date)
        if day.empty:
            return pd.DataFrame()

        signals = []
        signals.append(self._select_signal(day, "波动收敛", "squeeze_score", self.config.squeeze_limit))
        signals.append(self._select_signal(day, "波动扩张", "expansion_score", self.config.expansion_limit))
        signals.append(self._select_signal(day, "异常波动", "anomaly_score", self.config.anomaly_limit))
        result = pd.concat([df for df in signals if not df.empty], ignore_index=True) if signals else pd.DataFrame()
        if result.empty:
            return result
        result = result.sort_values(["signal_type", "score"], ascending=[True, False])
        return result.drop_duplicates(["stock_code", "signal_type"]).reset_index(drop=True)

    def _resolve_signal_day(self, trade_date: str | pd.Timestamp | None = None) -> tuple[pd.Timestamp, pd.DataFrame]:
        features = self.feature_df.copy()
        if trade_date is None:
            signal_date = features["trade_date"].max()
        else:
            requested = pd.to_datetime(trade_date)
            dates = sorted(d for d in features["trade_date"].drop_duplicates().tolist() if d <= requested)
            if not dates:
                raise RuntimeError(f"波动策略没有覆盖日期: {trade_date}")
            signal_date = dates[-1]
        return signal_date, features[features["trade_date"].eq(signal_date)].copy()

    def _signal_conditions(self, day: pd.DataFrame, signal_type: str) -> tuple[dict[str, pd.Series], pd.Series, str, int]:
        source = day.copy()
        if signal_type == "波动收敛":
            trend_ok = (
                source["ma60_slope20"].ge(self.config.min_squeeze_ma60_slope20)
                | source["close_to_ma120"].ge(self.config.min_squeeze_close_to_ma120)
                | source["ma120"].isna()
            )
            conditions = {
                "波动收敛": source["squeeze_ratio"].between(0.35, 0.85),
                "贴近60日线": source["close_to_ma60"].between(-0.08, 0.12),
                "20日涨跌适中": source["ret_20d"].between(-0.18, 0.30),
                "趋势未破坏": trend_ok,
            }
            return conditions, self._and_conditions(conditions), "squeeze_score", self.config.squeeze_limit
        if signal_type == "波动扩张":
            perfect_bear = (
                (source["close"] < source["ma20"])
                & (source["ma20"] < source["ma60"])
                & (source["ma60"] < source["ma120"])
            )
            conditions = {
                "当日振幅放大": source["range_pct"] >= source["range_ma20"] * 1.35,
                "当日成交额放大": source["amount_ratio1_20"] >= self.config.min_expansion_amount_ratio1_20,
                "5日成交额活跃": source["amount_ratio5_20"] >= 1.05,
                "收盘站上20日线": source["close"] >= source["ma20"],
                "当日上涨": source["ret_1d"] > 0,
                "非空头排列": ~perfect_bear.fillna(False),
            }
            return conditions, self._and_conditions(conditions), "expansion_score", self.config.expansion_limit

        anomaly_conditions = {
            "当日振幅异常": source["range_pct"] >= source["range_ma20"] * 1.8,
            "5日成交额异常": source["amount_ratio5_20"] >= 2.2,
            "当日涨跌异常": source["ret_1d"].abs() >= 0.085,
        }
        mask = self._or_conditions(anomaly_conditions)
        return anomaly_conditions, mask, "anomaly_score", self.config.anomaly_limit

    @staticmethod
    def _and_conditions(conditions: dict[str, pd.Series]) -> pd.Series:
        if not conditions:
            return pd.Series(dtype=bool)
        values = list(conditions.values())
        mask = values[0].fillna(False)
        for item in values[1:]:
            mask = mask & item.fillna(False)
        return mask

    @staticmethod
    def _or_conditions(conditions: dict[str, pd.Series]) -> pd.Series:
        if not conditions:
            return pd.Series(dtype=bool)
        values = list(conditions.values())
        mask = values[0].fillna(False)
        for item in values[1:]:
            mask = mask | item.fillna(False)
        return mask

    def signal_funnel_summary(self, trade_date: str | pd.Timestamp | None = None) -> dict[str, Any]:
        if self.feature_df.empty:
            raise RuntimeError("请先调用 compute_features。")
        signal_date, day = self._resolve_signal_day(trade_date)
        summary: dict[str, Any] = {
            "signal_date": pd.to_datetime(signal_date).strftime("%Y-%m-%d"),
            "scanned_count": int(len(day)),
            "signals": {},
        }
        for signal_type in ("波动收敛", "波动扩张", "异常波动"):
            conditions, mask, score_col, limit = self._signal_conditions(day, signal_type)
            score_mask = day[score_col].notna()
            selected = day[mask & score_mask].sort_values(score_col, ascending=False).head(limit)
            summary["signals"][signal_type] = {
                "condition_counts": {name: int(series.fillna(False).sum()) for name, series in conditions.items()},
                "passed_count": int(mask.sum()),
                "score_available_count": int((mask & score_mask).sum()),
                "selected_limit": int(limit),
                "selected_count": int(len(selected)),
            }
        return summary

    def _select_signal(self, day: pd.DataFrame, signal_type: str, score_col: str, limit: int) -> pd.DataFrame:
        source = day.copy()
        _conditions, mask, _score_col, _limit = self._signal_conditions(source, signal_type)
        selected = source[mask & source[score_col].notna()].sort_values(score_col, ascending=False).head(limit)
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
            signal_type == "波动收敛",
            out["high20"] * 1.01,
            np.maximum(out["ma20"], out["close"] * 0.985),
        )
        out["invalid_price"] = np.minimum(out["ma20"], out["low20"] * 0.98)
        out["anomaly_category"] = np.where(
            signal_type == "异常波动",
            out.apply(self._anomaly_category, axis=1),
            "",
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
            "range_pct",
            "squeeze_ratio",
            "amount_ma20",
            "amount_ratio1_20",
            "amount_ratio5_20",
            "close_to_ma20",
            "close_to_ma60",
            "close_to_ma120",
            "ma60_slope20",
            "drawdown60",
            "anomaly_category",
            "watch_price",
            "invalid_price",
            "reason",
        ]
        return out[keep_cols]

    @staticmethod
    def _anomaly_category(row: pd.Series) -> str:
        ret_1d = float(row.get("ret_1d", 0.0)) if pd.notna(row.get("ret_1d")) else 0.0
        amount_ratio = float(row.get("amount_ratio5_20", 0.0)) if pd.notna(row.get("amount_ratio5_20")) else 0.0
        close_to_high20 = float(row.get("close_to_high20", 0.0)) if pd.notna(row.get("close_to_high20")) else 0.0
        drawdown60 = float(row.get("drawdown60", 0.0)) if pd.notna(row.get("drawdown60")) else 0.0

        high_position = close_to_high20 >= 0.94 or drawdown60 <= 0.08
        low_position = drawdown60 >= 0.25
        volume_spike = amount_ratio >= 1.6
        if ret_1d >= 0.03 and volume_spike:
            return "异常放量上涨"
        if ret_1d <= -0.03 and volume_spike:
            return "异常放量下跌"
        if high_position:
            return "高位巨震"
        if low_position:
            return "低位异动"
        return "异常震荡"

    @staticmethod
    def _reason_text(row: pd.Series) -> str:
        parts = []
        if row.get("signal_type") == "异常波动" and row.get("anomaly_category"):
            parts.append(f"异常分类={row['anomaly_category']}")
        if pd.notna(row.get("squeeze_ratio")):
            parts.append(f"20日振幅/60日振幅={row['squeeze_ratio']:.2f}")
        if pd.notna(row.get("amount_ratio5_20")):
            parts.append(f"5日成交额/20日={row['amount_ratio5_20']:.2f}")
        if pd.notna(row.get("amount_ratio1_20")):
            parts.append(f"当日成交额/20日={row['amount_ratio1_20']:.2f}")
        if pd.notna(row.get("close_to_ma60")):
            parts.append(f"距60日线={row['close_to_ma60'] * 100:.1f}%")
        if pd.notna(row.get("ma60_slope20")):
            parts.append(f"60日线20日斜率={row['ma60_slope20'] * 100:.1f}%")
        if pd.notna(row.get("drawdown60")):
            parts.append(f"距60日高点回撤={row['drawdown60'] * 100:.1f}%")
        return "；".join(parts)
