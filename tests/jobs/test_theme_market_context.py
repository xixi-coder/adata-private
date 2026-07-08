# -*- coding: utf-8 -*-
import unittest

import pandas as pd

from jobs.theme_monitor.market_context import MarketContextCollector


class _FakeMarket:
    @staticmethod
    def get_market_index_current(index_code):
        changes = {
            "000001": 0.4,
            "399001": 0.8,
            "399006": 1.2,
            "000300": 0.7,
        }
        return pd.DataFrame(
            [
                {
                    "index_code": index_code,
                    "price": 3000.0,
                    "change_pct": changes[index_code],
                    "trade_time": "2026-04-17 10:30:00",
                }
            ]
        )


class _FakeNorth:
    @staticmethod
    def north_flow_current():
        return pd.DataFrame([{"trade_time": "10:30", "net_tgt": 3_500_000_000}])


class _FakeSentiment:
    north = _FakeNorth()


class _FakeStock:
    market = _FakeMarket()


class _FakeAdata:
    stock = _FakeStock()
    sentiment = _FakeSentiment()


def _fake_yahoo(symbol):
    changes = {
        "^IXIC": 1.2,
        "^GSPC": 0.6,
        "^SOX": 2.1,
        "^N225": -0.2,
        "^KS11": 0.9,
        "^KQ11": 1.0,
        "^HSI": 0.5,
        "^HSTECH": 1.1,
    }
    return {"symbol": symbol, "price": 100.0, "change_pct": changes[symbol], "time": 1}


class ThemeMarketContextTest(unittest.TestCase):
    def test_collects_a_share_northbound_and_global_context(self):
        summary, context_df = MarketContextCollector(_FakeAdata(), yahoo_fetcher=_fake_yahoo).collect()

        self.assertEqual(summary["risk_appetite"], "强")
        self.assertEqual(summary["external_semi_tailwind"], "强")
        self.assertEqual(summary["external_ai_tailwind"], "强")
        self.assertEqual(summary["northbound_net_inflow_yi"], 35.0)
        self.assertEqual(len(context_df[context_df["scope"].eq("A股")]), 4)
        self.assertEqual(len(context_df[context_df["scope"].eq("海外")]), 8)


if __name__ == "__main__":
    unittest.main()
