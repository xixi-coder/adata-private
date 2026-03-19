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

    # 使用副本，避免调试脚本误改正式状态。
    state = copy.deepcopy(state)

    executed_sells, _ = time_call("_execute_pending_exits", strategy._execute_pending_exits, state, trade_date)
    executed_buys, _ = time_call("_execute_pending_entries", strategy._execute_pending_entries, state, trade_date)

    print("\n[result] trade_date:", trade_date)
    print("[result] executed_sells:", len(executed_sells))
    print("[result] executed_buys:", len(executed_buys))
    print("[result] positions_after_execute:", len(state.get("positions", {})))
