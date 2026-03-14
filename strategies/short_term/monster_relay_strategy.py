# -*- coding: utf-8 -*-
import os
import sys


CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(CURRENT_DIR)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from strategies.short_term.short_term_strategy_code import ShortTermDisagreementStrategy


class MonsterRelayStrategy(ShortTermDisagreementStrategy):
    """
    妖股接力版日线代理策略

    风格:
    - 聚焦全市场高弹性接力个股
    - 允许更强势追涨, 更贴近“龙头接力”
    - 牺牲稳定性换更高弹性, 回撤会显著放大
    """

    @staticmethod
    def _is_supported_equity_code(code: str) -> bool:
        if not (isinstance(code, str) and len(code) == 6 and code.isdigit()):
            return False
        return code.startswith(("000", "001", "002", "003", "300", "301", "600", "601", "603", "605", "688", "689"))

    def _market_ok(self, date_str: str) -> bool:
        if date_str not in self.benchmark_df.index:
            return False
        row = self.benchmark_df.loc[date_str]
        # 接力版只回避系统性杀跌日, 不再强依赖指数趋势
        return bool(row["change_pct"] > -1.6)

    def _signal_score(self, code: str, row):
        amount_ratio = row["amount"] / row["amt_ma5"] if row["amt_ma5"] > 0 else 0.0
        turn_ratio = row["turnover_ratio"] / row["turn_ma5"] if row["turn_ma5"] > 0 else 0.0
        breakout_pct = (row["close"] / row["close_hh30_prev"] - 1.0) * 100.0 if row["close_hh30_prev"] > 0 else 0.0
        limit = self._board_limit(code) * 100.0
        limit_gap = max(limit - row["pct_change"], 0.0)
        return (
            row["pct_change"] * 1.4
            + row["ret5"] * 0.35
            + row["ret10"] * 0.22
            + turn_ratio * 2.6
            + amount_ratio * 1.9
            + breakout_pct * 2.4
            + row["close_pos"] * 2.8
            - row["upper_shadow_pct"] * 1.1
            - limit_gap * 0.45
        )

    def _is_entry_signal(self, code: str, row) -> bool:
        limit = self._board_limit(code) * 100.0
        is_main_board = limit <= 10.0
        lower_rise = max(5.5 if is_main_board else self.min_rise_pct, limit * self.limit_lower_ratio)
        upper_rise = min(limit * self.limit_upper_ratio, 9.8 if is_main_board else self.max_rise_pct)

        cond1 = row["close"] > row["ma5"] > row["ma10"] and row["close"] > row["ma20"]
        cond2 = lower_rise <= row["pct_change"] <= upper_rise
        cond3 = (6.0 if is_main_board else self.min_turnover_pct) <= row["turnover_ratio"] <= self.max_turnover_pct
        cond4 = row["turn_ma5"] > 0 and row["turnover_ratio"] >= (row["turn_ma5"] * (1.25 if is_main_board else self.turnover_accel))
        cond5 = row["amount"] >= (row["amt_ma5"] * (1.25 if is_main_board else self.amount_accel)) and row["amount"] >= (180_000_000 if is_main_board else self.min_amount)
        cond6 = row["ret5"] >= (6.0 if is_main_board else self.min_ret5_pct) and (10.0 if is_main_board else self.min_ret10_pct) <= row["ret10"] <= self.max_ret10_pct
        cond7 = row["close_pos"] >= 0.72 and row["close"] >= (row["high"] * 0.99) and row["upper_shadow_pct"] <= (2.2 if is_main_board else 2.8)
        cond8 = row["body_pct"] >= (3.5 if is_main_board else 4.5) and row["range_pct"] >= (4.5 if is_main_board else 6.0) and row["low"] <= (row["pre_close"] * 1.06)
        cond9 = row["close"] >= (row["close_hh30_prev"] * 1.01) and row["high"] >= (row["high_hh10_prev"] * 1.005)
        cond10 = -2.0 <= row["prev_pct_change"] <= (10.0 if is_main_board else 19.5)
        cond11 = row["gap_open_pct"] <= (5.8 if is_main_board else 9.5)
        cond12 = row["ret20"] <= (90.0 if is_main_board else 160.0)
        return bool(cond1 and cond2 and cond3 and cond4 and cond5 and cond6 and cond7 and cond8 and cond9 and cond10 and cond11 and cond12)


if __name__ == "__main__":
    strategy = MonsterRelayStrategy(
        initial_capital=1_000_000,
        max_positions=3,
        universe_size=None,
        max_position_weight=0.33,
        stop_loss_pct=0.07,
        take_profit_pct=0.18,
        trailing_stop_pct=0.10,
        max_hold_days=3,
        min_rise_pct=8.0,
        max_rise_pct=19.6,
        min_turnover_pct=8.0,
        max_turnover_pct=42.0,
        turnover_accel=1.35,
        amount_accel=1.35,
        min_amount=250_000_000,
        min_ret5_pct=10.0,
        min_ret10_pct=18.0,
        max_ret10_pct=120.0,
        limit_lower_ratio=0.55,
        limit_upper_ratio=0.985,
        min_listing_days=180,
    )

    strategy.load_data()

    start = "2021-03-01"
    end = "2026-03-03"
    strategy.run_backtest(start, end)

    output_dir = os.path.join(CURRENT_DIR, "monster_relay_outputs")
    strategy.save_outputs(output_dir)
