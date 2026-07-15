import unittest

import numpy as np
import pandas as pd

from jobs.trend.run_daily import _build_email_body, _to_output_df
from strategies.trend import TrendTradingConfig, TrendTradingStrategy
from strategies.volatility import QualityGateConfig


def _trend_frame(code: str, mode: str, n_days: int = 240) -> pd.DataFrame:
    dates = pd.bdate_range("2025-01-01", periods=n_days)
    close = np.linspace(8.0, 15.0, n_days)
    close[-80:] = np.linspace(11.0, 16.0, 80)
    open_ = close * 0.997
    high = close * 1.012
    low = close * 0.988
    amount = np.full(n_days, 180_000_000.0)
    if mode == "breakout":
        close[-1] = high[-21:-1].max() * 1.025
        open_[-1] = close[-1] * 0.98
        high[-1] = close[-1] * 1.01
        low[-1] = open_[-1] * 0.995
        amount[-1] = 300_000_000.0
    elif mode == "pullback":
        close[-8:] = np.array([16.0, 15.92, 15.86, 15.80, 15.76, 15.73, 15.75, 15.82])
        open_[-1] = close[-1] * 0.995
        high[-1] = close[-1] * 1.01
        low[-1] = close[-20:].mean() * 0.997
    volume = amount / close
    return pd.DataFrame(
        {
            "stock_code": code,
            "trade_date": dates,
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
            "amount": amount,
            "pre_close": np.r_[close[0], close[:-1]],
        }
    )


class TrendTradingStrategyTest(unittest.TestCase):
    def _strategy(self) -> TrendTradingStrategy:
        return TrendTradingStrategy(
            TrendTradingConfig(
                quality=QualityGateConfig(min_amount_ma20=50_000_000, min_price=2.0),
                universe_size=None,
                breakout_limit=10,
                pullback_limit=10,
                min_ma60_slope20=0.01,
            )
        )

    def test_finds_breakout_and_pullback_candidates(self):
        strategy = self._strategy()
        strategy.set_panel(pd.concat([_trend_frame("600001", "breakout"), _trend_frame("600002", "pullback")]))
        strategy.compute_features(
            stock_meta={
                "600001": {"short_name": "突破股", "list_date": pd.Timestamp("2020-01-01")},
                "600002": {"short_name": "回踩股", "list_date": pd.Timestamp("2020-01-01")},
            }
        )

        signals = strategy.latest_signals()

        self.assertIn("趋势突破", set(signals["signal_type"]))
        self.assertIn("趋势回踩", set(signals["signal_type"]))
        self.assertTrue({"watch_price", "invalid_price", "reason"}.issubset(signals.columns))

    def test_rejects_broken_downtrend(self):
        frame = _trend_frame("600003", "normal")
        frame["close"] = np.linspace(18.0, 8.0, len(frame))
        frame["open"] = frame["close"] * 1.003
        frame["high"] = frame["close"] * 1.01
        frame["low"] = frame["close"] * 0.99
        frame["pre_close"] = np.r_[frame["close"].iloc[0], frame["close"].iloc[:-1]]
        strategy = self._strategy()
        strategy.set_panel(frame)
        strategy.compute_features(stock_meta={"600003": {"short_name": "下跌股", "list_date": "2020-01-01"}})

        self.assertTrue(strategy.latest_signals().empty)

    def test_output_and_email_expose_trade_levels(self):
        strategy = self._strategy()
        strategy.set_panel(_trend_frame("600001", "breakout"))
        strategy.compute_features(stock_meta={"600001": {"short_name": "突破股", "list_date": "2020-01-01"}})
        output = _to_output_df(strategy.latest_signals())
        summary = {
            "run_time": "2026-07-14 18:00:00",
            "trade_date": "2026-07-14",
            "signal_date": "2026-07-14",
            "candidate_count": len(output),
            "quality_report": strategy.quality_report,
        }

        body = _build_email_body(summary, output)

        self.assertIn("观察价", output.columns)
        self.assertIn("失效价", output.columns)
        self.assertIn("趋势突破", body)
        self.assertIn("不构成个性化投资建议", body)
