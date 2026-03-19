# -*- coding: utf-8 -*-
import numpy as np


class ExecutionMixin:
    def _execute_pending_exits(self, state: dict, trade_date: str) -> list[dict]:
        # 执行上一交易日生成的卖单，按当日开盘价成交。
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
        # 执行上一交易日生成的买单，按 score 从高到低成交。
        # 资金管理约束：持仓上限 + 单票仓位上限 + A股100股整数手。
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
        # 收盘后更新持仓状态，并生成“下一交易日待执行”的卖出信号。
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
        # 收盘后扫描买入候选：
        # - 先过市场开关 _market_ok
        # - 再过三维共振 _is_entry_signal
        # - 最后按 score 排序，保留 max_positions * 3 个待执行候选
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

    def _pending_entries_snapshot(self, state: dict) -> list[dict]:
        snapshot = []
        for item in state.get("pending_entries", []):
            snapshot.append(
                {
                    "code": item["code"],
                    "short_name": item.get("short_name", self.stock_names.get(item["code"], item["code"])),
                    "score": round(float(item.get("score", 0.0)), 3),
                    "signal_date": item.get("signal_date", ""),
                    "entry_shape": item.get("entry_shape", ""),
                    "close_price": round(float(item.get("close_price", 0.0)), 3),
                }
            )
        snapshot.sort(key=lambda item: item["score"], reverse=True)
        return snapshot

    def _pending_exits_snapshot(self, state: dict) -> list[dict]:
        snapshot = []
        for item in state.get("pending_exits", []):
            code = item["code"]
            pos = state.get("positions", {}).get(code, {})
            snapshot.append(
                {
                    "code": code,
                    "short_name": pos.get("short_name", self.stock_names.get(code, code)),
                    "signal_date": item.get("signal_date", ""),
                    "close_price": round(float(pos.get("last_price", pos.get("buy_price", 0.0))), 3),
                    "reason": self._label_exit_reason(item.get("reason", "")),
                }
            )
        return snapshot
