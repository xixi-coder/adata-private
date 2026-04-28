import unittest

import numpy as np
import pandas as pd

from strategies.a_share_allocation import AShareAllocationStrategy, StrategyConfig


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


if __name__ == "__main__":
    unittest.main()
