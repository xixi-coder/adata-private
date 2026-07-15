import unittest
from unittest.mock import patch

from jobs.trend.run_daily import _read_universe_size


class TrendJobTest(unittest.TestCase):
    def test_universe_defaults_to_all_stocks(self):
        with patch.dict("os.environ", {}, clear=True):
            self.assertIsNone(_read_universe_size())

    def test_universe_accepts_all_aliases_and_debug_limit(self):
        for value in ("all", "none", "0"):
            with self.subTest(value=value), patch.dict("os.environ", {"TREND_UNIVERSE_SIZE": value}, clear=True):
                self.assertIsNone(_read_universe_size())
        with patch.dict("os.environ", {"TREND_UNIVERSE_SIZE": "100"}, clear=True):
            self.assertEqual(_read_universe_size(), 100)


if __name__ == "__main__":
    unittest.main()
