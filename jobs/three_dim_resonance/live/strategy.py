# -*- coding: utf-8 -*-
import os

from strategies.three_dim_resonance import ThreeDimResonanceStrategy
from jobs.common.a_share_metadata import load_stock_metadata
from jobs.common.cloud_cache_sync import (
    download_json_from_drive,
    sync_cache_from_drive,
)
from jobs.three_dim_resonance.live.data_date_mixin import DataDateMixin
from jobs.three_dim_resonance.live.execution_mixin import ExecutionMixin
from jobs.three_dim_resonance.live.output_mixin import OutputMixin
from jobs.three_dim_resonance.live.state_mixin import StateMixin


CURRENT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROJECT_ROOT = os.path.dirname(os.path.dirname(CURRENT_DIR))


class ThreeDimResonanceLiveStrategy(
    DataDateMixin,
    StateMixin,
    ExecutionMixin,
    OutputMixin,
    ThreeDimResonanceStrategy,
):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # run_daily 使用 5 年版缓存，和回测默认缓存文件区分开。
        self.project_root = PROJECT_ROOT
        self.full_cache_file = os.path.join(self.cache_dir, "full_data_v3_5year.pkl")
        self.state_file = os.path.join(self.cache_dir, "three_dim_live_state.json")
        self.summary_dir = os.path.join(CURRENT_DIR, "outputs")
        self.metadata = load_stock_metadata(PROJECT_ROOT)
        self.stock_names = {}
        self.last_entry_skip_reason = ""
        self.last_entry_diagnostics = {}
        try:
            self.today_k_coverage_min = float(os.getenv("THREE_DIM_TODAY_K_COVERAGE_MIN", "0.85"))
        except ValueError:
            self.today_k_coverage_min = 0.85

    def run_daily(self, requested_date: str = "") -> dict:
        # ---- Step 1: 同步云端缓存和状态 ----
        sync_cache_from_drive(self.project_root, "three_dim_cache_bundle.tar.gz", ["data/cache"])
        download_json_from_drive(
            self.project_root,
            "three_dim_live_state.json",
            self.state_file,
        )
        # ---- Step 2: 加载数据并确定本次信号日期 ----
        self.load_data()
        trade_date = self._resolve_trade_date(requested_date)
        next_trade_date = self._next_trade_date(trade_date)
        state = self.load_state()
        rerun_note = "建议模式：不执行买卖，仅输出当日建议。"

        # ---- Step 3: 仅生成建议，不执行成交 ----
        executed_sells = []
        executed_buys = []
        sell_signals, _pending_exits = self._close_side_updates(state, trade_date)
        buy_candidates = self._entry_candidates(state, trade_date)
        buy_suggestions = buy_candidates[: self.max_positions]

        summary = {
            "run_time": self._now_shanghai().strftime("%Y-%m-%d %H:%M:%S"),
            "signal_date": trade_date,
            "next_trade_date": next_trade_date,
            "candidate_reference_date": trade_date,
            "executed_buys": executed_buys,
            "executed_sells": executed_sells,
            "buy_suggestions": buy_suggestions,
            "sell_suggestions": sell_signals,
            "positions": self._positions_snapshot(state, trade_date),
            "cash": round(float(state["cash"]), 2),
            "status": "ok",
            "note": rerun_note,
            "entry_skip_reason": self.last_entry_skip_reason,
            "entry_diagnostics": self.last_entry_diagnostics,
        }
        print(
            f"[daily] {trade_date} 建议汇总: "
            f"买入={len(buy_suggestions)} 卖出={len(sell_signals)} "
            f"持仓={len(summary['positions'])} 现金={summary['cash']:.2f}"
        )
        # ---- Step 6: 产出本地文件 + 邮件正文 ----
        self._write_outputs(summary)
        return summary


__all__ = ["ThreeDimResonanceLiveStrategy", "CURRENT_DIR", "PROJECT_ROOT"]
