# -*- coding: utf-8 -*-
import copy
import os

import jobs.three_dim_resonance.live.output_mixin as output_mixin_mod
from jobs.three_dim_resonance.stage_common import build_strategy, maybe_sync_cloud, time_call


if __name__ == "__main__":
    strategy = build_strategy()
    maybe_sync_cloud(strategy)

    if os.getenv("PROFILE_SKIP_UPLOAD", "1") == "1":
        output_mixin_mod.upload_file_to_drive = lambda *args, **kwargs: False
        print("[info] PROFILE_SKIP_UPLOAD=1，跳过上传，仅测本地写文件。")

    _, _ = time_call("load_data", strategy.load_data)
    trade_date, _ = time_call("_resolve_trade_date", strategy._resolve_trade_date, os.getenv("TRADE_DATE", "").strip())
    next_trade_date, _ = time_call("_next_trade_date", strategy._next_trade_date, trade_date)
    state, _ = time_call("load_state", strategy.load_state)

    state = copy.deepcopy(state)
    executed_sells, _ = time_call("_execute_pending_exits", strategy._execute_pending_exits, state, trade_date)
    executed_buys, _ = time_call("_execute_pending_entries", strategy._execute_pending_entries, state, trade_date)
    sell_signals, _ = time_call("_close_side_updates", strategy._close_side_updates, state, trade_date)
    buy_candidates, _ = time_call("_entry_candidates", strategy._entry_candidates, state, trade_date)

    summary = {
        "run_time": strategy._now_shanghai().strftime("%Y-%m-%d %H:%M:%S"),
        "signal_date": trade_date,
        "next_trade_date": next_trade_date,
        "candidate_reference_date": trade_date,
        "executed_buys": executed_buys,
        "executed_sells": executed_sells,
        "buy_suggestions": buy_candidates[: strategy.max_positions],
        "sell_suggestions": sell_signals,
        "positions": strategy._positions_snapshot(state, trade_date),
        "cash": round(float(state["cash"]), 2),
        "status": "ok",
        "note": "",
    }

    _, _ = time_call("_write_outputs", strategy._write_outputs, summary)
    print("\n[result] summary written to:", strategy.summary_dir)
