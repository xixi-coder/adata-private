import unittest

import numpy as np
import pandas as pd

from strategies.boll import BollStrategy, BollStrategyConfig
from strategies.volatility import QualityGateConfig


def _boll_frame(code: str, mode: str, n_days: int = 220, amount: float = 180_000_000) -> pd.DataFrame:
    dates = pd.bdate_range("2025-01-01", periods=n_days)
    x = np.arange(n_days)
    close = 10 + np.sin(x / 5) * 0.8
    close[-70:] = 10 + np.sin(np.arange(70) / 4) * 0.18
    open_ = close.copy()
    high = close + 0.10
    low = close - 0.10

    if mode == "buy":
        open_[-1] = 9.95
        close[-1] = 10.05
        high[-1] = 10.10
        low[-1] = 9.55
    elif mode == "sell":
        close[-20:] = np.linspace(10.0, 10.65, 20)
        open_[-1] = 10.62
        close[-1] = 10.66
        high[-1] = 11.08
        low[-1] = 10.58
    else:
        close[-1] = 10.0
        open_[-1] = 10.0
        high[-1] = 10.08
        low[-1] = 9.92

    rows = []
    for i, date in enumerate(dates):
        day_amount = amount * (1.8 if mode == "sell" and i >= n_days - 5 else 1.0)
        rows.append(
            {
                "stock_code": code,
                "trade_date": date,
                "open": open_[i],
                "high": high[i],
                "low": low[i],
                "close": close[i],
                "volume": day_amount / close[i],
                "amount": day_amount,
                "pre_close": close[i - 1] if i > 0 else close[i],
            }
        )
    return pd.DataFrame(rows)


class BollStrategyTest(unittest.TestCase):
    def test_boll_signals_include_lower_band_buy_and_upper_band_stagnation(self):
        panel = pd.concat(
            [
                _boll_frame("600100", "buy"),
                _boll_frame("600101", "sell"),
                _boll_frame("600102", "none"),
            ],
            ignore_index=True,
        )
        meta = {
            "600100": {"short_name": "下轨票", "list_date": pd.Timestamp("2020-01-01")},
            "600101": {"short_name": "上轨票", "list_date": pd.Timestamp("2020-01-01")},
            "600102": {"short_name": "普通票", "list_date": pd.Timestamp("2020-01-01")},
        }
        strategy = BollStrategy(
            BollStrategyConfig(
                quality=QualityGateConfig(min_amount_ma20=50_000_000, min_price=2.0),
                universe_size=None,
                buy_limit=10,
                sell_limit=10,
                max_bandwidth_ratio=1.20,
                max_mid_slope20=0.12,
            )
        )
        strategy.set_panel(panel)
        strategy.compute_features(stock_meta=meta)
        signals = strategy.latest_signals()

        self.assertIn("下轨止跌观察", set(signals["signal_type"]))
        self.assertIn("上轨放量滞涨", set(signals["signal_type"]))
        self.assertIn("600100", set(signals[signals["signal_type"].eq("下轨止跌观察")]["stock_code"]))
        self.assertIn("600101", set(signals[signals["signal_type"].eq("上轨放量滞涨")]["stock_code"]))

    def test_quality_gate_still_filters_st_and_illiquid_names(self):
        panel = pd.concat(
            [
                _boll_frame("600110", "buy", amount=180_000_000),
                _boll_frame("600111", "buy", amount=20_000_000),
                _boll_frame("600112", "buy", amount=180_000_000),
            ],
            ignore_index=True,
        )
        meta = {
            "600110": {"short_name": "正常票", "list_date": pd.Timestamp("2020-01-01")},
            "600111": {"short_name": "低流动", "list_date": pd.Timestamp("2020-01-01")},
            "600112": {"short_name": "ST问题", "list_date": pd.Timestamp("2020-01-01")},
        }
        strategy = BollStrategy(
            BollStrategyConfig(
                quality=QualityGateConfig(min_amount_ma20=100_000_000, min_price=2.0),
                universe_size=None,
            )
        )
        strategy.set_panel(panel)
        features = strategy.compute_features(stock_meta=meta)

        self.assertEqual(set(features["stock_code"].unique()), {"600110"})
        self.assertIn("流动性不足", strategy.quality_report["reject_counts"])
        self.assertIn("ST退市或非普通股", strategy.quality_report["reject_counts"])


if __name__ == "__main__":
    unittest.main()
