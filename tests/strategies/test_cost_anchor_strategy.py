import unittest

import pandas as pd

from strategies.cost_anchor import CostAnchorConfig, CostAnchorStrategy


class CostAnchorStrategyTest(unittest.TestCase):
    def test_score_and_filter_keeps_only_near_anchor(self):
        strategy = CostAnchorStrategy(CostAnchorConfig(near_low=-0.08, near_high=0.10))
        anchors = pd.DataFrame(
            [
                {
                    "stock_code": "002557",
                    "stock_name": "洽洽食品",
                    "anchor_type": "员工持股成本",
                    "anchor_price": 23.26,
                    "current_price": 23.15,
                    "anchor_date": "2026-03-02",
                    "event_date": "2026-03-03",
                    "lockup": "",
                    "holder_name": "员工持股计划",
                    "amount": 26_897_864,
                    "source": "test",
                    "note": "",
                },
                {
                    "stock_code": "000001",
                    "stock_name": "平安银行",
                    "anchor_type": "定增价",
                    "anchor_price": 10.0,
                    "current_price": 12.0,
                    "anchor_date": "2026-03-02",
                    "event_date": "2026-03-03",
                    "lockup": "0.5年",
                    "holder_name": "机构",
                    "amount": 1_000_000,
                    "source": "test",
                    "note": "",
                },
            ]
        )

        result = strategy._score_and_filter(anchors)

        self.assertEqual(len(result), 1)
        self.assertEqual(result.iloc[0]["stock_code"], "002557")
        self.assertIn("signal", result.columns)
        self.assertLess(abs(result.iloc[0]["distance_pct"]), 0.01)

    def test_shareholder_events_with_average_price_become_anchors(self):
        strategy = CostAnchorStrategy()
        events = pd.DataFrame(
            [
                {
                    "stock_code": "600873",
                    "stock_name": "梅花生物",
                    "current_price": 10.02,
                    "holder_name": "梅花生物科技集团股份有限公司2026年员工持股计划",
                    "trade_average_price": 10.03,
                    "change_shares_10k": 627.11,
                    "change_ratio_total_pct": 0.22,
                    "start_date": "",
                    "end_date": "2026-04-23",
                    "announce_date": "2026-04-24",
                    "market": "二级市场",
                    "source": "test",
                    "note": "",
                }
            ]
        )

        anchors = strategy.build_shareholder_increase_anchors(events)

        self.assertEqual(len(anchors), 1)
        self.assertEqual(anchors.iloc[0]["anchor_type"], "员工持股成本")
        self.assertAlmostEqual(anchors.iloc[0]["anchor_price"], 10.03)
        self.assertGreater(anchors.iloc[0]["amount"], 60_000_000)


if __name__ == "__main__":
    unittest.main()
