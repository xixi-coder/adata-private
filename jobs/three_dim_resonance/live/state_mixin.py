# -*- coding: utf-8 -*-
import json
import os

from jobs.common.cloud_cache_sync import write_json


class StateMixin:
    def _default_state(self) -> dict:
        # 状态文件是 run_daily 的“记忆体”：
        # - pending_entries / pending_exits：上一交易日收盘后生成，下一交易日开盘执行
        # - last_run_trade_date：避免同一交易日重复成交
        return {
            "initial_capital": self.initial_capital,
            "cash": self.initial_capital,
            "positions": {},
            "pending_entries": [],
            "pending_exits": [],
            "completed_trades": [],
            "last_run_trade_date": "",
        }

    def load_state(self) -> dict:
        os.makedirs(self.cache_dir, exist_ok=True)
        if os.path.exists(self.state_file):
            with open(self.state_file, "r", encoding="utf-8") as f:
                state = json.load(f)
        else:
            state = self._default_state()
        state.setdefault("cash", self.initial_capital)
        state.setdefault("positions", {})
        state.setdefault("pending_entries", [])
        state.setdefault("pending_exits", [])
        state.setdefault("completed_trades", [])
        state.setdefault("last_run_trade_date", "")
        return state

    def save_state(self, state: dict):
        write_json(self.state_file, state)
