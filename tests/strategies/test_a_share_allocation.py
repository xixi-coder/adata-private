import json
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

from strategies.a_share_allocation import AShareAllocationStrategy, StrategyConfig
from jobs.a_share_allocation.run_daily import (
    _apply_cross_strategy_to_candidates,
    _apply_cross_strategy_to_portfolio,
    _load_cross_strategy_signals,
    _portfolio_review,
    _portfolio_risk_summary,
)


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

    def test_portfolio_review_downgrades_technically_weak_holdings(self):
        positions = [{"code": "600522", "name": "中天科技", "weight": 1.0, "market": "A"}]
        day_scores = pd.DataFrame(
            [
                {
                    "stock_code": "600522",
                    "final_score": 45.86,
                    "close": 18.0,
                    "ma20": 20.0,
                    "ma60": 17.0,
                    "high_60": 24.0,
                    "ret_20": -0.16,
                }
            ]
        )

        review = _portfolio_review(day_scores, positions)

        self.assertEqual(review.iloc[0]["趋势"], "趋势转弱")
        self.assertEqual(review.iloc[0]["建议动作"], "减仓观察")

    def test_portfolio_review_uses_candle_volume_and_capital_flow_proxy(self):
        positions = [{"code": "600522", "name": "中天科技", "weight": 1.0, "market": "A"}]
        day_scores = pd.DataFrame(
            [
                {
                    "stock_code": "600522",
                    "final_score": 62.0,
                    "close": 18.0,
                    "ma20": 17.5,
                    "ma60": 16.0,
                    "high_60": 19.0,
                    "ret_20": 0.02,
                    "ret_1d": -0.03,
                    "close_pos": 0.20,
                    "upper_shadow_ratio": 0.42,
                    "amount_ratio1_20": 1.80,
                    "volume_ratio_5_20": 1.30,
                    "cmf5": -0.12,
                    "cmf20": -0.03,
                    "net_amt3": -120_000_000,
                    "net_amt5": -260_000_000,
                }
            ]
        )

        review = _portfolio_review(day_scores, positions)

        self.assertEqual(review.iloc[0]["趋势"], "趋势转弱")
        self.assertEqual(review.iloc[0]["建议动作"], "反弹减仓")
        self.assertIn("CMF5-0.120", review.iloc[0]["理由"])

    def test_load_cross_strategy_signals_collects_latest_outputs(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            volatility_dir = base / "jobs" / "volatility" / "outputs"
            volatility_dir.mkdir(parents=True)
            pd.DataFrame(
                [
                    {"股票代码": "600001", "信号类型": "波动扩张"},
                    {"股票代码": "600002", "信号类型": "异常波动", "异常分类": "异常放量下跌"},
                ]
            ).to_csv(volatility_dir / "latest_candidates.csv", index=False, encoding="utf-8-sig")

            boll_dir = base / "jobs" / "boll" / "outputs"
            boll_dir.mkdir(parents=True)
            pd.DataFrame(
                [
                    {"股票代码": "600003", "信号类型": "下轨止跌观察"},
                    {"股票代码": "600004", "信号类型": "上轨放量滞涨"},
                ]
            ).to_csv(boll_dir / "latest_candidates.csv", index=False, encoding="utf-8-sig")

            three_dim_dir = base / "jobs" / "three_dim_resonance" / "outputs"
            three_dim_dir.mkdir(parents=True)
            (three_dim_dir / "latest_summary.json").write_text(
                json.dumps(
                    {
                        "buy_suggestions": [{"code": "600005"}],
                        "sell_suggestions": [{"code": "600006", "reason": "趋势走弱"}],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            context = _load_cross_strategy_signals(str(base))

        self.assertIn("波动扩张", context["by_code"]["600001"]["opportunity"])
        self.assertIn("异常放量下跌", context["by_code"]["600002"]["risk"])
        self.assertIn("BOLL下轨止跌", context["by_code"]["600003"]["watch"])
        self.assertIn("BOLL上轨滞涨", context["by_code"]["600004"]["risk"])
        self.assertIn("三维买入建议", context["by_code"]["600005"]["opportunity"])
        self.assertIn("三维卖出建议(趋势走弱)", context["by_code"]["600006"]["risk"])

    def test_cross_strategy_context_updates_portfolio_and_candidates(self):
        context = {
            "by_code": {
                "600001": {"risk": ["BOLL上轨滞涨"], "opportunity": [], "watch": []},
                "600002": {"risk": [], "opportunity": ["波动扩张", "三维买入建议"], "watch": []},
            },
            "source_counts": {},
        }
        portfolio = pd.DataFrame(
            [
                {
                    "股票代码": "600001",
                    "股票名称": "风险票",
                    "主题": "测试",
                    "仓位": 10.0,
                    "评分": 80.0,
                    "趋势": "上升趋势",
                    "建议动作": "继续持有",
                    "理由": "原始理由",
                }
            ]
        )
        candidates = pd.DataFrame(
            [
                {
                    "股票代码": "600002",
                    "股票名称": "机会票",
                    "收盘价": 10.0,
                    "日涨跌%": 1.0,
                    "总分": 88.0,
                    "入选依据": "综合评分靠前",
                    "操作提示": "可观察",
                }
            ]
        )

        portfolio_out = _apply_cross_strategy_to_portfolio(portfolio, context)
        candidates_out = _apply_cross_strategy_to_candidates(candidates, context)

        self.assertEqual(portfolio_out.iloc[0]["建议动作"], "减仓观察")
        self.assertIn("BOLL上轨滞涨", portfolio_out.iloc[0]["策略联动"])
        self.assertIn("波动扩张", candidates_out.iloc[0]["策略联动"])
        self.assertIn("多策略机会", candidates_out.iloc[0]["入选依据"])


if __name__ == "__main__":
    unittest.main()
