import unittest

import numpy as np
import pandas as pd

from strategies.a_share_allocation import AShareAllocationStrategy, StrategyConfig
from jobs.a_share_allocation.run_daily import _portfolio_review, _portfolio_risk_summary


def _build_panel(n_stocks: int = 28, n_days: int = 170) -> pd.DataFrame:
    rng = np.random.default_rng(2026)
    dates = pd.bdate_range("2025-01-01", periods=n_days)
    rows = []
    for i in range(n_stocks):
        code = f"{600000 + i:06d}"
        drift = 0.012 + i * 0.0005
        noise = rng.normal(0, 0.05 + (i % 5) * 0.01, size=n_days)
        close = 8 + i * 0.15 + np.cumsum(drift + noise)
        close = np.clip(close, 2.5, None)
        open_ = close * (1 + rng.normal(0, 0.003, size=n_days))
        high = np.maximum(open_, close) * (1 + rng.uniform(0.002, 0.018, size=n_days))
        low = np.minimum(open_, close) * (1 - rng.uniform(0.002, 0.018, size=n_days))
        volume = 8_000_000 * (1 + i / 30 + rng.normal(0, 0.07, size=n_days))
        volume = np.clip(volume, 1_000_000, None)
        amount = volume * close
        for j, date in enumerate(dates):
            rows.append(
                {
                    "stock_code": code,
                    "trade_date": date,
                    "open": open_[j],
                    "high": high[j],
                    "low": low[j],
                    "close": close[j],
                    "volume": volume[j],
                    "amount": amount[j],
                    "turnover_ratio": 4.0 + i * 0.1,
                }
            )
    return pd.DataFrame(rows)


class AShareAllocationStrategyTest(unittest.TestCase):
    def test_compute_scores_produces_ranked_candidates(self):
        strategy = AShareAllocationStrategy(
            StrategyConfig(min_history_days=80, min_amount_ma20=5_000_000, max_positions=8)
        )
        strategy.set_panel(_build_panel())
        scores = strategy.compute_scores(include_dividend=False)

        self.assertIn("final_score", scores.columns)
        self.assertIn("eligible", scores.columns)
        self.assertGreater(scores["final_score"].notna().sum(), 0)
        self.assertGreaterEqual(scores["final_score"].dropna().min(), 0)
        self.assertLessEqual(scores["final_score"].dropna().max(), 100)

    def test_backtest_runs_and_emits_metrics(self):
        strategy = AShareAllocationStrategy(
            StrategyConfig(
                initial_capital=500_000,
                min_history_days=80,
                min_amount_ma20=5_000_000,
                max_positions=6,
                rebalance_period=10,
            )
        )
        strategy.set_panel(_build_panel())
        strategy.compute_scores(include_dividend=False)
        metrics = strategy.run_backtest(start_date="2025-06-01")

        self.assertIn("final_asset", metrics)
        self.assertIn("max_drawdown", metrics)
        self.assertGreater(metrics["final_asset"], 0)
        self.assertGreater(len(strategy.equity_curve), 0)

    def test_portfolio_risk_summary_detects_ai_chain_concentration(self):
        positions = [
            {"code": "300308", "name": "中际旭创", "weight": 24.54, "market": "A"},
            {"code": "002463", "name": "沪电股份", "weight": 22.11, "market": "A"},
            {"code": "688012", "name": "中微公司", "weight": 17.78, "market": "A"},
            {"code": "688008", "name": "澜起科技", "weight": 12.90, "market": "A"},
            {"code": "00981", "name": "中芯国际", "weight": 7.10, "market": "HK"},
        ]

        risk = _portfolio_risk_summary(positions, "防守")

        self.assertGreater(risk["top3_weight"], 60)
        self.assertEqual(risk["target_equity_range"], "40%-60%")
        self.assertEqual(risk["parent_theme_exposure"][0]["theme"], "AI算力/半导体链")
        self.assertGreater(risk["parent_theme_exposure"][0]["weight"], 80)

    def test_portfolio_review_marks_hk_position_as_unscored(self):
        positions = [{"code": "00981", "name": "中芯国际", "weight": 7.10, "market": "HK"}]

        review = _portfolio_review(pd.DataFrame(columns=["stock_code"]), positions)

        self.assertEqual(review.iloc[0]["建议动作"], "暂不评分")
        self.assertEqual(review.iloc[0]["趋势"], "港股/非A股")


if __name__ == "__main__":
    unittest.main()
