import pickle
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from jobs.common.a_share_panel import load_a_share_panel, standardize_daily_df


def _frame(code: str, n_days: int = 3, amount: float = 100_000_000) -> pd.DataFrame:
    dates = pd.bdate_range("2026-01-01", periods=n_days)
    return pd.DataFrame(
        {
            "stock_code": [code] * n_days,
            "trade_time": dates,
            "open": [10.0] * n_days,
            "high": [10.5] * n_days,
            "low": [9.8] * n_days,
            "close": [10.2] * n_days,
            "volume": [amount / 10.2] * n_days,
            "amount": [amount] * n_days,
        }
    )


class ASharePanelTest(unittest.TestCase):
    def test_standardize_daily_df_normalizes_code_and_trade_time(self):
        out = standardize_daily_df("600001", _frame("600001.0"))

        self.assertEqual(out["stock_code"].tolist(), ["600001", "600001", "600001"])
        self.assertIn("trade_date", out.columns)
        self.assertIn("pre_close", out.columns)

    def test_load_a_share_panel_filters_unsupported_codes_and_limits_universe(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cache.pkl"
            payload = {
                "stock": {
                    "600001": _frame("600001", amount=200_000_000),
                    "300001": _frame("300001", amount=150_000_000),
                    "200001": _frame("200001", amount=300_000_000),
                }
            }
            with open(path, "wb") as f:
                pickle.dump(payload, f)

            panel = load_a_share_panel(str(path), universe_size=1)

        self.assertEqual(set(panel["stock_code"]), {"600001"})


if __name__ == "__main__":
    unittest.main()
