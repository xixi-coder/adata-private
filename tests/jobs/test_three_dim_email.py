import unittest

from jobs.three_dim_resonance.live.output_mixin import OutputMixin


class ThreeDimEmailTest(unittest.TestCase):
    def test_email_leads_with_market_and_action_summary(self):
        summary = {
            "signal_date": "2026-07-14",
            "next_trade_date": "2026-07-15",
            "buy_suggestions": [],
            "sell_suggestions": [],
            "positions": [],
            "entry_skip_reason": "无股票同时满足三维共振条件。",
            "entry_diagnostics": {
                "scanned_count": 1800,
                "stage_counts": {
                    "shape_ok": 12,
                    "indicator_ok": 320,
                    "capital_ok": 85,
                    "three_dim_ok": 0,
                },
                "market_gate": {
                    "summary": "允许开仓（震荡容错，趋势2/3，风控2/2）",
                    "checks": [
                        {"label": "收盘站上MA20", "ok": False},
                        {"label": "MA20位于MA60上方", "ok": True},
                    ],
                },
            },
            "note": "建议模式：不执行买卖，仅输出当日建议。",
        }

        body = OutputMixin._build_email_body(summary)

        self.assertIn("操作摘要：买入建议 0 只，卖出建议 0 只", body)
        self.assertIn("允许开仓（震荡容错，趋势2/3，风控2/2）", body)
        self.assertIn("扫描 1800 只 | 形态通过 12", body)
        self.assertIn("未通过：收盘站上MA20", body)


if __name__ == "__main__":
    unittest.main()
