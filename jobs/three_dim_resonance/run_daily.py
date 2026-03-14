# -*- coding: utf-8 -*-
import datetime
import json
import os
import pickle
import sys
from typing import Optional
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd


CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(os.path.dirname(CURRENT_DIR))

if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)
if CURRENT_DIR not in sys.path:
    sys.path.insert(0, CURRENT_DIR)

from strategies.three_dim_resonance import ThreeDimResonanceStrategy
from jobs.common.a_share_metadata import is_excluded_short_name, load_stock_metadata
from jobs.common.cloud_cache_sync import (
    download_json_from_drive,
    sync_cache_from_drive,
    sync_cache_to_drive,
    upload_file_to_drive,
    write_json,
)


class ThreeDimResonanceLiveStrategy(ThreeDimResonanceStrategy):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.project_root = PROJECT_ROOT
        self.full_cache_file = os.path.join(self.cache_dir, "full_data_v3_5year.pkl")
        self.state_file = os.path.join(self.cache_dir, "three_dim_live_state.json")
        self.summary_dir = os.path.join(CURRENT_DIR, "outputs")
        self.metadata = load_stock_metadata(PROJECT_ROOT)
        self.stock_names = {}

    @staticmethod
    def _now_shanghai() -> datetime.datetime:
        return datetime.datetime.now(ZoneInfo("Asia/Shanghai"))

    def _filter_non_st(self):
        filtered = {}
        for code, df in self.stock_data.items():
            meta = self.metadata.get(code, {})
            short_name = meta.get("short_name", code)
            if is_excluded_short_name(short_name):
                continue
            filtered[code] = df
            self.stock_names[code] = short_name
        self.stock_data = filtered

    def load_data(self):
        if not os.path.exists(self.full_cache_file):
            raise FileNotFoundError(f"未找到缓存: {self.full_cache_file}")
        super().load_data()
        self._filter_non_st()
        print(f"非ST股票池: {len(self.stock_data)}")

    def _resolve_trade_date(self, requested_date: str = "") -> str:
        if self.benchmark_df is None:
            raise RuntimeError("请先加载基准数据")
        all_dates = list(self.benchmark_df.index)
        if not all_dates:
            raise RuntimeError("基准交易日为空")
        if requested_date and requested_date in self.benchmark_df.index:
            return requested_date
        now_date = self._now_shanghai().strftime("%Y-%m-%d")
        valid_dates = [d for d in all_dates if d <= (requested_date or now_date)]
        if not valid_dates:
            raise RuntimeError("未找到可用交易日")
        return valid_dates[-1]

    def _next_trade_date(self, trade_date: str) -> str:
        all_dates = list(self.benchmark_df.index)
        idx = all_dates.index(trade_date)
        if idx >= len(all_dates) - 1:
            return ""
        return all_dates[idx + 1]

    def _default_state(self) -> dict:
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

    def _execute_pending_exits(self, state: dict, trade_date: str) -> list[dict]:
        executed = []
        for order in state.get("pending_exits", []):
            code = order["code"]
            pos = state["positions"].get(code)
            df = self.stock_data.get(code)
            if not pos or df is None or trade_date not in df.index:
                continue
            open_price = float(df.loc[trade_date, "open"])
            gross = pos["shares"] * open_price
            sell_fee = gross * self.sell_fee_rate
            net = gross - sell_fee
            state["cash"] += net
            pnl = net - pos["cost"]
            pnl_pct = pnl / pos["cost"] if pos["cost"] > 0 else 0.0
            state["completed_trades"].append(
                {
                    "code": code,
                    "short_name": pos.get("short_name", code),
                    "signal_date": order.get("signal_date", ""),
                    "buy_date": pos["buy_date"],
                    "sell_date": trade_date,
                    "buy_price": round(pos["buy_price"], 3),
                    "sell_price": round(open_price, 3),
                    "shares": int(pos["shares"]),
                    "holding_days": int(pos.get("holding_days", 0)),
                    "profit": round(pnl, 2),
                    "profit_pct": round(pnl_pct * 100.0, 2),
                    "reason": order["reason"],
                }
            )
            executed.append(
                {
                    "code": code,
                    "short_name": pos.get("short_name", code),
                    "trade_date": trade_date,
                    "sell_price": round(open_price, 3),
                    "reason": self._label_exit_reason(order["reason"]),
                }
            )
            del state["positions"][code]
        state["pending_exits"] = []
        return executed

    def _current_total_equity_from_state(self, state: dict, trade_date: str) -> float:
        market_value = 0.0
        for code, pos in state["positions"].items():
            df = self.stock_data.get(code)
            if df is None or trade_date not in df.index:
                market_value += pos["shares"] * pos.get("last_price", pos["buy_price"])
                continue
            market_value += pos["shares"] * float(df.loc[trade_date, "close"])
        return state["cash"] + market_value

    def _execute_pending_entries(self, state: dict, trade_date: str) -> list[dict]:
        executed = []
        orders = sorted(state.get("pending_entries", []), key=lambda item: item["score"], reverse=True)
        state["pending_entries"] = []
        for order in orders:
            if len(state["positions"]) >= self.max_positions:
                break
            code = order["code"]
            if code in state["positions"]:
                continue
            df = self.stock_data.get(code)
            if df is None or trade_date not in df.index:
                continue
            buy_price = float(df.loc[trade_date, "open"])
            if not np.isfinite(buy_price) or buy_price <= 0:
                continue
            slots_left = self.max_positions - len(state["positions"])
            total_equity = self._current_total_equity_from_state(state, trade_date)
            budget = min(state["cash"] / max(slots_left, 1), total_equity * self.max_position_weight)
            shares = int(budget / buy_price / 100) * 100
            if shares < 100:
                continue
            gross = shares * buy_price
            buy_fee = gross * self.buy_fee_rate
            cost = gross + buy_fee
            if cost > state["cash"]:
                continue
            state["cash"] -= cost
            state["positions"][code] = {
                "short_name": order.get("short_name", code),
                "buy_date": trade_date,
                "buy_price": buy_price,
                "shares": int(shares),
                "cost": float(cost),
                "holding_days": 0,
                "max_close": buy_price,
                "last_price": buy_price,
                "trailing_armed": False,
                "entry_shape": order.get("entry_shape", ""),
                "signal_date": order.get("signal_date", ""),
            }
            executed.append(
                {
                    "code": code,
                    "short_name": order.get("short_name", code),
                    "trade_date": trade_date,
                    "buy_price": round(buy_price, 3),
                    "signal_date": order.get("signal_date", ""),
                    "entry_shape": order.get("entry_shape", ""),
                }
            )
        return executed

    def _close_side_updates(self, state: dict, trade_date: str) -> tuple[list[dict], list[dict]]:
        sell_signals = []
        pending_exits = []
        for code, pos in state["positions"].items():
            df = self.stock_data.get(code)
            if df is None or trade_date not in df.index:
                continue
            idx = df.index.get_loc(trade_date)
            if isinstance(idx, slice):
                idx = idx.stop - 1
            row = df.iloc[idx]
            pos["holding_days"] = max(idx - df.index.get_loc(pos["buy_date"]), 0)
            pos["last_price"] = float(row["close"])
            pos["max_close"] = max(float(pos.get("max_close", pos["buy_price"])), pos["last_price"])
            if pos["holding_days"] < 1:
                continue
            reason = self._exit_reason(code, df, idx, pos)
            if not reason:
                continue
            record = {
                "code": code,
                "short_name": pos.get("short_name", code),
                "signal_date": trade_date,
                "close_price": round(float(row["close"]), 3),
                "reason": self._label_exit_reason(reason),
            }
            sell_signals.append(record)
            pending_exits.append({"code": code, "reason": reason, "signal_date": trade_date})
        return sell_signals, pending_exits

    def _entry_candidates(self, state: dict, trade_date: str) -> list[dict]:
        if not self._market_ok(trade_date):
            return []
        blocked = set(state["positions"].keys())
        candidates = []
        for code, df in self.stock_data.items():
            if code in blocked or trade_date not in df.index:
                continue
            signal_ok, meta = self._is_entry_signal(code, df, trade_date)
            if not signal_ok:
                continue
            row = df.loc[trade_date]
            candidates.append(
                {
                    "code": code,
                    "short_name": self.stock_names.get(code, code),
                    "score": round(self._signal_score(row, meta), 3),
                    "signal_date": trade_date,
                    "entry_shape": self._label_entry_shape(
                        "double_bottom" if meta.get("double_bottom") else "platform_breakout"
                    ),
                    "close_price": round(float(row["close"]), 3),
                }
            )
        candidates.sort(key=lambda item: item["score"], reverse=True)
        return candidates[: self.max_positions * 3]

    def _positions_snapshot(self, state: dict, trade_date: str) -> list[dict]:
        snapshot = []
        for code, pos in state["positions"].items():
            df = self.stock_data.get(code)
            close_price = pos.get("last_price", pos["buy_price"])
            if df is not None and trade_date in df.index:
                close_price = float(df.loc[trade_date, "close"])
            snapshot.append(
                {
                    "code": code,
                    "short_name": pos.get("short_name", code),
                    "buy_date": pos["buy_date"],
                    "buy_price": round(pos["buy_price"], 3),
                    "close_price": round(float(close_price), 3),
                    "holding_days": int(pos.get("holding_days", 0)),
                }
            )
        snapshot.sort(key=lambda item: item["holding_days"], reverse=True)
        return snapshot

    def run_daily(self, requested_date: str = "") -> dict:
        sync_cache_from_drive(self.project_root, "three_dim_cache_bundle.tar.gz", ["data/cache"])
        download_json_from_drive(
            self.project_root,
            "three_dim_live_state.json",
            self.state_file,
        )
        self.load_data()
        trade_date = self._resolve_trade_date(requested_date)
        next_trade_date = self._next_trade_date(trade_date)
        state = self.load_state()
        if state.get("last_run_trade_date") == trade_date:
            summary = {
                "run_time": self._now_shanghai().strftime("%Y-%m-%d %H:%M:%S"),
                "signal_date": trade_date,
                "next_trade_date": next_trade_date,
                "candidate_reference_date": trade_date,
                "executed_buys": [],
                "executed_sells": [],
                "buy_suggestions": [],
                "sell_suggestions": [],
                "positions": self._positions_snapshot(state, trade_date),
                "cash": round(float(state["cash"]), 2),
                "status": "skipped",
                "note": f"{trade_date} 已处理，通常表示今天不是新的交易日。",
            }
            self._write_outputs(summary)
            return summary

        executed_sells = self._execute_pending_exits(state, trade_date)
        executed_buys = self._execute_pending_entries(state, trade_date)
        sell_signals, pending_exits = self._close_side_updates(state, trade_date)
        buy_candidates = self._entry_candidates(state, trade_date)

        state["pending_exits"] = pending_exits
        state["pending_entries"] = buy_candidates
        state["last_run_trade_date"] = trade_date
        self.save_state(state)
        sync_cache_to_drive(self.project_root, "three_dim_cache_bundle.tar.gz", ["data/cache"])
        upload_file_to_drive(self.state_file, "three_dim_live_state.json", mime_type="application/json")

        summary = {
            "run_time": self._now_shanghai().strftime("%Y-%m-%d %H:%M:%S"),
            "signal_date": trade_date,
            "next_trade_date": next_trade_date,
            "candidate_reference_date": trade_date,
            "executed_buys": executed_buys,
            "executed_sells": executed_sells,
            "buy_suggestions": buy_candidates[: self.max_positions],
            "sell_suggestions": sell_signals,
            "positions": self._positions_snapshot(state, trade_date),
            "cash": round(float(state["cash"]), 2),
            "status": "ok",
            "note": "",
        }
        self._write_outputs(summary)
        return summary

    def _write_outputs(self, summary: dict):
        os.makedirs(self.summary_dir, exist_ok=True)
        date_key = summary["signal_date"].replace("-", "")
        summary_json_path = os.path.join(self.summary_dir, f"three_dim_summary_{date_key}.json")
        latest_summary_json_path = os.path.join(self.summary_dir, "latest_summary.json")
        email_body_path = os.path.join(self.summary_dir, "latest_email_body.txt")
        write_json(summary_json_path, summary)
        write_json(latest_summary_json_path, summary)

        lines = [
            "三维共振策略建议",
            f"候选依据日: {summary['candidate_reference_date']}",
            f"建议执行日: {summary['next_trade_date'] or '暂无下一交易日'}",
        ]
        if summary.get("note"):
            lines.append(f"备注: {summary['note']}")
        lines.extend(["", "建议卖出:"])
        if summary["sell_suggestions"]:
            for idx, item in enumerate(summary["sell_suggestions"], start=1):
                lines.append(
                    f"{idx}. {item['code']} {item['short_name']} "
                    f"收盘价={item['close_price']} 原因={item['reason']}"
                )
        else:
            lines.append("无")

        lines.extend(["", "建议买入:"])
        if summary["buy_suggestions"]:
            for idx, item in enumerate(summary["buy_suggestions"], start=1):
                lines.append(
                    f"{idx}. {item['code']} {item['short_name']} "
                    f"收盘价={item['close_price']} 分数={item['score']} 形态={item['entry_shape']}"
                )
        else:
            lines.append("无")

        lines.extend(["", "当前持仓:"])
        if summary["positions"]:
            for idx, item in enumerate(summary["positions"], start=1):
                lines.append(
                    f"{idx}. {item['code']} {item['short_name']} "
                    f"买入日={item['buy_date']} 买入价={item['buy_price']} "
                    f"收盘价={item['close_price']} 持仓天数={item['holding_days']}"
                )
        else:
            lines.append("无")

        with open(email_body_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        upload_file_to_drive(summary_json_path, os.path.basename(summary_json_path), mime_type="application/json")
        upload_file_to_drive(email_body_path, "three_dim_latest_email.txt", mime_type="text/plain")


if __name__ == "__main__":
    strategy = ThreeDimResonanceLiveStrategy(
        initial_capital=1_000_000,
        max_positions=8,
        universe_size=1800,
        max_position_weight=0.20,
    )
    summary = strategy.run_daily(os.getenv("TRADE_DATE", "").strip())
    print(json.dumps(summary, ensure_ascii=False, indent=2))
