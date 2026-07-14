import unittest

import pandas as pd

from strategies.three_dim_resonance.strategy import ThreeDimResonanceStrategy


class ThreeDimMarketGateTest(unittest.TestCase):
    @staticmethod
    def _strategy(rows):
        strategy = ThreeDimResonanceStrategy()
        strategy.benchmark_df = pd.DataFrame(rows).set_index("trade_date")
        return strategy

    def test_allows_controlled_pullback_below_ma20(self):
        rows = []
        for day in range(1, 7):
            rows.append(
                {
                    "trade_date": f"2026-01-0{day}",
                    "close": 100.0 if day < 6 else 99.0,
                    "ma20": 99.0 if day < 6 else 99.5,
                    "ma60": 95.0,
                    "change_pct": 0.0 if day < 6 else -1.0,
                }
            )
        status = self._strategy(rows)._market_gate_status("2026-01-06")

        self.assertTrue(status["ok"])
        self.assertEqual(status["regime"], "震荡容错")
        self.assertEqual(status["trend_passed"], 2)

    def test_blocks_large_daily_drop_even_when_trend_is_strong(self):
        rows = []
        for day in range(1, 7):
            rows.append(
                {
                    "trade_date": f"2026-01-0{day}",
                    "close": 100.0 if day < 6 else 101.0,
                    "ma20": 98.0 if day < 6 else 99.0,
                    "ma60": 95.0,
                    "change_pct": 0.0 if day < 6 else -2.1,
                }
            )
        status = self._strategy(rows)._market_gate_status("2026-01-06")

        self.assertFalse(status["ok"])
        self.assertEqual(status["regime"], "风险关闭")
        self.assertIn("触发风险底线", status["summary"])


if __name__ == "__main__":
    unittest.main()
