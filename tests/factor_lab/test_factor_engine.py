import unittest

import numpy as np
import pandas as pd

from strategies.factor_lab.factor_engine import (
    CORE_15_FACTORS,
    align_financials_to_daily,
    compute_a_share_factors,
    evaluate_factor_ic,
    quantile_group_test,
    run_core_15_pipeline,
)


def _build_panel(n_stocks: int = 40, n_days: int = 90) -> tuple[pd.DataFrame, pd.DataFrame]:
    rng = np.random.default_rng(7)
    dates = pd.bdate_range("2024-01-01", periods=n_days)
    rows = []

    for i in range(n_stocks):
        code = f"{600000 + i:06d}"
        close = 10 + i * 0.05 + np.cumsum(0.02 + 0.0008 * i + rng.normal(0, 0.08, size=n_days))
        open_ = close * (1 + rng.normal(0, 0.002, size=n_days))
        high = np.maximum(open_, close) * 1.01
        low = np.minimum(open_, close) * 0.99
        volume = 1_200_000 * (1 + i / 120 + rng.normal(0, 0.06, size=n_days))
        volume = np.clip(volume, 50_000, None)
        amount = volume * close
        float_shares = 120_000_000 + i * 800_000

        quarter_id = np.arange(n_days) // 20
        revenue = 2.5e9 + i * 1.5e7 + quarter_id * 0.12e9
        profit = revenue * (0.075 + i * 0.0004)
        operating_cashflow = profit * (1.04 + rng.normal(0, 0.02, size=n_days))

        for j, d in enumerate(dates):
            rows.append(
                {
                    "trade_date": d,
                    "stock_code": code,
                    "open": open_[j],
                    "high": high[j],
                    "low": low[j],
                    "close": close[j],
                    "volume": volume[j],
                    "amount": amount[j],
                    "float_shares": float_shares,
                    "industry": f"industry_{i % 5}",
                    "market_cap": close[j] * float_shares,
                    "pe": 14 + i * 0.2,
                    "pb": 1.2 + i * 0.02,
                    "ps": 2.0 + i * 0.01,
                    "roe": 0.08 + i * 0.001,
                    "revenue": revenue[j],
                    "profit": profit[j],
                    "operating_cashflow": operating_cashflow[j],
                }
            )

    panel_df = pd.DataFrame(rows).sort_values(["stock_code", "trade_date"])
    panel_df["pre_close"] = panel_df.groupby("stock_code")["close"].shift(1)

    index_close = 3800 + np.cumsum(rng.normal(0.8, 8, size=n_days))
    index_df = pd.DataFrame({"trade_date": dates, "close": index_close})
    return panel_df, index_df


class FactorEngineTest(unittest.TestCase):
    def test_financial_alignment_uses_notice_date(self):
        daily = pd.DataFrame(
            {
                "stock_code": ["000001"] * 5,
                "trade_date": pd.to_datetime(
                    ["2024-01-01", "2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05"]
                ),
                "close": [10, 10.1, 10.2, 10.3, 10.4],
            }
        )
        finance = pd.DataFrame(
            {
                "stock_code": ["000001", "000001"],
                "notice_date": pd.to_datetime(["2024-01-03", "2024-01-05"]),
                "pe": [10.0, 20.0],
            }
        )

        merged = align_financials_to_daily(daily, finance)
        pe_values = merged["pe"].tolist()
        self.assertTrue(np.isnan(pe_values[0]))
        self.assertTrue(np.isnan(pe_values[1]))
        self.assertEqual(pe_values[2], 10.0)
        self.assertEqual(pe_values[3], 10.0)
        self.assertEqual(pe_values[4], 20.0)

    def test_compute_core_factors(self):
        panel_df, index_df = _build_panel()
        factors_df = compute_a_share_factors(panel_df, index_df=index_df)

        for factor in CORE_15_FACTORS:
            self.assertIn(factor, factors_df.columns)

        self.assertIn("excess_ret_20", factors_df.columns)
        self.assertGreater(int(factors_df["ret_20"].notna().sum()), 0)
        self.assertGreater(int(factors_df["downside_vol_20"].notna().sum()), 0)

    def test_ic_and_group_test(self):
        panel_df, index_df = _build_panel()
        factors_df = compute_a_share_factors(panel_df, index_df=index_df)

        ic_result = evaluate_factor_ic(
            panel_df=factors_df,
            factor_col="ret_20",
            horizon=5,
            min_obs=20,
        )
        self.assertGreater(ic_result["obs_days"], 0)

        group_result = quantile_group_test(
            panel_df=factors_df,
            factor_col="ret_20",
            horizon=5,
            n_groups=10,
            min_obs=20,
        )
        self.assertGreater(group_result["obs_days"], 0)

    def test_pipeline_runs_end_to_end(self):
        panel_df, index_df = _build_panel()
        result = run_core_15_pipeline(
            panel_df=panel_df,
            index_df=index_df,
            min_market_cap=1e9,
            min_amount=5e6,
            neutralize=True,
        )
        self.assertFalse(result["panel"].empty)
        self.assertFalse(result["ic_summary"].empty)
        self.assertFalse(result["group_summary"].empty)


if __name__ == "__main__":
    unittest.main()

