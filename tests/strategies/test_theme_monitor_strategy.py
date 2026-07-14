# -*- coding: utf-8 -*-
import unittest

import pandas as pd

from strategies.theme_monitor import ThemeMonitorStrategy


class ThemeMonitorStrategyTest(unittest.TestCase):
    def test_build_theme_radar_scores_hot_theme(self):
        strategy = ThemeMonitorStrategy(top_limit=10, representative_limit=2)
        hot_stocks = pd.DataFrame(
            [
                {
                    "rank": 1,
                    "stock_code": "000001",
                    "short_name": "测试A",
                    "change_pct": 5.0,
                    "hot_value": 99,
                    "concept_tag": "机器人;减速器",
                },
                {
                    "rank": 2,
                    "stock_code": "000002",
                    "short_name": "测试B",
                    "change_pct": 4.0,
                    "hot_value": 90,
                    "concept_tag": "机器人",
                },
            ]
        )
        hot_concepts = pd.DataFrame(
            [
                {"rank": 1, "concept_code": "BK0001", "concept_name": "机器人", "change_pct": 3.0, "hot_value": 98},
                {"rank": 8, "concept_code": "BK0002", "concept_name": "算力", "change_pct": 1.0, "hot_value": 70},
            ]
        )
        hot_industries = pd.DataFrame(
            [{"rank": 2, "concept_code": "HY001", "concept_name": "机械设备", "change_pct": 2.0, "hot_value": 80}]
        )
        popularity_stocks = pd.DataFrame([{"rank": 1, "stock_code": "000001", "short_name": "测试A"}])

        radar, snapshot = strategy.build_theme_radar(
            hot_stocks=hot_stocks,
            hot_concepts=hot_concepts,
            hot_industries=hot_industries,
            popularity_stocks=popularity_stocks,
            previous_snapshot={},
        )

        self.assertFalse(radar.empty)
        top = radar.iloc[0]
        self.assertEqual(top["theme"], "机器人")
        self.assertEqual(top["hot_stock_count"], 2)
        self.assertEqual(top["popularity_overlap_count"], 1)
        self.assertIn("新晋", top["status"])
        self.assertIn("机器人", snapshot["themes"])

    def test_previous_snapshot_marks_cooling(self):
        strategy = ThemeMonitorStrategy(top_limit=10)
        previous = {"themes": {"算力": {"rank": 1, "score": 95.0}}}
        hot_concepts = pd.DataFrame(
            [{"rank": 18, "concept_code": "BK0002", "concept_name": "算力", "change_pct": -1.0, "hot_value": 30}]
        )

        radar, _ = strategy.build_theme_radar(
            hot_stocks=pd.DataFrame(),
            hot_concepts=hot_concepts,
            hot_industries=pd.DataFrame(),
            popularity_stocks=pd.DataFrame(),
            previous_snapshot=previous,
        )

        row = radar[radar["theme"].eq("算力")].iloc[0]
        self.assertEqual(row["status"], "降温")

    def test_hot_theme_with_falling_representatives_marks_divergence(self):
        strategy = ThemeMonitorStrategy(top_limit=10, representative_limit=2)
        hot_stocks = pd.DataFrame(
            [
                {
                    "rank": 1,
                    "stock_code": "000001",
                    "short_name": "测试A",
                    "change_pct": -6.0,
                    "concept_tag": "AI算力",
                },
                {
                    "rank": 2,
                    "stock_code": "000002",
                    "short_name": "测试B",
                    "change_pct": -4.0,
                    "concept_tag": "AI算力",
                },
            ]
        )
        hot_concepts = pd.DataFrame(
            [{"rank": 1, "concept_code": "BK0001", "concept_name": "AI算力", "change_pct": -3.0, "hot_value": 98}]
        )

        radar, _ = strategy.build_theme_radar(
            hot_stocks=hot_stocks,
            hot_concepts=hot_concepts,
            hot_industries=pd.DataFrame(),
            popularity_stocks=pd.DataFrame(),
            previous_snapshot={},
        )

        row = radar[radar["theme"].eq("AI算力")].iloc[0]
        self.assertEqual(row["status"], "高热分歧")
        self.assertEqual(row["avg_hot_stock_change_pct"], -5.0)

    def test_merges_concept_capital_flow(self):
        strategy = ThemeMonitorStrategy(top_limit=10)
        hot_concepts = pd.DataFrame(
            [{"rank": 3, "concept_code": "BK0003", "concept_name": "存储芯片", "change_pct": 1.5, "hot_value": 88}]
        )
        concept_capital_flow = pd.DataFrame(
            [
                {
                    "index_code": "BK0003",
                    "index_name": "存储芯片",
                    "change_pct": 1.5,
                    "main_net_inflow": 320_000_000.0,
                    "main_net_inflow_rate": 4.2,
                    "max_net_inflow": 120_000_000.0,
                }
            ]
        )

        radar, snapshot = strategy.build_theme_radar(
            hot_stocks=pd.DataFrame(),
            hot_concepts=hot_concepts,
            hot_industries=pd.DataFrame(),
            popularity_stocks=pd.DataFrame(),
            concept_capital_flow=concept_capital_flow,
            previous_snapshot={},
        )

        row = radar[radar["theme"].eq("存储芯片")].iloc[0]
        self.assertEqual(row["main_net_inflow_yi"], 3.2)
        self.assertEqual(row["main_net_inflow_rate"], 4.2)
        self.assertEqual(row["max_net_inflow_yi"], 1.2)
        self.assertEqual(snapshot["themes"]["存储芯片"]["main_net_inflow_yi"], 3.2)


if __name__ == "__main__":
    unittest.main()
