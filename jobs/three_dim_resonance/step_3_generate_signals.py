# -*- coding: utf-8 -*-
import copy
import os

from jobs.three_dim_resonance.stage_common import build_strategy, maybe_sync_cloud, time_call


if __name__ == "__main__":
    strategy = build_strategy()
    maybe_sync_cloud(strategy)

    _, _ = time_call("load_data", strategy.load_data)
    trade_date, _ = time_call("_resolve_trade_date", strategy._resolve_trade_date, os.getenv("TRADE_DATE", "").strip())
    state, _ = time_call("load_state", strategy.load_state)

    # 可选：先在副本上模拟执行待成交，便于更贴近 run_daily 行为。
    state = copy.deepcopy(state)
    _, _ = time_call("_execute_pending_exits", strategy._execute_pending_exits, state, trade_date)
    _, _ = time_call("_execute_pending_entries", strategy._execute_pending_entries, state, trade_date)

    sell_signals, _ = time_call("_close_side_updates", strategy._close_side_updates, state, trade_date)
    buy_candidates, _ = time_call("_entry_candidates", strategy._entry_candidates, state, trade_date)

    print("\n[result] trade_date:", trade_date)
    print("[result] sell_signals:", len(sell_signals))
    print("[result] buy_candidates:", len(buy_candidates))
    print("[result] buy_suggestions(top max_positions):", len(buy_candidates[: strategy.max_positions]))
