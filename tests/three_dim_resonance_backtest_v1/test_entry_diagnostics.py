import unittest
import importlib.util
from pathlib import Path

import pandas as pd


_MIXIN_PATH = Path(__file__).resolve().parents[2] / "jobs" / "three_dim_resonance" / "live" / "execution_mixin.py"
_SPEC = importlib.util.spec_from_file_location("three_dim_execution_mixin_for_test", _MIXIN_PATH)
_MODULE = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
_SPEC.loader.exec_module(_MODULE)
ExecutionMixin = _MODULE.ExecutionMixin


class _DummyExecution(ExecutionMixin):
    def __init__(self):
        self.max_positions = 2
        self.stock_names = {}
        self.stock_data = {}
        self.last_entry_skip_reason = ""
        self.last_entry_diagnostics = {}

    def _market_gate_status(self, trade_date):
        return {"ok": True, "checks": []}

    def _signal_score(self, row, meta):
        return 8.8

    def _entry_reason_detail(self, meta, row):
        return "测试明细"

    @staticmethod
    def _label_entry_shape(shape):
        return "平台突破" if shape == "platform_breakout" else "双底突破"


class EntryDiagnosticsTest(unittest.TestCase):
    def test_entry_candidates_records_stage_counts_and_near_misses(self):
        strategy = _DummyExecution()
        strategy.stock_data = {
            "000001": pd.DataFrame([{"close": 10.0}], index=["2026-03-13"]),
            "000002": pd.DataFrame([{"close": 11.0}], index=["2026-03-13"]),
        }

        def fake_components(df, trade_date):
            if float(df.loc[trade_date, "close"]) == 10.0:
                return {
                    "shape_ok": False,
                    "indicator_ok": True,
                    "capital_ok": True,
                    "three_dim_ok": False,
                    "double_bottom": False,
                }
            return {
                "shape_ok": True,
                "indicator_ok": False,
                "capital_ok": True,
                "three_dim_ok": False,
                "double_bottom": False,
            }

        strategy._evaluate_entry_components = fake_components
        state = {"positions": {}, "cash": 1_000_000.0}

        candidates = strategy._entry_candidates(state, "2026-03-13")

        self.assertEqual(candidates, [])
        diagnostics = strategy.last_entry_diagnostics
        self.assertEqual(diagnostics["scanned_count"], 2)
        self.assertEqual(diagnostics["stage_counts"]["only_missing_shape"], 1)
        self.assertEqual(diagnostics["stage_counts"]["only_missing_indicator"], 1)
        self.assertEqual(diagnostics["candidate_count"], 0)
        self.assertIn("000001(仅缺形态)", diagnostics["near_miss_samples"])

    def test_entry_candidates_include_trigger_and_invalid_prices(self):
        strategy = _DummyExecution()
        strategy.stock_data = {
            "000003": pd.DataFrame(
                [
                    {
                        "close": 10.2,
                        "low": 9.9,
                        "ma20": 9.8,
                        "prior_high_break": 10.5,
                        "platform_high": 10.3,
                    }
                ],
                index=["2026-03-13"],
            )
        }
        strategy.stock_names = {"000003": "触发票"}
        strategy._evaluate_entry_components = lambda df, trade_date: {
            "shape_ok": True,
            "indicator_ok": True,
            "capital_ok": True,
            "three_dim_ok": True,
            "double_bottom": False,
        }
        state = {"positions": {}, "cash": 1_000_000.0}

        candidates = strategy._entry_candidates(state, "2026-03-13")

        self.assertEqual(candidates[0]["code"], "000003")
        self.assertAlmostEqual(candidates[0]["trigger_price"], 10.531, places=3)
        self.assertAlmostEqual(candidates[0]["invalid_price"], 9.653, places=3)


if __name__ == "__main__":
    unittest.main()
