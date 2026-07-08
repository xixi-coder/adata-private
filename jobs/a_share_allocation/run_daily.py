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

from jobs.common.a_share_metadata import load_stock_metadata
from strategies.a_share_allocation import AShareAllocationStrategy, StrategyConfig


OUTPUT_DIR = os.path.join(CURRENT_DIR, "outputs")
SHARED_MARKET_CACHE_ARCHIVE = "three_dim_cache_bundle.tar.gz"


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
    import adata

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
        market = str(item.get("market") or item.get("exchange") or "").strip().upper()
        if not market and code.isdigit() and len(code) <= 5:
            market = "HK"
        if market in {"HK", "HKG", "港股"}:
            code = code.zfill(5) if code.isdigit() else code
            market = "HK"
        else:
            code = code.zfill(6) if code.isdigit() else code
            market = "A"
        if not code:
            continue
        positions.append(
            {
                "code": code,
                "name": str(item.get("name") or item.get("short_name") or ""),
                "market": market,
                "weight": float(item.get("weight", item.get("position_pct", 0)) or 0),
                "cost": float(item.get("cost", 0) or 0),
            }
        )
    return positions


def _position_theme(pos: dict[str, Any]) -> str:
    raw_theme = str(pos.get("theme") or pos.get("sector") or "").strip()
    if raw_theme:
        return raw_theme
    code = str(pos.get("code") or "")
    name = str(pos.get("name") or "")
    text = f"{code} {name}"
    if any(key in text for key in ["中际旭创", "天孚通信", "中天科技"]):
        return "AI算力-光通信"
    if any(key in text for key in ["沪电股份", "胜宏科技"]):
        return "AI算力-PCB"
    if any(key in text for key in ["中微公司", "中芯国际", "通富微电"]):
        return "半导体制造/设备/封测"
    if any(key in text for key in ["澜起科技"]):
        return "AI算力-芯片设计"
    return "其他"


def _theme_parent(theme: str) -> str:
    if theme.startswith("AI算力") or theme.startswith("半导体"):
        return "AI算力/半导体链"
    return theme


def _portfolio_risk_summary(positions: list[dict[str, Any]], market_regime: str) -> dict[str, Any]:
    if not positions:
        return {
            "total_weight": 0.0,
            "top1_weight": 0.0,
            "top3_weight": 0.0,
            "top5_weight": 0.0,
            "theme_exposure": [],
            "parent_theme_exposure": [],
            "risk_flags": ["未配置持仓。"],
            "target_equity_range": _target_equity_range(market_regime),
        }

    weights = sorted([float(pos.get("weight") or 0) for pos in positions], reverse=True)
    total_weight = sum(weights)
    theme_map: dict[str, float] = {}
    parent_map: dict[str, float] = {}
    for pos in positions:
        weight = float(pos.get("weight") or 0)
        theme = _position_theme(pos)
        theme_map[theme] = theme_map.get(theme, 0.0) + weight
        parent = _theme_parent(theme)
        parent_map[parent] = parent_map.get(parent, 0.0) + weight

    theme_exposure = sorted(
        [{"theme": theme, "weight": round(weight, 2)} for theme, weight in theme_map.items()],
        key=lambda item: item["weight"],
        reverse=True,
    )
    parent_theme_exposure = sorted(
        [{"theme": theme, "weight": round(weight, 2)} for theme, weight in parent_map.items()],
        key=lambda item: item["weight"],
        reverse=True,
    )
    flags = []
    if weights and weights[0] >= 25:
        flags.append(f"单票仓位 {weights[0]:.1f}% 已接近/超过25%，不建议继续加仓。")
    if sum(weights[:3]) >= 60:
        flags.append(f"前三大仓位合计 {sum(weights[:3]):.1f}%，组合对头部个股较敏感。")
    if sum(weights[:5]) >= 75:
        flags.append(f"前五大仓位合计 {sum(weights[:5]):.1f}%，回撤主要由核心持仓决定。")
    top_parent = parent_theme_exposure[0] if parent_theme_exposure else {"theme": "", "weight": 0}
    if float(top_parent["weight"]) >= 60:
        flags.append(f"{top_parent['theme']} 暴露 {top_parent['weight']:.1f}%，分散持仓无法显著降低主线退潮风险。")
    if total_weight >= 90 and market_regime == "防守":
        flags.append("市场状态防守但组合接近满仓，优先降低高弹性高相关持仓。")
    if not flags:
        flags.append("组合集中度暂未触发强风险阈值。")
    return {
        "total_weight": round(total_weight, 2),
        "top1_weight": round(weights[0] if weights else 0, 2),
        "top3_weight": round(sum(weights[:3]), 2),
        "top5_weight": round(sum(weights[:5]), 2),
        "theme_exposure": theme_exposure,
        "parent_theme_exposure": parent_theme_exposure,
        "risk_flags": flags,
        "target_equity_range": _target_equity_range(market_regime),
    }


def _target_equity_range(market_regime: str) -> str:
    if market_regime == "防守":
        return "40%-60%"
    if market_regime == "中性修复":
        return "60%-75%"
    if market_regime == "偏进攻":
        return "75%-90%"
    return "50%-70%"


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
        market = str(pos.get("market") or "A").upper()
        row = lookup.loc[code] if market == "A" and code in lookup.index else None
        weight = float(pos.get("weight") or 0)
        name = pos.get("name") or code
        if row is None:
            action = "暂不评分" if market != "A" else "单独检查"
            trend = "港股/非A股" if market != "A" else "无数据"
            reason = (
                "当前复盘只覆盖A股行情，该持仓仅计入组合集中度，暂不做技术评分。"
                if market != "A"
                else "本次策略池未覆盖该标的，请单独检查数据、停牌或代码类型。"
            )
            rows.append(
                {
                    "股票代码": code,
                    "股票名称": name,
                    "市场": market,
                    "主题": _position_theme(pos),
                    "仓位": weight,
                    "收盘价": "",
                    "评分": "",
                    "趋势": trend,
                    "建议动作": action,
                    "理由": reason,
                }
            )
            continue

        score = float(row["final_score"]) if pd.notna(row["final_score"]) else 0.0
        close = float(row["close"])
        ma20 = float(row["ma20"]) if pd.notna(row["ma20"]) else np.nan
        ma60 = float(row["ma60"]) if pd.notna(row["ma60"]) else np.nan
        high60 = float(row["high_60"]) if pd.notna(row["high_60"]) else np.nan
        ret_20 = float(row["ret_20"]) if "ret_20" in row.index and pd.notna(row["ret_20"]) else np.nan
        close_to_high_60 = close / high60 if pd.notna(high60) and high60 else np.nan
        below_ma20 = pd.notna(ma20) and close < ma20
        below_ma60 = pd.notna(ma60) and close < ma60
        deep_pullback = pd.notna(close_to_high_60) and close_to_high_60 < 0.80
        weak_20d = pd.notna(ret_20) and ret_20 < -0.12
        ret_1d = float(row["ret_1d"]) if "ret_1d" in row.index and pd.notna(row["ret_1d"]) else np.nan
        close_pos = float(row["close_pos"]) if "close_pos" in row.index and pd.notna(row["close_pos"]) else np.nan
        upper_shadow = (
            float(row["upper_shadow_ratio"])
            if "upper_shadow_ratio" in row.index and pd.notna(row["upper_shadow_ratio"])
            else np.nan
        )
        amount_ratio1_20 = (
            float(row["amount_ratio1_20"])
            if "amount_ratio1_20" in row.index and pd.notna(row["amount_ratio1_20"])
            else np.nan
        )
        volume_ratio = (
            float(row["volume_ratio_5_20"])
            if "volume_ratio_5_20" in row.index and pd.notna(row["volume_ratio_5_20"])
            else np.nan
        )
        cmf5 = float(row["cmf5"]) if "cmf5" in row.index and pd.notna(row["cmf5"]) else np.nan
        cmf20 = float(row["cmf20"]) if "cmf20" in row.index and pd.notna(row["cmf20"]) else np.nan
        net_amt3 = float(row["net_amt3"]) if "net_amt3" in row.index and pd.notna(row["net_amt3"]) else np.nan
        net_amt5 = float(row["net_amt5"]) if "net_amt5" in row.index and pd.notna(row["net_amt5"]) else np.nan
        weak_candle = (
            (pd.notna(upper_shadow) and upper_shadow >= 0.35 and pd.notna(close_pos) and close_pos <= 0.55)
            or (pd.notna(close_pos) and close_pos <= 0.25)
        )
        volume_distribution = pd.notna(amount_ratio1_20) and amount_ratio1_20 >= 1.4 and (
            (pd.notna(ret_1d) and ret_1d < 0) or (pd.notna(close_pos) and close_pos <= 0.45)
        )
        capital_outflow = (
            (pd.notna(cmf5) and cmf5 < -0.05 and (pd.isna(cmf20) or cmf20 < 0))
            or (pd.notna(net_amt3) and net_amt3 < 0 and pd.notna(net_amt5) and net_amt5 < 0)
        )
        technical_risk_count = sum([below_ma20, deep_pullback, weak_20d, weak_candle, volume_distribution, capital_outflow])

        if below_ma60:
            trend = "走弱"
        elif below_ma20 or deep_pullback or weak_20d or technical_risk_count >= 2:
            trend = "趋势转弱"
        elif pd.notna(ma20) and pd.notna(ma60) and close >= ma20 and ma20 >= ma60:
            trend = "上升趋势"
        else:
            trend = "趋势未确认"

        if below_ma60:
            action = "反弹减仓"
            reason = f"收盘价跌破60日线，反弹到20日线或前平台附近优先降风险。"
        elif below_ma20 and (deep_pullback or score < 55):
            action = "减仓观察"
            reason = "技术面已跌破20日线且回撤/评分不支持强势持有，先降低趋势走坏风险。"
        elif deep_pullback and score < 60:
            action = "减仓观察"
            reason = "距60日高点回撤较深，即使尚未跌破60日线，也不按强趋势处理。"
        elif technical_risk_count >= 3 and score < 65:
            action = "反弹减仓"
            reason = "K线形态、量能和资金代理多项转弱，反弹优先降低风险。"
        elif technical_risk_count >= 2 and score < 70:
            action = "减仓观察"
            reason = "K线形态/成交量/资金代理已有两项以上走弱，不再按强势持有处理。"
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
        if pd.notna(upper_shadow):
            position_note.append(f"上影线占比{upper_shadow * 100:.1f}%")
        if pd.notna(close_pos):
            position_note.append(f"收盘位置{close_pos * 100:.1f}%")
        if pd.notna(amount_ratio1_20):
            position_note.append(f"当日成交额/20日{amount_ratio1_20:.2f}")
        if pd.notna(volume_ratio):
            position_note.append(f"5日量能/20日{volume_ratio:.2f}")
        if pd.notna(cmf5):
            position_note.append(f"CMF5{cmf5:.3f}")
        if pd.notna(cmf20):
            position_note.append(f"CMF20{cmf20:.3f}")

        rows.append(
            {
                "股票代码": code,
                "股票名称": name,
                "市场": market,
                "主题": _position_theme(pos),
                "仓位": weight,
                "收盘价": round(close, 3),
                "评分": round(score, 2),
                "趋势": trend,
                "建议动作": action,
                "理由": reason + ("；" + "，".join(position_note) if position_note else ""),
            }
        )
    return pd.DataFrame(rows)


def _candidate_review(day_scores: pd.DataFrame, stock_meta: dict[str, dict[str, Any]], limit: int = 50) -> pd.DataFrame:
    rows = []
    source = day_scores[day_scores["final_score"].notna()].sort_values("final_score", ascending=False).head(limit)
    for row in source.itertuples():
        stock_code = str(row.stock_code).zfill(6)
        stock_name = stock_meta.get(stock_code, {}).get("short_name") or stock_code
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
                "股票代码": stock_code,
                "股票名称": stock_name,
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
    risk = summary.get("portfolio_risk", {})
    lines = [
        "A股持仓复盘",
        f"{summary['signal_date']} | 市场: {summary['market_regime']} | 建议: {summary['operation_advice']}",
        f"目标权益仓位: {risk.get('target_equity_range', '')} | 当前配置仓位: {risk.get('total_weight', 0)}%",
        f"集中度: Top1 {risk.get('top1_weight', 0)}% / Top3 {risk.get('top3_weight', 0)}% / Top5 {risk.get('top5_weight', 0)}%",
    ]
    if portfolio_df.empty:
        lines.extend(["", "未配置 A_SHARE_PORTFOLIO_JSON。本任务将只生成候选文件，不输出持仓战报。"])
    else:
        lines.extend(["", "一、组合风险"])
        for flag in risk.get("risk_flags", [])[:4]:
            lines.append(f"- {flag}")
        theme_lines = []
        for item in risk.get("parent_theme_exposure", [])[:4]:
            theme_lines.append(f"{item['theme']} {item['weight']}%")
        if theme_lines:
            lines.append("- 主题暴露: " + "；".join(theme_lines))

        lines.extend(["", "二、需要处理"])
        reduce_df = portfolio_df[portfolio_df["建议动作"].isin(["逢高减仓", "反弹减仓"])]
        if reduce_df.empty:
            lines.append("无强制减仓项。")
        else:
            for line in _format_table(reduce_df, ["股票代码", "股票名称", "主题", "仓位", "评分", "建议动作", "理由"], limit=8):
                lines.append(line)

        lines.extend(["", "三、继续观察"])
        watch_df = portfolio_df[portfolio_df["建议动作"].isin(["继续持有", "单独检查", "暂不评分"])]
        if watch_df.empty:
            lines.append("无。")
        else:
            for line in _format_table(watch_df, ["股票代码", "股票名称", "主题", "仓位", "评分", "趋势", "建议动作"], limit=12):
                lines.append(line)

        lines.extend(["", "四、可加仓观察"])
        add_df = portfolio_df[portfolio_df["建议动作"].eq("分批加仓")]
        if add_df.empty or summary["operation_advice"] in {"只减不加", "需要调仓"}:
            lines.append("无。当前优先控制仓位和相关性。")
        else:
            for line in _format_table(add_df, ["股票代码", "股票名称", "主题", "仓位", "评分", "建议动作", "理由"], limit=5):
                lines.append(line)

    lines.extend(
        [
            "",
            "五、补位候选 Top5",
        ]
    )
    for line in _format_table(
        top_df,
        ["股票代码", "股票名称", "收盘价", "日涨跌%", "总分", "入选依据", "操作提示"],
        limit=5,
    ):
        lines.append(line)

    lines.extend(
        [
            "",
            "提示: 本邮件用于持仓风控和复盘，不构成收益承诺。完整候选、交易和权益曲线见 Actions artifact。",
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

    sync_cache_from_drive = None
    sync_cache_to_drive = None
    try:
        from jobs.common.cloud_cache_sync import sync_cache_from_drive as _sync_cache_from_drive
        from jobs.common.cloud_cache_sync import sync_cache_to_drive as _sync_cache_to_drive

        sync_cache_from_drive = _sync_cache_from_drive
        sync_cache_to_drive = _sync_cache_to_drive
        sync_cache_from_drive(PROJECT_ROOT, SHARED_MARKET_CACHE_ARCHIVE, ["data/cache"])
    except Exception as exc:
        print(f"缓存同步不可用，继续使用本地缓存: {exc}")
    if allow_online_update:
        from strategies.short_term.short_term_strategy_code import ShortTermDisagreementStrategy

        updater = ShortTermDisagreementStrategy()
        max_update_codes = _read_int_env("A_SHARE_REVIEW_MAX_UPDATE_CODES", 0)
        updater.load_data(
            allow_online_update=True,
            max_update_codes=max_update_codes if max_update_codes > 0 else None,
        )
        updater.sync_active_cache_to_shared()
        if sync_cache_to_drive:
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
    stock_meta = load_stock_metadata(PROJECT_ROOT)

    target_ts = pd.to_datetime(trade_date)
    available_dates = sorted(d for d in strategy.score_df["trade_date"].drop_duplicates().tolist() if d <= target_ts)
    if not available_dates:
        raise RuntimeError(f"策略评分没有覆盖交易日: {trade_date}")
    signal_date = available_dates[-1]
    backtest_start = (signal_date - pd.Timedelta(days=180)).strftime("%Y-%m-%d")
    metrics = strategy.run_backtest(start_date=backtest_start, end_date=signal_date.strftime("%Y-%m-%d"))

    day_scores = strategy.score_df[strategy.score_df["trade_date"] == signal_date].copy()
    day_scores = day_scores.sort_values("final_score", ascending=False)
    qualified_scores = day_scores[day_scores["final_score"].notna()].copy()
    top_raw_df = qualified_scores[
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
            "amount_ratio1_20",
            "close_pos",
            "upper_shadow_ratio",
            "lower_shadow_ratio",
            "cmf5",
            "cmf20",
            "net_amt3",
            "net_amt5",
        ]
    ].head(50)
    top_df = _candidate_review(day_scores, stock_meta, limit=50)

    top_raw_df.to_csv(os.path.join(OUTPUT_DIR, "latest_top_candidates_raw.csv"), index=False, encoding="utf-8-sig")
    top_df.to_csv(os.path.join(OUTPUT_DIR, "latest_top_candidates.csv"), index=False, encoding="utf-8-sig")
    pd.DataFrame(strategy.trade_logs).to_csv(os.path.join(OUTPUT_DIR, "latest_trades.csv"), index=False, encoding="utf-8-sig")
    pd.DataFrame(strategy.equity_curve).to_csv(os.path.join(OUTPUT_DIR, "latest_equity.csv"), index=False, encoding="utf-8-sig")
    pd.DataFrame([metrics]).to_csv(os.path.join(OUTPUT_DIR, "latest_metrics.csv"), index=False, encoding="utf-8-sig")

    positions = _parse_portfolio()
    portfolio_df = _portfolio_review(day_scores, positions)
    portfolio_df.to_csv(os.path.join(OUTPUT_DIR, "latest_portfolio_review.csv"), index=False, encoding="utf-8-sig")

    market_regime, market_note = _market_regime(strategy, signal_date)
    portfolio_risk = _portfolio_risk_summary(positions, market_regime)
    candidate_count = int(day_scores["final_score"].notna().sum())
    parent_exposure = portfolio_risk.get("parent_theme_exposure", [])
    top_parent_weight = float(parent_exposure[0].get("weight", 0)) if parent_exposure else 0.0
    if market_regime == "防守":
        operation_advice = "只减不加"
    elif portfolio_risk.get("top3_weight", 0) >= 60 or top_parent_weight >= 60:
        operation_advice = "控制集中度"
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
        "portfolio_risk": portfolio_risk,
        "metrics": metrics,
    }
    _write_json(os.path.join(OUTPUT_DIR, "latest_summary.json"), summary)

    body = _build_email_body(summary, top_df, portfolio_df, metrics)
    with open(os.path.join(OUTPUT_DIR, "latest_email_body.txt"), "w", encoding="utf-8") as f:
        f.write(body + "\n")
    print(body)


if __name__ == "__main__":
    main()
