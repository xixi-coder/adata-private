# -*- coding: utf-8 -*-
import os
import sys


CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
if CURRENT_DIR not in sys.path:
    sys.path.insert(0, CURRENT_DIR)

from short_term_strategy_code import ShortTermDisagreementStrategy


class AggressiveShortTermStrategy(ShortTermDisagreementStrategy):
    """
    激进版日线短线策略

    目标:
    - 在现有日线数据上提升交易频次
    - 保留非 ST / 非指数类过滤
    - 接受更高波动, 用更宽的强势股定义换取进攻性
    """

    def _market_ok(self, date_str: str) -> bool:
        if date_str not in self.benchmark_df.index:
            return False
        row = self.benchmark_df.loc[date_str]
        # 激进版只在明显系统性杀跌日关掉开仓
        return bool(row["change_pct"] > -2.2)

    def _signal_score(self, code, row):
        amount_ratio = row["amount"] / row["amt_ma5"] if row["amt_ma5"] > 0 else 0.0
        turn_ratio = row["turnover_ratio"] / row["turn_ma5"] if row["turn_ma5"] > 0 else 0.0
        breakout_pct = (row["close"] / row["close_hh30_prev"] - 1.0) * 100.0 if row["close_hh30_prev"] > 0 else 0.0
        limit = self._board_limit(code) * 100.0
        gap_to_limit = max(limit - row["pct_change"], 0.0)
        return (
            row["pct_change"] * 0.9
            + row["ret5"] * 0.3
            + row["ret10"] * 0.12
            + turn_ratio * 1.8
            + amount_ratio * 1.1
            + row["close_pos"] * 2.2
            + breakout_pct * 1.6
            - row["upper_shadow_pct"] * 0.8
            - gap_to_limit * 0.35
        )

    def _is_entry_signal(self, code: str, row) -> bool:
        limit = self._board_limit(code) * 100.0
        lower_rise = max(self.min_rise_pct, limit * self.limit_lower_ratio)
        upper_rise = min(
            self.max_rise_pct if limit <= 10.0 else limit * self.limit_upper_ratio,
            limit * self.limit_upper_ratio,
        )

        cond1 = row["close"] > row["ma5"] > row["ma10"] and row["close"] > row["ma20"]
        cond2 = lower_rise <= row["pct_change"] <= upper_rise
        cond3 = self.min_turnover_pct <= row["turnover_ratio"] <= self.max_turnover_pct
        cond4 = row["turn_ma5"] > 0 and row["turnover_ratio"] >= (row["turn_ma5"] * self.turnover_accel)
        cond5 = row["amount"] >= (row["amt_ma5"] * self.amount_accel) and row["amount"] >= self.min_amount
        cond6 = row["ret5"] >= self.min_ret5_pct and self.min_ret10_pct <= row["ret10"] <= self.max_ret10_pct
        cond7 = row["close_pos"] >= 0.62 and row["close"] >= (row["high"] * 0.985) and row["upper_shadow_pct"] <= 3.2
        cond8 = row["body_pct"] >= 2.0 and row["range_pct"] >= 4.0 and row["low"] <= (row["pre_close"] * 1.06)
        cond9 = row["close"] >= (row["close_hh30_prev"] * 0.99) or row["high"] >= (row["high_hh10_prev"] * 0.995)
        cond10 = -6.0 <= row["prev_pct_change"] <= 9.5
        cond11 = row["gap_open_pct"] <= 8.5
        return bool(cond1 and cond2 and cond3 and cond4 and cond5 and cond6 and cond7 and cond8 and cond9 and cond10 and cond11)


if __name__ == "__main__":
    strategy = AggressiveShortTermStrategy(
        initial_capital=1_000_000,
        max_positions=4,
        universe_size=None,
        max_position_weight=0.30,
        stop_loss_pct=0.06,
        take_profit_pct=0.18,
        trailing_stop_pct=0.08,
        max_hold_days=5,
        min_rise_pct=4.0,
        max_rise_pct=19.5,
        min_turnover_pct=3.0,
        max_turnover_pct=45.0,
        turnover_accel=1.15,
        amount_accel=1.15,
        min_amount=150_000_000,
        min_ret5_pct=2.0,
        min_ret10_pct=5.0,
        max_ret10_pct=120.0,
        limit_lower_ratio=0.35,
        limit_upper_ratio=0.98,
    )

    strategy.load_data()

    start = "2021-03-01"
    end = "2026-03-03"
    strategy.run_backtest(start, end)

    output_dir = os.path.join(CURRENT_DIR, "aggressive_daily_outputs")
    strategy.save_outputs(output_dir)
