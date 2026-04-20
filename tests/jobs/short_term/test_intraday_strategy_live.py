# -*- coding: utf-8 -*-
import datetime as dt
import os
import tempfile
import unittest
from unittest.mock import patch

import pandas as pd

from jobs.short_term.intraday_strategy_live import IntradaySignalStrategy


def _minute_frame(rows):
    return pd.DataFrame(rows)


class IntradayStrategyMinuteCacheTest(unittest.TestCase):
    def setUp(self):
        self.strategy = IntradaySignalStrategy(candidate_size=1)

    def _write_cached_minute(self, base_dir: str, trade_date: str, code: str, df: pd.DataFrame):
        out_dir = os.path.join(base_dir, "minute_live", trade_date)
        os.makedirs(out_dir, exist_ok=True)
        df.to_csv(os.path.join(out_dir, f"{code}.csv"), index=False, encoding="utf-8-sig")

    def test_morning_run_keeps_cached_minute_data(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            self.strategy.minute_cache_dir = os.path.join(tmpdir, "minute_live")
            cached_df = _minute_frame(
                [
                    {
                        "trade_time": "2026-04-17 09:30:00",
                        "price": 10.0,
                        "change": 0.0,
                        "change_pct": 1.0,
                        "volume": 100,
                        "avg_price": 10.0,
                        "amount": 1000,
                    },
                    {
                        "trade_time": "2026-04-17 09:31:00",
                        "price": 10.1,
                        "change": 0.1,
                        "change_pct": 1.1,
                        "volume": 120,
                        "avg_price": 10.05,
                        "amount": 1212,
                    },
                ]
            )
            self._write_cached_minute(tmpdir, "2026-04-17", "000001", cached_df)

            with patch.object(
                self.strategy,
                "_now_shanghai",
                return_value=dt.datetime(2026, 4, 17, 10, 20, tzinfo=dt.timezone(dt.timedelta(hours=8))),
            ), patch("jobs.short_term.intraday_strategy_live.adata.stock.market.get_market_min") as mocked_fetch:
                result = self.strategy.fetch_minute_data("000001", trade_date="2026-04-17", prefer_cache=True)

            self.assertEqual(len(result), 2)
            self.assertListEqual(result["time_only"].tolist(), ["09:30:00", "09:31:00"])
            mocked_fetch.assert_not_called()

    def test_afternoon_run_refreshes_and_merges_minute_data(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            self.strategy.minute_cache_dir = os.path.join(tmpdir, "minute_live")
            cached_df = _minute_frame(
                [
                    {
                        "trade_time": "2026-04-17 09:30:00",
                        "price": 10.0,
                        "change": 0.0,
                        "change_pct": 1.0,
                        "volume": 100,
                        "avg_price": 10.0,
                        "amount": 1000,
                    },
                    {
                        "trade_time": "2026-04-17 09:31:00",
                        "price": 10.1,
                        "change": 0.1,
                        "change_pct": 1.1,
                        "volume": 120,
                        "avg_price": 10.05,
                        "amount": 1212,
                    },
                ]
            )
            fresh_df = _minute_frame(
                [
                    {
                        "trade_time": "2026-04-17 09:30:00",
                        "price": 10.0,
                        "change": 0.0,
                        "change_pct": 1.0,
                        "volume": 100,
                        "avg_price": 10.0,
                        "amount": 1000,
                    },
                    {
                        "trade_time": "2026-04-17 09:31:00",
                        "price": 10.1,
                        "change": 0.1,
                        "change_pct": 1.1,
                        "volume": 120,
                        "avg_price": 10.05,
                        "amount": 1212,
                    },
                    {
                        "trade_time": "2026-04-17 13:05:00",
                        "price": 10.4,
                        "change": 0.4,
                        "change_pct": 4.0,
                        "volume": 180,
                        "avg_price": 10.25,
                        "amount": 1872,
                    },
                    {
                        "trade_time": "2026-04-17 14:29:00",
                        "price": 10.6,
                        "change": 0.6,
                        "change_pct": 5.5,
                        "volume": 220,
                        "avg_price": 10.38,
                        "amount": 2332,
                    },
                ]
            )
            self._write_cached_minute(tmpdir, "2026-04-17", "000001", cached_df)

            with patch.object(
                self.strategy,
                "_now_shanghai",
                return_value=dt.datetime(2026, 4, 17, 14, 30, tzinfo=dt.timezone(dt.timedelta(hours=8))),
            ), patch(
                "jobs.short_term.intraday_strategy_live.adata.stock.market.get_market_min",
                return_value=fresh_df,
            ) as mocked_fetch:
                result = self.strategy.fetch_minute_data("000001", trade_date="2026-04-17", prefer_cache=True)

            self.assertEqual(len(result), 4)
            self.assertListEqual(
                result["time_only"].tolist(),
                ["09:30:00", "09:31:00", "13:05:00", "14:29:00"],
            )
            self.assertGreaterEqual(result["cum_amount"].iloc[-1], result["cum_amount"].iloc[-2])
            mocked_fetch.assert_called_once()


if __name__ == "__main__":
    unittest.main()
