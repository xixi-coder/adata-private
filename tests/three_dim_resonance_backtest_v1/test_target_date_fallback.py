import os
import tempfile
import unittest
from unittest.mock import patch

from strategies.three_dim_resonance.strategy import ThreeDimResonanceStrategy


class TargetDateFallbackTest(unittest.TestCase):
    def test_fallback_prefers_clock_date_when_benchmark_is_stale(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            benchmark_file = os.path.join(tmpdir, "benchmark_000300.csv")
            with open(benchmark_file, "w", encoding="utf-8") as fh:
                fh.write("index_code,trade_date,trade_time,open,high,low,close,volume,amount,change,change_pct\n")
                fh.write("300,2026-03-13,2026-03-13 00:00:00,1,1,1,1,1,1,0,0\n")

            strategy = ThreeDimResonanceStrategy()
            strategy.benchmark_file = benchmark_file

            with patch.object(strategy, "_now_shanghai") as mocked_now:
                import datetime as dt

                mocked_now.return_value = dt.datetime(2026, 3, 18, 15, 40)
                self.assertEqual(strategy._fallback_target_date_from_benchmark("2026-03-18"), "2026-03-18")


if __name__ == "__main__":
    unittest.main()
