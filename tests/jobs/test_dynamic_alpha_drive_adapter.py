import pickle
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

from jobs.dynamic_alpha.data_adapter import (
    attach_realized_dividend_yield,
    choose_complete_date,
    load_fundamentals,
    parse_cash_dividend_per_share,
    select_liquid_codes,
)


class DynamicAlphaDriveAdapterTest(unittest.TestCase):
    def test_complete_date_clamps_incomplete_latest_sessions(self):
        coverage = pd.DataFrame(
            {
                "trade_date": pd.to_datetime(["2026-08-05", "2026-08-06", "2026-08-07", "2026-08-10"]),
                "stock_count": [100, 99, 98, 40],
            }
        )

        effective, audit = choose_complete_date(
            coverage,
            requested_end_date=None,
            complete_ratio=0.95,
            coverage_lookback=20,
        )

        self.assertEqual(effective, pd.Timestamp("2026-08-07"))
        self.assertEqual(audit["latest_observed_date"], "2026-08-10")
        self.assertEqual(audit["minimum_complete_count"], 95)

    def test_finance_adapter_filters_bad_dates_and_annualizes_interim_values(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            finance_dir = Path(tmpdir)
            pd.DataFrame(
                {
                    "stock_code": [1, 1, 1],
                    "notice_date": ["2025-04-30", "1900-01-01", "2025-08-30"],
                    "report_date": ["2025-03-31", "2024-12-31", "2025-06-30"],
                    "roe_wtd": [4.0, 10.0, 8.0],
                    "gross_margin": [30.0, 31.0, 32.0],
                    "asset_liab_ratio": [40.0, 41.0, 42.0],
                    "basic_eps": [0.25, 1.0, 0.60],
                    "net_asset_ps": [5.0, 4.8, 5.2],
                    "oper_cf_ps": [0.20, 0.8, 0.50],
                }
            ).to_csv(finance_dir / "000001.csv", index=False)

            adapted, audit = load_fundamentals(
                finance_dir,
                ["000001"],
                latest_market_date=pd.Timestamp("2025-12-31"),
            )

        self.assertEqual(len(adapted), 2)
        self.assertEqual(audit["invalid_notice_rows"], 1)
        first = adapted.sort_values("announce_date").iloc[0]
        self.assertAlmostEqual(first["eps_ttm"], 1.0)
        self.assertAlmostEqual(first["operating_cashflow_ps_ttm"], 0.8)
        self.assertAlmostEqual(first["roe"], 0.04)

    def test_liquidity_selection_requires_quote_on_effective_date(self):
        current = pd.DataFrame(
            {
                "trade_date": ["2026-08-06", "2026-08-07"],
                "amount": [100.0, 100.0],
            }
        )
        stale = pd.DataFrame(
            {
                "trade_date": ["2026-08-05", "2026-08-06"],
                "amount": [1_000.0, 1_000.0],
            }
        )

        codes, audit = select_liquid_codes(
            {"000001": current, "000002": stale},
            pd.Timestamp("2026-08-07"),
            max_stocks=10,
        )

        self.assertEqual(codes, ["000001"])
        self.assertEqual(audit["available_stock_count"], 1)

    def test_dividend_yield_uses_only_realized_ex_dividend_events(self):
        dates = pd.to_datetime(["2025-01-02", "2025-06-02", "2025-06-03", "2026-06-03"])
        panel = pd.DataFrame(
            {
                "stock_code": "000001",
                "trade_date": dates,
                "close": [10.0, 10.0, 10.0, 10.0],
            }
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            dividend_dir = Path(tmpdir)
            pd.DataFrame(
                {
                    "dividend_plan": ["10股派3.00元"],
                    "ex_dividend_date": ["2025-06-03"],
                }
            ).to_csv(dividend_dir / "000001.csv", index=False)
            enriched, audit = attach_realized_dividend_yield(panel, dividend_dir)

        values = enriched.set_index("trade_date")["dividend_yield_ttm"]
        self.assertEqual(values.loc[pd.Timestamp("2025-06-02")], 0.0)
        self.assertAlmostEqual(values.loc[pd.Timestamp("2025-06-03")], 0.03)
        self.assertEqual(values.loc[pd.Timestamp("2026-06-03")], 0.0)
        self.assertEqual(audit["parsed_event_count"], 1)
        self.assertAlmostEqual(parse_cash_dividend_per_share("10股派6.00元，10股转赠1股"), 0.6)


if __name__ == "__main__":
    unittest.main()
