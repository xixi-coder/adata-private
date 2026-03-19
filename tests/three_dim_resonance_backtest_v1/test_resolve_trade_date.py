import datetime as dt
import unittest
from unittest.mock import patch

import pandas as pd

from jobs.three_dim_resonance.run_daily import ThreeDimResonanceLiveStrategy


class ResolveTradeDateTest(unittest.TestCase):
    def setUp(self):
        self.strategy = ThreeDimResonanceLiveStrategy()
        self.strategy.benchmark_df = pd.DataFrame(
            {"close": [1.0, 1.1]},
            index=["2026-03-17", "2026-03-18"],
        )

    def test_before_close_uses_previous_trade_day(self):
        with patch.object(self.strategy, "_now_shanghai", return_value=dt.datetime(2026, 3, 18, 11, 59, 0)):
            self.assertEqual(self.strategy._resolve_trade_date(""), "2026-03-17")

    def test_after_close_uses_same_day(self):
        with patch.object(self.strategy, "_now_shanghai", return_value=dt.datetime(2026, 3, 18, 15, 40, 0)):
            self.assertEqual(self.strategy._resolve_trade_date(""), "2026-03-18")

    def test_explicit_requested_date_is_respected(self):
        with patch.object(self.strategy, "_now_shanghai", return_value=dt.datetime(2026, 3, 18, 11, 59, 0)):
            self.assertEqual(self.strategy._resolve_trade_date("2026-03-18"), "2026-03-18")


if __name__ == "__main__":
    unittest.main()
