# -*- coding: utf-8 -*-
from __future__ import annotations

import datetime as dt
import json
import os
import sys
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd


CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(os.path.dirname(CURRENT_DIR))

if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import adata
from jobs.common.cloud_cache_sync import SHARED_MARKET_CACHE_ARCHIVE, sync_cache_from_drive, sync_cache_to_drive
from strategies.a_share_allocation import AShareAllocationStrategy, StrategyConfig
from strategies.short_term.short_term_strategy_code import ShortTermDisagreementStrategy


OUTPUT_DIR = os.path.join(CURRENT_DIR, "outputs")


def _now_shanghai() -> dt.datetime:
    return dt.datetime.now(ZoneInfo("Asia/Shanghai"))


def _read_bool_env(name: str, default: bool) -> bool:
    value = os.getenv(name, "").strip().lower()
    if value == "":
        return default
    return value in {"1", "true", "yes", "y", "on"}


def _read_int_env(name: str, default: int) -> int:
    value = os.getenv(name, "").strip()
    return default if value == "" else int(value)


def _read_float_env(name: str, default: float) -> float:
    value = os.getenv(name, "").strip()
    return default if value == "" else float(value)


def _write_json(path: str, payload: dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def _load_trade_calendar(year: int) -> pd.DataFrame:
    calendar = adata.stock.info.trade_calendar(year=year)
    if calendar is None or calendar.empty:
        return pd.DataFrame()
    calendar = calendar.copy()
    calendar["trade_date"] = pd.to_datetime(calendar["trade_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    calendar["trade_status"] = pd.to_numeric(calendar["trade_status"], errors="coerce").fillna(0).astype(int)
    return calendar.dropna(subset=["trade_date"])


def resolve_trade_date(requested: str | None = None) -> tuple[str, bool, str]:
    today = requested or _now_shanghai().strftime("%Y-%m-%d")
    try:
        year = pd.to_datetime(today).year
    except Exception:
        return today, False, f"日期格式无效: {today}"

    calendar = _load_trade_calendar(year)
    if calendar.empty:
        # 兜底：交易日历不可用时只按工作日判断，避免网络短暂失败导致任务完全不可用。
        weekday = pd.to_datetime(today).weekday()
        is_weekday = weekday < 5
        note = "交易日历不可用，已退化为工作日判断。" if is_weekday else "非工作日，跳过。"
        return today, is_weekday, note

    row = calendar[calendar["trade_date"] == today]
    if row.empty:
        return today, False, "交易日历未包含该日期，跳过。"
    is_trade_day = int(row.iloc[0]["trade_status"]) == 1
    return today, is_trade_day, "交易日，执行复盘。" if is_trade_day else "非A股交易日，跳过复盘。"


def _parse_portfolio() -> list[dict[str, Any]]:
    raw = os.getenv("A_SHARE_PORTFOLIO_JSON", "").strip()
    if not raw:
        return []
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"A_SHARE_PORTFOLIO_JSON 解析失败: {exc}")
        return []
    if isinstance(payload, dict):
        payload = payload.get("positions", [])
    if not isinstance(payload, list):
        return []
    positions = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        code = str(item.get("code") or item.get("stock_code") or "").strip()
        if code.endswith(".0"):
            code = code[:-2]
        code = code.zfill(6) if code.isdigit() else code
        if not code:
            continue
        positions.append(
            {
                "code": code,
                "name": str(item.get("name") or item.get("short_name") or ""),
                "weight": float(item.get("weight", item.get("position_pct", 0)) or 0),
                "cost": float(item.get("cost", 0) or 0),
            }
        )
    return positions


def _market_regime(strategy: AShareAllocationStrategy, latest_date: pd.Timestamp) -> tuple[str, str]:
    if strategy.benchmark_df.empty:
        return "未知", "未找到沪深300基准缓存。"
    bench = strategy.benchmark_df[strategy.benchmark_df["trade_date"] <= latest_date]
    if bench.empty:
        return "未知", "基准缓存没有覆盖信号日期。"
    row = bench.iloc[-1]
    close = float(row["close"])
    ma120 = float(row["ma120"]) if pd.notna(row["ma120"]) else np.nan
    slope = float(row["ma60_slope"]) if pd.notna(row["ma60_slope"]) else np.nan
    if pd.notna(ma120) and close >= ma120:
        return "偏进攻", f"沪深300收盘 {close:.2f}，位于120日均线 {ma120:.2f} 上方。"
    if pd.notna(slope) and slope >= 0:
        return "中性修复", f"沪深300仍未确认强势，但60日均线斜率为正。"
    return "防守", f"沪深300弱于120日均线，且60日均线斜率未改善。"


def _portfolio_review(day_scores: pd.DataFrame, positions: list[dict[str, Any]]) -> pd.DataFrame:
    if not positions:
        return pd.DataFrame()
    rows = []
    lookup = day_scores.set_index("stock_code")
    for pos in positions:
        code = pos["code"]
        row = lookup.loc[code] if code in lookup.index else None
        weight = float(pos.get("weight") or 0)
        name = pos.get("name") or code
        if row is None:
            rows.append(
                {
                    "股票代码": code,
                    "股票名称": name,
                    "仓位": weight,
                    "收盘价": "",
                    "评分": "",
                    "趋势": "无数据",
                    "建议动作": "继续持有",
                    "理由": "本次策略池未覆盖该标的，请单独检查数据、停牌或代码类型。",
                }
            )
            continue

        score = float(row["final_score"]) if pd.notna(row["final_score"]) else 0.0
        close = float(row["close"])
        ma20 = float(row["ma20"]) if pd.notna(row["ma20"]) else np.nan
        ma60 = float(row["ma60"]) if pd.notna(row["ma60"]) else np.nan
        high60 = float(row["high_60"]) if pd.notna(row["high_60"]) else np.nan
        trend = "上升趋势" if pd.notna(ma60) and close > ma60 else "走弱"

        if pd.notna(ma60) and close < ma60:
            action = "反弹减仓"
            reason = f"收盘价跌破60日线，反弹到20日线或前平台附近优先降风险。"
        elif weight > 25 and score < 75:
            action = "逢高减仓"
            reason = "单票仓位超过25%，且组合评分未达到强势持有阈值，接近前高或放量冲高时降到25%以内。"
        elif score >= 78 and weight <= 12 and trend == "上升趋势":
            action = "分批加仓"
            reason = "评分较高且趋势未走坏，但仍建议小比例分批，不追高。"
        elif score < 45:
            action = "反弹减仓"
            reason = "组合评分偏弱，若基本面没有新增强化，反弹时降低占用资金。"
        else:
            action = "继续持有"
            reason = "评分和趋势尚未触发强制减仓规则，按既定仓位跟踪。"

        position_note = []
        if pd.notna(high60):
            position_note.append(f"距60日高点{(close / high60 - 1) * 100:.1f}%")
        if pd.notna(ma20):
            position_note.append(f"20日线{ma20:.2f}")
        if pd.notna(ma60):
            position_note.append(f"60日线{ma60:.2f}")

        rows.append(
            {
                "股票代码": code,
                "股票名称": name,
                "仓位": weight,
                "收盘价": round(close, 3),
                "评分": round(score, 2),
                "趋势": trend,
                "建议动作": action,
                "理由": reason + ("；" + "，".join(position_note) if position_note else ""),
            }
        )
    return pd.DataFrame(rows)


def _candidate_review(day_scores: pd.DataFrame, limit: int = 50) -> pd.DataFrame:
    rows = []
    source = day_scores.sort_values("final_score", ascending=False).head(limit)
    for row in source.itertuples():
        ret_1d = float(row.ret_1d) if pd.notna(row.ret_1d) else np.nan
        ret_5 = float(row.ret_5) if pd.notna(row.ret_5) else np.nan
        ret_20 = float(row.ret_20) if pd.notna(row.ret_20) else np.nan
        close = float(row.close)
        ma20 = float(row.ma20) if pd.notna(row.ma20) else np.nan
        ma60 = float(row.ma60) if pd.notna(row.ma60) else np.nan
        close_to_high = float(row.close_to_high_60) if pd.notna(row.close_to_high_60) else np.nan
        volume_ratio = float(row.volume_ratio_5_20) if pd.notna(row.volume_ratio_5_20) else np.nan

        trend_state = "上升趋势" if pd.notna(ma60) and close > ma60 else "趋势未确认"
        if pd.notna(ret_1d) and ret_1d < 0:
            if pd.notna(ma20) and close >= ma20 and pd.notna(ma60) and close >= ma60:
                action_hint = "绿盘回踩观察，不追；重点看能否守住20日线/60日线"
            else:
                action_hint = "绿盘走弱，暂不作为买点"
        elif pd.notna(ret_1d) and ret_1d > 0.05:
            action_hint = "涨幅偏大，避免追高；等回踩或次日确认"
        else:
            action_hint = "可观察，需配合量能和指数环境"

        reasons = []
        if pd.notna(ret_20) and ret_20 > 0:
            reasons.append(f"20日涨幅{ret_20 * 100:.1f}%")
        if pd.notna(close_to_high) and close_to_high >= 0.92:
            reasons.append(f"接近60日高点{close_to_high * 100:.1f}%")
        if pd.notna(volume_ratio) and volume_ratio >= 1.2:
            reasons.append(f"5日量能为20日均量{volume_ratio:.1f}倍")
        if pd.notna(ma60) and close > ma60:
            reasons.append("站上60日线")
        if not reasons:
            reasons.append("综合评分靠前")

        rows.append(
            {
                "股票代码": row.stock_code,
                "收盘价": round(close, 3),
                "日涨跌%": round(ret_1d * 100, 2) if pd.notna(ret_1d) else "",
                "5日涨跌%": round(ret_5 * 100, 2) if pd.notna(ret_5) else "",
                "20日涨跌%": round(ret_20 * 100, 2) if pd.notna(ret_20) else "",
                "总分": round(float(row.final_score), 2),
                "趋势分": round(float(row.trend_score), 2),
                "短线分": round(float(row.short_score), 2),
                "状态": trend_state,
                "入选依据": "；".join(reasons),
                "操作提示": action_hint,
            }
        )
    return pd.DataFrame(rows)


def _format_table(df: pd.DataFrame, columns: list[str], limit: int = 10) -> list[str]:
    if df.empty:
        return ["无"]
    lines = []
    for idx, (_, row) in enumerate(df.head(limit).iterrows(), start=1):
        parts = [_format_cell(row.get(col, "")) for col in columns]
        lines.append(f"{idx}. " + " | ".join(parts))
    return lines


def _format_cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and (not np.isfinite(value)):
        return ""
    if pd.isna(value):
        return ""
    if isinstance(value, float):
        return f"{value:.4f}".rstrip("0").rstrip(".")
    return str(value)


def _build_email_body(
    summary: dict[str, Any],
    top_df: pd.DataFrame,
    portfolio_df: pd.DataFrame,
    metrics: dict[str, Any],
) -> str:
    lines = [
        "A股每日投资复盘",
        "",
        "A. 今日组合结论",
        f"- 信号日期: {summary['signal_date']}",
        f"- 市场状态: {summary['market_regime']}。{summary['market_note']}",
        f"- 策略建议: {summary['operation_advice']}",
        f"- 候选数量: {summary['candidate_count']}，回测区间收益: {metrics.get('total_return', '')}，最大回撤: {metrics.get('max_drawdown', '')}",
        "",
        "B. 持仓逐一判断",
    ]
    if portfolio_df.empty:
        lines.append("- 未配置 A_SHARE_PORTFOLIO_JSON，仅输出策略候选和市场风险。")
    else:
        for line in _format_table(portfolio_df, ["股票代码", "股票名称", "仓位", "评分", "趋势", "建议动作", "理由"], limit=20):
            lines.append(line)

    lines.extend(["", "C. 调仓优先顺序"])
    if portfolio_df.empty:
        lines.append("- 无持仓配置；优先只把候选池作为观察清单，不直接追买。")
    else:
        reduce_df = portfolio_df[portfolio_df["建议动作"].isin(["逢高减仓", "反弹减仓"])]
        add_df = portfolio_df[portfolio_df["建议动作"].eq("分批加仓")]
        if reduce_df.empty and add_df.empty:
            lines.append("- 暂无强制调仓信号，按持仓触发位跟踪。")
        else:
            for line in _format_table(reduce_df, ["股票代码", "股票名称", "建议动作", "理由"], limit=10):
                lines.append(line)
            if not add_df.empty:
                lines.append("可加仓观察：")
                for line in _format_table(add_df, ["股票代码", "股票名称", "建议动作", "理由"], limit=5):
                    lines.append(line)

    lines.extend(["", "D. 明日重点观察清单"])
    lines.append("1. 沪深300是否继续站稳/修复关键均线，决定组合仓位上限。")
    lines.append("2. 高分候选是否放量突破后仍能收在20日线和60日线上方。")
    lines.append("3. 已高仓位或高度相关持仓是否出现冲高回落，优先控制集中度。")
    lines.append("4. 关注财报、政策、商品价格和外围市场对主线风险偏好的影响。")

    lines.extend(["", "E. 今日策略候选 Top"])
    for line in _format_table(
        top_df,
        ["股票代码", "收盘价", "日涨跌%", "总分", "趋势分", "短线分", "入选依据", "操作提示"],
        limit=15,
    ):
        lines.append(line)

    lines.extend(
        [
            "",
            "风险提示",
            "以上为研究复盘和风险管理建议，不构成个性化投资顾问服务或收益承诺。",
        ]
    )
    return "\n".join(lines)


def _write_skip_outputs(trade_date: str, note: str) -> None:
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    now = _now_shanghai().strftime("%Y-%m-%d %H:%M:%S")
    summary = {
        "run_time": now,
        "trade_date": trade_date,
        "is_trade_day": False,
        "note": note,
    }
    _write_json(os.path.join(OUTPUT_DIR, "latest_summary.json"), summary)
    body = "\n".join(
        [
            "A股每日投资复盘",
            "",
            f"运行时间: {now}",
            f"日期: {trade_date}",
            f"状态: {note}",
            "",
            "非交易日不生成投资建议。",
        ]
    )
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

    allow_online_update = _read_bool_env("A_SHARE_REVIEW_ALLOW_ONLINE_UPDATE", True)
    universe_size = _read_int_env("A_SHARE_REVIEW_UNIVERSE_SIZE", 800)
    max_positions = _read_int_env("A_SHARE_REVIEW_MAX_POSITIONS", 18)
    rebalance_period = _read_int_env("A_SHARE_REVIEW_REBALANCE_PERIOD", 20)
    min_amount_ma20 = _read_float_env("A_SHARE_REVIEW_MIN_AMOUNT_MA20", 80_000_000)
    include_dividend = _read_bool_env("A_SHARE_REVIEW_INCLUDE_DIVIDEND", True)

    sync_cache_from_drive(PROJECT_ROOT, SHARED_MARKET_CACHE_ARCHIVE, ["data/cache"])
    if allow_online_update:
        updater = ShortTermDisagreementStrategy()
        max_update_codes = _read_int_env("A_SHARE_REVIEW_MAX_UPDATE_CODES", 0)
        updater.load_data(
            allow_online_update=True,
            max_update_codes=max_update_codes if max_update_codes > 0 else None,
        )
        updater.sync_active_cache_to_shared()
        sync_cache_to_drive(PROJECT_ROOT, SHARED_MARKET_CACHE_ARCHIVE, ["data/cache"])

    config = StrategyConfig(
        max_positions=max_positions,
        rebalance_period=rebalance_period,
        min_amount_ma20=min_amount_ma20,
    )
    strategy = AShareAllocationStrategy(config)
    strategy.load_market_cache(universe_size=universe_size)
    if os.path.exists(strategy.benchmark_cache_file):
        strategy.load_benchmark()
    strategy.compute_scores(include_dividend=include_dividend)

    target_ts = pd.to_datetime(trade_date)
    available_dates = sorted(d for d in strategy.score_df["trade_date"].drop_duplicates().tolist() if d <= target_ts)
    if not available_dates:
        raise RuntimeError(f"策略评分没有覆盖交易日: {trade_date}")
    signal_date = available_dates[-1]
    backtest_start = (signal_date - pd.Timedelta(days=180)).strftime("%Y-%m-%d")
    metrics = strategy.run_backtest(start_date=backtest_start, end_date=signal_date.strftime("%Y-%m-%d"))

    day_scores = strategy.score_df[strategy.score_df["trade_date"] == signal_date].copy()
    day_scores = day_scores.sort_values("final_score", ascending=False)
    top_raw_df = day_scores[
        [
            "stock_code",
            "close",
            "ret_1d",
            "ret_5",
            "final_score",
            "core_score",
            "dividend_score",
            "trend_score",
            "short_score",
            "dividend_yield_ttm",
            "amount_ma20",
            "ret_20",
            "vol_60",
            "ma20",
            "ma60",
            "close_to_high_60",
            "volume_ratio_5_20",
        ]
    ].head(50)
    top_df = _candidate_review(day_scores, limit=50)

    top_raw_df.to_csv(os.path.join(OUTPUT_DIR, "latest_top_candidates_raw.csv"), index=False, encoding="utf-8-sig")
    top_df.to_csv(os.path.join(OUTPUT_DIR, "latest_top_candidates.csv"), index=False, encoding="utf-8-sig")
    pd.DataFrame(strategy.trade_logs).to_csv(os.path.join(OUTPUT_DIR, "latest_trades.csv"), index=False, encoding="utf-8-sig")
    pd.DataFrame(strategy.equity_curve).to_csv(os.path.join(OUTPUT_DIR, "latest_equity.csv"), index=False, encoding="utf-8-sig")
    pd.DataFrame([metrics]).to_csv(os.path.join(OUTPUT_DIR, "latest_metrics.csv"), index=False, encoding="utf-8-sig")

    portfolio_df = _portfolio_review(day_scores, _parse_portfolio())
    portfolio_df.to_csv(os.path.join(OUTPUT_DIR, "latest_portfolio_review.csv"), index=False, encoding="utf-8-sig")

    market_regime, market_note = _market_regime(strategy, signal_date)
    candidate_count = int(day_scores["final_score"].notna().sum())
    if market_regime == "防守":
        operation_advice = "只减不加"
    elif portfolio_df.empty:
        operation_advice = "不需要调仓"
    elif portfolio_df["建议动作"].isin(["逢高减仓", "反弹减仓"]).any():
        operation_advice = "需要调仓"
    elif portfolio_df["建议动作"].eq("分批加仓").any():
        operation_advice = "可小幅加仓"
    else:
        operation_advice = "不需要调仓"

    summary = {
        "run_time": _now_shanghai().strftime("%Y-%m-%d %H:%M:%S"),
        "trade_date": trade_date,
        "signal_date": signal_date.strftime("%Y-%m-%d"),
        "is_trade_day": True,
        "note": trade_note,
        "market_regime": market_regime,
        "market_note": market_note,
        "operation_advice": operation_advice,
        "candidate_count": candidate_count,
        "metrics": metrics,
    }
    _write_json(os.path.join(OUTPUT_DIR, "latest_summary.json"), summary)

    body = _build_email_body(summary, top_df, portfolio_df, metrics)
    with open(os.path.join(OUTPUT_DIR, "latest_email_body.txt"), "w", encoding="utf-8") as f:
        f.write(body + "\n")
    print(body)


if __name__ == "__main__":
    main()
