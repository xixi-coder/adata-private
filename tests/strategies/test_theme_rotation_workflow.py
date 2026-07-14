# -*- coding: utf-8 -*-
import unittest

import pandas as pd

from strategies.theme_rotation_workflow import ThemeRotationWorkflow


class ThemeRotationWorkflowTest(unittest.TestCase):
    def test_technology_becomes_main_line_when_scores_dominate(self):
        radar = pd.DataFrame(
            [
                {
                    "theme": "AI算力",
                    "score": 88,
                    "change_pct": 3.2,
                    "hot_stock_count": 5,
                    "popularity_overlap_count": 2,
                    "hot_value": 95,
                    "status": "持续发酵",
                    "representatives": "000001 测试A",
                },
                {
                    "theme": "半导体",
                    "score": 82,
                    "change_pct": 2.5,
                    "hot_stock_count": 4,
                    "popularity_overlap_count": 1,
                    "hot_value": 88,
                    "status": "快速升温",
                    "representatives": "000002 测试B",
                },
                {
                    "theme": "创新药",
                    "score": 64,
                    "change_pct": 1.1,
                    "hot_stock_count": 2,
                    "popularity_overlap_count": 0,
                    "hot_value": 70,
                    "status": "震荡观察",
                    "representatives": "000003 测试C",
                },
            ]
        )

        plan, summary = ThemeRotationWorkflow().build_plan(
            radar,
            market_context={
                "risk_appetite": "强",
                "external_ai_tailwind": "强",
                "external_semi_tailwind": "强",
            },
        )

        self.assertEqual(plan.iloc[0]["basket"], "科技成长")
        self.assertEqual(plan.iloc[0]["action"], "主线")
        self.assertEqual(summary["main_line"], "科技成长")
        self.assertGreater(plan.iloc[0]["target_weight"], 0)
        self.assertIn("159995", plan.iloc[0]["suggested_etfs"])

    def test_innovation_drug_prefers_hk_connect_etfs(self):
        radar = pd.DataFrame(
            [
                {
                    "theme": "创新药",
                    "score": 88,
                    "change_pct": 2.2,
                    "hot_stock_count": 5,
                    "popularity_overlap_count": 2,
                    "hot_value": 92,
                    "status": "快速升温",
                    "representatives": "000001 测试A",
                },
            ]
        )

        plan, _ = ThemeRotationWorkflow().build_plan(radar, market_context={"risk_appetite": "强"})
        innovation = plan[plan["basket"].eq("创新药")].iloc[0]

        self.assertTrue(innovation["suggested_etfs"].startswith("3174.HK"))
        self.assertIn("159567", innovation["suggested_etfs"])

    def test_crowded_current_weight_reduces_main_line_target(self):
        radar = pd.DataFrame(
            [
                {
                    "theme": "AI算力",
                    "score": 96,
                    "change_pct": 6.5,
                    "hot_stock_count": 9,
                    "popularity_overlap_count": 5,
                    "hot_value": 100,
                    "representatives": "000001 测试A",
                }
            ]
        )

        plan, _ = ThemeRotationWorkflow().build_plan(
            radar,
            market_context={"risk_appetite": "强"},
            current_positions={"科技成长": 0.32},
        )

        tech = plan[plan["basket"].eq("科技成长")].iloc[0]
        self.assertGreaterEqual(tech["crowding_score"], 55)
        self.assertLess(tech["target_weight"], tech["max_weight"])
        self.assertIn("拥挤", tech["note"])

    def test_avoided_basket_has_no_suggested_etf(self):
        plan, _ = ThemeRotationWorkflow().build_plan(pd.DataFrame(), market_context={})

        avoided = plan[plan["action"].eq("回避")].iloc[0]
        self.assertEqual(avoided["suggested_etfs"], "暂不建议")


if __name__ == "__main__":
    unittest.main()
