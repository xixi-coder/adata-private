import unittest

import numpy as np
import pandas as pd

from strategies.volatility import QualityGateConfig, VolatilityStrategy, VolatilityStrategyConfig
from jobs.volatility.run_daily import _attach_cluster_tags, _build_email_body, _cluster_summary, _fetch_stock_tag_from_adata, _to_output_df


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

    def test_signal_funnel_summary_exposes_condition_counts(self):
        panel = pd.concat(
            [
                _stock_frame("600020", amount=220_000_000, squeeze=True),
                _stock_frame("600021", amount=260_000_000),
            ],
            ignore_index=True,
        )
        meta = {
            "600020": {"short_name": "漏斗一", "list_date": pd.Timestamp("2020-01-01")},
            "600021": {"short_name": "漏斗二", "list_date": pd.Timestamp("2020-01-01")},
        }
        strategy = VolatilityStrategy(
            VolatilityStrategyConfig(
                quality=QualityGateConfig(min_amount_ma20=50_000_000, min_price=2.0),
                universe_size=None,
            )
        )
        strategy.set_panel(panel)
        strategy.compute_features(stock_meta=meta)

        funnel = strategy.signal_funnel_summary()

        self.assertEqual(funnel["scanned_count"], 2)
        self.assertIn("波动收敛", funnel["signals"])
        self.assertIn("condition_counts", funnel["signals"]["波动扩张"])
        self.assertIn("当日成交额放大", funnel["signals"]["波动扩张"]["condition_counts"])
        self.assertGreaterEqual(funnel["signals"]["异常波动"]["selected_count"], 0)

    def test_anomaly_category_is_exposed_in_output(self):
        row = pd.Series(
            {
                "ret_1d": -0.045,
                "amount_ratio5_20": 2.1,
                "close_to_high20": 0.90,
                "drawdown60": 0.18,
            }
        )
        self.assertEqual(VolatilityStrategy._anomaly_category(row), "异常放量下跌")

        signals = pd.DataFrame(
            [
                {
                    "trade_date": pd.Timestamp("2026-07-08"),
                    "stock_code": "600010",
                    "short_name": "异动票",
                    "signal_type": "异常波动",
                    "score": 88.0,
                    "risk_level": "高",
                    "close": 10.5,
                    "ret_1d": -0.045,
                    "ret_20d": 0.02,
                    "range_pct": 0.12,
                    "squeeze_ratio": 1.2,
                    "amount_ma20": 200_000_000,
                    "amount_ratio1_20": 2.0,
                    "amount_ratio5_20": 2.1,
                    "close_to_ma20": 0.01,
                    "close_to_ma60": 0.03,
                    "close_to_ma120": 0.05,
                    "ma60_slope20": 0.01,
                    "drawdown60": 0.18,
                    "watch_price": 10.3,
                    "invalid_price": 9.8,
                    "anomaly_category": "异常放量下跌",
                    "reason": "异常分类=异常放量下跌",
                }
            ]
        )

        output = _to_output_df(signals)

        self.assertEqual(output.iloc[0]["异常分类"], "异常放量下跌")

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

    def test_email_body_is_compact_and_hides_reject_reasons(self):
        candidates = pd.DataFrame(
            [
                {
                    "股票代码": "600010",
                    "股票名称": "芯片一",
                    "板块/主题": "半导体",
                    "信号类型": "波动扩张",
                    "评分": 88.0,
                    "风险等级": "低",
                    "收盘价": 10.5,
                    "观察价": 10.3,
                    "失效价": 9.8,
                }
            ]
        )
        summary = {
            "signal_date": "2026-07-08",
            "candidate_count": 1,
            "quality_report": {
                "initial_stock_count": 5000,
                "accepted_stock_count": 2000,
                "reject_counts": {"流动性不足": 1200},
            },
            "cluster_summary": [{"tag": "半导体", "count": 1, "avg_score": 88.0, "top_names": ["600010 芯片一"]}],
        }

        body = _build_email_body(summary, candidates)

        self.assertIn("一、资金聚焦", body)
        self.assertIn("二、波动扩张", body)
        self.assertNotIn("票质过滤剔除原因", body)
        self.assertNotIn("流动性不足", body)


if __name__ == "__main__":
    unittest.main()
