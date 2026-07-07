# -*- coding: utf-8 -*-
from __future__ import annotations

import datetime as dt
import json
import os
import smtplib
import sys
from email.message import EmailMessage
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd


CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(os.path.dirname(CURRENT_DIR))

if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from jobs.common.a_share_metadata import load_stock_metadata
from strategies.volatility import QualityGateConfig, VolatilityStrategy, VolatilityStrategyConfig


OUTPUT_DIR = os.path.join(CURRENT_DIR, "outputs")
SHARED_MARKET_CACHE_ARCHIVE = "three_dim_cache_bundle.tar.gz"
RISK_KEYWORDS = ("退市", "立案", "调查", "诉讼", "资金占用", "违规担保", "债务", "处罚", "冻结")


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

    try:
        calendar = _load_trade_calendar(year)
    except Exception as exc:
        calendar = pd.DataFrame()
        print(f"交易日历获取失败，退化为工作日判断: {exc}")
    if calendar.empty:
        weekday = pd.to_datetime(today).weekday()
        is_weekday = weekday < 5
        note = "交易日历不可用，已退化为工作日判断。" if is_weekday else "非工作日，跳过。"
        return today, is_weekday, note

    row = calendar[calendar["trade_date"] == today]
    if row.empty:
        return today, False, "交易日历未包含该日期，跳过。"
    is_trade_day = int(row.iloc[0]["trade_status"]) == 1
    return today, is_trade_day, "交易日，执行波动结构扫描。" if is_trade_day else "非A股交易日，跳过扫描。"


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


def _format_table(df: pd.DataFrame, columns: list[str], limit: int = 15) -> list[str]:
    if df.empty:
        return ["无"]
    lines = []
    for idx, (_, row) in enumerate(df.head(limit).iterrows(), start=1):
        lines.append(f"{idx}. " + " | ".join(_format_cell(row.get(col, "")) for col in columns))
    return lines


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
    if signals.empty:
        return pd.DataFrame(
            columns=[
                "信号日期",
                "股票代码",
                "股票名称",
                "信号类型",
                "评分",
                "风险等级",
                "收盘价",
                "日涨跌%",
                "20日涨跌%",
                "当日振幅%",
                "20/60日振幅比",
                "20日均成交额(亿)",
                "5/20日成交额比",
                "距20日线%",
                "距60日线%",
                "60日回撤%",
                "观察价",
                "失效价",
                "入选依据",
            ]
        )
    out = pd.DataFrame(
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
            "当日振幅%": (signals["range_pct"] * 100).round(2),
            "20/60日振幅比": signals["squeeze_ratio"].round(2),
            "20日均成交额(亿)": (signals["amount_ma20"] / 100_000_000).round(2),
            "5/20日成交额比": signals["amount_ratio5_20"].round(2),
            "距20日线%": (signals["close_to_ma20"] * 100).round(2),
            "距60日线%": (signals["close_to_ma60"] * 100).round(2),
            "60日回撤%": (signals["drawdown60"] * 100).round(2),
            "观察价": signals["watch_price"].round(3),
            "失效价": signals["invalid_price"].round(3),
            "入选依据": signals["reason"],
        }
    )
    return out


def _build_email_body(summary: dict[str, Any], candidates: pd.DataFrame) -> str:
    report = summary.get("quality_report", {})
    reject_counts = report.get("reject_counts", {})
    lines = [
        "A股波动结构扫描",
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

    lines.extend(["", "波动收敛 Top"])
    lines.extend(_format_table(candidates[candidates["信号类型"].eq("波动收敛")], ["股票代码", "股票名称", "评分", "风险等级", "收盘价", "观察价", "失效价", "入选依据"], limit=12))
    lines.extend(["", "波动扩张 Top"])
    lines.extend(_format_table(candidates[candidates["信号类型"].eq("波动扩张")], ["股票代码", "股票名称", "评分", "风险等级", "收盘价", "观察价", "失效价", "入选依据"], limit=12))
    lines.extend(["", "异常波动 Top"])
    lines.extend(_format_table(candidates[candidates["信号类型"].eq("异常波动")], ["股票代码", "股票名称", "评分", "风险等级", "收盘价", "观察价", "失效价", "入选依据"], limit=8))
    lines.extend(
        [
            "",
            "使用提示",
            "- 波动收敛偏观察，重点看是否放量突破观察价。",
            "- 波动扩张偏确认，避免在当日大涨后追高。",
            "- 异常波动优先作为风险提醒，不默认视为机会。",
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
    body = "\n".join(["A股波动结构扫描", "", f"日期: {trade_date}", f"状态: {note}", "", "非交易日不生成候选。"])
    with open(os.path.join(OUTPUT_DIR, "latest_email_body.txt"), "w", encoding="utf-8") as f:
        f.write(body + "\n")


def _send_email_if_configured() -> None:
    smtp_user = os.getenv("SMTP_USER", "").strip()
    smtp_pass = os.getenv("SMTP_PASS", "").strip()
    mail_to = os.getenv("MAIL_TO", "").strip()
    if not smtp_user or not smtp_pass or not mail_to:
        print("未配置邮件参数，跳过邮件通知。")
        return

    body_file = os.path.join(OUTPUT_DIR, "latest_email_body.txt")
    with open(body_file, "r", encoding="utf-8") as f:
        body = f.read().strip()

    msg = EmailMessage()
    msg["Subject"] = "波动结构策略扫描"
    msg["From"] = smtp_user
    msg["To"] = mail_to
    msg.set_content(body)

    recipients = [item.strip() for item in mail_to.split(",") if item.strip()]
    host = os.getenv("SMTP_HOST", "smtp.163.com")
    port = int(os.getenv("SMTP_PORT", "465"))
    with smtplib.SMTP_SSL(host, port) as server:
        server.login(smtp_user, smtp_pass)
        server.send_message(msg, to_addrs=recipients)


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
        min_history_days=_read_int_env("VOLATILITY_MIN_HISTORY_DAYS", 180),
        min_listing_days=_read_int_env("VOLATILITY_MIN_LISTING_DAYS", 180),
        min_amount_ma20=_read_float_env("VOLATILITY_MIN_AMOUNT_MA20", 100_000_000),
        min_valid_days20=_read_int_env("VOLATILITY_MIN_VALID_DAYS20", 16),
        min_price=_read_float_env("VOLATILITY_MIN_PRICE", 3.0),
        max_drawdown60=_read_float_env("VOLATILITY_MAX_DRAWDOWN60", 0.40),
        min_mine_score=_read_float_env("VOLATILITY_MIN_MINE_SCORE", 75.0),
    )
    config = VolatilityStrategyConfig(
        quality=quality,
        universe_size=_read_int_env("VOLATILITY_UNIVERSE_SIZE", 2500),
        squeeze_limit=_read_int_env("VOLATILITY_SQUEEZE_LIMIT", 80),
        expansion_limit=_read_int_env("VOLATILITY_EXPANSION_LIMIT", 80),
        anomaly_limit=_read_int_env("VOLATILITY_ANOMALY_LIMIT", 40),
    )

    strategy = VolatilityStrategy(config)
    strategy.load_market_cache()
    strategy.compute_features(stock_meta=stock_meta)
    rough_signals = strategy.latest_signals(trade_date)

    enable_mine = _read_bool_env("VOLATILITY_ENABLE_MINE_CLEARANCE", True)
    if enable_mine and not rough_signals.empty:
        candidate_codes = rough_signals.sort_values("score", ascending=False)["stock_code"].drop_duplicates().tolist()
        mine_risks = _load_mine_risks(candidate_codes, _read_int_env("VOLATILITY_MAX_MINE_CHECKS", 120))
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
    }
    _write_json(os.path.join(OUTPUT_DIR, "latest_summary.json"), summary)

    body = _build_email_body(summary, candidates)
    with open(os.path.join(OUTPUT_DIR, "latest_email_body.txt"), "w", encoding="utf-8") as f:
        f.write(body + "\n")
    print(body)

    if _read_bool_env("VOLATILITY_SEND_EMAIL_IN_SCRIPT", False):
        _send_email_if_configured()


if __name__ == "__main__":
    main()
