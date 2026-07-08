# -*- coding: utf-8 -*-
import datetime as dt
import unittest

import pandas as pd

from jobs.common.market_data_context import resolve_market_data_context


def _calendar(_year: int) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "trade_date": ["2026-04-16", "2026-04-17", "2026-04-20"],
            "trade_status": [1, 1, 1],
        }
    )


class MarketDataContextTest(unittest.TestCase):
    def test_intraday_uses_previous_daily_and_today_intraday(self):
        context = resolve_market_data_context(
            now=dt.datetime(2026, 4, 17, 10, 20),
            trade_calendar_loader=_calendar,
        )

        self.assertEqual(context.session, "morning")
        self.assertEqual(context.daily_target_date, "2026-04-16")
        self.assertEqual(context.intraday_date, "2026-04-17")
        self.assertFalse(context.allow_today_daily)
        self.assertTrue(context.should_fetch_intraday)

    def test_after_close_allows_today_daily(self):
        context = resolve_market_data_context(
            now=dt.datetime(2026, 4, 17, 15, 45),
            trade_calendar_loader=_calendar,
        )

        self.assertEqual(context.session, "after_close")
        self.assertEqual(context.daily_target_date, "2026-04-17")
        self.assertTrue(context.allow_today_daily)
        self.assertFalse(context.should_fetch_intraday)

    def test_non_trading_uses_latest_completed_trade_date(self):
        context = resolve_market_data_context(
            now=dt.datetime(2026, 4, 18, 10, 20),
            trade_calendar_loader=_calendar,
        )

        self.assertEqual(context.session, "non_trading")
        self.assertEqual(context.daily_target_date, "2026-04-17")
        self.assertFalse(context.should_fetch_intraday)


if __name__ == "__main__":
    unittest.main()
