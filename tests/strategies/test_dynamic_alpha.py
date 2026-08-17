import unittest

import numpy as np
import pandas as pd

from strategies.dynamic_alpha.strategy import DynamicAlphaConfig, DynamicAlphaStrategy


def _panel(stock_count: int = 24, sessions: int = 180) -> pd.DataFrame:
    dates = pd.bdate_range("2024-01-02", periods=sessions)
    rows = []
    for number in range(stock_count):
        code = f"{number + 1:06d}"
        drift = -0.0003 + number * 0.00008
        phase = number / 5.0
        close = 10.0 + number * 0.2
        previous = close
        industry = f"industry-{number % 4}"
        for index, date in enumerate(dates):
            daily_return = drift + 0.002 * np.sin(index / 9.0 + phase)
            close = max(2.0, previous * (1.0 + daily_return))
            open_price = previous * (1.0 + daily_return * 0.25)
            rows.append(
                {
                    "stock_code": code,
                    "stock_name": f"股票{number}",
                    "industry": industry,
                    "trade_date": date,
                    "open": open_price,
                    "high": max(open_price, close) * 1.01,
                    "low": min(open_price, close) * 0.99,
                    "close": close,
                    "pre_close": previous,
                    "volume": 20_000_000 + number * 100_000,
                    "amount": 300_000_000 + number * 1_000_000,
                }
            )
            previous = close
    return pd.DataFrame(rows)


def _config(**overrides) -> DynamicAlphaConfig:
    values = {
        "min_history_days": 60,
        "min_amount_ma20": 1_000_000,
        "universe_limit": 0,
        "max_positions": 8,
        "entry_fraction": 0.25,
        "exit_fraction": 0.50,
        "min_industry_members": 3,
        "forward_return_days": 5,
        "adaptive_lookback": 60,
        "min_ic_observations": 3,
        "min_ic_stocks": 8,
        "minimum_market_exposure": 0.30,
        "max_stock_weight": 0.20,
        "max_industry_weight": 0.40,
    }
    values.update(overrides)
    return DynamicAlphaConfig(**values)


class DynamicAlphaStrategyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.panel = _panel()

    def test_fundamentals_become_available_only_after_announcement_session(self):
        announce_date = pd.Timestamp("2024-05-15")
        fundamentals = []
        for number in range(24):
            fundamentals.append(
                {
                    "stock_code": f"{number + 1:06d}",
                    "announce_date": announce_date,
                    "roe": 0.05 + number * 0.005,
                    "cashflow_to_profit": 0.80 + number * 0.01,
                    "gross_margin": 0.20 + number * 0.002,
                    "debt_ratio": 0.70 - number * 0.01,
                    "earnings_yield": 0.03 + number * 0.001,
                    "fcf_yield": 0.02 + number * 0.001,
                    "book_to_price": 0.30 + number * 0.005,
                }
            )
        strategy = DynamicAlphaStrategy(_config())
        features = strategy.prepare(self.panel, pd.DataFrame(fundamentals))

        on_announcement = features[features["trade_date"] == announce_date]
        next_session = features[features["trade_date"] > announce_date]["trade_date"].min()
        after_announcement = features[features["trade_date"] == next_session]

        self.assertTrue(on_announcement["roe"].isna().all())
        self.assertTrue(after_announcement["roe"].notna().all())
        self.assertTrue(on_announcement["factor_quality"].isna().all())
        self.assertTrue(after_announcement["factor_quality"].notna().all())

    def test_adaptive_weights_ignore_ic_whose_outcome_is_not_known(self):
        strategy = DynamicAlphaStrategy(_config(min_ic_observations=2, adaptive_share=0.40))
        known_dates = pd.bdate_range("2024-06-03", periods=3)
        strategy.ic_history = pd.DataFrame(
            [
                {
                    "signal_date": date - pd.Timedelta(days=7),
                    "known_at": date,
                    "factor": factor,
                    "ic": value,
                    "stock_count": 30,
                }
                for date in known_dates
                for factor, value in (("momentum", 0.20), ("trend", 0.01), ("risk", -0.10))
            ]
            + [
                {
                    "signal_date": pd.Timestamp("2024-06-20"),
                    "known_at": pd.Timestamp("2024-07-01"),
                    "factor": "momentum",
                    "ic": -1.0,
                    "stock_count": 30,
                }
            ]
        )
        available = ["momentum", "trend", "risk"]
        before_future_is_known = strategy.factor_weights_as_of("2024-06-10", available)

        strategy.ic_history = strategy.ic_history.iloc[:-1].copy()
        without_future_row = strategy.factor_weights_as_of("2024-06-10", available)

        self.assertEqual(before_future_is_known, without_future_row)
        self.assertAlmostEqual(sum(before_future_is_known.values()), 1.0)
        self.assertLessEqual(max(before_future_is_known.values()), 0.35 + 1e-9)

    def test_per_share_financials_create_daily_point_in_time_value_factors(self):
        fundamentals = pd.DataFrame(
            [
                {
                    "stock_code": f"{number + 1:06d}",
                    "announce_date": "2024-05-15",
                    "eps_ttm": 1.0 + number * 0.01,
                    "operating_cashflow_ps_ttm": 1.2 + number * 0.01,
                    "net_asset_ps": 5.0 + number * 0.05,
                }
                for number in range(24)
            ]
        )
        strategy = DynamicAlphaStrategy(_config())
        features = strategy.prepare(self.panel, fundamentals)
        after = features[features["trade_date"] > pd.Timestamp("2024-05-15")]

        self.assertTrue(after["earnings_yield"].notna().all())
        self.assertTrue(after["operating_cashflow_yield"].notna().all())
        self.assertTrue(after["book_to_price"].notna().all())
        self.assertTrue(after.loc[after["eligible"], "factor_value"].notna().all())

    def test_target_weights_respect_stock_and_industry_caps(self):
        strategy = DynamicAlphaStrategy(_config(max_stock_weight=0.18, max_industry_weight=0.35))
        selected = pd.DataFrame(
            {
                "stock_code": [f"{index:06d}" for index in range(1, 9)],
                "industry": ["A", "A", "A", "A", "B", "B", "C", "C"],
                "alpha_score": np.linspace(2.0, 0.5, 8),
                "idio_vol20": np.linspace(0.15, 0.35, 8),
            }
        )
        weights = strategy._build_target_weights(selected, 0.90)

        self.assertLessEqual(max(weights.values()), 0.18 + 1e-9)
        for industry in ["A", "B", "C"]:
            members = set(selected.loc[selected["industry"] == industry, "stock_code"])
            self.assertLessEqual(sum(value for code, value in weights.items() if code in members), 0.35 + 1e-9)
        self.assertLessEqual(sum(weights.values()), 0.90 + 1e-9)

    def test_limit_prices_block_impossible_fills(self):
        strategy = DynamicAlphaStrategy(_config())
        main_board_limit_up = pd.Series(
            {"open": 11.0, "pre_close": 10.0, "volume": 1_000_000, "amount": 10_000_000, "is_st": False}
        )
        main_board_limit_down = pd.Series(
            {"open": 9.0, "pre_close": 10.0, "volume": 1_000_000, "amount": 10_000_000, "is_st": False}
        )

        self.assertFalse(strategy._can_trade(main_board_limit_up, "600001", "BUY"))
        self.assertFalse(strategy._can_trade(main_board_limit_down, "600001", "SELL"))
        self.assertTrue(strategy._can_trade(main_board_limit_up, "300001", "BUY"))
        self.assertEqual(strategy._price_limit_ratio("300001", True), 0.20)

    def test_unfilled_rebalance_sell_is_returned_for_daily_retry(self):
        strategy = DynamicAlphaStrategy(_config())
        date = pd.Timestamp("2024-09-02")
        rows = pd.DataFrame(
            [
                {
                    "stock_code": "600001",
                    "open": 9.0,
                    "close": 9.0,
                    "pre_close": 10.0,
                    "volume": 1_000_000,
                    "amount": 10_000_000,
                    "is_st": False,
                }
            ]
        ).set_index("stock_code", drop=False)
        positions = {
            "600001": {
                "shares": 1_000,
                "entry_price": 10.0,
                "entry_date": date - pd.Timedelta(days=10),
                "last_close": 10.0,
            }
        }

        cash, unresolved = strategy._execute_rebalance(
            date,
            rows,
            {},
            date - pd.Timedelta(days=1),
            positions,
            100_000.0,
            [],
        )

        self.assertEqual(cash, 100_000.0)
        self.assertEqual(unresolved, {"600001"})
        self.assertIn("600001", positions)

    def test_backtest_executes_weekly_signals_on_later_session(self):
        strategy = DynamicAlphaStrategy(_config())
        strategy.prepare(self.panel)
        result = strategy.run_backtest()

        self.assertFalse(result.signals.empty)
        self.assertFalse(result.trades.empty)
        self.assertTrue((result.signals["execution_date"] > result.signals["signal_date"]).all())
        buy_dates = set(pd.to_datetime(result.trades.loc[result.trades["side"] == "BUY", "trade_date"]))
        execution_dates = set(pd.to_datetime(result.signals["execution_date"]))
        self.assertTrue(buy_dates)
        self.assertTrue(buy_dates.issubset(execution_dates))
        self.assertTrue((result.trades["shares"] % strategy.config.board_lot == 0).all())
        self.assertIn("annual_return", result.metrics)
        self.assertIn("max_drawdown", result.metrics)


if __name__ == "__main__":
    unittest.main()
