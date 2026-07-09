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

from jobs.common.a_share_metadata import load_stock_metadata
from jobs.common.daily_job import (
    now_shanghai as _now_shanghai,
    read_bool_env as _read_bool_env,
    read_float_env as _read_float_env,
    read_int_env as _read_int_env,
    resolve_trade_date as _resolve_trade_date,
    write_json as _write_json,
)
from strategies.boll import BollStrategy, BollStrategyConfig
from strategies.volatility import QualityGateConfig


OUTPUT_DIR = os.path.join(CURRENT_DIR, "outputs")
SHARED_MARKET_CACHE_ARCHIVE = "three_dim_cache_bundle.tar.gz"


def resolve_trade_date(requested: str | None = None) -> tuple[str, bool, str]:
    return _resolve_trade_date(requested, action="执行BOLL战法扫描", skip_action="跳过扫描")


def _load_mine_risks(codes: list[str], max_checks: int) -> dict[str, dict[str, Any]]:
    if max_checks <= 0 or not codes:
        return {}
    try:
        import adata
    except Exception as exc:
        print(f"adata 导入失败，跳过扫雷接口: {exc}")
        return {}

    risks: dict[str, dict[str, Any]] = {}
    for code in codes[:max_checks]:
        try:
            df = adata.sentiment.mine.mine_clearance_tdx(code)
        except Exception as exc:
            print(f"扫雷接口失败，跳过 {code}: {exc}")
            continue
        if df is None or df.empty:
            continue
        score = pd.to_numeric(df.get("score"), errors="coerce").dropna()
        score_value = float(score.iloc[0]) if not score.empty else np.nan
        reasons = []
        for _, row in df.iterrows():
            reason = str(row.get("reason") or row.get("f_type") or "").strip()
            if reason and reason != "暂无风险项":
                reasons.append(reason)
        risks[code] = {"score": score_value, "reason": "；".join(reasons[:5])}
    return risks


def _to_output_df(signals: pd.DataFrame) -> pd.DataFrame:
    columns = [
        "信号日期",
        "股票代码",
        "股票名称",
        "信号类型",
        "评分",
        "风险等级",
        "收盘价",
        "日涨跌%",
        "20日涨跌%",
        "20日均成交额(亿)",
        "5/20日成交额比",
        "BOLL中轨",
        "BOLL上轨",
        "BOLL下轨",
        "带宽%",
        "带宽/60日均值",
        "中轨20日斜率%",
        "趋势环境",
        "下影线占比%",
        "上影线占比%",
        "收盘位置%",
        "观察价",
        "失效价",
        "入选依据",
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
            "20日涨跌%": (signals["ret_20d"] * 100).round(2),
            "20日均成交额(亿)": (signals["amount_ma20"] / 100_000_000).round(2),
            "5/20日成交额比": signals["amount_ratio5_20"].round(2),
            "BOLL中轨": signals["boll_mid"].round(3),
            "BOLL上轨": signals["boll_upper"].round(3),
            "BOLL下轨": signals["boll_lower"].round(3),
            "带宽%": (signals["boll_bandwidth"] * 100).round(2),
            "带宽/60日均值": signals["boll_bandwidth_ratio"].round(2),
            "中轨20日斜率%": (signals["boll_mid_slope20"] * 100).round(2),
            "趋势环境": signals["trend_environment"],
            "下影线占比%": (signals["lower_shadow_ratio"] * 100).round(2),
            "上影线占比%": (signals["upper_shadow_ratio"] * 100).round(2),
            "收盘位置%": (signals["close_pos"] * 100).round(2),
            "观察价": signals["watch_price"].round(3),
            "失效价": signals["invalid_price"].round(3),
            "入选依据": signals["reason"],
        }
    )


def _format_cell(value: Any) -> str:
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except TypeError:
        pass
    if isinstance(value, float):
        if not np.isfinite(value):
            return ""
        return f"{value:.4f}".rstrip("0").rstrip(".")
    return str(value)


def _format_table(df: pd.DataFrame, columns: list[str], limit: int = 12) -> list[str]:
    if df.empty:
        return ["无"]
    lines = []
    for idx, (_, row) in enumerate(df.head(limit).iterrows(), start=1):
        lines.append(f"{idx}. " + " | ".join(_format_cell(row.get(col, "")) for col in columns))
    return lines


def _build_email_body(summary: dict[str, Any], candidates: pd.DataFrame) -> str:
    report = summary.get("quality_report", {})
    reject_counts = report.get("reject_counts", {})
    lines = [
        "BOLL战法扫描",
        "",
        f"- 运行时间: {summary['run_time']}",
        f"- 请求日期: {summary['trade_date']}",
        f"- 信号日期: {summary.get('signal_date', '')}",
        f"- 候选数量: {summary.get('candidate_count', 0)}",
        f"- 股票池: 初始 {report.get('initial_stock_count', 0)}，通过票质过滤 {report.get('accepted_stock_count', 0)}，剔除 {report.get('rejected_stock_count', 0)}",
        "",
        "票质过滤剔除原因",
    ]
    if reject_counts:
        for reason, count in sorted(reject_counts.items(), key=lambda item: (-item[1], item[0])):
            lines.append(f"- {reason}: {count}")
    else:
        lines.append("- 无")

    lines.extend(["", "下轨止跌观察"])
    lines.extend(
        _format_table(
            candidates[candidates["信号类型"].eq("下轨止跌观察")],
            ["股票代码", "股票名称", "评分", "风险等级", "趋势环境", "收盘价", "BOLL下轨", "观察价", "失效价", "入选依据"],
            limit=15,
        )
    )
    lines.extend(["", "上轨放量滞涨"])
    lines.extend(
        _format_table(
            candidates[candidates["信号类型"].eq("上轨放量滞涨")],
            ["股票代码", "股票名称", "评分", "风险等级", "趋势环境", "收盘价", "BOLL上轨", "观察价", "失效价", "入选依据"],
            limit=15,
        )
    )
    lines.extend(
        [
            "",
            "使用提示",
            "- 下轨止跌观察只代表进入观察池，需要次日确认是否守住下轨并修复。",
            "- 上轨放量滞涨偏风险提醒，适合检查持仓止盈或减仓。",
            "- BOLL战法适合震荡缩口环境，单边趋势行情中要降低权重。",
            "",
            "风险提示",
            "以上为研究复盘和风险管理建议，不构成个性化投资顾问服务或收益承诺。",
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
    body = "\n".join(["BOLL战法扫描", "", f"日期: {trade_date}", f"状态: {note}", "", "非交易日不生成候选。"])
    with open(os.path.join(OUTPUT_DIR, "latest_email_body.txt"), "w", encoding="utf-8") as f:
        f.write(body + "\n")


def main() -> None:
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    requested_trade_date = os.getenv("TRADE_DATE", "").strip() or None
    trade_date, is_trade_day, trade_note = resolve_trade_date(requested_trade_date)
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
        min_history_days=_read_int_env("BOLL_MIN_HISTORY_DAYS", 180),
        min_listing_days=_read_int_env("BOLL_MIN_LISTING_DAYS", 180),
        min_amount_ma20=_read_float_env("BOLL_MIN_AMOUNT_MA20", 100_000_000),
        min_valid_days20=_read_int_env("BOLL_MIN_VALID_DAYS20", 16),
        min_price=_read_float_env("BOLL_MIN_PRICE", 3.0),
        max_drawdown60=_read_float_env("BOLL_MAX_DRAWDOWN60", 0.40),
        min_mine_score=_read_float_env("BOLL_MIN_MINE_SCORE", 75.0),
    )
    config = BollStrategyConfig(
        quality=quality,
        universe_size=_read_int_env("BOLL_UNIVERSE_SIZE", 2500),
        buy_limit=_read_int_env("BOLL_BUY_LIMIT", 80),
        sell_limit=_read_int_env("BOLL_SELL_LIMIT", 80),
        max_bandwidth_ratio=_read_float_env("BOLL_MAX_BANDWIDTH_RATIO", 0.90),
        max_mid_slope20=_read_float_env("BOLL_MAX_MID_SLOPE20", 0.08),
    )

    strategy = BollStrategy(config)
    strategy.load_market_cache()
    strategy.compute_features(stock_meta=stock_meta)
    rough_signals = strategy.latest_signals(trade_date)

    if _read_bool_env("BOLL_ENABLE_MINE_CLEARANCE", True) and not rough_signals.empty:
        candidate_codes = rough_signals.sort_values("score", ascending=False)["stock_code"].drop_duplicates().tolist()
        mine_risks = _load_mine_risks(candidate_codes, _read_int_env("BOLL_MAX_MINE_CHECKS", 120))
        if mine_risks:
            strategy.compute_features(stock_meta=stock_meta, mine_risks=mine_risks)
            final_signals = strategy.latest_signals(trade_date)
        else:
            final_signals = rough_signals
    else:
        final_signals = rough_signals

    candidates = _to_output_df(final_signals)
    candidates.to_csv(os.path.join(OUTPUT_DIR, "latest_candidates.csv"), index=False, encoding="utf-8-sig")

    signal_date = ""
    if not final_signals.empty:
        signal_date = pd.to_datetime(final_signals["trade_date"].max()).strftime("%Y-%m-%d")
    elif not strategy.feature_df.empty:
        signal_date = pd.to_datetime(strategy.feature_df["trade_date"].max()).strftime("%Y-%m-%d")

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
