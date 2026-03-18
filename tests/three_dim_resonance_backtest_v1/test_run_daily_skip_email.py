import os
import tempfile
import unittest
from unittest.mock import patch

import pandas as pd

from jobs.three_dim_resonance.run_daily import ThreeDimResonanceLiveStrategy


class RunDailySkipEmailTest(unittest.TestCase):
    @patch("jobs.three_dim_resonance.run_daily.upload_file_to_drive", return_value=False)
    @patch("jobs.three_dim_resonance.run_daily.download_json_from_drive", return_value=False)
    @patch("jobs.three_dim_resonance.run_daily.sync_cache_from_drive", return_value=False)
    def test_skipped_run_reuses_pending_suggestions(self, _sync, _download, _upload):
        with tempfile.TemporaryDirectory() as tmpdir:
            strategy = ThreeDimResonanceLiveStrategy(max_positions=2, universe_size=1)
            strategy.summary_dir = tmpdir
            strategy.stock_names = {"000001": "平安银行"}
            strategy.stock_data = {
                "000001": pd.DataFrame(
                    [{"open": 9.8, "close": 10.0}],
                    index=["2026-03-13"],
                )
            }
            strategy.load_data = lambda: None
            strategy._resolve_trade_date = lambda requested_date="": "2026-03-13"
            strategy._next_trade_date = lambda trade_date: ""

            state = {
                "initial_capital": 1_000_000.0,
                "cash": 800_000.0,
                "positions": {
                    "000001": {
                        "short_name": "平安银行",
                        "buy_date": "2026-03-10",
                        "buy_price": 9.5,
                        "shares": 10_000,
                        "cost": 95_000.0,
                        "holding_days": 3,
                        "last_price": 10.0,
                    }
                },
                "pending_entries": [
                    {
                        "code": "000001",
                        "short_name": "平安银行",
                        "score": 91.236,
                        "signal_date": "2026-03-13",
                        "entry_shape": "平台突破",
                        "close_price": 10.0,
                    }
                ],
                "pending_exits": [
                    {
                        "code": "000001",
                        "reason": "trend_break",
                        "signal_date": "2026-03-13",
                    }
                ],
                "completed_trades": [],
                "last_run_trade_date": "2026-03-13",
            }
            strategy.load_state = lambda: state

            summary = strategy.run_daily()

            self.assertEqual(summary["status"], "skipped")
            self.assertEqual(summary["buy_suggestions"][0]["code"], "000001")
            self.assertEqual(summary["sell_suggestions"][0]["reason"], "趋势走弱")

            with open(os.path.join(tmpdir, "latest_email_body.txt"), "r", encoding="utf-8") as fh:
                email_body = fh.read()
            self.assertIn("本邮件沿用上次待执行建议", email_body)
            self.assertIn("建议卖出:", email_body)
            self.assertIn("趋势走弱", email_body)
            self.assertIn("建议买入:", email_body)
            self.assertIn("平安银行", email_body)


if __name__ == "__main__":
    unittest.main()
