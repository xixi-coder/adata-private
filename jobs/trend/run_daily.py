# -*- coding: utf-8 -*-
from __future__ import annotations

import os
import sys
from typing import Any

import numpy as np
import pandas as pd


CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(os.path.dirname(CURRENT_DIR))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from jobs.boll.run_daily import _load_mine_risks
from jobs.common.a_share_metadata import load_stock_metadata
from jobs.common.daily_job import (
    now_shanghai as _now_shanghai,
    read_bool_env as _read_bool_env,
    read_float_env as _read_float_env,
    read_int_env as _read_int_env,
    resolve_trade_date as _resolve_trade_date,
    write_json as _write_json,
)
from strategies.trend import TrendTradingConfig, TrendTradingStrategy
from strategies.volatility import QualityGateConfig


OUTPUT_DIR = os.path.join(CURRENT_DIR, "outputs")
SHARED_MARKET_CACHE_ARCHIVE = "three_dim_cache_bundle.tar.gz"


def _read_universe_size() -> int | None:
    value = os.getenv("TREND_UNIVERSE_SIZE", "").strip().lower()
    if not value or value in {"all", "none", "0"}:
        return None
    size = int(value)
    return size if size > 0 else None


def resolve_trade_date(requested: str | None = None) -> tuple[str, bool, str]:
    return _resolve_trade_date(requested, action="执行趋势交易扫描", skip_action="跳过扫描")


def _to_output_df(signals: pd.DataFrame) -> pd.DataFrame:
    columns = [
        "信号日期", "股票代码", "股票名称", "信号类型", "评分", "风险等级", "收盘价", "日涨跌%",
        "10日涨跌%", "20日涨跌%", "60日涨跌%", "20日线", "60日线", "120日线",
        "60日线20日斜率%", "当日/20日成交额比", "5/20日成交额比", "距20日线%", "60日回撤%",
        "观察价", "失效价", "入选依据",
    ]
    if signals.empty:
        return pd.DataFrame(columns=columns)
    return pd.DataFrame(
        {
            "信号日期": pd.to_datetime(signals["trade_date"]).dt.strftime("%Y-%m-%d"),
            "股票代码": signals["stock_code"],
            "股票名称": signals["short_name"].replace("", np.nan).fillna(signals["stock_code"]),
            "信号类型": signals["signal_type"],
            "评分": signals["score"].round(2),
            "风险等级": signals["risk_level"],
            "收盘价": signals["close"].round(3),
            "日涨跌%": (signals["ret_1d"] * 100).round(2),
            "10日涨跌%": (signals["ret_10d"] * 100).round(2),
            "20日涨跌%": (signals["ret_20d"] * 100).round(2),
            "60日涨跌%": (signals["ret_60d"] * 100).round(2),
            "20日线": signals["ma20"].round(3),
            "60日线": signals["ma60"].round(3),
            "120日线": signals["ma120"].round(3),
            "60日线20日斜率%": (signals["ma60_slope20"] * 100).round(2),
            "当日/20日成交额比": signals["amount_ratio1_20"].round(2),
            "5/20日成交额比": signals["amount_ratio5_20"].round(2),
            "距20日线%": (signals["distance_to_ma20"] * 100).round(2),
            "60日回撤%": (signals["drawdown60"] * 100).round(2),
            "观察价": signals["watch_price"].round(3),
            "失效价": signals["invalid_price"].round(3),
            "入选依据": signals["reason"],
        }
    )


def _format_rows(df: pd.DataFrame, limit: int = 15) -> list[str]:
    if df.empty:
        return ["无"]
    rows = []
    for idx, (_, row) in enumerate(df.head(limit).iterrows(), start=1):
        rows.append(
            f"{idx}. {row['股票代码']} | {row['股票名称']} | {row['评分']} | {row['风险等级']} | "
            f"{row['收盘价']} | {row['60日涨跌%']} | {row['当日/20日成交额比']} | "
            f"{row['观察价']} | {row['失效价']} | {row['入选依据']}"
        )
    return rows


def _build_email_body(summary: dict[str, Any], candidates: pd.DataFrame) -> str:
    report = summary.get("quality_report", {})
    lines = [
        "趋势交易选股扫描", "",
        f"- 运行时间: {summary['run_time']}",
        f"- 请求日期: {summary['trade_date']}",
        f"- 信号日期: {summary.get('signal_date', '')}",
        f"- 候选数量: {summary.get('candidate_count', 0)}",
        f"- 股票池: 初始 {report.get('initial_stock_count', 0)}，通过票质过滤 {report.get('accepted_stock_count', 0)}",
        "", "趋势突破",
    ]
    lines.extend(_format_rows(candidates[candidates["信号类型"].eq("趋势突破")]))
    lines.extend(["", "趋势回踩"])
    lines.extend(_format_rows(candidates[candidates["信号类型"].eq("趋势回踩")]))
    lines.extend(
        [
            "", "使用提示",
            "- 趋势突破等待次日确认不跌回突破位，避免高开追涨。",
            "- 趋势回踩以20日线为锚，收盘有效跌破失效价时退出观察。",
            "- 单票控制仓位并设置止损；趋势信号不代表确定收益。",
            "", "风险提示", "以上为量化研究和复盘结果，不构成个性化投资建议或收益承诺。",
        ]
    )
    return "\n".join(lines)


def _write_skip_outputs(trade_date: str, note: str) -> None:
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    summary = {
        "run_time": _now_shanghai().strftime("%Y-%m-%d %H:%M:%S"),
        "trade_date": trade_date,
        "is_trade_day": False,
        "note": note,
    }
    _write_json(os.path.join(OUTPUT_DIR, "latest_summary.json"), summary)
    body = f"趋势交易选股扫描\n\n日期: {trade_date}\n状态: {note}\n\n非交易日不生成候选。"
    with open(os.path.join(OUTPUT_DIR, "latest_email_body.txt"), "w", encoding="utf-8") as f:
        f.write(body + "\n")


def main() -> None:
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    requested = os.getenv("TRADE_DATE", "").strip() or None
    trade_date, is_trade_day, trade_note = resolve_trade_date(requested)
    if not is_trade_day:
        _write_skip_outputs(trade_date, trade_note)
        print(trade_note)
        return

    try:
        from jobs.common.cloud_cache_sync import sync_cache_from_drive

        sync_cache_from_drive(PROJECT_ROOT, SHARED_MARKET_CACHE_ARCHIVE, ["data/cache"])
    except Exception as exc:
        print(f"缓存同步不可用，继续使用本地缓存: {exc}")

    stock_meta = load_stock_metadata(PROJECT_ROOT)
    quality = QualityGateConfig(
        min_history_days=_read_int_env("TREND_MIN_HISTORY_DAYS", 180),
        min_listing_days=_read_int_env("TREND_MIN_LISTING_DAYS", 180),
        min_amount_ma20=_read_float_env("TREND_MIN_AMOUNT_MA20", 100_000_000),
        min_valid_days20=_read_int_env("TREND_MIN_VALID_DAYS20", 16),
        min_price=_read_float_env("TREND_MIN_PRICE", 3.0),
        max_drawdown60=_read_float_env("TREND_MAX_DRAWDOWN60", 0.35),
        min_mine_score=_read_float_env("TREND_MIN_MINE_SCORE", 75.0),
    )
    config = TrendTradingConfig(
        quality=quality,
        universe_size=_read_universe_size(),
        breakout_limit=_read_int_env("TREND_BREAKOUT_LIMIT", 50),
        pullback_limit=_read_int_env("TREND_PULLBACK_LIMIT", 50),
        min_ma60_slope20=_read_float_env("TREND_MIN_MA60_SLOPE20", 0.02),
        min_breakout_amount_ratio=_read_float_env("TREND_MIN_BREAKOUT_AMOUNT_RATIO", 1.20),
    )
    strategy = TrendTradingStrategy(config)
    strategy.load_market_cache()
    strategy.compute_features(stock_meta=stock_meta)
    rough_signals = strategy.latest_signals(trade_date)

    final_signals = rough_signals
    if _read_bool_env("TREND_ENABLE_MINE_CLEARANCE", True) and not rough_signals.empty:
        codes = rough_signals.sort_values("score", ascending=False)["stock_code"].drop_duplicates().tolist()
        mine_risks = _load_mine_risks(codes, _read_int_env("TREND_MAX_MINE_CHECKS", 100))
        if mine_risks:
            strategy.compute_features(stock_meta=stock_meta, mine_risks=mine_risks)
            final_signals = strategy.latest_signals(trade_date)

    candidates = _to_output_df(final_signals)
    candidates.to_csv(os.path.join(OUTPUT_DIR, "latest_candidates.csv"), index=False, encoding="utf-8-sig")
    signal_date = ""
    if not strategy.feature_df.empty:
        signal_date = pd.to_datetime(strategy._resolve_signal_day(trade_date)[0]).strftime("%Y-%m-%d")
    summary = {
        "run_time": _now_shanghai().strftime("%Y-%m-%d %H:%M:%S"),
        "trade_date": trade_date,
        "signal_date": signal_date,
        "is_trade_day": True,
        "note": trade_note,
        "candidate_count": int(len(candidates)),
        "quality_report": strategy.quality_report,
        "signal_funnel": strategy.signal_funnel_summary(trade_date),
    }
    _write_json(os.path.join(OUTPUT_DIR, "latest_summary.json"), summary)
    body = _build_email_body(summary, candidates)
    with open(os.path.join(OUTPUT_DIR, "latest_email_body.txt"), "w", encoding="utf-8") as f:
        f.write(body + "\n")
    print(body)


if __name__ == "__main__":
    main()
