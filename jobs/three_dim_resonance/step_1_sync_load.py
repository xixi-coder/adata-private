# -*- coding: utf-8 -*-
import os

from jobs.three_dim_resonance.stage_common import build_strategy, maybe_sync_cloud, time_call


if __name__ == "__main__":
    strategy = build_strategy()
    maybe_sync_cloud(strategy)

    _, _ = time_call("load_data", strategy.load_data)
    trade_date, _ = time_call("_resolve_trade_date", strategy._resolve_trade_date, os.getenv("TRADE_DATE", "").strip())
    next_trade_date, _ = time_call("_next_trade_date", strategy._next_trade_date, trade_date)
    state, _ = time_call("load_state", strategy.load_state)

    print("\n[result] trade_date:", trade_date)
    print("[result] next_trade_date:", next_trade_date or "")
    print("[result] positions:", len(state.get("positions", {})))
    print("[result] pending_entries:", len(state.get("pending_entries", [])))
    print("[result] pending_exits:", len(state.get("pending_exits", [])))
