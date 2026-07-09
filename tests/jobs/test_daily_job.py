import unittest
from unittest.mock import patch

import pandas as pd

from jobs.common import daily_job


class DailyJobTest(unittest.TestCase):
    def test_resolve_trade_date_uses_calendar_status_and_action_text(self):
        calendar = pd.DataFrame(
            [
                {"trade_date": "2026-07-09", "trade_status": 1},
                {"trade_date": "2026-07-10", "trade_status": 0},
            ]
        )
        with patch("jobs.common.daily_job.load_trade_calendar", return_value=calendar):
            trade_date, is_trade_day, note = daily_job.resolve_trade_date(
                "2026-07-09",
                action="执行测试任务",
                skip_action="跳过测试任务",
            )

        self.assertEqual(trade_date, "2026-07-09")
        self.assertTrue(is_trade_day)
        self.assertEqual(note, "交易日，执行测试任务。")

    def test_resolve_trade_date_falls_back_to_weekday_when_calendar_fails(self):
        with patch("jobs.common.daily_job.load_trade_calendar", side_effect=RuntimeError("network")):
            trade_date, is_trade_day, note = daily_job.resolve_trade_date("2026-07-09")

        self.assertEqual(trade_date, "2026-07-09")
        self.assertTrue(is_trade_day)
        self.assertEqual(note, "交易日历不可用，已退化为工作日判断。")


if __name__ == "__main__":
    unittest.main()
