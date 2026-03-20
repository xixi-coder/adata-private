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
        reason_counts = {}
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
            reason_detail = self._exit_reason_detail(reason, df, idx, pos)
            record = {
                "code": code,
                "short_name": pos.get("short_name", code),
                "signal_date": trade_date,
                "close_price": round(float(row["close"]), 3),
                "reason": self._label_exit_reason(reason),
                "reason_detail": reason_detail,
            }
            # 12
            sell_signals.append(record)
            pending_exits.append({"code": code, "reason": reason, "signal_date": trade_date})
            reason_counts[record["reason"]] = reason_counts.get(record["reason"], 0) + 1
        if not state["positions"]:
            print(f"[exit] {trade_date} 当前无持仓，卖出建议为空。")
            return sell_signals, pending_exits
        print(
            f"[exit] {trade_date} 持仓扫描={len(state['positions'])} "
            f"卖出建议={len(sell_signals)}"
        )
        if reason_counts:
            reason_summary = ", ".join(
                f"{reason}:{count}" for reason, count in sorted(reason_counts.items(), key=lambda item: item[1], reverse=True)
            )
            print(f"[exit] 卖出原因分布: {reason_summary}")
        else:
            print(f"[exit] {trade_date} 无触发卖出条件的持仓。")
        return sell_signals, pending_exits

    def _entry_candidates(self, state: dict, trade_date: str) -> list[dict]:
        # 收盘后扫描买入候选：
        # - 先过市场开关 _market_ok
        # - 再过三维共振 _is_entry_signal
        # - 最后按 score 排序，保留 max_positions * 3 个待执行候选
        market_status = self._market_gate_status(trade_date)
        if not market_status["ok"]:
            print(f"[entry] {trade_date} 市场开关关闭，跳过买入候选扫描。")
            if market_status.get("checks"):
                failed = [item for item in market_status["checks"] if not item["ok"]]
                failed_text = " | ".join(
                    f"{item['label']}未通过({item['detail']})"
                    for item in failed
                )
                print(f"[entry] 市场开关失败明细: {failed_text}")
            else:
                print(f"[entry] 市场开关失败明细: {market_status.get('error', '未知原因')}")
            return []
        blocked = set(state["positions"].keys())
        candidates = []
        stage_counts = {
            "shape_ok": 0,
            "indicator_ok": 0,
            "capital_ok": 0,
            "three_dim_ok": 0,
            "missing_shape": 0,
            "missing_indicator": 0,
            "missing_capital": 0,
            "only_missing_shape": 0,
            "only_missing_indicator": 0,
            "only_missing_capital": 0,
        }
        skipped_missing_day_k = 0
        scanned = 0
        near_miss_samples = []
        for code, df in self.stock_data.items():
            if code in blocked:
                continue
            if trade_date not in df.index:
                skipped_missing_day_k += 1
                continue
            scanned += 1
            meta = self._evaluate_entry_components(df, trade_date)
            shape_ok = bool(meta["shape_ok"])
            indicator_ok = bool(meta["indicator_ok"])
            capital_ok = bool(meta["capital_ok"])
            three_dim_ok = bool(meta["three_dim_ok"])

            stage_counts["shape_ok"] += int(shape_ok)
            stage_counts["indicator_ok"] += int(indicator_ok)
            stage_counts["capital_ok"] += int(capital_ok)
            stage_counts["three_dim_ok"] += int(three_dim_ok)
            stage_counts["missing_shape"] += int(not shape_ok)
            stage_counts["missing_indicator"] += int(not indicator_ok)
            stage_counts["missing_capital"] += int(not capital_ok)
            stage_counts["only_missing_shape"] += int(indicator_ok and capital_ok and not shape_ok)
            stage_counts["only_missing_indicator"] += int(shape_ok and capital_ok and not indicator_ok)
            stage_counts["only_missing_capital"] += int(shape_ok and indicator_ok and not capital_ok)

            if not three_dim_ok:
                pass_dim_count = int(shape_ok) + int(indicator_ok) + int(capital_ok)
                if pass_dim_count == 2 and len(near_miss_samples) < 5:
                    if not shape_ok:
                        miss_dim = "形态"
                    elif not indicator_ok:
                        miss_dim = "指标"
                    else:
                        miss_dim = "资金"
                    near_miss_samples.append(f"{code}(仅缺{miss_dim})")
                continue
            row = df.loc[trade_date]
            candidates.append(
                {
                    "code": code,
                    "short_name": self.stock_names.get(code, code),
                    "score": round(self._signal_score(row, meta), 3),
                    "signal_date": trade_date,
                    "reason": "三维共振通过",
                    "reason_detail": self._entry_reason_detail(meta, row),
                    "entry_shape": self._label_entry_shape(
                        "double_bottom" if meta.get("double_bottom") else "platform_breakout"
                    ),
                    "close_price": round(float(row["close"]), 3),
                }
            )
        candidates.sort(key=lambda item: item["score"], reverse=True)
        final_candidates = candidates[: self.max_positions * 3]

        print(
            f"[entry] {trade_date} 候选扫描: 股票池={len(self.stock_data)} "
            f"持仓占用={len(blocked)} 可扫描={scanned} 缺当日日K={skipped_missing_day_k}"
        )
        print(
            f"[entry] 维度通过: 形态={stage_counts['shape_ok']} 指标={stage_counts['indicator_ok']} "
            f"资金={stage_counts['capital_ok']} 三维共振={stage_counts['three_dim_ok']}"
        )
        print(
            f"[entry] 卡点统计: 缺形态={stage_counts['missing_shape']} 缺指标={stage_counts['missing_indicator']} "
            f"缺资金={stage_counts['missing_capital']} 仅缺形态={stage_counts['only_missing_shape']} "
            f"仅缺指标={stage_counts['only_missing_indicator']} 仅缺资金={stage_counts['only_missing_capital']}"
        )
        if near_miss_samples:
            print(f"[entry] 临门一脚样本: {' | '.join(near_miss_samples)}")
        if final_candidates:
            preview = ", ".join(
                f"{item['code']}({item['short_name']},score={item['score']})"
                for item in final_candidates[: self.max_positions]
            )
            print(
                f"[entry] 建议买入(top {min(len(final_candidates), self.max_positions)}): {preview}"
            )
        else:
            print(f"[entry] {trade_date} 建议买入为空：无股票同时满足三维共振条件。")
        return final_candidates

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
