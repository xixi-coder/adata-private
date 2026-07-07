import unittest

import numpy as np
import pandas as pd

from strategies.volatility import QualityGateConfig, VolatilityStrategy, VolatilityStrategyConfig
from jobs.volatility.run_daily import _attach_cluster_tags, _cluster_summary, _fetch_stock_tag_from_adata


def _stock_frame(
    code: str,
    n_days: int = 220,
    base_price: float = 12.0,
    amount: float = 180_000_000,
    squeeze: bool = False,
) -> pd.DataFrame:
    rng = np.random.default_rng(abs(hash(code)) % 2**32)
    dates = pd.bdate_range("2025-01-01", periods=n_days)
    drift = 0.015
    noise = rng.normal(0, 0.04, size=n_days)
    if squeeze:
        noise[-35:] = rng.normal(0, 0.012, size=35)
    close = base_price + np.cumsum(drift + noise)
    close = np.clip(close, 3.2, None)
    open_ = close * (1 + rng.normal(0, 0.002, size=n_days))
    spread = rng.uniform(0.006, 0.018, size=n_days)
    if squeeze:
        spread[-25:] = rng.uniform(0.003, 0.008, size=25)
    high = np.maximum(open_, close) * (1 + spread)
    low = np.minimum(open_, close) * (1 - spread)
    volume = amount / close
    rows = []
    for i, date in enumerate(dates):
        rows.append(
            {
                "stock_code": code,
                "trade_date": date,
                "open": open_[i],
                "high": high[i],
                "low": low[i],
                "close": close[i],
                "volume": volume[i],
                "amount": amount,
                "pre_close": close[i - 1] if i > 0 else close[i],
            }
        )
    return pd.DataFrame(rows)


class VolatilityStrategyTest(unittest.TestCase):
    def test_quality_gate_filters_bad_names_and_illiquid_stocks(self):
        panel = pd.concat(
            [
                _stock_frame("600001", amount=180_000_000, squeeze=True),
                _stock_frame("600002", amount=20_000_000),
                _stock_frame("600003", amount=180_000_000),
                _stock_frame("600004", n_days=80, amount=180_000_000),
            ],
            ignore_index=True,
        )
        meta = {
            "600001": {"short_name": "好公司", "list_date": pd.Timestamp("2020-01-01")},
            "600002": {"short_name": "低流动", "list_date": pd.Timestamp("2020-01-01")},
            "600003": {"short_name": "*ST风险", "list_date": pd.Timestamp("2020-01-01")},
            "600004": {"short_name": "新股", "list_date": pd.Timestamp("2025-10-01")},
        }
        strategy = VolatilityStrategy(
            VolatilityStrategyConfig(quality=QualityGateConfig(min_amount_ma20=100_000_000), universe_size=None)
        )
        strategy.set_panel(panel)
        features = strategy.compute_features(stock_meta=meta)

        self.assertEqual(set(features["stock_code"].unique()), {"600001"})
        self.assertEqual(strategy.quality_report["accepted_stock_count"], 1)
        self.assertIn("流动性不足", strategy.quality_report["reject_counts"])
        self.assertIn("ST退市或非普通股", strategy.quality_report["reject_counts"])

    def test_latest_signals_returns_ranked_candidates(self):
        panel = pd.concat(
            [
                _stock_frame("600010", amount=220_000_000, squeeze=True),
                _stock_frame("600011", amount=260_000_000, squeeze=True),
                _stock_frame("600012", amount=240_000_000),
            ],
            ignore_index=True,
        )
        meta = {
            "600010": {"short_name": "波动一", "list_date": pd.Timestamp("2020-01-01")},
            "600011": {"short_name": "波动二", "list_date": pd.Timestamp("2020-01-01")},
            "600012": {"short_name": "波动三", "list_date": pd.Timestamp("2020-01-01")},
        }
        strategy = VolatilityStrategy(
            VolatilityStrategyConfig(
                quality=QualityGateConfig(min_amount_ma20=50_000_000, min_price=2.0),
                universe_size=None,
                squeeze_limit=10,
                expansion_limit=10,
                anomaly_limit=10,
            )
        )
        strategy.set_panel(panel)
        strategy.compute_features(stock_meta=meta)
        signals = strategy.latest_signals()

        self.assertFalse(signals.empty)
        self.assertIn("signal_type", signals.columns)
        self.assertIn("risk_level", signals.columns)
        self.assertIn("amount_ratio1_20", signals.columns)
        self.assertTrue(set(signals["stock_code"]).issubset({"600010", "600011", "600012"}))

    def test_cluster_summary_groups_expansion_candidates(self):
        candidates = pd.DataFrame(
            [
                {"股票代码": "600010", "股票名称": "芯片一", "信号类型": "波动扩张", "评分": 88.0},
                {"股票代码": "600011", "股票名称": "芯片二", "信号类型": "波动扩张", "评分": 82.0},
                {"股票代码": "300010", "股票名称": "机器人", "信号类型": "波动扩张", "评分": 79.0},
            ]
        )
        tagged = _attach_cluster_tags(
            candidates,
            {
                "600010": {"industry": "半导体", "concept": ""},
                "600011": {"industry": "半导体", "concept": ""},
                "300010": {"industry": "机器人", "concept": ""},
            },
        )
        summary = _cluster_summary(tagged)

        self.assertEqual(summary[0]["tag"], "半导体")
        self.assertEqual(summary[0]["count"], 2)
        self.assertGreater(summary[0]["avg_score"], 80)

    def test_fetch_stock_tag_from_adata_uses_industry_and_concepts(self):
        class FakeInfo:
            @staticmethod
            def get_industry_sw(stock_code):
                return pd.DataFrame(
                    [
                        {"stock_code": stock_code, "industry_name": "半导体", "industry_type": "一级行业"},
                        {"stock_code": stock_code, "industry_name": "模拟芯片", "industry_type": "二级行业"},
                    ]
                )

            @staticmethod
            def get_concept_east(stock_code):
                return pd.DataFrame([{"stock_code": stock_code, "name": "先进封装"}, {"stock_code": stock_code, "name": "人工智能"}])

        class FakeStock:
            info = FakeInfo()

        class FakeAdata:
            stock = FakeStock()

        tags = _fetch_stock_tag_from_adata(FakeAdata(), "600010")

        self.assertEqual(tags["industry"], "半导体")
        self.assertIn("先进封装", tags["concept"])


if __name__ == "__main__":
    unittest.main()
