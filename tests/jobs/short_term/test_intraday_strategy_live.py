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


def _minute_row(trade_time: str, price: float, change_pct: float, volume: int = 100_000):
    return {
        "trade_time": trade_time,
        "price": price,
        "change": price - 10.0,
        "change_pct": change_pct,
        "volume": volume,
        "avg_price": price,
        "amount": price * volume,
    }


def _daily_row(**overrides):
    data = {
        "open": 10.2,
        "close": 10.8,
        "high": 10.9,
        "low": 10.05,
        "pre_close": 10.0,
        "pct_change": 8.0,
        "ma5": 10.0,
        "ma10": 9.7,
        "ma20": 9.4,
        "amount": 250_000_000,
        "amt_ma5": 150_000_000,
        "turnover_ratio": 8.0,
        "turn_ma5": 5.0,
        "ret5": 8.0,
        "ret10": 12.0,
        "ret20": 25.0,
        "upper_shadow_pct": 1.0,
        "close_pos": 0.88,
        "close_hh30_prev": 10.5,
        "high_hh10_prev": 10.8,
        "prev_pct_change": 2.0,
        "gap_open_pct": 2.0,
    }
    data.update(overrides)
    return pd.Series(data)


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

    def test_signal_waits_for_three_minute_confirmation(self):
        strategy = IntradaySignalStrategy(
            candidate_size=1,
            signal_start_time="09:46:00",
            signal_end_time="09:50:00",
            min_intraday_amount=0,
            min_first30_amount_ratio=0.0,
        )
        rows = [_minute_row(f"2026-04-24 09:{minute:02d}:00", 10.0, 0.0) for minute in range(30, 46)]
        rows.extend(
            [
                _minute_row("2026-04-24 09:46:00", 10.15, 2.1),
                _minute_row("2026-04-24 09:47:00", 10.16, 2.2),
                _minute_row("2026-04-24 09:48:00", 10.17, 2.3),
            ]
        )
        minute_df = strategy._normalize_minute_df("000001", _minute_frame(rows))
        signal = strategy.generate_signal_for_stock(
            pd.Series(
                {
                    "code": "000001",
                    "short_name": "测试股",
                    "prev_amount": 1_000_000,
                    "prev_pct_change": 5.0,
                    "daily_score": 10.0,
                }
            ),
            minute_df,
        )

        self.assertEqual(signal["signal_time"], "2026-04-24 09:48:00")

    def test_single_minute_pulse_does_not_trigger_signal(self):
        strategy = IntradaySignalStrategy(
            candidate_size=1,
            signal_start_time="09:46:00",
            signal_end_time="09:50:00",
            min_intraday_amount=0,
            min_first30_amount_ratio=0.0,
        )
        rows = [_minute_row(f"2026-04-24 09:{minute:02d}:00", 10.0, 0.0) for minute in range(30, 46)]
        rows.extend(
            [
                _minute_row("2026-04-24 09:46:00", 10.55, 5.5),
                _minute_row("2026-04-24 09:47:00", 10.10, 1.0),
                _minute_row("2026-04-24 09:48:00", 10.08, 0.8),
            ]
        )
        minute_df = strategy._normalize_minute_df("000001", _minute_frame(rows))
        signal = strategy.generate_signal_for_stock(
            pd.Series(
                {
                    "code": "000001",
                    "short_name": "测试股",
                    "prev_amount": 1_000_000,
                    "prev_pct_change": 5.0,
                    "daily_score": 10.0,
                }
            ),
            minute_df,
        )

        self.assertEqual(signal, {})

    def test_daily_candidate_rejects_overheated_blowoff_volume(self):
        strategy = IntradaySignalStrategy(candidate_size=1)
        stable_row = _daily_row()
        blowoff_row = _daily_row(
            amount=800_000_000,
            ret20=85.0,
            turnover_ratio=26.0,
            upper_shadow_pct=2.7,
        )

        self.assertTrue(strategy._is_daily_candidate("603000", stable_row))
        self.assertFalse(strategy._is_daily_candidate("603000", blowoff_row))

    def test_signal_rejects_chasing_far_from_open(self):
        strategy = IntradaySignalStrategy(
            candidate_size=1,
            signal_start_time="09:46:00",
            signal_end_time="09:50:00",
            min_intraday_amount=0,
            min_first30_amount_ratio=0.0,
            max_open_to_signal_pct=0.03,
            max_vwap_extension_pct=0.20,
            max_minute_return_pct=0.20,
        )
        rows = [_minute_row(f"2026-04-24 09:{minute:02d}:00", 10.0, 0.0) for minute in range(30, 46)]
        rows.extend(
            [
                _minute_row("2026-04-24 09:46:00", 10.40, 4.0),
                _minute_row("2026-04-24 09:47:00", 10.41, 4.1),
                _minute_row("2026-04-24 09:48:00", 10.42, 4.2),
            ]
        )
        minute_df = strategy._normalize_minute_df("000001", _minute_frame(rows))
        signal = strategy.generate_signal_for_stock(
            pd.Series(
                {
                    "code": "000001",
                    "short_name": "测试股",
                    "prev_amount": 1_000_000,
                    "prev_pct_change": 5.0,
                    "daily_score": 10.0,
                }
            ),
            minute_df,
        )

        self.assertEqual(signal, {})

    def test_intraday_market_gate_blocks_weak_indexes(self):
        strategy = IntradaySignalStrategy(
            candidate_size=1,
            intraday_market_index_codes=["000300", "399006"],
            min_intraday_market_change_pct=-0.8,
        )

        def fake_index_min(index_code):
            return pd.DataFrame(
                {
                    "index_code": [index_code],
                    "change_pct": [-0.6 if index_code == "000300" else -1.1],
                }
            )

        with patch("jobs.short_term.intraday_strategy_live.adata.stock.market.get_market_index_min", fake_index_min):
            self.assertFalse(strategy._intraday_market_ok())

        self.assertEqual(strategy.last_intraday_market_snapshot["000300"], -0.6)
        self.assertEqual(strategy.last_intraday_market_snapshot["399006"], -1.1)


if __name__ == "__main__":
    unittest.main()
