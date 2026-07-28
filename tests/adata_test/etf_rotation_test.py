import unittest
from argparse import Namespace
from pathlib import Path
from tempfile import TemporaryDirectory

import numpy as np
import pandas as pd

from jobs.etf_rotation.backtest import RotationConfig, _target_weights, run_backtest
from jobs.etf_rotation.run import build_config


class EtfRotationTestCase(unittest.TestCase):
    def test_optimized_profile_uses_short_trend_with_independent_volatility(self):
        config = build_config(
            Namespace(
                profile="optimized",
                capital=100_000.0,
                cost=0.001,
                rebalance_threshold=0.02,
            )
        )
        self.assertEqual("optimized", config.profile_name)
        self.assertEqual(20, config.long_window)
        self.assertEqual(20, config.volatility_window)
        self.assertEqual((0.0, 0.0, 1.0), config.momentum_weights)
        self.assertEqual(1.0, config.leader_weight)
        self.assertEqual(0.30, config.medium_volatility)
        self.assertEqual(0.40, config.high_volatility)

    def test_screenshot_profile_matches_published_rule_shape(self):
        config = build_config(
            Namespace(
                profile="screenshot",
                capital=100_000.0,
                cost=0.001,
                rebalance_threshold=0.02,
            )
        )
        self.assertEqual(20, config.long_window)
        self.assertEqual((0.0, 0.0, 1.0), config.momentum_weights)
        self.assertEqual(1.0, config.leader_weight)
        self.assertFalse(config.require_medium_return_positive)

    def test_target_weights_apply_trend_and_volatility_filters(self):
        row = pd.Series(
            {
                "513100_close": 120.0,
                "513100_trend_ma": 100.0,
                "513100_ret_medium": 0.20,
                "513100_score": 0.18,
                "513100_vol_short": 0.25,
                "159915_close": 110.0,
                "159915_trend_ma": 100.0,
                "159915_ret_medium": 0.10,
                "159915_score": 0.10,
                "159915_vol_short": 0.35,
            }
        )
        weights, leader, reason = _target_weights(row, ["513100", "159915"], RotationConfig(), None)
        self.assertEqual("513100", leader)
        self.assertAlmostEqual(0.56, weights["513100"])
        self.assertAlmostEqual(0.24, weights["159915"])
        self.assertIn("波动率降仓", reason)

    def test_single_asset_target_uses_trend_and_volatility_rules(self):
        config = build_config(
            Namespace(
                profile="optimized",
                capital=100_000.0,
                cost=0.001,
                rebalance_threshold=0.02,
            )
        )
        row = pd.Series(
            {
                "513100_close": 120.0,
                "513100_trend_ma": 100.0,
                "513100_ret_medium": 0.10,
                "513100_score": 0.10,
                "513100_vol_short": 0.35,
            }
        )
        weights, leader, reason = _target_weights(row, ["513100"], config, None)
        self.assertEqual("513100", leader)
        self.assertAlmostEqual(0.80, weights["513100"])
        self.assertIn("波动率降仓", reason)

        row["513100_close"] = 90.0
        weights, leader, reason = _target_weights(row, ["513100"], config, leader)
        self.assertIsNone(leader)
        self.assertEqual(0.0, weights["513100"])
        self.assertIn("未通过趋势过滤", reason)

    def test_signal_is_executed_on_next_trading_day_open(self):
        dates = pd.bdate_range("2023-01-02", periods=180)
        frames = {}
        for code, growth in [("513100", 0.0020), ("159915", 0.0005)]:
            close = 100 * np.cumprod(np.repeat(1 + growth, len(dates)))
            frames[code] = pd.DataFrame(
                {
                    "trade_date": dates,
                    "open": close * 0.999,
                    "close": close,
                }
            )
        result = run_backtest(frames, start_date="2023-07-03")
        nasdaq_only = run_backtest({"513100": frames["513100"]}, start_date="2023-07-03")
        first_signal = result.signals.iloc[0]
        first_trade = result.trades.iloc[0]
        self.assertGreater(pd.Timestamp(first_trade["trade_date"]), pd.Timestamp(first_signal["signal_date"]))
        self.assertEqual(pd.Timestamp(first_trade["trade_date"]), pd.Timestamp(first_signal["execute_date"]))
        self.assertGreater(result.summary["trade_count"], 0)
        self.assertLess(result.summary["transaction_cost"], RotationConfig().initial_capital)

        with TemporaryDirectory() as output_dir:
            result.write(output_dir, comparisons={"nasdaq_only": nasdaq_only})
            report = (Path(output_dir) / "report.html").read_text(encoding="utf-8")
            self.assertIn("ETF轮动回测报告", report)
            self.assertIn("历史回撤", report)
            self.assertIn("成交点K线", report)
            self.assertIn('data-code="513100"', report)
            self.assertIn('data-code="159915"', report)
            self.assertIn('data-chart-id="nasdaq_only"', report)
            self.assertIn("仅纳指ETF择时", report)
            self.assertIn('"side":"BUY"', report)
            self.assertTrue((Path(output_dir) / "nasdaq_only_trades.csv").exists())
            self.assertGreater(nasdaq_only.summary["trade_count"], 0)
            self.assertIn("benchmark_513100", result.nav.columns)
            for code in ("513100", "159915"):
                self.assertIn(f"open_{code}", result.nav.columns)
                self.assertIn(f"high_{code}", result.nav.columns)
                self.assertIn(f"low_{code}", result.nav.columns)
                self.assertIn(f"close_{code}", result.nav.columns)
                self.assertTrue((result.nav[f"high_{code}"] >= result.nav[f"open_{code}"]).all())
                self.assertTrue((result.nav[f"low_{code}"] <= result.nav[f"close_{code}"]).all())


if __name__ == "__main__":
    unittest.main()
