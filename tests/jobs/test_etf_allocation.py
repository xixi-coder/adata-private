import unittest

import numpy as np
import pandas as pd

from jobs.etf_allocation.strategy import ETFAllocationConfig, build_target_weights, prepare_snapshot


class ETFAllocationTest(unittest.TestCase):
    def test_selects_one_broad_and_two_sectors(self):
        snapshot = pd.DataFrame(
            [
                {"code": "510300", "name": "沪深300ETF", "group": "宽基", "eligible": True, "score": 0.20, "close": 110, "ma200": 100, "ma60": 108, "prior_ma60": 105},
                {"code": "510500", "name": "中证500ETF", "group": "宽基", "eligible": True, "score": 0.30, "close": 110, "ma200": 100, "ma60": 108, "prior_ma60": 105},
                {"code": "512480", "name": "半导体ETF", "group": "行业", "eligible": True, "score": 0.40},
                {"code": "512400", "name": "有色金属ETF", "group": "行业", "eligible": True, "score": 0.35},
                {"code": "512800", "name": "银行ETF", "group": "行业", "eligible": True, "score": 0.10},
            ]
        )
        weights, reason, defensive = build_target_weights(snapshot)
        self.assertFalse(defensive)
        self.assertAlmostEqual(weights["510500"], 0.40)
        self.assertAlmostEqual(weights["512480"], 0.20)
        self.assertAlmostEqual(weights["512400"], 0.20)
        self.assertAlmostEqual(sum(weights.values()), 0.80)
        self.assertIn("正常仓位", reason)

    def test_benchmark_trend_failure_reduces_exposure(self):
        snapshot = pd.DataFrame(
            [
                {"code": "510300", "name": "沪深300ETF", "group": "宽基", "eligible": False, "score": -0.10, "close": 90, "ma200": 100, "ma60": 95, "prior_ma60": 98},
                {"code": "510500", "name": "中证500ETF", "group": "宽基", "eligible": True, "score": 0.20},
                {"code": "512800", "name": "银行ETF", "group": "行业", "eligible": True, "score": 0.10},
            ]
        )
        weights, _, defensive = build_target_weights(snapshot)
        self.assertTrue(defensive)
        self.assertAlmostEqual(sum(weights.values()), 0.0)

    def test_snapshot_filters_downtrend_and_illiquid_etf(self):
        dates = pd.bdate_range("2025-01-01", periods=220)
        frames = {}
        for code, growth, amount in [
            ("510300", 0.001, 100_000_000),
            ("512480", -0.001, 100_000_000),
            ("512800", 0.001, 10_000_000),
        ]:
            close = 1.0 * np.cumprod(np.repeat(1 + growth, len(dates)))
            frames[code] = pd.DataFrame(
                {"trade_date": dates, "close": close, "amount": amount}
            )
        snapshot = prepare_snapshot(frames, dates[-1], ETFAllocationConfig())
        eligible = snapshot.set_index("code")["eligible"].to_dict()
        self.assertTrue(eligible["510300"])
        self.assertFalse(eligible["512480"])
        self.assertFalse(eligible["512800"])


if __name__ == "__main__":
    unittest.main()
